"""EN07 "no failed run" gate: every workbook through the console's own run, offline.

    python tools/corpus_runner.py [paths ...] [--out reports/<folder>] [--fresh] [--only substring]

Runs the same run_pipeline the console runs (Discovery, Parsing, Data model, App build, Validation)
with no Snowflake session, then renders every sheet on the workbook's bundled data the way the
Validation page does (v3_evidence.sheet_checks). Nothing is written to Snowflake and no Cortex call
is made; the Snowflake-only steps (binding to tables, Semantic View, Cortex) are covered by the
hosted console run and recorded in the EN01 tables.

Outcome per workbook stage: done / review / failed. Outcome per sheet:
  Converted        engine supports it and it rendered (or rendered with 0 rows)
  Refused          engine refuses it with a stated reason (no blank sheet is claimed)
  Needs Snowflake  data is not bundled with the workbook (live connection); rendered in the hosted run
  Failed           an exception, a render error, or a sheet-level warning shown instead of the content
                   (e.g. "measure not convertible", "could not render") on a sheet the engine said it supports
  Converted (warning)  rendered, but a filter could not be applied (listed)
A workbook FAILS the gate when any stage failed or any sheet Failed. Exit code 1 when any workbook
fails. Per-workbook results are written to <out>/runs/<name>.json, so a long corpus can be run in
parts; --fresh re-runs everything. No workbook or sheet names are used to decide anything.
"""
import argparse
import csv
import datetime
import hashlib
import json
import sys
import time
import traceback
from unittest.mock import patch
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CONVERTIBLE = ('UNVERIFIED', 'PARTIAL')
REFUSED = ('UNSUPPORTED', 'INVALID')
NO_LOCAL_DATA = '__set after the first corpus run__'   # render note meaning: data not bundled


def corpus(paths):
    if paths:
        return [Path(p) for p in paths]
    found = set()
    folders = [ROOT, ROOT / 'Workbooks', ROOT / 'tests' / 'corpus_frozen']
    folders += sorted(p.parent for p in (ROOT / 'Workbooks').glob('cloud_*/manifest.json'))
    for folder in folders:
        for pattern in ('*.twb', '*.twbx'):
            found.update(folder.glob(pattern))
    return sorted(found, key=lambda p: str(p.relative_to(ROOT)).lower())


def cloud_entry(path):
    """The manifest entry when the file belongs to a frozen Tableau Cloud test set (fetch_cloud_project.py)."""
    manifest = path.parent / 'manifest.json'
    if not manifest.exists():
        return None
    try:
        data = json.loads(manifest.read_text(encoding='utf-8'))
    except ValueError:
        return None
    for entry in data.get('workbooks') or []:
        if entry.get('file') == path.name:
            return entry
    return None


def _slug(path):
    rel = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else path.name
    return hashlib.sha256(rel.encode()).hexdigest()[:10] + '_' + ''.join(
        c if c.isalnum() else '_' for c in path.stem)[:60]


def _quiet():
    import logging
    try:
        import streamlit.logger as SL
        SL.set_log_level('error')
    except Exception:
        pass
    for name in list(logging.root.manager.loggerDict):
        if name.startswith('streamlit'):
            logging.getLogger(name).setLevel(logging.ERROR)


class _messages_per_sheet:
    """Record what each sheet showed the user while it rendered: st.error / st.warning / st.info and
    markdown text. A sheet that rendered by showing an error is a failure, not a conversion."""

    KINDS = {'error': 'error', 'exception': 'error', 'warning': 'warning', 'info': 'warning',
             'markdown': 'text', 'write': 'text', 'html': 'text', 'caption': 'text'}

    def __init__(self, store):
        self.store, self.saved = store, {}

    def __enter__(self):
        import streamlit as st
        import headless_render as H
        current = {'key': None}
        for name, kind in self.KINDS.items():
            real = getattr(st, name, None)
            if real is None:
                continue
            self.saved[name] = real

            def spy(*a, _real=real, _kind=kind, **k):
                if current['key'] is not None:
                    text = str(a[0]) if a else ''
                    self.store.setdefault(current['key'], {}).setdefault(_kind, []).append(text[:300])
                return _real(*a, **k)
            setattr(st, name, spy)
        real_capture = H._capture_all
        self.saved_capture = (H, real_capture)

        def capture(sheet, where, dashboard_name=None, *a, **k):
            current['key'] = (dashboard_name, sheet.get('name') if isinstance(sheet, dict) else None)
            try:
                return real_capture(sheet, where, dashboard_name, *a, **k)
            finally:
                current['key'] = None
        H._capture_all = capture
        return self

    def __exit__(self, *exc):
        import streamlit as st
        for name, real in self.saved.items():
            setattr(st, name, real)
        H, real_capture = self.saved_capture
        H._capture_all = real_capture
        return False


