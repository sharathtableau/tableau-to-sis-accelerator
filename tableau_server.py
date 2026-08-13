"""Tableau Server / Tableau Cloud REST client -- R1 (pull a workbook by link).

Vendored (not imported from ~/.claude/skills/tableau) because the SiS deploy
bundle cannot depend on anything outside this repo -- see NEW_CHAT.md's R1
scope note. Trimmed to exactly what R1 needs: sign in with a Personal Access
Token, resolve a pasted view/workbook URL to a workbook id, and download the
.twbx. Adapted from ~/.claude/skills/tableau/scripts/rest_api_client.py.

Credential rule (matches this project's own prohibited-action boundary): the
PAT is read from an env var / Streamlit secret by the CALLER and passed in --
this module never prompts for, stores, or logs it.
"""
import os
import re
import xml.etree.ElementTree as ET
from typing import Optional, Dict, Any
from urllib.parse import urlparse

import requests

XMLNS = {"t": "http://tableau.com/api"}
DEFAULT_API_VERSION = "3.22"


# Name of the Snowflake Secret object bound to this app's SECRETS clause in
# Snowsight (ALTER STREAMLIT ... SET SECRETS = (SIS_SECRET_NAME = <secret>)).
# Must match the SQL setup in tableau_server_sis_setup.sql exactly -- it's a
# string key, not auto-discovered.
SIS_SECRET_NAME = "tableau_pat"


class TableauAuthError(Exception):
    pass


class TableauLookupError(Exception):
    pass


def parse_site_url(url: str) -> Dict[str, str]:
    """Lenient parse of ANY pasted Tableau link (home page, workbook page,
    view page -- doesn't matter) down to just server_url + site_content_url,
    for the browse-by-dropdown flow: the user only needs to point at their
    SITE, not name a specific workbook. Raises ValueError only when the
    string isn't a URL at all."""
    parsed = urlparse(url.strip())
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"not a URL: {url!r}")
    server_url = f"{parsed.scheme}://{parsed.netloc}"
    frag = (parsed.fragment or parsed.path.lstrip("/")).lstrip("/")
    parts = [p for p in frag.split("/") if p]
    site = parts[1] if parts[:1] == ["site"] and len(parts) >= 2 else ""
    return {"server_url": server_url, "site_content_url": site}


def parse_tableau_url(url: str) -> Dict[str, str]:
    """Parse a pasted Tableau Cloud/Server view or workbook URL.

    Handles the two shapes that actually appear in the wild:
      Tableau Cloud:  https://<pod>.online.tableau.com/#/site/<site>/views/<wb>/<view>
                       https://<pod>.online.tableau.com/#/site/<site>/workbooks/<id>
      Tableau Server: https://<server>/#/site/<site>/views/<wb>/<view>
                       https://<server>/#/views/<wb>/<view>                (default site)

    Returns dict: server_url, site_content_url, content_url (workbook slug,
    '' if the URL names a workbook id directly instead), workbook_id (or '').
    Raises ValueError on a URL that doesn't match either shape -- never
    guesses, matching this project's "surface a choice, don't guess" rule.
    """
    parsed = urlparse(url.strip())
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"not a URL: {url!r}")
    server_url = f"{parsed.scheme}://{parsed.netloc}"

    # Tableau's SPA router puts the real path after a literal '#'.
    frag = parsed.fragment or parsed.path.lstrip("/")
    frag = frag.lstrip("/")
    parts = [p for p in frag.split("/") if p]

    site = ""
    workbook_id = ""
    content_url = ""

    if parts[:1] == ["site"] and len(parts) >= 2:
        site = parts[1]
        rest = parts[2:]
    else:
        rest = parts

    if rest[:1] == ["views"] and len(rest) >= 2:
        # /views/<workbookSlug>/<viewSlug> -- content_url identifies the
        # workbook; a REST lookup (find_workbook_by_content_url) resolves it
        # to a real id.
        content_url = rest[1]
    elif rest[:1] == ["workbooks"] and len(rest) >= 2:
        candidate = rest[1]
        if candidate.isdigit():
            # Tableau's web UI routes workbook OVERVIEW pages (the "Views"
            # list tab) through a purely-numeric internal id -- confirmed
            # live (2026-07-28) that the REST API 404s on it directly
            # (GET /workbooks/<numeric-id> -> 404006 Resource Not Found) and
            # it is NOT a contentUrl slug either (those are never all-digit).
            # There is no REST-resolvable path from this id alone -- fail
            # clearly instead of silently trying (and predictably failing) a
            # contentUrl lookup that can never match.
            raise ValueError(
                f"this Tableau URL uses the workbook overview page's internal "
                f"numeric id ({candidate!r}), which the REST API cannot "
                f"resolve. Open a specific VIEW inside the workbook (or use "
                f"Tableau's Share -> Copy Link) to get a URL shaped like "
                f".../views/<workbook>/<view> instead: {url!r}"
            )
        elif re.fullmatch(r"[0-9a-fA-F-]{8,}", candidate):
            # A real REST GUID (e.g. from a Share -> Copy Link on some
            # Tableau versions) -- usable directly, no lookup needed.
            workbook_id = candidate
        else:
            content_url = candidate
    else:
        raise ValueError(
            f"unrecognized Tableau URL shape (expected .../views/<wb>/<view> "
            f"or .../workbooks/<id>): {url!r}"
        )

    return {
        "server_url": server_url,
        "site_content_url": site,
        "content_url": content_url,
        "workbook_id": workbook_id,
    }


