"""R47: every corpus workbook's datasources through the hosted binding path, offline: a Snowflake stand-in over
DuckDB (SHOW TABLES, COMMENT, write_pandas, DESCRIBE, INFORMATION_SCHEMA) runs the very code the app runs --
v3_source_binding (live Snowflake sources), v3_data_load.load (the "Load the workbook's own data" button) and the
next run's automatic find (find_model / find_loaded / find_reusable). Prints one row per datasource with the
outcome and the reason, so binding failures are found across the corpus, not one report at a time.
  python tools/binding_sweep.py [workbook ...] [--json out.json]"""
import contextlib
import io
import json
import os
import re
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools')]

_TYPES = {'VARCHAR': 'VARCHAR(16777216)', 'BIGINT': 'NUMBER(38,0)', 'INTEGER': 'NUMBER(38,0)', 'DOUBLE': 'FLOAT',
          'FLOAT': 'FLOAT', 'DATE': 'DATE', 'TIMESTAMP': 'TIMESTAMP_NTZ(9)', 'BOOLEAN': 'BOOLEAN',
          'TIMESTAMP_NS': 'TIMESTAMP_NTZ(9)', 'HUGEINT': 'NUMBER(38,0)', 'SMALLINT': 'NUMBER(38,0)',
          'TINYINT': 'NUMBER(38,0)'}


class Row(dict):
    def __getitem__(self, k):
        if isinstance(k, int):
            return list(self.values())[k]
        return dict.__getitem__(self, k)

    def as_dict(self):
        return dict(self)


class FakeSnow:
    """Snowflake as the binding / load code uses it, over DuckDB."""
    def __init__(self):
        import duckdb
        self.con = duckdb.connect()
        self.con.execute("ATTACH ':memory:' AS WBR_DB")
        self.con.execute("CREATE MACRO to_date(x) AS CAST(x AS DATE)")
        self.con.execute("CREATE MACRO to_timestamp_ntz(x) AS CAST(x AS TIMESTAMP)")
        self.comments = {}

    def _run(self, text):
        t = text.strip()
        m = re.match(r"SHOW TABLES(?: LIKE '([^']*)')? IN SCHEMA (\w+)\.(\w+)$", t, re.I)
        if m:
            like, db, sch = m.groups()
            rows = self.con.execute("SELECT table_name FROM information_schema.tables WHERE table_catalog=? AND "
                                    "table_schema=?", [db, sch]).fetchall()
            out = []
            for (n,) in rows:
                if like and not re.fullmatch(re.escape(like).replace('%', '.*').replace('_', '.'), n, re.I):
                    continue
                out.append(Row(name=n.upper(), comment=self.comments.get(f'{db}.{sch}.{n}'.upper(), '')))
            return out
        m = re.match(r"COMMENT ON TABLE ([\w.]+) IS '(.*)'$", t, re.S)
        if m:
            self.comments[m.group(1).upper()] = m.group(2).replace("''", "'")
            return []
        m = re.match(r"SELECT COUNT\(\*\) AS N FROM (\w+)\.INFORMATION_SCHEMA\.SCHEMATA WHERE SCHEMA_NAME = '(\w+)'",
                     t, re.I)
        if m:
            n = self.con.execute("SELECT COUNT(*) FROM information_schema.schemata WHERE catalog_name=? AND "
                                 "schema_name=?", list(m.groups())).fetchone()[0]
            return [Row(N=n)]
        if t.upper().startswith('DESCRIBE TABLE '):
            out = self.con.execute('DESCRIBE ' + t[len('DESCRIBE TABLE '):]).fetchall()
            return [Row(name=r[0], type=_TYPES.get(str(r[1]).split('(')[0].upper(), r[1]), NAME=r[0],
                        TYPE=_TYPES.get(str(r[1]).split('(')[0].upper(), r[1])) for r in out]
        if 'CURRENT_ACCOUNT()' in t:
            return [Row(LOCATOR='X', ACCOUNT_NAME='C2GPARTNERS', ORG='WB19670', REGION='R')]
        t = re.sub(r'\bTIMESTAMP_NTZ\b', 'TIMESTAMP', t)
        cur = self.con.execute(t)
        if cur.description is None:
            return []
        names = [d[0] for d in cur.description]
        return [Row(zip(names, r)) for r in cur.fetchall()]

    def sql(self, text, params=None):
        outer = self

        class R:
            def collect(_):
                return outer._run(text)
        return R()

    def write_pandas(self, df, name, database, schema, auto_create_table=True, overwrite=False,
                     quote_identifiers=False):
        d = df.copy()
        d.columns = [str(c).upper() for c in d.columns]
        self.con.register('__w', d)
        self.con.execute(f'CREATE TABLE {database}.{schema}.{name} AS SELECT * FROM __w')
        self.con.unregister('__w')