def run_one(path):
    import streamlit as st
    _quiet()
    import v3_console as C
    import v3_evidence as EV
    raw = path.read_bytes()
    out = {'file': str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
           'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw), 'stages': {}, 'sheets': [],
           'error': None, 'seconds': None}
    t0 = time.time()
    entry = cloud_entry(path)
    try:
        if entry:
            # Exactly the console's Tableau Cloud path (published-dashboard scoping, Cloud address for the
            # Tableau reference), with the frozen file and views in place of the live download.
            if entry.get('sha256') and entry['sha256'] != out['sha256']:
                raise ValueError('file does not match the frozen manifest hash')
            out['cloud'] = {k: entry.get(k) for k in ('id', 'name', 'project', 'content_url', 'published_views')}
            source = {'origin': 'tableau-cloud', 'name': entry.get('name') or path.stem,
                      'server_url': entry['server_url'], 'site_content_url': entry['site_content_url'],
                      'workbook_id': entry['id'], 'content_url': entry.get('content_url')}
            fetched = {'filename': path.name, 'bytes': raw, 'workbook_id': entry['id'], 'name': entry.get('name')}
            views = entry.get('published_views')
            with patch('tableau_server.fetch_workbook_by_id', return_value=fetched), \
                    patch('tableau_server.list_published_views',
                          **({'return_value': views} if views is not None else {'side_effect': RuntimeError('views')})):
                run = C.run_pipeline(source, st.empty())
        else:
            run = C.run_pipeline({'origin': 'upload', 'name': path.name, 'raw': raw}, st.empty())
    except Exception as exc:                       # run_pipeline itself must never raise
        out.update(error=f'run_pipeline raised {type(exc).__name__}: {str(exc)[:300]}',
                   trace=traceback.format_exc()[-2000:], seconds=round(time.time() - t0, 1))
        return out
    out['pipeline_seconds'] = round(time.time() - t0, 1)
    _quiet()
    out['stages'] = dict(run.get('states') or {})
    if run.get('failed'):
        out['error'] = f"{run['failed']} failed: {str(run.get('error') or '')[:400]}"
    result = run.get('result') or {}
    assessment = result.get('assessment') or {}
    out['issues'] = [str(i)[:300] for i in assessment.get('issues') or []]
    out['untranslated_calculations'] = assessment.get('untranslated_calculations')
    states = {(r.get('dashboard'), r.get('sheet')): r for r in assessment.get('sheets') or []}
    sheet_ds = {(d.get('name'), s.get('name')): s.get('datasource')
                for d in (result.get('ir') or {}).get('dashboards', []) for s in d.get('sheets', [])}
    missing = set(run.get('missing') or [])
    out['missing_bindings'] = sorted(missing)
    if result and not states:
        out['note'] = 'no sheet occurrences in the published dashboards'
    checks = {}
    render_error = None
    messages = {}
    if result and not out['issues']:
        try:
            run.setdefault('bindings', {})
            EV.sheet_checks(run, False)                 # the console's own draw rule (render_status)
            checks = {(r['Dashboard'], r['Sheet']): r for r in run.get('sheet_checks') or []}
        except Exception as exc:
            render_error = f'sheet rendering raised {type(exc).__name__}: {str(exc)[:300]}'
            out['render_trace'] = traceback.format_exc()[-2000:]
    for key, a in states.items():
        row = {'dashboard': key[0], 'sheet': key[1], 'kind': a.get('kind'), 'engine_state': a.get('state'),
               'reason': (a.get('reason') or '')[:4000]}
        c = checks.get(key)
        if a.get('state') in REFUSED:
            row['outcome'] = 'Refused'
        elif sheet_ds.get(key) in missing:          # live Snowflake source: drawn in the hosted run
            row.update(outcome='Needs Snowflake', note='datasource needs a Snowflake binding: ' + str(sheet_ds.get(key)))
        elif render_error:
            row.update(outcome='Failed', note=render_error)
        elif c is None:
            row.update(outcome='Failed', note='sheet was not rendered')
        else:
            rend = str(c.get('Renders on data'))
            row.update(renders=rend, rows=c.get('Rows'), output=c.get('Output'), note=(c.get('Note') or '')[:300],
                       shown_messages=(c.get('Messages') or '')[:400])
            if rend == 'Error' and NO_LOCAL_DATA in str(c.get('Note') or ''):
                row['outcome'] = 'Needs Snowflake'
            else:
                row['outcome'] = ('Failed' if rend == 'Error' else
                                  'Converted (warning)' if rend.startswith('Yes (filter warning)') else
                                  'Converted (0 rows)' if rend.startswith('Yes (0 rows)') else 'Converted')
        out['sheets'].append(row)
    lay = result.get('layout_check') or {}
    out['layout'] = dict(lay.get('summary') or {})
    # release 26c gate: a zone not drawn is accepted only when the source file itself lacks it
    rows_ = lay.get('rows') or []
    # release 26f: a zone type the app does not draw yet (highlighter, web page) is reported, not hidden
    out['layout']['not_supported'] = sum(1 for z in rows_ if z.get('Status') == 'Not drawn'
                                         and (z.get('Note') or '').startswith('Tableau '))
    out['layout']['unexplained'] = sum(1 for z in rows_
                                       if z.get('Status') == 'Moved' or (z.get('Status') == 'Not drawn'
                                       and 'not packaged with the workbook' not in (z.get('Note') or '')
                                       and not (z.get('Note') or '').startswith('Tableau ')))
    out['seconds'] = round(time.time() - t0, 1)
    return out