class TableauRestClient:
    """Minimal REST client: sign in, resolve a workbook, download it."""

    def __init__(self, server_url: str, api_version: str = DEFAULT_API_VERSION):
        self.server_url = server_url.rstrip("/")
        self.api_version = api_version
        self.base_url = f"{self.server_url}/api/{api_version}"
        self.auth_token: Optional[str] = None
        self.site_id: Optional[str] = None

    def _headers(self, content_type: str = "application/xml",
                accept: Optional[str] = None) -> Dict[str, str]:
        """REAL BUG, found live 2026-08-04: `Accept` was hardcoded to
        "application/xml" regardless of `content_type`, so every VDS call
        (JSON-only -- it has no XML representation at all) failed with
        `400 "No acceptable representation"` on EVERY datasource, on the
        real account -- a textbook HTTP content-negotiation mismatch, not a
        permissions or auth problem. `accept` now defaults to `content_type`
        (the two are the same for every existing XML caller, so nothing
        about the classic REST API changes) and VDS passes JSON explicitly
        for both."""
        headers = {"Content-Type": content_type,
                  "Accept": accept if accept is not None else content_type}
        if self.auth_token:
            headers["X-Tableau-Auth"] = self.auth_token
        return headers

    def _check(self, resp: "requests.Response", expected: int = 200) -> None:
        if resp.status_code == expected:
            return
        try:
            root = ET.fromstring(resp.content)
            err = root.find(".//t:error", XMLNS)
            if err is not None:
                code = err.get("code", "unknown")
                summary = err.findtext("t:summary", "Unknown error", XMLNS)
                detail = err.findtext("t:detail", "", XMLNS)
                raise TableauAuthError(f"Tableau API error {code}: {summary} — {detail}")
        except ET.ParseError:
            pass
        raise TableauAuthError(f"HTTP {resp.status_code}: {resp.text[:300]}")

    def sign_in_with_pat(self, token_name: str, token_secret: str,
                          site_content_url: str = "") -> None:
        url = f"{self.base_url}/auth/signin"
        req = ET.Element("tsRequest")
        creds = ET.SubElement(req, "credentials",
                              personalAccessTokenName=token_name,
                              personalAccessTokenSecret=token_secret)
        ET.SubElement(creds, "site", contentUrl=site_content_url)
        resp = requests.post(url, data=ET.tostring(req, encoding="unicode"),
                             headers=self._headers(), timeout=30)
        self._check(resp)
        root = ET.fromstring(resp.content)
        c = root.find(".//t:credentials", XMLNS)
        self.auth_token = c.get("token")
        self.site_id = c.find("t:site", XMLNS).get("id")

    def sign_out(self) -> None:
        if not self.auth_token:
            return
        try:
            requests.post(f"{self.base_url}/auth/signout",
                          headers=self._headers(), timeout=15)
        finally:
            self.auth_token = None
            self.site_id = None

    def _get_paginated(self, path: str, tag: str, page_size: int = 1000) -> "list[ET.Element]":
        """GET a REST list endpoint, following <pagination> until every page
        is fetched -- a site with >1 page (>1000 items) must not silently
        show only the first page in a dropdown, that's the same class of
        silent-partial-result bug this project guards against elsewhere."""
        sep = "&" if "?" in path else "?"
        elements: "list[ET.Element]" = []
        page = 1
        while True:
            url = f"{self.base_url}/{path}{sep}pageSize={page_size}&pageNumber={page}"
            resp = requests.get(url, headers=self._headers(), timeout=30)
            self._check(resp)
            root = ET.fromstring(resp.content)
            batch = root.findall(f".//t:{tag}", XMLNS)
            elements.extend(batch)
            pag = root.find(".//t:pagination", XMLNS)
            total = int(pag.get("totalAvailable", len(elements))) if pag is not None else len(elements)
            if len(elements) >= total or not batch:
                break
            page += 1
        return elements

    def list_projects(self) -> "list[Dict[str, Any]]":
        els = self._get_paginated(f"sites/{self.site_id}/projects", "project")
        return [{"id": p.get("id"), "name": p.get("name"),
                 "parent_project_id": p.get("parentProjectId")} for p in els]

    def list_workbooks(self) -> "list[Dict[str, Any]]":
        els = self._get_paginated(f"sites/{self.site_id}/workbooks", "workbook")
        out = []
        for wb in els:
            proj = wb.find("t:project", XMLNS)
            out.append({
                "id": wb.get("id"), "name": wb.get("name"),
                "content_url": wb.get("contentUrl"),
                "project_id": proj.get("id") if proj is not None else None,
                "project_name": proj.get("name") if proj is not None else None,
            })
        return out

    def find_workbook_by_content_url(self, content_url: str) -> Dict[str, Any]:
        """Resolve a workbook's URL slug to its REST id via a server-side filter."""
        url = (f"{self.base_url}/sites/{self.site_id}/workbooks"
               f"?filter=contentUrl:eq:{content_url}")
        resp = requests.get(url, headers=self._headers(), timeout=30)
        self._check(resp)
        root = ET.fromstring(resp.content)
        wb = root.find(".//t:workbook", XMLNS)
        if wb is None:
            raise TableauLookupError(
                f"no workbook found with contentUrl={content_url!r} on this site")
        return {"id": wb.get("id"), "name": wb.get("name"),
                "content_url": wb.get("contentUrl")}

    def download_workbook_bytes(self, workbook_id: str,
                                 include_extract: bool = True) -> bytes:
        """Download a workbook's .twbx content as bytes (never touches disk --
        the caller hands the bytes straight to pipeline.onboard, same as an
        uploaded file's up.getvalue())."""
        url = f"{self.base_url}/sites/{self.site_id}/workbooks/{workbook_id}/content"
        if not include_extract:
            url += "?includeExtract=false"
        resp = requests.get(url, headers=self._headers(), stream=True, timeout=120)
        self._check(resp)
        return resp.content

    def list_views(self, workbook_id: Optional[str] = None) -> "list[Dict[str, Any]]":
        """List views on the site, or scoped to one workbook -- the id each
        maps to a dashboard/sheet, needed to pull that section's own rendered
        data (R2's tableau_truth). Scoping by workbook (rather than filtering
        the site-wide list client-side) matches this project's own R1
        pagination discipline -- a large site's global view list could span
        many pages for no reason when the caller already knows the workbook."""
        if workbook_id:
            path = f"sites/{self.site_id}/workbooks/{workbook_id}/views"
        else:
            path = f"sites/{self.site_id}/views"
        els = self._get_paginated(path, "view")
        out = []
        for v in els:
            wb = v.find("t:workbook", XMLNS)
            out.append({
                "id": v.get("id"), "name": v.get("name"),
                "content_url": v.get("contentUrl"),
                "workbook_id": wb.get("id") if wb is not None else workbook_id,
            })
        return out

    def query_view_data_csv(self, view_id: str) -> str:
        """R2 -- pull a view's data AS RENDERED (the same aggregation Tableau
        itself displays on screen), not the underlying extract rows. This is
        the literal ground-truth number for a dashboard section: Tableau's
        REST 'Query View Data' endpoint returns a CSV of the summary data
        backing the view exactly as the user would see it, so a value pulled
        here can be compared straight against the migrated app's SQL result
        with no re-aggregation on our side."""
        url = f"{self.base_url}/sites/{self.site_id}/views/{view_id}/data"
        resp = requests.get(url, headers=self._headers(), timeout=60)
        self._check(resp)
        return resp.content.decode("utf-8-sig")

    def query_view_image(self, view_id: str, high_resolution: bool = True) -> bytes:
        """R8 -- pull a view's RENDERED IMAGE (PNG bytes) via Tableau REST's
        'Query View Image' endpoint. This is the real Tableau-side half of
        R8's vision validation: the actual pixels Tableau renders, pulled
        over REST -- never a screenshot of any Tableau UI, never something
        this project scrapes or guesses at. high_resolution=True asks for
        the higher-DPI render (?resolution=high) -- closer to what a human
        reviewer or a vision model actually needs to read small KPI text."""
        url = f"{self.base_url}/sites/{self.site_id}/views/{view_id}/image"
        if high_resolution:
            url += "?resolution=high"
        resp = requests.get(url, headers=self._headers(), stream=True, timeout=90)
        self._check(resp)
        return resp.content


    def list_datasources(self) -> "list[Dict[str, Any]]":
        """Published data sources on the site. VDS (below) queries a PUBLISHED
        datasource by LUID, so this is how a workbook's measure is located.

        IMPORTANT PRECONDITION, stated rather than discovered later: a
        workbook published with EMBEDDED data has no separately-published
        datasource, so it will NOT appear here and VDS cannot query it. That
        is a real limit of the approach, not a bug in this call -- the caller
        surfaces it as a stated reason."""
        els = self._get_paginated(f"sites/{self.site_id}/datasources",
                                  "datasource")
        out = []
        for e in els:
            proj = e.find("{*}project")
            out.append({"id": e.get("id"), "name": e.get("name"),
                        "content_url": e.get("contentUrl"),
                        "type": e.get("type"),
                        "project": proj.get("name") if proj is not None else None})
        return out

    def vds_read_metadata(self, datasource_luid: str) -> "list[Dict[str, Any]]":
        """VizQL Data Service /read-metadata -- the fields a datasource
        actually exposes. Used to confirm a measure EXISTS before asking for
        its aggregate, so a missing field is reported as "not present in this
        datasource" instead of surfacing as an opaque query error."""
        url = (f"{self.server_url}/api/v1/vizql-data-service/read-metadata")
        body = {"datasource": {"datasourceLuid": datasource_luid}}
        resp = requests.post(url, headers=self._headers("application/json"),
                             json=body, timeout=60)
        self._check(resp)
        payload = resp.json() or {}
        return payload.get("data", payload.get("fields", [])) or []

    def vds_query_aggregate(self, datasource_luid: str,
                            field_captions: "list[str]",
                            function: str = "SUM") -> "Dict[str, float]":
        """VizQL Data Service /query-datasource -- ask TABLEAU'S OWN ENGINE for
        a TRUE AGGREGATE of each field, with no dimensions in the query.

        THIS IS THE POINT OF THE WHOLE APPROACH (chosen by the user 2026-07-30
        over the previous "sum a rendered dashboard view" tier, which was
        unsound): a query carrying ONLY aggregated fields and NO grouping
        fields returns ONE ROW -- the real grand total, computed by Tableau
        itself against the governed datasource. It does not depend on any
        dashboard happening to display a total, and it cannot be inflated by a
        crosstab repeating rows (the 6x Quantity error found live 2026-07-30)
        or deflated by a view's own filter, because there is no view involved.

        Returns {field_caption: value}. Response key spellings differ by
        Tableau version ("SUM(Discount)" vs "Discount"), so both are accepted
        rather than assuming one -- an unmatched field is simply absent from
        the result, never guessed at."""
        url = (f"{self.server_url}/api/v1/vizql-data-service/query-datasource")
        body = {
            "datasource": {"datasourceLuid": datasource_luid},
            "query": {"fields": [{"fieldCaption": c, "function": function}
                                 for c in field_captions]},
        }
        resp = requests.post(url, headers=self._headers("application/json"),
                             json=body, timeout=120)
        self._check(resp)
        rows = (resp.json() or {}).get("data") or []
        if not rows:
            return {}
        row = rows[0]                      # aggregate-only query => single row
        out: "Dict[str, float]" = {}
        for cap in field_captions:
            for key in (f"{function}({cap})", cap, f"{function.lower()}({cap})"):
                if key in row:
                    try:
                        out[cap] = float(row[key])
                    except (TypeError, ValueError):
                        pass
                    break
        return out


