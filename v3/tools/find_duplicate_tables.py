"""Find tables that hold exactly the same data in WBR_DB.PIPELINE_DEMO and write a clean-up script (release 23).

  python tools\\find_duplicate_tables.py
  python tools\\find_duplicate_tables.py --out reports\\duplicates.sql

Read-only: it runs SELECT / SHOW queries only and executes nothing it writes. Tables with the same row count
and the same columns are compared with HASH_AGG over all columns; identical ones form a group.

In each group it KEEPS one table this accelerator loaded (tag 'v3load'), preferring the one used by the most
Semantic Views and workbooks, then the one with a data fingerprint, then the oldest. The script it writes:
  1. COMMENT ON TABLE <kept> adding every source fingerprint of the other copies, so later runs of those
     workbooks find the kept table instead of loading again;
  2. DROP SEMANTIC VIEW for views built only on copies that will be dropped (the kept table's view stays);
  3. DROP TABLE for the other copies loaded by this accelerator.
Tables made outside this accelerator (no 'v3load' tag, e.g. by the original accelerator) are listed as
"same data" but never proposed for dropping: other apps may read them. Run the script only after checking
that no deployed app reads a table it drops; re-run any migration that used a dropped copy.
"""
import argparse
import datetime
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB, SCHEMA = 'WBR_DB', 'PIPELINE_DEMO'
EVIDENCE = 'WBR_DB.TABLEAU_ACCELERATOR_V3_EVIDENCE'
TAG = 'v3load'


def _rows(sess, sql, params=None):
    out = []
    for r in sess.sql(sql, params=params or []).collect():
        d = r.as_dict() if hasattr(r, 'as_dict') else dict(r)
        out.append({str(k).upper(): v for k, v in d.items()})
    return out


def _q(c):
    return c if re.fullmatch(r'[A-Z_][A-Z0-9_$]*', c) else '"' + c.replace('"', '""') + '"'


def inventory(sess):
    tables = {r['TABLE_NAME']: {'name': r['TABLE_NAME'], 'rows': r['ROW_COUNT'], 'comment': r.get('COMMENT') or '',
                                'created': r.get('CREATED'), 'cols': []}
              for r in _rows(sess, f"SELECT TABLE_NAME, ROW_COUNT, COMMENT, CREATED FROM {DB}.INFORMATION_SCHEMA.TABLES "
                                   "WHERE TABLE_SCHEMA = ? AND TABLE_TYPE = 'BASE TABLE'", [SCHEMA])}
    for r in _rows(sess, f'SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE FROM {DB}.INFORMATION_SCHEMA.COLUMNS '
                         'WHERE TABLE_SCHEMA = ? ORDER BY TABLE_NAME, COLUMN_NAME', [SCHEMA]):
        if r['TABLE_NAME'] in tables:
            tables[r['TABLE_NAME']]['cols'].append((r['COLUMN_NAME'], r['DATA_TYPE']))
    return tables


def usage(sess):
    """Table -> {'views': set, 'workbooks': set} from the shared Semantic View registry and data loads."""
    out = {}

    def add(table, kind, value):
        if table and value:
            out.setdefault(str(table).split('.')[-1].upper(), {'views': set(), 'workbooks': set()})[kind].add(value)
    try:
        for r in _rows(sess, f'SELECT VIEW_NAME, WORKBOOK_NAME, TABLES FROM {EVIDENCE}.SEMANTIC_VIEW_EVENTS WHERE VERIFIED'):
            tabs = r.get('TABLES')
            tabs = json.loads(tabs) if isinstance(tabs, str) else (tabs or [])
            for t in tabs if isinstance(tabs, list) else []:
                add(t, 'views', r['VIEW_NAME'])
                add(t, 'workbooks', r.get('WORKBOOK_NAME'))
    except Exception:
        pass
    try:
        for r in _rows(sess, f'SELECT TABLE_NAME, WORKBOOK_NAME FROM {EVIDENCE}.DATA_LOADS'):
            for t in str(r.get('TABLE_NAME') or '').split(','):
                add(t.strip(), 'workbooks', r.get('WORKBOOK_NAME'))
    except Exception:
        pass
    return out


