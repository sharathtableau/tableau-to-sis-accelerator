"""Estate catalogue harvest from a laptop (release 20b, SL01). Same code and same tables as the app's
Estate catalogue page; use it for large projects or for a folder of .twb / .twbx files.

  Tableau project (and its sub-projects):
    python tools\\harvest_catalogue.py --site "https://prod-useast-b.online.tableau.com/#/site/b360bi" --project "Tableau to Streamlit"
  Folder of workbooks:
    python tools\\harvest_catalogue.py --folder "C:\\path\\to\\workbooks" --scope "My test folder"
  Read and show only (nothing written, no Snowflake sign-in):
    add --plan-only

Read-only on Tableau: sign in, list, download each workbook WITHOUT its extract, read view counts, sign
out. The Personal Access Token is read from TABLEAU_PAT_NAME / TABLEAU_PAT_SECRET or asked for here (the
secret is hidden); it is never printed, logged or saved. Snowflake sign-in uses your Snowflake CLI
connection (default: wbr). Workbooks unchanged since the last harvest are skipped; an interrupted
harvest can simply be run again. Cortex describes the distinct metrics at the end (at most 25 calls,
40 metrics each); --no-cortex skips that.
"""
import argparse
import collections
import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _token():
    name = os.environ.get('TABLEAU_PAT_NAME') or input('Tableau PAT name: ').strip()
    secret = os.environ.get('TABLEAU_PAT_SECRET') or getpass.getpass('Tableau PAT secret (hidden): ').strip()
    if not name or not secret:
        raise SystemExit('A Tableau Personal Access Token is required.')
    return name, secret


def _print(row, label):
    extra = f" ({row.get('ERROR_CLASS')}: {row.get('NOTE')})" if row.get('ERROR_CLASS') else ''
    print(f"  {row['STATUS']:<10} {label}{extra}", flush=True)


def _summary(harvest):
    parsed = [r['_parsed'] for r in harvest.results if r.get('_parsed')]
    routes = collections.Counter(d['ROUTE'] for p in parsed for d in p['datasources'])
    metrics = collections.defaultdict(set)
    for r in harvest.results:
        for m in (r.get('_parsed') or {}).get('metrics', []):
            metrics[m['METRIC_FP']].add(r['WORKBOOK_KEY'])
    c = harvest.counts
    print(f"\n{c['harvested']} read, {c['unchanged']} unchanged, {c['failed']} failed (of {c['found']}).")
    if parsed:
        print(f"Read this time: {sum(len(p['datasources']) for p in parsed)} datasources, "
              f"{sum(len(p['calculations']) for p in parsed)} calculations, {len(metrics)} distinct metrics "
              f"({sum(1 for v in metrics.values() if len(v) > 1)} in 2+ workbooks).")
        for k, n in routes.most_common():
            print(f'  {n:>4}  {k}')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument('--project', help='Tableau project name (sub-projects included unless --no-subprojects)')
    src.add_argument('--folder', help='folder of .twb / .twbx files (sub-folders included)')
    ap.add_argument('--site', help='Tableau site URL (with --project)')
    ap.add_argument('--scope', help='name to show for a folder harvest (default: the folder name)')
    ap.add_argument('--no-subprojects', action='store_true')
    ap.add_argument('--connection', default='wbr')
    ap.add_argument('--no-cortex', action='store_true')
    ap.add_argument('--plan-only', action='store_true', help='read and show only; write nothing')
    a = ap.parse_args(argv)
    import v3_catalog as C
    if a.project and not a.site:
        raise SystemExit('--site is required with --project')
    sess = None
    if not a.plan_only:
        import pipeline
        sess = pipeline.snow_session(a.connection)
        if not C.ready(sess):
            raise SystemExit('The catalogue tables do not exist yet: run sql\\en01_evidence_store.sql in Snowsight '
                             '(Run All, role WBR_OWNER) first.')
    who = getpass.getuser()
    if a.folder:
        folder = os.path.abspath(a.folder)
        files = C.folder_workbooks(folder)
        harvest = C.Harvest(sess, 'laptop', a.scope or os.path.basename(folder), site=None, who=who,
                            write_rows=not a.plan_only)
        print(f'{len(files)} workbook(s) in {folder}', flush=True)
        harvest.start(len(files))
        for f in files:
            _print(C.harvest_file(harvest, folder, f), os.path.relpath(f, folder))
    else:
        import tableau_server as TS
        site = TS.parse_site_url(a.site)
        name, secret = _token()
        client = TS.TableauRestClient(site['server_url'])
        client.sign_in_with_pat(name, secret, site['site_content_url'])
        try:
            projects = client.list_projects()
            match = [p for p in projects if (p.get('name') or '').casefold() == a.project.casefold()]
            if len(match) != 1:
                _ids, path = C.project_tree(projects, None, include_sub=False)
                raise SystemExit(f'{len(match)} projects named {a.project!r}. Projects: '
                                 + ', '.join(sorted({path(p["id"]) for p in projects})[:60]))
            books, path = C.tableau_workbooks(client, match[0]['id'], not a.no_subprojects)
            harvest = C.Harvest(sess, 'laptop', path(match[0]['id']), site=a.site, who=who, write_rows=not a.plan_only)
            print(f'{len(books)} workbook(s) in {path(match[0]["id"])}'
                  + ('' if a.no_subprojects else ' and its sub-projects'), flush=True)
            harvest.start(len(books))
            for w in books:
                row = C.harvest_tableau_workbook(harvest, client, w, path, site['server_url'], site['site_content_url'])
                _print(row, f"{path(w.get('project_id'))} / {w.get('name')}")
        finally:
            client.sign_out()
    if a.plan_only:
        _summary(harvest)
        print('\nPlan only: nothing written.')
        return 0
    print('\nDescribing metrics with Cortex…' if not a.no_cortex else '', flush=True)
    harvest.finish(describe_metrics=not a.no_cortex)
    _summary(harvest)
    if not a.no_cortex:
        print(f'{harvest.described} metric description(s) passed code\'s check.')
    print('Open the app, page "Estate catalogue", scope "' + harvest.scope + '".')
    return 1 if harvest.counts['failed'] else 0


if __name__ == '__main__':
    sys.exit(main())