def pull_tableau_aggregates(server_url: str, site_content_url: str,
                            field_captions: "list[str]",
                            datasource_name_hints: "Optional[list[str]]" = None,
                            token_name: Optional[str] = None,
                            token_secret: Optional[str] = None) -> "Dict[str, Any]":
    """Self-contained sign-in -> find published datasources -> ask VDS for the
    TRUE aggregate of each requested field -> sign out. One session for the
    whole workbook, same shape as pull_all_view_csvs/pull_all_view_images.

    Returns {"values": {caption: value}, "notes": [...], "datasources": [...]}.
    `notes` explains EVERY outcome -- which datasource answered, which fields
    it did not carry, and (the common real-world case) that the site has no
    published datasource at all because the workbook embeds its data. Nothing
    here guesses: a field that cannot be resolved is simply absent."""
    name, secret = _resolve_pat(token_name, token_secret)
    client = TableauRestClient(server_url)
    values: "Dict[str, float]" = {}
    notes: "list[Dict[str, Any]]" = []
    try:
        client.sign_in_with_pat(name, secret, site_content_url)
        try:
            datasources = client.list_datasources()
        except Exception as e:
            return {"values": {}, "datasources": [],
                    "notes": [{"datasource": "-", "error":
                               f"could not list published datasources -- "
                               f"{type(e).__name__}: {e}"}]}
        if not datasources:
            notes.append({"datasource": "-", "error":
                          "this site has NO published data sources -- a "
                          "workbook published with EMBEDDED data cannot be "
                          "queried by the VizQL Data Service. Publish the "
                          "datasource separately to enable true Tableau "
                          "aggregates."})
        hints = [h.lower() for h in (datasource_name_hints or [])]
        ordered = sorted(
            datasources,
            key=lambda d: 0 if any(h in (d.get("name") or "").lower()
                                   for h in hints) else 1)
        for ds in ordered:
            remaining = [c for c in field_captions if c not in values]
            if not remaining:
                break
            try:
                got = client.vds_query_aggregate(ds["id"], remaining)
            except Exception as e:
                notes.append({"datasource": ds.get("name"),
                              "error": f"{type(e).__name__}: {e}"})
                continue
            values.update({k: v for k, v in got.items() if k not in values})
            notes.append({"datasource": ds.get("name"),
                          "matched": sorted(got),
                          "not_found": sorted(set(remaining) - set(got))})
        return {"values": values, "datasources": datasources, "notes": notes}
    finally:
        client.sign_out()


