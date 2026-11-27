"""Release 26 scoreboard: every sheet of a workbook against Tableau's own values, offline.

    python tools/exact_scoreboard.py Workbooks/cloud_20261010/Superstore_Tableau2024_3.twbx \
        --captures reports/release26_exact/tableau_captures_20261101.json [--out reports/release26_exact]

For each published dashboard sheet it reports:
  Engine today   what the current app shows for it (Converted / Refused) and, when converted, whether its
                 values equal Tableau's (the existing v3_sheet_capture comparison)
  Exact plan     the release 26 plan from the authored shelves (and its addressing variants): Verified /
                 Not matched (with the exact difference) / Did not run
Nothing is written to Snowflake and no Cortex call is made. No workbook or sheet names are used as logic.
"""
import argparse
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))


def pipeline_run(path):
    import streamlit as st
    import corpus_runner as CR
    import v3_console as C
    CR._quiet()
    raw = path.read_bytes()
    entry = CR.cloud_entry(path)
    if entry:
        source = {'origin': 'tableau-cloud', 'name': entry.get('name') or path.stem,
                  'server_url': entry['server_url'], 'site_content_url': entry['site_content_url'],
                  'workbook_id': entry['id'], 'content_url': entry.get('content_url')}
        fetched = {'filename': path.name, 'bytes': raw, 'workbook_id': entry['id'], 'name': entry.get('name')}
        views = entry.get('published_views')
        with patch('tableau_server.fetch_workbook_by_id', return_value=fetched), \
                patch('tableau_server.list_published_views', return_value=views):
            return C.run_pipeline(source, st.empty()), entry
    return C.run_pipeline({'origin': 'upload', 'name': path.name, 'raw': raw}, st.empty()), None


def captures_for(bundle, content_url):
    out = {}
    for key, cap in bundle.get('captures', {}).items():
        if content_url and key.split('/')[0] != content_url:
            continue
        out[cap.get('dashboard')] = cap
    return out


def scoped_sheets(path, run_ir):
    """The scoped (V3) description of every published sheet, before any original-engine substitution."""
    import tableau_parser
    ir = tableau_parser.build_ir(str(path), scoped=True)
    names = {d.get('name') for d in run_ir.get('dashboards', [])}
    return {(d['name'], s['name']): s for d in ir['dashboards'] if d['name'] in names for s in d['sheets']}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('workbook')
    ap.add_argument('--captures', required=True)
    ap.add_argument('--out', default='reports/release26_exact')
    ap.add_argument('--only', action='append', default=[], help='sheet name (debug: prints the query and rows)')
    a = ap.parse_args(argv)
    path = Path(a.workbook)
    bundle = json.loads(Path(a.captures).read_text(encoding='utf-8'))
    t0 = time.time()
    run, entry = pipeline_run(path)
    caps = captures_for(bundle, (entry or {}).get('content_url'))
    import engine
    import parameter_actions
    import v3_evidence as EV
    import v3_exact_loop as X
    import v3_sheet_capture as SC
    from v3_local import preview_context
    result = run['result']
    ir = result['ir']
    run.setdefault('bindings', {})
    EV.sheet_checks(run, False)
    today = {(r['Dashboard'], r['Sheet']): r for r in run['sheet_checks']}
    scoped = scoped_sheets(path, ir)
    rows = []
    with preview_context(ir, run['bindings'], snowflake=False), EV.offscreen():
        parameter_actions.reset(parameter_actions.context(engine))
        for (dash, name), sheet in scoped.items():
            if a.only and name not in a.only:
                continue
            if a.only:
                import v3_sheet_plan as P
                try:
                    pl = P.from_sheet(sheet, ir)
                    with engine.field_scope(sheet):
                        comp = P.compile_blend if pl.get('blend') else P.compile_query
                        sql, _c = comp(pl, sheet, engine, P.sheet_where(sheet, engine))
                    print('SQL', name, sql[:1500])
                    print(P.execute(pl, sheet, engine).head(5).to_string()[:1500])
                except Exception as exc:
                    print('DEBUG', name, type(exc).__name__, exc)
            cap = caps.get(dash)
            entry_t = (cap or {}).get('sheets', {}).get(name)
            t = today.get((dash, name), {})
            state = t.get('Renders on data', '—')
            engine_match = '—'
            values = (run.get('sheet_values') or {}).get((dash, name))
            if entry_t is not None and values and state not in ('Refused',):
                run_sheet = next((s for d in ir['dashboards'] if d['name'] == dash for s in d['sheets']
                                  if s['name'] == name), None)
                summary, _ = SC.compare_sheet(entry_t, values.get('all') or values.get('rows'), run_sheet)
                engine_match = summary.get('Status')
            plan, ev = X.prove(sheet, engine, entry_t, ir=ir)
            best = ev.get('best') or {}
            visual = X.visual_check(plan, sheet) if plan else {'status': '—', 'issues': []}
            rows.append({'Dashboard': dash, 'Sheet': name, 'Engine today': state, 'Engine vs Tableau': engine_match,
                         'Exact plan': ev['status'], 'Plan source': best.get('source'),
                         'Tableau rows': best.get('rows_tableau'), 'App rows': best.get('rows_app'),
                         'Matched values': best.get('matched'), 'Different': best.get('different'),
                         'Missing': best.get('missing_in_app'), 'Extra': best.get('extra_in_app'),
                         'Visual': visual['status'], 'Visual issues': visual['issues'][:4],
                         'Reasons': (best.get('reasons') or [r for t_ in ev['tried'] for r in t_.get('reasons', [])])[:4],
                         'Detail': (best.get('details') or [])[:8], 'Tried': [(t_.get('source'), t_.get('status'))
                                                                             for t_ in ev['tried']]})
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    target = out / f'scoreboard_{path.stem}.json'
    target.write_text(json.dumps({'workbook': str(path), 'seconds': round(time.time() - t0, 1), 'sheets': rows},
                                 indent=1, default=str), encoding='utf-8')
    verified = sum(r['Exact plan'] == 'Verified' for r in rows)
    print(f'{path.name}: {verified} of {len(rows)} sheets Verified against Tableau ({target})')
    for r in rows:
        print(f"  {r['Dashboard'][:18]:18} {r['Sheet'][:26]:26} today={r['Engine today'][:10]:10} "
              f"eng={str(r['Engine vs Tableau'])[:16]:16} plan={r['Exact plan']:12} "
              f"m={r['Matched values']} d={r['Different']} miss={r['Missing']} x={r['Extra']} | "
              f"{'; '.join(str(x) for x in r['Reasons'])[:230]}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
