"""tableau_zone_crosstab.py -- real per-sheet Tableau crosstab data for a
sheet nested inside a multi-sheet dashboard, where NO REST endpoint can
reach it.

WHY THIS EXISTS (R18, 2026-08-20/21, see ARCHITECTURE.md S28 for the full
trace). Tableau's REST "Query View Data" endpoint (tableau_server.py's
query_view_data_csv) exports exactly ONE sheet's data per dashboard view --
whichever Tableau treats as the default -- regardless of which sheet was
actually requested. This is not a code gap deep_validation.py's tiered
matcher (_assign_dashboard_csv_by_header) can ever close for a sheet whose
data was never in that export to begin with; two real, safe fixes already
shipped there (Tier 4: a map sheet's geo field; Tier 5: strong+weak dims)
close cases where the matcher just wasn't looking hard enough, but sheets
like Commission Model's QuotaAttainment/OTE/Sales or Shipping's 3 sheets
need data Tableau's dashboard-level REST export never sends at all.

Sharath's own prior Tableau-automation project (Snapshot Insights) states
this outright in its own code: "REST API strategy is intentionally
disabled -- Tableau REST API always returns the dashboard's DEFAULT sheet
regardless of which sheet was requested -- only Selenium can select the
correct sheet from the Download Crosstab dialog." Reproduced here with
Playwright (proven inside Snowflake's container runtime by R15's worker
notebook, unlike Selenium which is workstation-only in this project) and a
DIFFERENT auth mechanism than that project's credential-login fallback:
the already-authenticated REST PAT token is injected as a browser cookie,
never submitted through the login form, so it never touches Tableau's
Blend SSO + MFA wall (ARCHITECTURE.md S26's documented, real block on
credential-based automation) at all.

Every interaction in _select_sheet_and_download below was found by reading
the REAL DOM live against the account, not guessed:
  - context.add_cookies() (unlike Selenium's add_cookie) does not require
    the browser to already be on the cookie's domain -- no CDP workaround
    needed, unlike the Selenium prototype this was ported from.
  - The real toolbar lives inside an iframe (title="Data Visualization"),
    found by dumping data-tb-test-id attributes once a top-level search
    came back empty.
  - The Crosstab dialog's sheet thumbnails (role="option", tabindex="0")
    do NOT flip their own aria-selected state on a plain synthetic click
    (whole card OR inner label -- both tried, both confirmed dead ends by
    reading aria-selected back from the real DOM afterward). Only a
    focus() + keyboard Enter reliably does. This is the one genuinely
    surprising step; skip it and the dialog's own Download button
    (data-tb-test-id="export-crosstab-export-Button") stays permanently
    disabled with no error raised anywhere.
  - The visible text "Download" appears on TWO different buttons at once
    once the dialog is open (the toolbar's own Download dropdown button,
    plus the dialog's actual submit button) -- must target the dialog's
    own data-tb-test-id, not a text match, or the click just re-opens the
    dropdown instead of submitting.

VERIFIED LIVE 2026-08-21 against the real account (workbook
"R1 Test Upload - Superstore", dashboard "Commission Model"): downloaded
QuotaAttainment.xlsx and CommissionProjection.xlsx, each independently
parsed with openpyxl -- 41 real sales people, full-precision values
matching the live dashboard's own displayed numbers exactly (e.g. Barbara
Davis: 0.806938 = the dashboard's displayed "81%").

WORKSTATION-PROVEN, NOT YET SNOWFLAKE-PROVEN. R15 already proved Playwright
runs inside a Snowflake Notebook's container runtime, and a first raw-HTTP
feasibility check from inside Snowflake's compute to
prod-useast-b.online.tableau.com already returned a real 200 (network
egress works) -- but the FULL render (this module's actual iframe/dialog
interaction) has not yet been attempted from inside Snowflake, and may
need further network-rule egress additions the same iterative way every
prior fix in this project has needed them. Do not assume it works inside
Snowflake until it has been run there for real.
"""

import io
import logging
import os
import tempfile

log = logging.getLogger(__name__)


class CrosstabFetchError(RuntimeError):
    """Raised with a specific, actionable reason -- never a bare failure.
    The caller (deep_validation.py's evidence pipeline) reports this
    verbatim rather than silently treating the sheet as unresolvable."""