def _pat_from_sis_secret() -> "tuple[Optional[str], Optional[str]]":
    """Streamlit-in-Snowflake has NO .env/OS-env-var secret mechanism -- the
    real one is a Snowflake Secret object bound to the app's SECRETS clause,
    read via the `_snowflake` module (only importable when actually running
    inside a Snowsight-hosted app). Returns (None, None) outside SiS or when
    no secret is bound -- never raises, so local dev (env vars) is unaffected.
    See tableau_server_sis_setup.sql for the one-time account setup this
    depends on (network rule + secret + external access integration)."""
    try:
        import _snowflake  # noqa: F401 -- only resolvable inside SiS
    except ImportError:
        return None, None
    try:
        cred = _snowflake.get_username_password(SIS_SECRET_NAME)
        return cred.username, cred.password
    except Exception:
        return None, None


def _resolve_pat(token_name: Optional[str], token_secret: Optional[str]) -> "tuple[str, str]":
    """Shared PAT resolution: explicit args -> TABLEAU_PAT_NAME/TABLEAU_PAT_SECRET
    env vars (local dev / non-SiS deployments) -> the Snowflake Secret object
    bound to this app's SECRETS clause (SIS_SECRET_NAME -- see
    tableau_server_sis_setup.sql). Raises TableauAuthError, never silently
    proceeds unauthenticated."""
    token_name = token_name or os.environ.get("TABLEAU_PAT_NAME")
    token_secret = token_secret or os.environ.get("TABLEAU_PAT_SECRET")
    if not token_name or not token_secret:
        token_name, token_secret = _pat_from_sis_secret()
    if not token_name or not token_secret:
        raise TableauAuthError(
            "no Tableau Personal Access Token configured -- set "
            "TABLEAU_PAT_NAME / TABLEAU_PAT_SECRET (env var, for local dev) "
            "or bind a Snowflake Secret named "
            f"'{SIS_SECRET_NAME}' to this app (see "
            "tableau_server_sis_setup.sql) before pulling a workbook by link. "
            "Note: a Streamlit-in-Snowflake app also needs an External Access "
            "Integration to reach Tableau's domain at all -- see the same "
            "setup script.")
    return token_name, token_secret


