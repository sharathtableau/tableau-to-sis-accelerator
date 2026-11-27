"""Rationalisation report from a laptop (release 22a, SL03): same code and tables as the Rationalisation tab
of the app's Estate catalogue page. Use it for large scopes.

  python tools\\rationalise_catalogue.py --scope "Tableau to Streamlit"
  python tools\\rationalise_catalogue.py --scope "Tableau to Streamlit" --no-cortex
  python tools\\rationalise_catalogue.py --scope "Tableau to Streamlit" --xlsx report.xlsx

Read-only on the data (one SELECT per group); writes only the RAT_ tables. Snowflake sign-in uses your
Snowflake CLI connection (default: wbr).
"""
import argparse
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--scope', required=True, help='the harvested scope (Estate catalogue "Scope" list)')
    ap.add_argument('--connection', default='wbr')
    ap.add_argument('--no-cortex', action='store_true')
    ap.add_argument('--xlsx', help='also save the report to this Excel file')
    a = ap.parse_args(argv)
    import pipeline
    import v3_rationalise as R
    sess = pipeline.snow_session(a.connection)
    if not R.ready(sess):
        raise SystemExit('The rationalisation tables do not exist yet: run the release 22 set-up SQL in Snowsight first.')
    run = R.Run(sess, a.scope, via='laptop', who=getpass.getuser(), cortex=not a.no_cortex)
    counts = run.execute(progress=lambda t: print('  ' + t, flush=True))
    print(f'\nScope {a.scope}: {len(run.info)} metrics, {len(run.groups)} groups compared.')
    for c in R.CLASSES:
        print(f'  {counts.get(c, 0):>5}  {R.LABELS[c]}')
    for g in run.groups:
        print(f"  {g['id']:<5} {g['verdict']:<13} {g['source']:<10} {g['name']}" + (f"  ({g['reason']})" if g.get('reason') and g['verdict'] == 'not_testable' else ''))
    if a.xlsx:
        found = R.latest(sess, a.scope)
        if found:
            Path(a.xlsx).write_bytes(R.excel(*found))
            print('Report saved to ' + a.xlsx)
    print('Open the app: Estate catalogue, scope "' + a.scope + '", tab Rationalisation.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
