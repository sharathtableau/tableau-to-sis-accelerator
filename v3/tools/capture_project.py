"""Tableau's own per-sheet numbers for every dashboard of a Tableau Cloud project (the reference the exact loop
proves sheets against). REST "Query View Data" returns one sheet per view; this reads EVERY worksheet of every view
with the official Embedding API (tools/tableau_sheet_capture.js, getSummaryDataReaderAsync).

    python -X utf8 tools/capture_project.py --server https://<pod>.online.tableau.com --site <site>
                                            --project "<name>" [--limit N] [--out reports/<folder>]

* REST (read-only): the owner's Personal Access Token from the environment (TABLEAU_PAT_NAME / TABLEAU_PAT_SECRET;
  never read or printed here) lists the project's workbooks and views and downloads each workbook file.
* Browser: a Chromium window opens on the Tableau site; the OWNER signs in there (this tool never sees credentials).
  The window keeps its own profile folder (<out>/.browser_profile) so later runs need no new sign-in.
* Writes per workbook: <out>/<workbook file> and <out>/<workbook stem>.capture.json (tableau-capture-bundle/1).
  Unattended: --connected-app (hidden browser, no person signs in) uses a Tableau Connected App token built from
  TABLEAU_CA_CLIENT_ID / TABLEAU_CA_SECRET_ID / TABLEAU_CA_SECRET_VALUE / TABLEAU_CA_USERNAME (set by the owner;
  never printed or written; a fresh 5-minute token per view, its own browser profile).
  Nothing is written to Tableau or Snowflake; no Cortex call. Then run tools/sheet_census.py --folder <out>.
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

BUNDLE = 'tableau-capture-bundle/1'
SHEET = 'tableau-sheet-capture/1'
# The capture runs on a blank page the browser serves itself on the site's origin (never sent to Tableau): Tableau's
# own web-client page breaks the Embedding API's <tableau-viz> element ("Failed to construct 'HTMLElement'", 11 Nov).
CAPTURE_PAGE = '/__v3_sheet_capture__.html'
BLANK = '<!doctype html><html><head><meta charset="utf-8"><title>V3 sheet capture</title></head><body></body></html>'


def view_path(site, content_url):
    """REST view contentUrl ('Workbook/sheets/View') -> the browser path of that view."""
    wb, _, view = content_url.partition('/sheets/')
    path = f"/views/{quote(wb)}/{quote(view)}"
    return f"/t/{site}{path}" if site else path


CA_VARS = ('TABLEAU_CA_CLIENT_ID', 'TABLEAU_CA_SECRET_ID', 'TABLEAU_CA_SECRET_VALUE', 'TABLEAU_CA_USERNAME')


def connected_app_token(env=None, now=None, jti=None, minutes=5):
    """A Tableau Connected App (direct trust) JWT for embedding views, built from environment variables only
    (client id, secret id, secret value, the Tableau user it acts as). Short-lived; never printed or stored."""
    import base64
    import hashlib
    import hmac
    import uuid
    env = os.environ if env is None else env
    missing = [k for k in CA_VARS if not env.get(k)]
    if missing:
        raise ValueError('Connected App settings missing from the environment: ' + ', '.join(missing))
    b64 = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b'=').decode()
    enc = lambda obj: b64(json.dumps(obj, separators=(',', ':')).encode())
    head = enc({'alg': 'HS256', 'typ': 'JWT', 'kid': env['TABLEAU_CA_SECRET_ID'], 'iss': env['TABLEAU_CA_CLIENT_ID']})
    body = enc({'iss': env['TABLEAU_CA_CLIENT_ID'], 'aud': 'tableau', 'sub': env['TABLEAU_CA_USERNAME'],
                'jti': jti or str(uuid.uuid4()), 'exp': int(now if now is not None else time.time()) + minutes * 60,
                'scp': ['tableau:views:embed']})
    sig = hmac.new(env['TABLEAU_CA_SECRET_VALUE'].encode(), f'{head}.{body}'.encode(), hashlib.sha256).digest()
    return f'{head}.{body}.{b64(sig)}'


def reason(exc):
    """The first line of a browser error, without URLs (they can carry tokens) and at most 200 characters."""
    line = (str(exc).strip().splitlines() or [''])[0]
    line = re.sub(r'^(page\.evaluate|Error):\s*', '', line)
    line = re.sub(r'https?://\S+', '<url>', line)
    return line[:200]


def failed(path, name, exc):
    """A view that could not be captured is kept as evidence (error type and first line), never dropped silently."""
    return {'schema': SHEET, 'method': 'Tableau Embedding API v3 Worksheet.getSummaryDataReaderAsync',
            'view': path, 'dashboard': name, 'captured_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'sheets': {}, 'error': f'{type(exc).__name__}: {reason(exc)}'}


def bundle(site, workbook, captures):
    return {'schema': BUNDLE, 'site': site,
            'captures': {f"{workbook}/{c.get('dashboard')}": c for c in captures}}


def _project_views(server, site, project, limit):
    import tableau_server as TS
    from reference_census import project_workbooks
    books = project_workbooks(TS.list_site_contents(server, site), project)
    books = books[:limit] if limit else books
    name, secret = TS._resolve_pat(None, None)
    client = TS.TableauRestClient(server)
    client.sign_in_with_pat(name, secret, site)
    try:
        return [(w, client.list_views(w['id'])) for w in books]
    finally:
        client.sign_out()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--server', required=True)
    ap.add_argument('--site', default='')
    ap.add_argument('--project', required=True)
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--max-rows', type=int, default=20000)
    ap.add_argument('--connected-app', action='store_true',
                    help='unattended: hidden browser signed in by a Connected App token (TABLEAU_CA_* variables)')
    ap.add_argument('--out', default=str(ROOT / 'reports' / ('sheet_capture_' + time.strftime('%Y%m%d'))))
    a = ap.parse_args()
    import tableau_server as TS
    from playwright.sync_api import sync_playwright
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    origin = a.server.rstrip('/')
    books = _project_views(origin, a.site, a.project, a.limit)
    print(f'{len(books)} workbook(s), {sum(len(v) for _, v in books)} view(s) in project "{a.project}"', flush=True)
    js = (ROOT / 'tools' / 'tableau_sheet_capture.js').read_text(encoding='utf-8')
    with sync_playwright() as pw:
        if a.connected_app:
            connected_app_token()                              # fails early, naming any missing variable
        ctx = pw.chromium.launch_persistent_context(
            str(out / ('.browser_profile_ca' if a.connected_app else '.browser_profile')), headless=a.connected_app,
                                                   viewport={'width': 1300, 'height': 950})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.route(origin + CAPTURE_PAGE,
                   lambda route: route.fulfill(status=200, content_type='text/html', body=BLANK))
        if not a.connected_app:
            page.goto(f"{origin}/#/site/{a.site}/home" if a.site else origin)
            input('Sign in to Tableau in the browser window (if asked), then press Enter here to start capturing... ')
        for w, views in books:
            stem = Path(w.get('name') or w['id']).name
            fetched = TS.fetch_workbook_by_id(origin, a.site, w['id'], name_hint=w.get('name'))
            (out / fetched['filename']).write_bytes(fetched['bytes'])
            caps = []
            for v in views:
                path = view_path(a.site, v.get('content_url') or '')
                print(f"  {w.get('name')} / {v.get('name')}: capturing (up to ~2 min, leave the window open)...",
                      flush=True)
                try:
                    page.goto(origin + CAPTURE_PAGE)
                    cap = page.evaluate(
                        '([js, origin, path, maxRows, token]) => { (0, eval)(js); '
                        'return captureDashboard(origin, path, { maxRows, token }); }',
                        [js, origin, path, a.max_rows, connected_app_token() if a.connected_app else None])
                    caps.append(cap)
                    print(f"  {w.get('name')} / {v.get('name')}: {len(cap.get('sheets') or {})} sheet(s)", flush=True)
                except Exception as exc:                       # first line only, URLs removed
                    caps.append(failed(path, v.get('name'), exc))
                    print(f"  {w.get('name')} / {v.get('name')}: FAILED {reason(exc)}", flush=True)
            target = out / (Path(fetched['filename']).stem + '.capture.json')
            target.write_text(json.dumps(bundle(a.site, stem, caps), indent=1), encoding='utf-8')
        if not a.connected_app:
            input('Finished. Look at the browser window if anything failed, then press Enter here to close it... ')
        ctx.close()
    print(f'Captures written to {out}. Next: python -X utf8 tools/sheet_census.py --folder "{out}"', flush=True)


if __name__ == '__main__':
    main()