def _view_url(server_url: str, site_content_url: str, dashboard_content_url: str) -> str:
    """The browser-facing view URL for a dashboard, derived from its own
    REST contentUrl (tableau_server.list_views()'s content_url field, e.g.
    'R1TestUpload-Superstore/sheets/CommissionModel') -- NOT the display
    name. FOUND LIVE: passing the display name ('Commission Model', with
    its space URL-encoded to 'Commission%20Model') looks plausible but the
    dashboard never renders behind it -- confirmed by the toolbar iframe
    never appearing. The REST contentUrl's own slug ('CommissionModel', no
    space) is what the browser router actually needs; Tableau's REST API
    puts the view under '.../sheets/<slug>', while the browser URL drops
    the '/sheets' segment."""
    import re

    raw_cu = dashboard_content_url.strip("/")
    browser_cu = re.sub(r"/sheets(?=/|$)", "", raw_cu)
    server_url = server_url.rstrip("/")
    site_prefix = f"/t/{site_content_url}" if site_content_url else ""
    return f"{server_url}{site_prefix}/views/{browser_cu}"


def _xlsx_bytes_to_csv(data: bytes) -> str:
    """Convert a downloaded crosstab .xlsx to CSV text -- the shape
    evidence.set_tableau_csv()/csv_rows() already expects, so this plugs
    into the exact same downstream path the REST crosstab pull uses."""
    import csv as _csv

    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True)
    out = io.StringIO()
    writer = _csv.writer(out)
    ws = wb.worksheets[0]
    for row in ws.iter_rows(values_only=True):
        if any(cell is not None for cell in row):
            writer.writerow(row)
    wb.close()
    return out.getvalue().strip()


