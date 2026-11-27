"""CX2 in the product: Validation -> "Visual review (Cortex)". Advisory only.

Runs the SAME core as the laptop runner (cx2_visual_review.review: per-zone pairs, multimodal AI_COMPLETE, strict
parse, CX9 budget / injection guard / per-call cost, code verdicts, fix queue). Differences from the laptop tool:

  * Tableau image: the published view over REST with the bound PAT secret through the app's external access
    integration (tableau_server), pulled at the dashboard's authored size so zone rectangles line up.
  * App image: the app's own capture. Each sheet zone is drawn by the deployed renderer at its zone size
    (headless_render.render_sheet_to_png), so no laptop browser or preview server is involved.
  * Verdicts: rendered-page geometry and DOM text exist only in the laptop browser run, so in-app verdicts are
    XML_BACKED / ADVISORY / NO_DIFFERENCE (CONFIRMED_GEOMETRY / CONFIRMED_DOM need the laptop run).
  * Corpus reach needs the corpus on disk: the in-app queue shows the zones and constructs; reach is filled in
    by the laptop gate (tools/cx2_run.py).

Cortex or Tableau error text never reaches the UI: failures are shown as status codes.
"""
import io
import time
import uuid

import cx2_visual_review as cx2
import cx2_safety

STATE_KEY = 'cx2'
SKIP_KINDS = {'blank', 'image', 'web', 'bitmap', 'box', 'layout-flow', 'layout-basic'}


def canvas_rects(dash, engine):
    """Dashboard canvas zones -> [(zone dict, (x, y, w, h) canvas px)] in the authored W x H canvas."""
    W, H = engine.canvas_size(dash.get('canvas'))
    sx, sy = W / 100000, H / 100000
    out = []
    for i, z in enumerate(dash.get('canvas_zones') or []):
        if not isinstance(z, dict) or not all(isinstance(z.get(k), (int, float)) for k in ('x', 'y', 'w', 'h')):
            continue
        out.append((dict(z, _key=f'v3cz{i}'), (z['x'] * sx, z['y'] * sy, z['w'] * sx, z['h'] * sy)))
    return W, H, out


def _zone_xml(ir, dash, zone):
    """The worksheet XML (and the zone's own XML) that governs a zone: the verifier's only XML evidence."""
    for s in dash.get('sheets') or []:
        if s.get('name') == zone.get('sheet'):
            return (s.get('worksheet_semantics') or {}).get('source_xml') or ''
    return ''


def tableau_png(result, view_name, W, H):
    """Tableau's own render of the published view at the authored canvas size -> (png bytes or None, code)."""
    import tableau_server as TS
    try:
        name, secret = TS._resolve_pat(None, None)
        client = TS.TableauRestClient(result['cloud_server_url'], api_version='3.24')
        try:
            client.sign_in_with_pat(name, secret, result['cloud_site_content_url'])
            views = client.list_views(workbook_id=result['cloud_workbook_id'])
            view = next((v for v in views if v['name'] == view_name), None)
            if not view:
                return None, 'TABLEAU_VIEW_NOT_PUBLISHED'
            return client.query_view_image(view['id'], high_resolution=True, viz_width=W, viz_height=H), 'OK'
        finally:
            try:
                client.sign_out()
            except Exception:
                pass
    except Exception as exc:
        return None, 'TABLEAU_IMAGE_FAILED:' + type(exc).__name__


def app_zone_png(sheet, rect, where_parts=None):
    """The deployed renderer draws one sheet at its zone size (the app's own capture)."""
    import headless_render as H
    x, y, w, h = rect
    s = dict(sheet, _canvas=True, _wpx=max(40, int(w) - 10), _hpx=max(24, int(h)))
    png, why = H.render_sheet_to_png(s, where_parts=where_parts or [], scale=1.0, width=int(w))
    return png, ('OK' if png else 'APP_ZONE_NOT_DRAWN')


def build_pairs(run, dash, tab_png, engine, snowflake, max_zones=20):
    """-> (pairs, skipped). Each pair: {'key','label','kind','rect','xml','issues','dom_text','png'}."""
    from PIL import Image
    from v3_local import preview_context
    import parameter_actions
    W, H, zones = canvas_rects(dash, engine)
    tab = Image.open(io.BytesIO(tab_png)).convert('RGB')
    by_name = {s.get('name'): s for s in dash.get('sheets') or []}
    pairs, skipped = [], []
    zones = [(z, r) for z, r in zones if z.get('kind') == 'sheet' and z.get('sheet') in by_name
             and r[2] >= 30 and r[3] >= 16]
    zones.sort(key=lambda zr: -zr[1][2] * zr[1][3])
    with preview_context(run['result']['ir'], run['bindings'], snowflake=snowflake):
        parameter_actions.reset(parameter_actions.context(engine))
        for z, rect in zones[:max_zones]:
            try:
                png, code = app_zone_png(by_name[z['sheet']], rect)
            except Exception as exc:
                png, code = None, 'APP_ZONE_FAILED:' + type(exc).__name__
            if not png:
                skipped.append({'zone': z['_key'], 'sheet': z['sheet'], 'status': code})
                continue
            img = cx2.pair_image(tab, Image.open(io.BytesIO(png)), rect, W, H, app_is_crop=True)
            buf = io.BytesIO(); img.save(buf, format='PNG')
            pairs.append({'key': z['_key'], 'label': 'sheet: ' + str(z['sheet']), 'kind': 'sheet', 'rect': rect,
                          'xml': _zone_xml(run['result']['ir'], dash, z), 'issues': [], 'dom_text': '',
                          'png': buf.getvalue()})
    return pairs, skipped