def verdict(r):
    failed_stage = any(v == 'failed' for v in (r.get('stages') or {}).values())
    failed_sheets = sum(1 for s in r.get('sheets') or [] if s['outcome'] == 'Failed')
    layout_bad = (r.get('layout') or {}).get('unexplained', 0)
    return 'FAIL' if (r.get('error') or failed_stage or failed_sheets or layout_bad) else 'PASS'


def summarise(results, out):
    rows = []
    for r in results:
        c = Counter(s['outcome'] for s in r.get('sheets') or [])
        rows.append({'workbook': r['file'], 'gate': verdict(r), 'sheets': sum(c.values()),
                     'converted': c['Converted'] + c['Converted (warning)'], 'zero_rows': c['Converted (0 rows)'],
                     'refused': c['Refused'],
                     'needs_snowflake': c['Needs Snowflake'],
                     'failed_sheets': c['Failed'],
                     'stages': ' / '.join(f'{k}:{v}' for k, v in (r.get('stages') or {}).items()),
                     'layout': ' / '.join(f'{k}:{v}' for k, v in (r.get('layout') or {}).items()),
                     'error': (r.get('error') or '')[:200], 'seconds': r.get('seconds')})
    with (out / 'corpus_runner.csv').open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ['workbook'])
        w.writeheader()
        w.writerows(rows)
    with (out / 'corpus_sheets.csv').open('w', newline='', encoding='utf-8') as f:
        keys = ['workbook', 'dashboard', 'sheet', 'kind', 'engine_state', 'outcome', 'renders', 'rows', 'output',
                'reason', 'note', 'shown_messages']
        w = csv.DictWriter(f, fieldnames=keys, extrasaction='ignore')
        w.writeheader()
        for r in results:
            for s in r.get('sheets') or []:
                w.writerow(dict(s, workbook=r['file']))
    total = Counter()
    for r in rows:
        total.update({k: r[k] for k in ('sheets', 'converted', 'zero_rows', 'refused', 'needs_snowflake', 'failed_sheets')})
    failing = [r for r in rows if r['gate'] == 'FAIL']
    lines = [f"# EN07 corpus run — {datetime.datetime.now(datetime.timezone.utc):%Y-%m-%d %H:%M} UTC", '',
             f"Workbooks: {len(rows)}; gate PASS {len(rows) - len(failing)}, FAIL {len(failing)}.",
             f"Sheet occurrences: {total['sheets']}; converted and rendered {total['converted']}; rendered with 0 rows "
             f"{total['zero_rows']} (check against Tableau); refused with a reason {total['refused']}; needs Snowflake {total['needs_snowflake']}; failed {total['failed_sheets']}.",
             '', 'Offline run: no Snowflake session, no Cortex calls. "Converted" means the engine built and '
             'rendered the sheet on bundled data; it is not a Tableau parity verdict.', '',
             '| Workbook | Gate | Sheets | Converted | 0 rows | Refused | Needs Snowflake | Failed | Error |',
             '|---|---|---|---|---|---|---|---|---|']
    lines += [f"| {r['workbook']} | {r['gate']} | {r['sheets']} | {r['converted']} | {r['zero_rows']} | {r['refused']} | "
              f"{r['needs_snowflake']} | {r['failed_sheets']} | {r['error'].replace('|', '/')} |" for r in rows]
    (out / 'corpus_summary.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return failing, total


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('paths', nargs='*')
    ap.add_argument('--out', default='reports/en07_corpus')
    ap.add_argument('--fresh', action='store_true')
    ap.add_argument('--only', default=None, help='run only files whose path contains this text')
    ap.add_argument('--summary-only', action='store_true')
    args = ap.parse_args()
    try:                                   # Windows consoles: never fail on a workbook name
        sys.stdout.reconfigure(errors='replace')
    except Exception:
        pass
    out = ROOT / args.out
    (out / 'runs').mkdir(parents=True, exist_ok=True)
    files = corpus(args.paths)
    results = []
    for path in files:
        target = out / 'runs' / (_slug(path) + '.json')
        if args.only and args.only.lower() not in str(path).lower():
            if target.exists():
                results.append(json.loads(target.read_text(encoding='utf-8')))
            continue
        if target.exists() and not args.fresh or args.summary_only:
            if target.exists():
                results.append(json.loads(target.read_text(encoding='utf-8')))
            continue
        print('running', path.name, flush=True)
        r = run_one(path)
        target.write_text(json.dumps(r, indent=1, default=str), encoding='utf-8')
        print(f"  {verdict(r)} in {r['seconds']}s", flush=True)
        results.append(r)
    failing, total = summarise(results, out)
    print(f"{len(results)} workbooks; {len(failing)} fail the gate; sheets {dict(total)}")
    for r in failing:
        print('FAIL', r['workbook'], r['error'] or f"{r['failed_sheets']} sheet(s) failed")
    return 1 if failing else 0


if __name__ == '__main__':
    raise SystemExit(main())