def fetch_sheet_crosstab_csv(
    server_url: str,
    site_content_url: str,
    token: str,
    dashboard_content_url: str,
    sheet_name: str,
    *,
    headless: bool = True,
    timeout_s: int = 60,
) -> str:
    """Download ONE nested worksheet's real crosstab via the actual
    Download -> Crosstab dialog, and return it as CSV text.

    token is the already-signed-in REST PAT token (tableau_server's
    TableauRestClient.auth_token) -- injected as a browser cookie, never
    submitted through the login form. dashboard_content_url is the target
    DASHBOARD's own REST contentUrl (tableau_server.list_views()'s
    content_url field for that view, e.g.
    'R1TestUpload-Superstore/sheets/CommissionModel') -- not a display
    name; see _view_url's docstring for why the display name silently
    fails to render.

    Raises CrosstabFetchError with a specific, stated reason on any
    failure (auth didn't take, toolbar never appeared, sheet not in the
    dialog, download never fired) -- never returns a guess."""
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    domain = server_url.replace("https://", "").replace("http://", "").split("/")[0]
    view_url = _view_url(server_url, site_content_url, dashboard_content_url)

    with tempfile.TemporaryDirectory(prefix="tableau_crosstab_") as download_dir:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            try:
                context = browser.new_context(accept_downloads=True)
                context.add_cookies([
                    {"name": "workgroup_session_id", "value": token, "domain": domain, "path": "/"},
                    {"name": "XSRF-TOKEN", "value": token, "domain": domain, "path": "/"},
                ])
                page = context.new_page()
                try:
                    page.goto(view_url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
                except PWTimeout:
                    raise CrosstabFetchError(
                        f"navigation to {view_url!r} timed out after {timeout_s}s")

                if "sso." in page.url or "/idp/" in page.url:
                    raise CrosstabFetchError(
                        "cookie injection did not authenticate -- landed on an "
                        f"SSO/login page ({page.url}); token may be expired")

                frame = _find_viz_frame(page, timeout_s=min(timeout_s, 30))
                if frame is None:
                    raise CrosstabFetchError(
                        f"the real Tableau toolbar never appeared within "
                        f"{min(timeout_s, 30)}s -- dashboard may not have "
                        f"rendered (iframe urls seen: "
                        f"{[fr.url for fr in page.frames]})")

                _open_crosstab_dialog(frame)
                _select_sheet(frame, sheet_name, timeout_s=min(timeout_s, 15))
                path = _click_download_and_wait(page, frame, download_dir,
                                                 timeout_s=min(timeout_s, 30))
                with open(path, "rb") as fh:
                    data = fh.read()
                return _xlsx_bytes_to_csv(data)
            finally:
                browser.close()


def _find_viz_frame(page, timeout_s: int):
    """The real toolbar (Download button and everything else) lives inside
    an iframe (title="Data Visualization"), not the main document -- found
    live by dumping data-tb-test-id attributes once a top-level search came
    back empty. Polls because the iframe can exist before its own content
    (including the toolbar) has finished rendering."""
    import time

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for fr in page.frames:
            if fr.url and "views/" in fr.url:
                btn = fr.query_selector(
                    '[data-tb-test-id="viz-viewer-toolbar-button-download"]')
                if btn:
                    return fr
        time.sleep(1)
    return None


def _open_crosstab_dialog(frame):
    frame.click('[data-tb-test-id="viz-viewer-toolbar-button-download"]')
    try:
        frame.wait_for_selector("text=Crosstab", timeout=8000)
    except Exception:
        raise CrosstabFetchError("Download dropdown opened but no Crosstab option appeared")
    frame.click("text=Crosstab")
    # Let the dialog finish populating every sheet thumbnail -- a first-open
    # race can render only a subset (confirmed live: 2 of 4 thumbnails on
    # the very first open, all 4 after ~1.5s).
    frame.wait_for_timeout(1500)


def _select_sheet(frame, sheet_name: str, timeout_s: int):
    try:
        thumb = frame.wait_for_selector(
            f'[data-tb-test-id^="sheet-thumbnail-"][title="{sheet_name}"]',
            timeout=timeout_s * 1000)
    except Exception:
        visible = [t.get_attribute("title") for t in
                   frame.query_selector_all('[data-tb-test-id^="sheet-thumbnail-"]')]
        raise CrosstabFetchError(
            f"sheet {sheet_name!r} not offered by this dashboard's Crosstab "
            f"dialog -- sheets shown: {visible}")

    # A plain synthetic click (whole card OR inner label -- both tried) never
    # flips this dialog's aria-selected state, confirmed by reading the real
    # DOM back afterward. Only focus() + keyboard Enter reliably does.
    thumb.click()
    thumb.focus()
    frame.page.keyboard.press("Enter")
    frame.wait_for_timeout(500)
    if thumb.get_attribute("aria-selected") != "true":
        frame.page.keyboard.press(" ")
        frame.wait_for_timeout(500)
    if thumb.get_attribute("aria-selected") != "true":
        raise CrosstabFetchError(
            f"selecting {sheet_name!r} in the Crosstab dialog did not "
            "register (aria-selected stayed false after click+Enter+Space)")
    return thumb


def _click_download_and_wait(page, frame, download_dir: str, timeout_s: int) -> str:
    # The visible text "Download" appears on TWO buttons once the dialog is
    # open: the toolbar's own dropdown button, and the dialog's real submit
    # button (data-tb-test-id="export-crosstab-export-Button", confirmed
    # live -- the toolbar one has no such id). Target the real one directly
    # rather than a text match that could hit either.
    btn = frame.query_selector('[data-tb-test-id="export-crosstab-export-Button"]')
    if btn is None:
        raise CrosstabFetchError(
            "the dialog's own Download button was not found -- "
            "export-crosstab-export-Button is missing from the DOM")
    if btn.get_attribute("disabled") is not None:
        raise CrosstabFetchError(
            "the dialog's Download button is still disabled -- sheet "
            "selection did not take even though aria-selected read true")
    try:
        with page.expect_download(timeout=timeout_s * 1000) as dl_info:
            # force=True: Tableau's modal glass overlay sometimes still
            # reports as intercepting pointer events in headless mode even
            # once the real button is visible/enabled/stable.
            btn.click(force=True)
        download = dl_info.value
    except Exception as exc:
        raise CrosstabFetchError(
            f"clicked the dialog's Download button but no file download "
            f"fired within {timeout_s}s: {type(exc).__name__}: {exc}") from exc
    out_path = os.path.join(download_dir, download.suggested_filename)
    download.save_as(out_path)
    return out_path
