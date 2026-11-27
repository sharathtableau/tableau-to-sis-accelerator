"""Reference census: Tableau's OWN numbers (REST 'Query View Data') against the app, for every workbook of a Tableau
Cloud project. Turns "the sheet runs" into "equal to Tableau" counts across the corpus, without a browser.

    python -X utf8 tools/reference_census.py --server https://<pod>.online.tableau.com --site <site> --project "<name>"
                                             [--limit N] [--out reports/<folder>]

Tableau: the owner's Personal Access Token from the environment (TABLEAU_PAT_NAME / TABLEAU_PAT_SECRET, set by the
owner in the PowerShell session; never read or printed here). Snowflake: the owner's named connection profile
(V3_PREVIEW_CONNECTION, default "wbr"), read-only queries. No Cortex call (the intake skips the Cortex review).

Per published view: v3_reference.compare_view -> Match / Difference found / Not comparable / Tableau reference
unavailable (one sheet per view or 'not comparable'; never guessed). REST view data returns a dashboard's FIRST sheet
only, so a dashboard view compares one sheet. Errors are reported as codes. Writes <out>/reference_census.md / .json.
"""
import argparse
from contextlib import contextmanager, nullcontext
import json
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

STATUSES = ('Match', 'Difference found', 'Not comparable', 'Tableau reference unavailable')


def project_workbooks(site, project):
    ids = {p['id'] for p in site.get('projects') or [] if p.get('name') == project}
    return [w for w in site.get('workbooks') or []
            if w.get('project_id') in ids or w.get('project_name') == project]


def run(workbooks, fetch, compare, log=print):
    """fetch(workbook_id, name) -> {'filename', 'bytes', 'published_views'};
    compare(fetched, workbook_id) -> [compare_view records]."""
    rows = []
    for w in workbooks:
        try:
            fetched = fetch(w['id'], w.get('name'))
            recs = compare(fetched, w['id'])
            rows += [dict(r, workbook=w.get('name')) for r in recs]
        except Exception as exc:                      # codes only (no Tableau / Snowflake error text)
            rows.append({'workbook': w.get('name'), 'View': '(workbook)', 'Sheet': '—',
                         'Status': 'WORKBOOK_FAILED:' + type(exc).__name__, 'Matched values': 0,
                         'Mismatched values': 0, 'Rows missing in app': 0, 'Note': ''})
        log(f"{w.get('name')}: " + ', '.join(f'{k} {v}' for k, v in
                                             Counter(r['Status'] for r in rows if r['workbook'] == w.get('name')).items()))
    return rows


def report(rows, project):
    counts = Counter(r['Status'] for r in rows)
    other = sorted(k for k in counts if k not in STATUSES)
    lines = [f'# Reference census — project "{project}" — {time.strftime("%Y-%m-%d %H:%M")}', '',
             f'Views: {len(rows)} · ' + ' · '.join(f'{k} {counts.get(k, 0)}' for k in STATUSES + tuple(other)), '',
             '| Workbook | Views | Match | Difference | Not comparable |', '|---|---|---|---|---|']
    by = defaultdict(Counter)
    for r in rows:
        by[r['workbook']][r['Status']] += 1
    for wb, c in sorted(by.items()):
        lines.append(f"| {wb} | {sum(c.values())} | {c.get('Match', 0)} | {c.get('Difference found', 0)} | "
                     f"{c.get('Not comparable', 0)} |")
    lines += ['', '## Differences and refusals', '', '| Workbook | View | Sheet | Status | Matched | Mismatched | Note |',
              '|---|---|---|---|---|---|---|']
    for r in rows:
        if r['Status'] != 'Match':
            lines.append(f"| {r['workbook']} | {r['View']} | {r.get('Sheet', '—')} | {r['Status']} | "
                         f"{r.get('Matched values', 0)} | {r.get('Mismatched values', 0)} | "
                         f"{str(r.get('Note', '')).replace('|', '/')[:160]} |")
    return '\n'.join(lines) + '\n'


def choose_source(bindings):
    """('snowflake', table bindings) when the workbook is bound to Snowflake tables; else ('bundled', its own data
    files -- the extract / files Tableau Cloud queries too, compared on local DuckDB); else (None, {})."""
    from plan_census import snowflake_bindings
    sf = snowflake_bindings(bindings)
    if sf:
        return 'snowflake', sf
    local = {k: v for k, v in (bindings or {}).items() if isinstance(v, str) and v}
    return ('bundled', local) if local else (None, {})


@contextmanager
def local_only():
    """Set the Snowflake session aside for one workbook's local comparison; always restored."""
    import backend
    saved, active = backend._EXTERNAL_SESSION, backend._active_session
    backend.set_session(None)
    # a Session made with Session.builder is also Snowpark's GLOBAL active session, which backend falls back to
    backend._active_session = lambda: None
    try:
        yield
    finally:
        backend._active_session = active
        backend.set_session(saved)


_STATUS = {'Verified': 'Match', 'Partly verified': 'Difference found', 'Not matched': 'Difference found'}