def list_site_contents(server_url: str, site_content_url: str = "",
                        token_name: Optional[str] = None,
                        token_secret: Optional[str] = None) -> Dict[str, Any]:
    """Sign in, list every project + workbook on the site, sign out. Self-
    contained (same shape as fetch_workbook) -- no session held open across
    Streamlit reruns. Feeds the project/workbook DROPDOWN flow, which sidesteps
    URL-shape guessing entirely (see the 2026-07-28 numeric-workbook-id find:
    Tableau's own web UI links aren't uniformly REST-resolvable)."""
    token_name, token_secret = _resolve_pat(token_name, token_secret)
    client = TableauRestClient(server_url)
    try:
        client.sign_in_with_pat(token_name, token_secret, site_content_url)
        return {"projects": client.list_projects(), "workbooks": client.list_workbooks()}
    finally:
        client.sign_out()


def fetch_workbook_by_id(server_url: str, site_content_url: str, workbook_id: str,
                          name_hint: Optional[str] = None,
                          token_name: Optional[str] = None,
                          token_secret: Optional[str] = None,
                          include_extract: bool = True) -> Dict[str, Any]:
    """Download a workbook whose REAL id is already known (from
    list_site_contents' dropdown data) -- no URL parsing, no contentUrl
    lookup, nothing to misinterpret."""
    token_name, token_secret = _resolve_pat(token_name, token_secret)
    client = TableauRestClient(server_url)
    try:
        client.sign_in_with_pat(token_name, token_secret, site_content_url)
        data = client.download_workbook_bytes(workbook_id, include_extract=include_extract)
        fname = re.sub(r"[^0-9A-Za-z._-]+", "_", name_hint or workbook_id)
        if not fname.lower().endswith((".twb", ".twbx")):
            fname += ".twbx"
        return {"filename": fname, "bytes": data, "workbook_id": workbook_id, "name": name_hint}
    finally:
        client.sign_out()


