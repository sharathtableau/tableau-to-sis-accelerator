"""
app_screenshot.py -- REAL screenshots of the REAL generated Streamlit app.

WHY THIS EXISTS, and what it replaces
-------------------------------------
The Streamlit-side visual evidence used to be a headless RE-RENDER: capture
the Altair/Plotly objects engine.py builds, export them, and composite them
into something dashboard-shaped (headless_render.render_dashboard_to_png).
That made the capture a SECOND RENDERER, and every layout decision it made
independently of the app was a fabrication -- reported as a migration defect
when it differed from Tableau.

On 2026-08-07 five separate bugs of exactly that kind were found and fixed in
one session (the dashboard zone tree thrown away; panel arrangement assumed
horizontal; charts exported at Vega-Lite's default step and then magnified;
rank tables drawn as raw HTML and therefore invisible; unrenderable glyphs
drawn as tofu boxes). Then the user asked the question that ends the whole
argument: *why are you rendering it again -- just screenshot the app.*

They were right. Worse, the re-render was FLATTERING the app: on Customer
Analysis it drew all 30 customer names because it gave the chart more height
than the app does, while the real app drops every other label. A capture that
makes the migration look better than it is, is worse than no capture.

So: run the generated app for real and photograph it.

WHY THIS IS POSSIBLE AT ALL (the old blocker was narrower than it looked)
-------------------------------------------------------------------------
headless_render's docstring rejects screenshotting "the DEPLOYED app" --
correctly: a deployed Streamlit-in-Snowflake app sits behind SSO, and the SiS
sandbox has no browser runtime and no outbound access. But the artifact under
validation is the GENERATED APP, and it runs perfectly well on localhost from
a laptop (restart_apps.py already does this). No SSO is involved, because we
never touch the deployed copy.

SCOPE, stated honestly: this needs a real browser and a local Streamlit, so it
works when validation is run from a workstation -- which is how the validation
packs are actually generated (the Tableau REST connection lives there too). It
CANNOT run from inside the deployed SiS app. `available()` says so, and the
caller is expected to report visual evidence as BLOCKED with that reason
rather than silently substituting an approximation.

WHAT IT DOES NOT REPLACE
------------------------
headless_render.capture_sheet_chart still supplies the STREAMLIT DATA leg of
the row-level three-way comparison (the app's own chart dataframe). That is a
data capture, not a picture, and it stays exactly as it is. Only the IMAGE
path moves here.
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

# The capture viewport. 1800px matches the width the reports already lay out
# at and is close to Tableau's own exported view width, so the two images are
# comparable without rescaling either. 2x device scale keeps text crisp for a
# human reviewer AND for Cortex vision (a downscaled screenshot loses exactly
# the axis labels and KPI values a reviewer needs to read).
VIEWPORT_W = 1800
VIEWPORT_H = 1200
SCALE = 2


def available():
    """(True, None) when a real screenshot is possible here, else
    (False, reason). The reason is meant to be shown to the user as the
    BLOCKED explanation, so it must say what is missing, not just 'no'.

    Each precondition is probed SEPARATELY and names itself, because "no
    screenshot" has several very different causes with very different fixes,
    and a single generic message sends the reader looking in the wrong place.
    A real run inside the deployed SiS sandbox (2026-08-14) returned visual
    BLOCKED for all ten dashboards, and the report showed only "Missing
    required Streamlit dashboard screenshot" -- true, but useless: it does not
    say whether Playwright is absent, Chromium is absent, or the app failed to
    boot. The environment is diagnosed here; failures that can only appear
    later (the app not starting, a tab not rendering) are reported per
    dashboard by capture_app itself."""
    if _in_sis():
        return False, (
            "running inside the Streamlit-in-Snowflake sandbox, which has no "
            "browser and cannot run Playwright. This is expected, not a "
            "defect: generate the validation pack from a workstation (where "
            "the Tableau REST pull already runs) and the real screenshot is "
            "captured there. Visual evidence stays BLOCKED here rather than "
            "being substituted with a server-side re-render.")
    try:
        import streamlit  # noqa: F401
    except Exception as exc:
        return False, (f"streamlit is not importable here ({type(exc).__name__}), "
                       "so the generated app cannot be started to photograph it")
    if shutil.which("streamlit") is None and not _streamlit_module_runnable():
        return False, ("no runnable `streamlit` command on PATH and "
                       "`python -m streamlit` is unavailable, so the generated "
                       "app cannot be served for capture")
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return False, ("playwright is not installed, so the running app "
                       "cannot be photographed (pip install playwright && "
                       "playwright install chromium)")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            browser.close()
    except Exception as exc:
        return False, (f"no usable Chromium for playwright ({type(exc).__name__}) "
                       f"-- run `playwright install chromium`")
    return True, None


def _in_sis():
    """True when running inside Streamlit in Snowflake, where no browser
    exists. Detected from the sandbox's own marker rather than by trying to
    launch a browser and reading the failure, so the reason we report is the
    real one instead of a misleading 'Chromium missing'."""
    if os.environ.get("SNOWFLAKE_STREAMLIT_APP_NAME"):
        return True
    try:
        import snowflake.snowpark.context as _ctx
        _ctx.get_active_session()
        return not os.environ.get("SNOWFLAKE_HOME") and not shutil.which("streamlit")
    except Exception:
        return False


def _streamlit_module_runnable():
    try:
        import importlib.util
        return importlib.util.find_spec("streamlit.web.cli") is not None
    except Exception:
        return False


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http(url, timeout=120):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.status == 200:
                    return True, None
        except Exception as exc:
            last = exc
        time.sleep(1)
    return False, f"app did not start within {timeout}s ({last})"


def _settle(page, tries=40, quiet=3):
    """Wait until the DOM stops growing. Streamlit STREAMS its render, so
    `networkidle` alone fires while charts are still being drawn -- a
    screenshot taken then catches a half-painted dashboard and reports it as
    a migration defect."""
    last, stable = None, 0
    for _ in range(tries):
        page.wait_for_timeout(500)
        try:
            now = page.evaluate("document.body.innerHTML.length")
        except Exception:
            continue
        if now == last:
            stable += 1
            if stable >= quiet:
                return True
        else:
            stable = 0
        last = now
    return False


def capture_app(app_path, width=VIEWPORT_W, height=VIEWPORT_H, scale=SCALE,
                port=None, startup_timeout=120):
    """Run `app_path` locally and screenshot every dashboard TAB.

    The generated app renders its dashboards as `st.tabs(...)` (engine.run),
    so each tab is clicked and its own tabpanel photographed -- not the whole
    page, which would also catch the tab strip and any sibling panel.

    Returns (shots, notes) where shots is {tab_title: png_bytes} and notes is
    a list of {"dashboard", "captured": bool, "reason": str_or_None}. Always
    tears the server and browser down, including on failure -- a leaked
    Streamlit process holds its port and silently serves stale code to the
    NEXT run (the exact failure restart_apps.py exists to clean up)."""
    ok, why = available()
    if not ok:
        return {}, [{"dashboard": None, "captured": False, "reason": why}]
    if not os.path.exists(app_path):
        return {}, [{"dashboard": None, "captured": False,
                     "reason": f"generated app not found: {app_path}"}]

    from playwright.sync_api import sync_playwright

    port = port or _free_port()
    url = f"http://localhost:{port}"
    # Streamlit's own console output (e.g. a "missing ScriptRunContext"
    # warning per rerun) is redirected to a FILE, never `subprocess.PIPE`
    # left undrained. FOUND 2026-08-10: with PIPE, the OS pipe buffer (64 KB
    # on Windows) fills after a couple of dashboards render, and the
    # Streamlit process then BLOCKS ON WRITE mid-render -- capture_app
    # timed out on every tab after the first two, every time, and looked
    # exactly like a slow/broken app. It was a hung child process.
    log_path = os.path.join(tempfile.mkdtemp(prefix="st_shot_"), "server.log")
    log_fh = open(log_path, "wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", app_path,
         "--server.port", str(port), "--server.headless", "true",
         "--server.fileWatcherType", "none",
         "--browser.gatherUsageStats", "false"],
        stdout=log_fh, stderr=subprocess.STDOUT,
        cwd=os.path.dirname(os.path.abspath(app_path)) or None)

    shots, notes = {}, []
    try:
        up, why = _wait_http(url, startup_timeout)
        if not up:
            return {}, [{"dashboard": None, "captured": False, "reason": why}]

        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(
                    viewport={"width": width, "height": height},
                    device_scale_factor=scale)
                page.goto(url, wait_until="networkidle", timeout=startup_timeout * 1000)
                _settle(page)

                tabs = page.locator('button[role="tab"]')
                # WAIT for the tabs rather than counting immediately. A settled
                # DOM does not mean a finished app: Streamlit paints its
                # "RUNNING..." shell first, and the tab strip only appears once
                # the script reaches st.tabs(). Against a LOCAL DuckDB backend
                # that is near-instant, so counting straight away worked; run
                # the same app inside Snowflake, where every query is a
                # round trip, and the page still reads "RUNNING..." here --
                # which looked exactly like an app that had failed to start
                # (found 2026-08-14 running the capture in a Snowflake
                # notebook: 0 tabs, no error, a perfectly healthy app).
                try:
                    tabs.first.wait_for(timeout=startup_timeout * 1000)
                except Exception:
                    pass
                n = tabs.count()
                if not n:
                    body = ""
                    try:
                        body = page.inner_text("body")[:200].replace("\n", " ")
                    except Exception:
                        pass
                    still_running = "RUNNING" in body.upper()
                    return {}, [{"dashboard": None, "captured": False,
                                 "reason": ("the app was still running after "
                                            f"{startup_timeout}s and never drew "
                                            "its dashboard tabs -- it needs "
                                            "longer, or a query is hanging"
                                            if still_running else
                                            "the app rendered no dashboard tabs "
                                            "(it may have failed to start -- "
                                            "check the app's own error output)")
                                           + (f" [page: {body!r}]" if body else "")}]
                titles = [tabs.nth(i).inner_text().strip() for i in range(n)]
                for i, title in enumerate(titles):
                    try:
                        tabs.nth(i).click()
                        # Wait on the PANEL, not just the page. Switching tabs
                        # collapses the outgoing panel to height 0 and the
                        # incoming one grows as Streamlit repaints it, while
                        # document length can already be stable -- so a
                        # page-level settle alone raced the repaint and
                        # produced a <1 KB screenshot of a 0-height panel
                        # (hit on Order Details and Executive Overview).
                        thin = None
                        try:
                            page.wait_for_function(
                                """(ix) => {
                                     const els = document.querySelectorAll(
                                         'div[role="tabpanel"]');
                                     const el = els[ix];
                                     return el && !el.hasAttribute('hidden')
                                            && el.getBoundingClientRect().height > 50;
                                   }""",
                                arg=i, timeout=45000)
                        except Exception:
                            # A genuinely empty dashboard (a caption-only or
                            # scaffolding tab) never grows past 50px. Capture
                            # it anyway and SAY it looked empty -- an empty
                            # dashboard is a real finding, not a capture error.
                            thin = ("the app rendered this tab nearly empty "
                                    "(panel never exceeded 50px tall)")
                        _settle(page)
                        panel = page.locator('div[role="tabpanel"]').nth(i)
                        png = panel.screenshot()
                        shots[title] = png
                        notes.append({"dashboard": title, "captured": True,
                                      "reason": thin})
                    except Exception as exc:
                        notes.append({"dashboard": title, "captured": False,
                                      "reason": f"{type(exc).__name__}: {exc}"})
            finally:
                browser.close()
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        log_fh.close()
    if any(not n["captured"] for n in notes):
        try:
            tail = open(log_path, "rb").read()[-2000:].decode("utf-8", "replace")
        except Exception:
            tail = "(server log unreadable)"
        notes.append({"dashboard": None, "captured": False,
                      "reason": f"the app server's own log, last 2000 bytes:\n{tail}"})
    return shots, notes
