"""Load a workbook's own data into Snowflake from a laptop (release 21a, EN42).

For datasources the hosted app cannot load itself: Tableau extracts (.hyper) need Tableau's Hyper engine,
which runs on a laptop but not inside Streamlit in Snowflake. Bundled Excel/CSV can be loaded here too.

The tool uses exactly the same code as the app (v3_data_load): same table names, same type rules, same
fingerprint. Run the migration again afterwards: the app finds the loaded tables by fingerprint and binds
them read-only. Nothing is overwritten; a changed file is loaded as <NAME>_V2.

Usage (PowerShell, from the lab folder):
  python tools\\load_workbook_data.py "C:\\path\\to\\Workbook.twbx"            # shows the plan, asks before loading
  python tools\\load_workbook_data.py "C:\\path\\to\\Workbook.twbx" --yes      # no question (scripted runs)
  python tools\\load_workbook_data.py "C:\\path\\to\\Workbook.twbx" --plan-only
Snowflake sign-in uses your Snowflake CLI connection (default: wbr).
"""
import argparse
import getpass
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _py(v):
    """tableauhyperapi returns its own Date / Timestamp types; pandas would read them as missing values."""
    if hasattr(v, 'to_datetime'):
        return v.to_datetime()
    if hasattr(v, 'to_date'):
        return v.to_date()
    return v


def decode_extract(raw, loc, root):
    """The .hyper of one datasource -> one DataFrame at fact grain (relationship extracts flattened)."""
    import pandas as pd
    from tableauhyperapi import HyperProcess, Connection, Telemetry
    import v3_data_load as DL
    data = DL._read_member(raw, loc['files'][0]['member'])
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'extract.hyper')
        with open(path, 'wb') as fh:
            fh.write(data)
        tables = {}
        with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hp:
            with Connection(hp.endpoint, path) as con:
                for schema in con.catalog.get_schema_names():
                    for t in con.catalog.get_table_names(schema):
                        cols = [c.name.unescaped for c in con.catalog.get_table_definition(t).columns]
                        if t.name.unescaped in tables:
                            raise ValueError('Extract table names collide across schemas')
                        frame = pd.DataFrame(con.execute_list_query(f'SELECT * FROM {t}'), columns=cols)
                        for c in frame.columns:      # Hyper Date/Timestamp objects -> Python date/datetime
                            if frame[c].dtype == object:
                                frame[c] = frame[c].map(_py)
                        tables[t.name.unescaped] = frame
    if not tables:
        raise ValueError('The extract has no tables')
    if len(tables) == 1:
        return next(iter(tables.values()))
    return tables          # release 21b: one table per logical table (v3_data_entities); joined only as fallback


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('workbook')
    ap.add_argument('--connection', default='wbr')
    ap.add_argument('--yes', action='store_true')
    ap.add_argument('--plan-only', action='store_true')
    a = ap.parse_args(argv)
    import tableau_parser
    import v3_data_load as DL
    path = Path(a.workbook)
    raw = path.read_bytes()
    ir = tableau_parser.build_ir(str(path), scoped=True)
    root = tableau_parser.load_twb_xml(str(path))
    plans, frames = [], {}
    for _did, scope in (ir.get('field_scopes') or {}).items():
        cap = scope['caption']
        loc = DL.locate(raw, root, ir, cap)
        decoded = None
        if loc['kind'] == 'extract':
            try:
                decoded = decode_extract(raw, loc, root)
            except Exception as exc:
                plans.append(dict(loc, ok=False, reason=f'Extract not decoded: {type(exc).__name__}: {exc}'))
                continue
        p = DL.plan(raw, root, ir, cap, decoded=decoded)
        plans.append(p)
        if p['ok']:
            frames[cap] = decoded
    print(f'\nWorkbook: {path.name}\n')
    for p in plans:
        if p.get('ok') and p.get('mode') == 'entities':
            print(f"  LOAD  {p['caption']}: one table per logical table (reused when the same data is already there)")
            for e in p['entities']:
                print(f"          {e['caption']}: {e['rows']:,} rows, {e['columns']} columns -> {DL.LOAD_DB}.{DL.LOAD_SCHEMA}.{e['target']}")
            for n in p.get('notes') or []:
                print('          note: ' + n)
        elif p.get('ok'):
            notes = ('  [' + '; '.join(p.get('notes') or []) + ']') if p.get('notes') else ''
            print(f"  LOAD  {p['caption']}: {p['rows']:,} rows, {p['columns']} columns -> "
                  f"{DL.LOAD_DB}.{DL.LOAD_SCHEMA}.{p['target']} (or _V2 if a different version exists){notes}")
        else:
            print(f"  SKIP  {p['caption']}: {p.get('reason') or 'nothing to load'}")
    todo = [p for p in plans if p.get('ok')]
    if not todo or a.plan_only:
        print('\nNothing loaded.' if not todo else '\nPlan only: nothing loaded.')
        return 0
    if not a.yes and input(f'\nLoad {len(todo)} table(s) into Snowflake as connection "{a.connection}"? [y/N] ').strip().lower() != 'y':
        print('Nothing loaded.')
        return 0
    import pipeline
    import v3_store
    session = pipeline.snow_session(a.connection)
    who = getpass.getuser()
    failed = 0
    for p in todo:
        cap = p['caption']
        try:
            rec = DL.load(session, raw, root, ir, cap, who=who, via='laptop', decoded=frames[cap])
            where = (', '.join(t['table'] for t in rec['table']['tables'].values()) if isinstance(rec['table'], dict)
                     else rec['table'])
            print(f"  OK    {cap}: {rec['action']} -> {where}" + (f" ({rec['rows']:,} rows)" if rec.get('rows') else ''))
            v3_store.log_load(None, dict(DL.log_row({'name': path.name}, rec, 'laptop'), RECORDED_BY=who), sess=session)
        except Exception as exc:
            failed += 1
            print(f'  FAIL  {cap}: {type(exc).__name__}: {str(exc)[:300]}')
    print('\nNow run the migration again in the app: it finds these tables by fingerprint and binds them.')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