def sweep(path):
    import plan_census as PC
    import tableau_parser as TP
    import v3_data_load as DL
    import v3_data_entities as ENT
    from v3_source_binding import resolve_sources
    out = []
    raw = path.read_bytes()
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            r = PC.intake(path)
        except Exception as e:
            return [{'workbook': path.name, 'datasource': '*', 'outcome': 'INTAKE_FAILED',
                     'reason': f'{type(e).__name__}: {str(e)[:200]}'}]
    root = TP.load_twb_xml(str(path))
    caps = [s['caption'] for s in r['ir'].get('field_scopes', {}).values()]
    snow = FakeSnow()
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            _bound, reports = resolve_sources(root, r['ir'], snow)
    except Exception as e:
        reports = []
    rep = {x.get('caption'): x for x in reports}
    for cap in caps:
        row = {'workbook': path.name, 'datasource': cap}
        try:
            loc = DL.locate(raw, root, r['ir'], cap)
        except Exception as e:
            row.update(outcome='LOCATE_FAILED', reason=f'{type(e).__name__}: {str(e)[:200]}')
            out.append(row)
            continue
        row['kind'] = loc['kind']
        sr = rep.get(cap) or {}
        if loc['kind'] == 'none':
            row.update(outcome='LIVE_BOUND' if sr.get('state') == 'BOUND' else 'LIVE_NOT_BOUND',
                       reason=str(loc.get('reason') or sr.get('reason'))[:160] + ' | ' + str(sr.get('reason'))[:120])
            out.append(row)
            continue
        decoded = None
        if loc['kind'] == 'extract':
            # the laptop loader's path (tools/load_workbook_data.py): decode the .hyper, then the same load
            try:
                import load_workbook_data as LW
                decoded = LW.decode_extract(raw, loc, root)
            except Exception as e:
                row.update(outcome='EXTRACT_DECODE_FAILED', reason=f'{type(e).__name__}: {str(e)[:200]}')
                out.append(row)
                continue
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rec = DL.load(snow, raw, root, r['ir'], cap, decoded=decoded)
            t = rec.get('table')
            row['load'] = ('model ' + str(len(t.get('tables') or {})) + ' tables') if isinstance(t, dict) else 'table'
            row['load_notes'] = '; '.join(rec.get('notes') or [])[:240]
        except Exception as e:
            row.update(outcome='LOAD_FAILED', reason=f'{type(e).__name__}: {str(e)[:240]}',
                       where=traceback.extract_tb(e.__traceback__)[-1].name)
            out.append(row)
            continue
        # the next run: found again without loading
        try:
            again = None
            if loc.get('sha256') and ENT.multi(root, r['ir'], cap):
                try:                                  # as the app: a model that does not check is not found
                    again = ENT.find_model(snow, root, r['ir'], cap, loc['sha256'])
                except Exception:
                    again = None
            if not again and loc.get('sha256'):
                hit = DL.find_loaded(snow, cap, loc['sha256'])
                again = DL.check_loaded(snow, hit, DL.fields_for(r['ir'], loc['datasource_id'])) if hit else None
            if not again:
                again = DL.find_reusable(snow, raw, root, r['ir'], cap)
            row.update(outcome='LOADED_AND_FOUND' if again else 'LOADED_NOT_FOUND_NEXT_RUN')
        except Exception as e:
            row.update(outcome='FOUND_CHECK_FAILED', reason=f'{type(e).__name__}: {str(e)[:240]}')
        out.append(row)
    return out


def corpus():
    seen = []
    for p in sorted((ROOT / 'Workbooks').rglob('*')):
        if p.suffix.lower() in ('.twbx', '.twb') and not p.name.startswith('~') and 'before_' not in str(p):
            seen.append(p)
    return seen


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    dest = sys.argv[sys.argv.index('--json') + 1] if '--json' in sys.argv else None
    if dest in args:
        args.remove(dest)
    rows = []
    for p in ([Path(a) for a in args] or corpus()):
        try:
            rows += sweep(p)
        except Exception as e:
            rows.append({'workbook': p.name, 'datasource': '*', 'outcome': 'SWEEP_ERROR', 'reason': str(e)[:200]})
        print(json.dumps(rows[-1])[:300], flush=True) if rows else None
    if dest:
        Path(dest).write_text(json.dumps(rows, indent=1))
