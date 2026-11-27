"""Corpus census of the Tableau-comparison path (v3_sheet_plan): can every sheet be planned and queried?

    python -X utf8 tools/plan_census.py [paths ...] [--sf] [--out reports/<folder>]

Offline (default): sheets whose own datasource has bundled data run on local DuckDB; the rest are NO_DATA.
--sf: read-only queries on live Snowflake through the owner's named connection profile (env V3_PREVIEW_CONNECTION,
default "wbr"; credentials stay in the profile and are never read or printed). Only the Snowflake table bindings the
workbook itself declares are used (DATABASE.SCHEMA.OBJECT or a relationship model); a datasource with none is
NO_DATA -- never guessed.

Per sheet one outcome: RAN, RAN_0_ROWS, NO_DATA, FAILED (with a normalised reason: no data values, no names).
Writes <out>/plan_census.md and .json; prints the ranked reasons. No Cortex, nothing written to Snowflake.
"""
import argparse
import glob
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUTCOMES = ('RAN', 'RAN_0_ROWS', 'NO_DATA', 'FAILED')
_TABLE = re.compile(r'[A-Za-z_][A-Za-z_0-9$]*\.[A-Za-z_][A-Za-z_0-9$]*\.[A-Za-z_][A-Za-z_0-9$]*')


def reason(exc):
    """Exception -> a short reason with identifiers and literals removed, so equal causes group together."""
    text = str(exc).split('\n')[0]
    text = re.sub(r"'[^']*'", "'…'", text)
    text = re.sub(r'"[^"]*"', '"…"', text)
    text = re.sub(r'\b\d+\b', 'N', text)
    return f'{type(exc).__name__}: {text[:140]}'


def snowflake_bindings(bindings):
    return {k: v for k, v in (bindings or {}).items()
            if isinstance(v, dict) or (isinstance(v, str) and _TABLE.fullmatch(v))}


def corpus_paths():
    pats = ('Workbooks/*.twbx', 'Workbooks/*.twb', 'Workbooks/cloud_20261010/*.tw*')
    return sorted({Path(p) for pat in pats for p in glob.glob(str(ROOT / pat))})


def intake(path, published_views=None):
    """v3_local.prepare_payload without the Cortex workbook review (the census makes no Cortex call). With a
    Snowflake session active, v3_source_binding.resolve_sources binds live datasources to their tables."""
    import hashlib
    import tempfile
    from tableau_server import normalize_workbook_download
    from v3_local import prepare
    raw = Path(path).read_bytes()
    normalized = normalize_workbook_download(Path(path).name, raw)
    folder = Path(tempfile.mkdtemp(prefix='tableau-v3-census-'))
    target = folder / (hashlib.sha256(raw).hexdigest() + Path(normalized).suffix.lower())
    target.write_bytes(raw)
    return prepare(target, raw, published_views)


def census(paths, sf=False, log=print):
    import engine
    import v3_sheet_plan as P
    from v3_local import preview_context
    rows = []
    for path in paths:
        try:
            r = intake(path)
        except Exception as exc:
            rows.append({'workbook': Path(path).name, 'sheet': '(workbook)', 'outcome': 'FAILED', 'reason': reason(exc)})
            continue
        ir = r['ir']
        bindings = snowflake_bindings(r['bindings']) if sf else {k: v for k, v in r['bindings'].items() if v}
        sheets = [s for d in ir.get('dashboards') or [] for s in d.get('sheets') or [] if s.get('worksheet_semantics')]
        seen, todo = set(), []
        for s in sheets:
            if s['name'] not in seen:
                seen.add(s['name'])
                todo.append(s)
        try:
            ctx = preview_context(ir, bindings, snowflake=sf)
            ctx.__enter__()
        except Exception as exc:
            rows += [{'workbook': Path(path).name, 'sheet': s['name'], 'outcome': 'FAILED', 'reason': reason(exc)}
                     for s in todo]
            continue
        try:
            for s in todo:
                cap = ((ir.get('field_scopes') or {}).get(s.get('datasource_id')) or {}).get('caption')
                row = {'workbook': Path(path).name, 'sheet': s['name'], 'outcome': 'NO_DATA', 'reason': ''}
                if bindings.get(cap):
                    try:
                        df = P.execute(P.from_sheet(s, ir), s, engine)
                        row['outcome'] = 'RAN' if len(df) else 'RAN_0_ROWS'
                    except Exception as exc:
                        row.update(outcome='FAILED', reason=reason(exc))
                rows.append(row)
        finally:
            ctx.__exit__(None, None, None)
        log(f"{Path(path).name}: " + ', '.join(f'{k} {v}' for k, v in
                                                Counter(x['outcome'] for x in rows if x['workbook'] == Path(path).name).items()))
    return rows


def report(rows, mode):
    counts = Counter(r['outcome'] for r in rows)
    fails = defaultdict(list)
    for r in rows:
        if r['outcome'] == 'FAILED':
            fails[r['reason']].append(r)
    lines = [f'# Comparison-path census ({mode}) — {time.strftime("%Y-%m-%d %H:%M")}', '',
             f'Sheets: {len(rows)} · ' + ' · '.join(f'{k} {counts.get(k, 0)}' for k in OUTCOMES), '',
             '| Sheets | Workbooks | Reason | Example |', '|---|---|---|---|']
    for rsn, items in sorted(fails.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        lines.append(f"| {len(items)} | {len({i['workbook'] for i in items})} | {rsn.replace('|', '/')} | "
                     f"{items[0]['workbook']} / {items[0]['sheet']} |")
    return '\n'.join(lines) + '\n'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('paths', nargs='*')
    ap.add_argument('--sf', action='store_true')
    ap.add_argument('--out', default=str(ROOT / 'reports' / ('plan_census_' + time.strftime('%Y%m%d'))))
    a = ap.parse_args()
    paths = [Path(p) for p in a.paths] or corpus_paths()
    if a.sf:
        import backend
        from snowflake.snowpark import Session
        print('opening the Snowflake session (finish any sign-in in your browser)', flush=True)
        backend.set_session(Session.builder.config('connection_name',
                                                   os.environ.get('V3_PREVIEW_CONNECTION', 'wbr')).create())
    rows = census(paths, sf=a.sf, log=lambda m: print(m, flush=True))
    os.makedirs(a.out, exist_ok=True)
    mode = 'Snowflake' if a.sf else 'offline'
    md = report(rows, mode)
    Path(a.out, 'plan_census_%s.md' % mode.lower()).write_text(md, encoding='utf-8')
    Path(a.out, 'plan_census_%s.json' % mode.lower()).write_text(json.dumps(rows, indent=1), encoding='utf-8')
    print(md)


if __name__ == '__main__':
    main()