def run_review(run, dash, session, engine, snowflake, model=cx2.DEFAULT_MODEL, budget_tokens=None, max_zones=20):
    """The whole in-app review for one dashboard. Stores and returns the record under run['cx2'][dashboard]."""
    result = run['result']
    rec = {'version': cx2.VERSION, 'safety': cx2_safety.VERSION, 'dashboard': dash.get('name'), 'model': model,
           'at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'status': None, 'findings': [], 'calls': [], 'queue': [],
           'skipped': [], 'guarded': 0, 'evaluation': []}
    store = run.setdefault(STATE_KEY, {})
    store[dash.get('name')] = rec
    if session is None:
        rec['status'] = 'NO_SNOWFLAKE_SESSION'
        return rec
    if not result.get('cloud_workbook_id'):
        rec['status'] = 'NOT_FROM_TABLEAU_CLOUD'
        return rec
    W, H, _ = canvas_rects(dash, engine)
    png, code = tableau_png(result, dash.get('name'), W, H)
    if not png:
        rec['status'] = code
        return rec
    pairs, rec['skipped'] = build_pairs(run, dash, png, engine, snowflake, max_zones=max_zones)
    if not pairs:
        rec['status'] = 'NO_ZONE_PAIRS'
        return rec
    run_id = time.strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6]
    stage_dir = 'app_' + run_id
    try:
        for p in pairs:
            session.file.put_stream(io.BytesIO(p['png']), f"{cx2.STAGE}/{stage_dir}/{p['key']}.png",
                                    auto_compress=False, overwrite=True)
    except Exception as exc:
        rec['status'] = 'STAGE_UPLOAD_FAILED:' + type(exc).__name__
        return rec
    budget = cx2_safety.Budget(budget_tokens or cx2_safety.DEFAULT_BUDGET_TOKENS)
    findings, calls, guarded = cx2.review(session, model, stage_dir, pairs, budget)
    rec.update(status='OK', run=run_id, findings=findings, calls=calls, guarded=len(guarded),
               budget=budget.summary(), queue=cx2.fix_queue(findings, {}, 0),
               pairs={p['key']: p['png'] for p in pairs},
               evaluation=cx2_safety.evaluation_rows(run_id, run.get('name'), dash.get('name'), findings))
    # CX9-02: the evaluation set and call costs are kept in WBR_DB.CX_EVAL (status code only)
    rec['eval_store'] = cx2_safety.record_evaluation(session, 'CX2', rec['evaluation'], calls, run_id=run_id)
    return rec


# ---------------------------------------------------------------- UI
_STATUS_TEXT = {
    'NO_SNOWFLAKE_SESSION': 'Needs the hosted app (a Snowflake session).',
    'NOT_FROM_TABLEAU_CLOUD': 'Pull the workbook from Tableau Cloud first (the review needs the published view).',
    'TABLEAU_VIEW_NOT_PUBLISHED': 'This dashboard is not a published view on Tableau Cloud.',
    'NO_ZONE_PAIRS': 'No sheet zone could be drawn for the comparison.',
}


