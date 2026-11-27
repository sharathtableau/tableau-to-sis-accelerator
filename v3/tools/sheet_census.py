"""Every captured sheet proven against Tableau's own numbers (v3_exact_loop.prove_run) -- the per-sheet census.

    python -X utf8 tools/sheet_census.py --folder reports/sheet_capture_<date> [--sf]

The folder holds, per workbook, its file and <stem>.capture.json (tools/capture_project.py). Data: with --sf the
workbook's Snowflake tables when it is bound to them (the owner's named connection profile, V3_PREVIEW_CONNECTION,
default "wbr"; read-only); otherwise the workbook's own bundled data on local DuckDB (what Tableau Cloud queries).
Writes <folder>/sheet_census.md / .json: per sheet Verified / Partly verified / Not matched / Not planned ...,
per-workbook counts and the differences ranked. No Cortex call, nothing written to Snowflake or Tableau.
"""
import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

ORDER = ('Verified', 'Partly verified', 'Not matched', 'Did not run', 'Invalid plan', 'Not planned',
         'No Tableau reference', 'No data', 'Capture failed')


def gaps(ir, capture_bytes, proven, no_data=None):
    """Rows for every workbook sheet prove_run did not compare, each with why: its view failed to capture, the
    capture has no such sheet, or the app has no plan for it. proven = {(dashboard, sheet)} compared."""
    import v3_sheet_capture as SC
    caps = {SC.canon(c.get('dashboard')): c for c in SC.dashboard_captures(SC.load(capture_bytes))}
    rows = []
    for dash in ir.get('dashboards') or []:
        cap = caps.get(SC.canon(dash.get('name'))) or caps.get(SC.canon(dash.get('title')))
        for s in dash.get('sheets') or []:
            key = (dash.get('name'), s.get('name'))
            if key in proven:
                continue
            if cap is None:
                continue                                    # not a published view: nothing to compare against
            if cap.get('error'):
                res, note = 'Capture failed', cap['error']
            elif no_data and s.get('worksheet_semantics'):
                res, note = 'No data', no_data
            elif not s.get('worksheet_semantics'):
                res, note = 'Not planned', 'the app has no sheet plan to compare'
            else:
                res, note = 'No Tableau reference', 'the view capture has no data for this sheet'
            rows.append({'Dashboard': key[0], 'Sheet': key[1], 'Result': res, 'Note': note})
    return rows


def census(folder, sf=False, log=print):
    import v3_exact_loop as X
    from plan_census import intake
    from reference_census import choose_source, local_only
    from contextlib import nullcontext
    folder = Path(folder)
    rows = []
    for cap in sorted(folder.glob('*.capture.json')):
        stem = cap.name[:-len('.capture.json')]
        book = next((p for p in folder.iterdir() if p.stem == stem and p.suffix.lower() in ('.twbx', '.twb')), None)
        if book is None:
            rows.append({'workbook': stem, 'Dashboard': '—', 'Sheet': '—', 'Result': 'WORKBOOK_MISSING'})
            continue
        try:
            r = intake(book) if sf else None
            kind, bindings = choose_source(r['bindings']) if r else (None, {})
            if kind != 'snowflake':
                with local_only():
                    r = intake(book)
                kind, bindings = choose_source(r['bindings'])
            run = {'capture_bytes': cap.read_bytes(), 'result': r, 'bindings': bindings}
            with (local_only() if kind == 'bundled' else nullcontext()):
                found = X.prove_run(run, snowflake=(kind == 'snowflake')) if kind else []
            found += gaps(r['ir'], run['capture_bytes'], {(x['Dashboard'], x['Sheet']) for x in found},
                          no_data=None if kind else ('no bundled data; bound to Snowflake: run with --sf' if not sf
                                                     else 'not bound to Snowflake tables or bundled data'))
            for row in found:
                rows.append(dict(row, workbook=book.name, Data=kind))
        except Exception as exc:                               # codes only
            rows.append({'workbook': book.name, 'Dashboard': '—', 'Sheet': '—',
                         'Result': 'WORKBOOK_FAILED:' + type(exc).__name__})
        log(f"{book.name if book else stem}: " + ', '.join(
            f'{k} {v}' for k, v in Counter(x['Result'] for x in rows if x['workbook'] in (stem, getattr(book, 'name', ''))).items()))
    return rows


def report(rows):
    counts = Counter(r['Result'] for r in rows)
    other = sorted(k for k in counts if k not in ORDER)
    lines = [f'# Sheet census (Tableau per-sheet capture vs the app) — {time.strftime("%Y-%m-%d %H:%M")}', '',
             f'Sheets: {len(rows)} · ' + ' · '.join(f'{k} {counts[k]}' for k in ORDER + tuple(other) if counts.get(k)),
             '', '| Workbook | Sheets | Verified | Partly | Not matched | Other |', '|---|---|---|---|---|---|']
    by = defaultdict(Counter)
    for r in rows:
        by[r['workbook']][r['Result']] += 1
    for wb, c in sorted(by.items()):
        lines.append(f"| {wb} | {sum(c.values())} | {c.get('Verified', 0)} | {c.get('Partly verified', 0)} | "
                     f"{c.get('Not matched', 0)} | {sum(c.values()) - c.get('Verified', 0) - c.get('Partly verified', 0) - c.get('Not matched', 0)} |")
    bad = [r for r in rows if r['Result'] != 'Verified']
    bad.sort(key=lambda r: (ORDER.index(r['Result']) if r['Result'] in ORDER else 99, -(r.get('Different') or 0)))
    lines += ['', '## Not verified (ranked)', '',
              '| Workbook | Dashboard | Sheet | Result | Equal | Different | Missing | Extra | Note |',
              '|---|---|---|---|---|---|---|---|---|']
    for r in bad:
        lines.append(f"| {r['workbook']} | {r.get('Dashboard', '—')} | {r.get('Sheet', '—')} | {r['Result']} | "
                     f"{r.get('Values equal') or 0} | {r.get('Different') or 0} | {r.get('Missing') or 0} | "
                     f"{r.get('Extra') or 0} | {str(r.get('Note') or '').replace('|', '/')[:200]} |")
    return '\n'.join(lines) + '\n'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--folder', required=True)
    ap.add_argument('--sf', action='store_true')
    a = ap.parse_args()
    if a.sf:
        import backend
        from snowflake.snowpark import Session
        print('opening the Snowflake session (finish any sign-in in your browser)', flush=True)
        backend.set_session(Session.builder.config('connection_name',
                                                   os.environ.get('V3_PREVIEW_CONNECTION', 'wbr')).create())
    rows = census(a.folder, sf=a.sf, log=lambda m: print(m, flush=True))
    md = report(rows)
    Path(a.folder, 'sheet_census.md').write_text(md, encoding='utf-8')
    Path(a.folder, 'sheet_census.json').write_text(json.dumps(rows, indent=1, default=str), encoding='utf-8')
    print(md)


if __name__ == '__main__':
    main()