def compare_plan(ir, view, engine):
    """One REST view against the app on the PLAN path (v3_exact_loop.prove, the comparison that verifies sheets
    against Tableau captures): every sheet of the view's dashboard is tried, the best result is kept. Statuses:
    Verified -> Match; Partly verified / Not matched -> Difference found; else Not comparable (reason)."""
    import v3_exact_loop as X
    from v3_reference import _sheets_for_view, compare_view, read_view_csv
    if view.get('error') or not view.get('csv'):
        return compare_view(ir, view, lambda *_: None)[0]          # 'Tableau reference unavailable' (codes only)
    header, rows = read_view_csv(view['csv'])
    entry = {'columns': header, 'rows': [[r.get(h) for h in header] for r in rows]}
    dash, sheets = _sheets_for_view(ir, view.get('view'))
    best = None
    for sheet in sheets:
        if not sheet.get('worksheet_semantics'):
            continue
        try:
            _plan, ev = X.prove(sheet, engine, entry, ir=ir)
        except Exception:
            continue
        res = ev.get('best')
        if res and (best is None or X._rank(res) > X._rank(best[1])):
            best = (sheet, res)
    rec = {'View': view.get('view'), 'Sheet': '—', 'Matched values': 0, 'Mismatched values': 0,
           'Rows missing in app': 0}
    if not best or best[1].get('status') not in _STATUS:
        why = '; '.join((best[1].get('reasons') or [])[:2]) if best else 'no sheet of this view could be planned'
        return dict(rec, Status='Not comparable', Note=(why or 'no sheet maps to these columns')[:300] +
                    ' | Tableau columns: ' + ', '.join(header)[:200])
    sheet, res = best
    return dict(rec, Sheet=sheet.get('name'), Status=_STATUS[res['status']],
                **{'Matched values': res.get('matched', 0), 'Mismatched values': res.get('different', 0),
                   'Rows missing in app': res.get('missing_in_app', 0),
                   'Note': f"{len(rows)} Tableau rows; " + '; '.join((res.get('reasons') or [])[:2])[:250]})


def _live_compare(server, site):
    """Production compare: intake (no Cortex) with the Snowflake session -> REST views -> compare_view."""
    import engine
    import parameter_actions
    import tableau_server as TS
    from plan_census import intake
    from v3_local import preview_context

    def compare(fetched, workbook_id):
        import tempfile
        folder = Path(tempfile.mkdtemp(prefix='tableau-v3-refcensus-'))
        path = folder / fetched['filename']
        path.write_bytes(fetched['bytes'])
        views = TS.pull_all_view_csvs(server, site, workbook_id)
        r = intake(path, published_views=fetched.get('published_views'))     # with the session: Snowflake bindings
        kind, bindings = choose_source(r['bindings'])
        if kind != 'snowflake':
            with local_only():        # no Snowflake table: the workbook's own bundled data, decoded locally
                r = intake(path, published_views=fetched.get('published_views'))
            kind, bindings = choose_source(r['bindings'])
        if not kind:
            return [{'View': v.get('view'), 'Sheet': '—', 'Status': 'Not comparable', 'Matched values': 0,
                     'Mismatched values': 0, 'Rows missing in app': 0,
                     'Note': 'no data: no Snowflake table bound and no bundled data (onboarding)'} for v in views]
        out = []
        note = 'Snowflake tables' if kind == 'snowflake' else "the workbook's bundled data (local)"
        guard = local_only() if kind == 'bundled' else nullcontext()
        with guard, preview_context(r['ir'], bindings, snowflake=(kind == 'snowflake')):
            parameter_actions.reset(parameter_actions.context(engine))
            for v in views:
                try:
                    rec = dict(compare_plan(r['ir'], v, engine))
                except Exception as exc:
                    rec = {'View': v.get('view'), 'Sheet': '—', 'Status': 'COMPARE_FAILED:' + type(exc).__name__,
                           'Matched values': 0, 'Mismatched values': 0, 'Rows missing in app': 0, 'Note': ''}
                rec['Data'] = note
                out.append(rec)
        return out
    return compare


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--server', required=True)
    ap.add_argument('--site', default='')
    ap.add_argument('--project', required=True)
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--out', default=str(ROOT / 'reports' / ('reference_census_' + time.strftime('%Y%m%d'))))
    a = ap.parse_args()
    import backend
    import tableau_server as TS
    from snowflake.snowpark import Session
    print('opening the Snowflake session (finish any sign-in in your browser)', flush=True)
    backend.set_session(Session.builder.config('connection_name',
                                               os.environ.get('V3_PREVIEW_CONNECTION', 'wbr')).create())
    workbooks = project_workbooks(TS.list_site_contents(a.server, a.site), a.project)
    if a.limit:
        workbooks = workbooks[:a.limit]
    print(f'{len(workbooks)} workbook(s) in project "{a.project}"', flush=True)
    fetch = lambda wid, name: TS.fetch_workbook_by_id(a.server, a.site, wid, name_hint=name)
    rows = run(workbooks, fetch, _live_compare(a.server, a.site), log=lambda m: print(m, flush=True))
    os.makedirs(a.out, exist_ok=True)
    md = report(rows, a.project)
    Path(a.out, 'reference_census.md').write_text(md, encoding='utf-8')
    Path(a.out, 'reference_census.json').write_text(json.dumps(rows, indent=1, default=str), encoding='utf-8')
    print(md)


if __name__ == '__main__':
    main()