def pull_all_view_csvs(server_url: str, site_content_url: str, workbook_id: str,
                        token_name: Optional[str] = None,
                        token_secret: Optional[str] = None) -> "list[Dict[str, Any]]":
    """R2 -- self-contained signin -> list every view in one workbook -> pull
    EACH view's rendered data CSV -> signout. One session for the whole
    workbook (not one signin per view) -- the multi-view analogue of
    list_site_contents/fetch_workbook_by_id's self-contained shape. A single
    view's CSV pull failing (e.g. a chart type the REST export can't handle)
    does not abort the rest -- that view's entry carries its own error, the
    remaining views still get pulled.

    Returns a list of {"view": name, "id": view_id, "csv": text_or_None,
    "error": None_or_str}. Pure REST client -- no knowledge of calc_metrics
    or how the CSV maps to anything; that's parity.py's job."""
    token_name, token_secret = _resolve_pat(token_name, token_secret)
    client = TableauRestClient(server_url)
    out = []
    try:
        client.sign_in_with_pat(token_name, token_secret, site_content_url)
        views = client.list_views(workbook_id=workbook_id)
        for v in views:
            try:
                csv_text = client.query_view_data_csv(v["id"])
                out.append({"view": v["name"], "id": v["id"],
                           "csv": csv_text, "error": None})
            except Exception as e:
                out.append({"view": v["name"], "id": v["id"], "csv": None,
                           "error": f"{type(e).__name__}: {e}"})
    finally:
        client.sign_out()
    return out