def render_tab(run, st, engine, session, snowflake):
    """Validation -> Visual review (Cortex)."""
    import pandas as pd
    st.caption('Cortex compares Tableau and the app zone by zone and proposes differences. Code assigns every '
               'verdict from the workbook XML; ADVISORY rows are never acted on, and every fix needs your approval. '
               'In the app the verdicts are XML_BACKED / ADVISORY / NO_DIFFERENCE; measured geometry and rendered '
               'text verdicts come from the laptop run (tools/cx2_run.py).')
    dashboards = [d for d in run['result']['ir'].get('dashboards', []) if d.get('canvas_zones')]
    if not dashboards:
        st.info('No dashboard drawn as a Tableau canvas in this workbook.')
        return
    names = [d['name'] for d in dashboards]
    cols = st.columns([0.45, 0.3, 0.25])
    pick = cols[0].selectbox('Dashboard', names, key='cx2-dash')
    budget = cols[1].number_input('Token cap for this review', min_value=10000, max_value=1000000,
                                  value=cx2_safety.DEFAULT_BUDGET_TOKENS, step=10000, key='cx2-budget')
    go = cols[2].button('Run visual review', type='primary', use_container_width=True, key='cx2-run',
                        disabled=session is None)
    if go:
        dash = next(d for d in dashboards if d['name'] == pick)
        with st.spinner('Pulling Tableau\'s image, drawing each zone and asking Cortex…'):
            try:
                run_review(run, dash, session, engine, snowflake, budget_tokens=int(budget))
            except Exception as exc:                      # codes only, never service text
                run.setdefault(STATE_KEY, {})[pick] = {'status': 'REVIEW_FAILED:' + type(exc).__name__}
    rec = (run.get(STATE_KEY) or {}).get(pick)
    if not rec:
        st.info('Not run yet for this dashboard.')
        return
    if rec.get('status') != 'OK':
        st.warning(_STATUS_TEXT.get(rec.get('status'), 'Visual review did not run: ' + str(rec.get('status'))))
        return
    counts = pd.Series([f['verdict'] for f in rec['findings']]).value_counts().to_dict() if rec['findings'] else {}
    b = rec.get('budget') or {}
    st.markdown(f"**{len(rec['findings'])} findings** · " + ' · '.join(f'{k} {v}' for k, v in counts.items())
                + f" · {len(rec['queue'])} fix-queue rule(s) · tokens used ≈ {b.get('used_tokens_est', 0):,} of "
                f"{b.get('limit_tokens', 0):,} · calls refused by the cap {b.get('calls_refused', 0)} · "
                f"guarded (instruction-like) findings dropped {rec.get('guarded', 0)}")
    st.subheader('Fix queue (general rules, proposed)')
    st.dataframe([{'Category': g['category'], 'XML construct': g['construct'], 'Zones': len(g['zones']),
                   'Examples': ' | '.join(f"{x['tableau']} → {x['app']}" for x in g['examples'])[:300]}
                  for g in rec['queue']] or [{'Category': '—'}], hide_index=True, use_container_width=True)
    show_adv = st.toggle('Show ADVISORY and NO_DIFFERENCE rows', value=False, key='cx2-adv')
    rows = [f for f in rec['findings'] if show_adv or f['verdict'] not in ('ADVISORY', 'NO_DIFFERENCE')]
    st.subheader('Findings')
    for i, f in enumerate(rec['findings']):
        if f not in rows:
            continue
        with st.container(border=True):
            st.markdown(f"**{f['zone_label']}** · {f['category']} · {f['severity']} · `{f['verdict']}`"
                        + (f" · {f['construct']}" if f.get('construct') else ''))
            st.caption(f"Tableau: {f['tableau']}  \nApp: {f['app']}  \nEvidence: {f['evidence']}")
            ev = rec['evaluation'][i] if i < len(rec['evaluation']) else None
            if ev is not None:
                c = st.columns([0.2, 0.2, 0.6])
                if c[0].button('Confirm', key=f'cx2-ok-{pick}-{i}'):
                    ev['owner_label'] = 'confirmed'
                    ev['label_store'] = cx2_safety.label_evaluation(session, ev.get('id'), 'confirmed')
                if c[1].button('Reject', key=f'cx2-no-{pick}-{i}'):
                    ev['owner_label'] = 'rejected'
                    ev['label_store'] = cx2_safety.label_evaluation(session, ev.get('id'), 'rejected')
                if ev.get('owner_label'):
                    c[2].caption('Your label: ' + ev['owner_label'])
    with st.expander('Per-call cost (CX9)'):
        st.dataframe(rec['calls'], hide_index=True, use_container_width=True)
        metrics = cx2_safety.evaluation_metrics(rec['evaluation'])
        if metrics:
            st.caption('Labelled so far (precision by verdict): ' + ', '.join(
                f"{k} {m['confirmed']}/{m['labelled']}" for k, m in metrics.items()))
    with st.expander('Across all runs (CX9 evaluation table)'):
        st.caption('Stored in WBR_DB.CX_EVAL: ' + str(rec.get('eval_store') or 'not stored'))
        hist, hstatus = cx2_safety.evaluation_history(session)
        costs, _cstatus = cx2_safety.cost_history(session)
        if hstatus == 'OK':
            st.dataframe([{'Step': k[0], 'Verdict': k[1], 'Rows': m['rows'], 'Labelled': m['labelled'],
                           'Confirmed': m['confirmed'], 'Precision': m['precision']} for k, m in sorted(hist.items())]
                         or [{'Step': '—'}], hide_index=True, use_container_width=True)
            st.dataframe([{'Step': k, **v} for k, v in sorted(costs.items())] or [{'Step': '—'}],
                         hide_index=True, use_container_width=True)
        else:
            st.caption('The evaluation tables are not created yet (one-time SQL for an owner to run):')
            st.code('\n'.join(cx2_safety.eval_setup_sql()), language='sql')
    import json
    st.download_button('Download the evaluation set (findings + your labels + call costs)',
                       json.dumps({'evaluation': rec['evaluation'], 'calls': rec['calls'], 'budget': rec.get('budget')},
                                  indent=1, default=str),
                       file_name=f"cx2_evaluation_{rec.get('run', 'run')}.json", mime='application/json',
                       key=f'cx2-eval-{pick}')
    with st.expander('Zone pairs (Tableau left, app right)'):
        for key, png in (rec.get('pairs') or {}).items():
            st.image(png, caption=key, use_container_width=True)
    if rec.get('skipped'):
        st.caption('Zones not compared: ' + ', '.join(f"{s['sheet']} ({s['status']})" for s in rec['skipped']))
