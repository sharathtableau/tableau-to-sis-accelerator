"""Download every workbook in one Tableau Cloud project (and its sub-projects) as a frozen test set.

    python tools/fetch_cloud_project.py --site "https://prod-useast-b.online.tableau.com/#/site/b360bi"
        --project "Tableau to Streamlit" --out Workbooks/cloud_20261009

Read-only on Tableau (sign in, list, download, list published views, sign out). Nothing is written
to Snowflake. The Personal Access Token is read from TABLEAU_PAT_NAME / TABLEAU_PAT_SECRET; when they
are not set, you are asked for them here (the secret is not echoed). The token is never printed,
logged or saved.

Writes <out>/<workbook file> for each workbook and <out>/manifest.json (workbook id, name, Cloud
address, project path, published views, file, sha256, size, download time). The folder is a FROZEN
test set: a second run refuses to overwrite it unless --refresh is given. A workbook that fails to
download is listed with the error class and the others continue.
"""
import argparse
import datetime
import getpass
import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import tableau_server as TS  # noqa: E402


def _token():
    name = os.environ.get('TABLEAU_PAT_NAME') or input('Tableau PAT name: ').strip()
    secret = os.environ.get('TABLEAU_PAT_SECRET') or getpass.getpass('Tableau PAT secret (hidden): ').strip()
    if not name or not secret:
        raise SystemExit('A Tableau Personal Access Token is required.')
    return name, secret


def _safe(name):
    return re.sub(r'[^A-Za-z0-9._ ()-]+', '_', name).strip() or 'workbook'


def project_tree(projects, wanted):
    """Ids of every project named `wanted` (case-insensitive) and all their sub-projects."""
    roots = {p['id'] for p in projects if (p.get('name') or '').casefold() == wanted.casefold()}
    if not roots:
        raise SystemExit(f'No project named {wanted!r} on this site. Projects: '
                         + ', '.join(sorted({p.get("name") or "" for p in projects})[:40]))
    ids, changed = set(roots), True
    while changed:
        changed = False
        for p in projects:
            if p.get('parent_project_id') in ids and p['id'] not in ids:
                ids.add(p['id'])
                changed = True
    by_id = {p['id']: p for p in projects}

    def path(pid):
        parts = []
        while pid in by_id:
            parts.append(by_id[pid].get('name') or '')
            pid = by_id[pid].get('parent_project_id')
        return ' / '.join(reversed(parts))
    return ids, path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--site', required=True)
    ap.add_argument('--project', required=True)
    ap.add_argument('--out', default='Workbooks/cloud_' + datetime.date.today().strftime('%Y%m%d'))
    ap.add_argument('--refresh', action='store_true', help='replace an existing frozen set')
    args = ap.parse_args()
    out = ROOT / args.out
    if (out / 'manifest.json').exists() and not args.refresh:
        raise SystemExit(f'{out} is a frozen test set already (manifest.json exists). Use --refresh to replace it.')
    out.mkdir(parents=True, exist_ok=True)
    site = TS.parse_site_url(args.site)
    name, secret = _token()
    client = TS.TableauRestClient(site['server_url'])
    entries, used = [], set()
    client.sign_in_with_pat(name, secret, site['site_content_url'])
    try:
        ids, path = project_tree(client.list_projects(), args.project)
        books = [w for w in client.list_workbooks() if w.get('project_id') in ids]
        print(f'{len(books)} workbook(s) in {args.project!r} and its sub-projects', flush=True)
        for w in sorted(books, key=lambda b: (path(b.get('project_id')), b.get('name') or '')):
            entry = {'id': w['id'], 'name': w.get('name'), 'content_url': w.get('content_url'),
                     'project': path(w.get('project_id')), 'server_url': site['server_url'],
                     'site_content_url': site['site_content_url']}
            try:
                data = client.download_workbook_bytes(w['id'])
                fname = TS.normalize_workbook_download(w.get('name') or w['id'], data)
                stem, ext = Path(_safe(fname)).stem, Path(fname).suffix.lower() or '.twbx'
                target = stem + ext
                if target.casefold() in used:                      # same name in two sub-projects
                    target = f'{stem} ({w["id"][:8]}){ext}'
                used.add(target.casefold())
                (out / target).write_bytes(data)
                try:
                    views = [v['name'] for v in client.list_views(w['id'])]
                except Exception as exc:
                    views, entry['views_error'] = None, type(exc).__name__
                entry.update(file=target, bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                             published_views=views)
                print(f'  ok   {entry["project"]} / {entry["name"]} -> {target} ({len(data):,} bytes)', flush=True)
            except Exception as exc:
                entry['error'] = type(exc).__name__ + ': ' + str(exc)[:200]
                print(f'  FAIL {entry["project"]} / {entry["name"]}: {type(exc).__name__}', flush=True)
            entries.append(entry)
    finally:
        client.sign_out()
    manifest = {'schema': 'cloud-test-set/1', 'frozen': True, 'site': args.site, 'project': args.project,
                'include_subprojects': True,
                'downloaded_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                'workbooks': entries}
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    ok = sum(1 for e in entries if e.get('file'))
    print(f'{ok} of {len(entries)} downloaded into {out}; manifest.json written (frozen).')
    return 0 if ok == len(entries) else 1


if __name__ == '__main__':
    raise SystemExit(main())