def pull_all_view_images(server_url: str, site_content_url: str, workbook_id: str,
                          token_name: Optional[str] = None,
                          token_secret: Optional[str] = None,
                          high_resolution: bool = True) -> "list[Dict[str, Any]]":
    """R8 -- self-contained signin -> list every view in one workbook -> pull
    EACH view's rendered PNG -> signout. Same self-contained, one-session-per-
    workbook shape as pull_all_view_csvs (R2) -- deliberately mirrored rather
    than re-invented, since the two are the image/data halves of the exact
    same 'pull every view once, real errors don't abort the rest' job. A
    single view's image pull failing does not abort the rest.

    Returns a list of {"view": name, "id": view_id, "png": bytes_or_None,
    "error": None_or_str}. Pure REST client -- no knowledge of what the
    image gets compared against; that's the vision-diff layer's job (R8,
    not yet built)."""
    token_name, token_secret = _resolve_pat(token_name, token_secret)
    client = TableauRestClient(server_url)
    out = []
    try:
        client.sign_in_with_pat(token_name, token_secret, site_content_url)
        views = client.list_views(workbook_id=workbook_id)
        for v in views:
            try:
                png = client.query_view_image(v["id"], high_resolution=high_resolution)
                out.append({"view": v["name"], "id": v["id"],
                           "png": png, "error": None})
            except Exception as e:
                out.append({"view": v["name"], "id": v["id"], "png": None,
                           "error": f"{type(e).__name__}: {e}"})
    finally:
        client.sign_out()
    return out


def fetch_view_image(server_url: str, site_content_url: str, view_id: str,
                      token_name: Optional[str] = None,
                      token_secret: Optional[str] = None,
                      high_resolution: bool = True) -> bytes:
    """R8 -- self-contained signin -> pull -> signout for ONE view's image,
    same shape as fetch_view_data (R2). Kept as its own entry point for the
    single-view case; pull_all_view_images is the one-session-for-many-views
    case, same split as the R2 CSV pair."""
    token_name, token_secret = _resolve_pat(token_name, token_secret)
    client = TableauRestClient(server_url)
    try:
        client.sign_in_with_pat(token_name, token_secret, site_content_url)
        return client.query_view_image(view_id, high_resolution=high_resolution)
    finally:
        client.sign_out()


def fetch_view_data(server_url: str, site_content_url: str, view_id: str,
                     token_name: Optional[str] = None,
                     token_secret: Optional[str] = None) -> str:
    """R2 -- self-contained sign-in/pull/sign-out (same shape as
    fetch_workbook_by_id) for one view's rendered data CSV. Kept as its own
    entry point rather than folded into fetch_workbook_by_id because a
    caller building tableau_truth needs many views' data from ONE session,
    not a fresh sign-in per view -- see list_views + this in a loop, both
    scoped to a single TableauRestClient session, for that case."""
    token_name, token_secret = _resolve_pat(token_name, token_secret)
    client = TableauRestClient(server_url)
    try:
        client.sign_in_with_pat(token_name, token_secret, site_content_url)
        return client.query_view_data_csv(view_id)
    finally:
        client.sign_out()


def fetch_workbook(url: str, token_name: Optional[str] = None,
                    token_secret: Optional[str] = None,
                    include_extract: bool = True) -> Dict[str, Any]:
    """End-to-end: pasted URL -> (filename, bytes). Kept as a direct-link
    fallback alongside the project/workbook dropdown flow (list_site_contents
    + fetch_workbook_by_id) -- pipeline_app.py offers both.

    Never reads a token from the pasted URL or a hardcoded default -- an
    unset PAT is a clear error, not a silent guess."""
    token_name, token_secret = _resolve_pat(token_name, token_secret)
    loc = parse_tableau_url(url)
    client = TableauRestClient(loc["server_url"])
    try:
        client.sign_in_with_pat(token_name, token_secret, loc["site_content_url"])
        wb_id = loc["workbook_id"]
        name = None
        if not wb_id:
            found = client.find_workbook_by_content_url(loc["content_url"])
            wb_id = found["id"]
            name = found["name"]
        data = client.download_workbook_bytes(wb_id, include_extract=include_extract)
        fname = re.sub(r"[^0-9A-Za-z._-]+", "_", name or loc["content_url"] or wb_id)
        if not fname.lower().endswith((".twb", ".twbx")):
            fname += ".twbx"
        return {"filename": fname, "bytes": data, "workbook_id": wb_id, "name": name}
    finally:
        client.sign_out()