def groups(sess, tables):
    """Lists of table names holding identical data (same columns, same rows, same HASH_AGG)."""
    by_shape = {}
    for t in tables.values():
        if t['rows'] and t['cols']:
            by_shape.setdefault((t['rows'], tuple(t['cols'])), []).append(t['name'])
    out = []
    for (_n, cols), names in sorted(by_shape.items()):
        if len(names) < 2:
            continue
        col_list = ', '.join(_q(c) for c, _t in cols)
        hashes = {}
        for name in sorted(names):
            h = _rows(sess, f'SELECT HASH_AGG({col_list}) AS H FROM {DB}.{SCHEMA}.{_q(name)}')[0]['H']
            hashes.setdefault(h, []).append(name)
        out += [sorted(v) for v in hashes.values() if len(v) > 1]
    return out


def choose(group, tables, use):
    """(kept table or None, tables to drop). Only tables this accelerator loaded are dropped."""
    ours = [n for n in group if tables[n]['comment'].startswith(TAG)]
    if not ours:
        return None, []
    rank = lambda n: (-len(use.get(n, {}).get('views', ())), -len(use.get(n, {}).get('workbooks', ())),
                      0 if 'data_sha256=' in tables[n]['comment'] else 1, str(tables[n]['created']), n)
    keep = sorted(ours, key=rank)[0]
    return keep, [n for n in ours if n != keep]


def script(sess, out_path=None):
    tables = inventory(sess)
    use = usage(sess)
    found = groups(sess, tables)
    lines = [f'-- Duplicate tables in {DB}.{SCHEMA} (generated {datetime.datetime.now():%Y-%m-%d %H:%M}; nothing was run).',
             '-- Review each group. Run only after checking no deployed app reads a table being dropped.',
             'USE ROLE WBR_OWNER;']
    summary = []
    for g in found:
        keep, drop = choose(g, tables, use)
        others = [n for n in g if n != keep and n not in drop]
        lines.append('')
        lines.append(f"-- Same data ({tables[g[0]]['rows']:,} rows): " + ', '.join(g))
        for n in g:
            u = use.get(n, {})
            lines.append(f"--   {n}: {'loaded by this accelerator' if n in drop or n == keep else 'made outside this accelerator (not dropped)'}"
                         f"; Semantic Views {len(u.get('views', ()))}; workbooks {', '.join(sorted(u.get('workbooks', ()))) or '-'}")
        if not keep:
            lines.append('--   Nothing proposed: none of these was loaded by this accelerator.')
            summary.append((g, None, []))
            continue
        shas = []
        for n in [keep] + drop:
            shas += [s for s in re.findall(r'src_sha256=([0-9a-f]{64})', tables[n]['comment']) if s not in shas]
        comment = tables[keep]['comment']
        extra = [s for s in shas if f'src_sha256={s}' not in comment]
        if extra:
            merged = comment + ''.join(f' src_sha256={s}' for s in extra)
            lines.append(f"COMMENT ON TABLE {DB}.{SCHEMA}.{_q(keep)} IS '{merged.replace(chr(39), chr(39) * 2)}';")
        keep_views = use.get(keep, {}).get('views', set())
        for n in drop:
            for v in sorted(use.get(n, {}).get('views', ())):
                if v not in keep_views:
                    lines.append(f'DROP SEMANTIC VIEW IF EXISTS {v};')
            lines.append(f'DROP TABLE IF EXISTS {DB}.{SCHEMA}.{_q(n)};')
        summary.append((g, keep, drop))
        if others:
            lines.append('--   Kept as they are (made outside this accelerator): ' + ', '.join(others))
    text = '\n'.join(lines) + '\n'
    if out_path:
        Path(out_path).write_text(text, encoding='utf-8')
    return text, summary


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--connection', default='wbr')
    ap.add_argument('--out', default=f'reports/duplicate_tables_{datetime.date.today():%Y%m%d}.sql')
    a = ap.parse_args(argv)
    import pipeline
    sess = pipeline.snow_session(a.connection)
    text, summary = script(sess, a.out)
    print(text)
    print(f'{len(summary)} group(s) of identical tables; {sum(len(d) for _g, _k, d in summary)} table(s) proposed for '
          f'dropping. Script saved to {a.out} (nothing was run).')
    return 0


if __name__ == '__main__':
    sys.exit(main())
