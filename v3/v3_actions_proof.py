"""AC02 (E19, milestone M7): dashboard interactions proved against Tableau.

    plan_actions   one selection per READY dashboard filter action: the most frequent member of the linked field on the
                   bound data (a month number for month actions); the Tableau field / formatted value to select is
                   written next to it
    capture        tools/tableau_sheet_capture.js captureActions(): default -> for each action select the marks on the
                   source sheet (selectMarksByValueAsync), record every sheet, clear the selection -> after the last
                   clear record every sheet again (Reset)
    prove_actions  the app replays each selection through its own action state (dashboard_actions.state_key, applied
                   by dashboard_actions.predicates in both the renderer and the sheet plan) and every captured sheet is
                   proved with v3_exact_loop.prove; an action is VERIFIED only when every captured sheet is Verified;
                   'reset' = no selection against Tableau's state after clearing

Read-only; no Cortex; codes only. Parameter actions and non-READY actions are reported, never assumed.
"""
import calendar
import json

PLAN_SCHEMA = 'v3-actions-plan/1'
CAPTURE_SCHEMA = 'tableau-sheet-capture-actions/1'
MONTHS = [calendar.month_name[i] for i in range(1, 13)]


def _ready(dash):
    return [a for a in dash.get('actions') or [] if a.get('status') == 'READY']


def plan_actions(dash, engine):
    """{'schema', 'dashboard', 'actions': [{action_id, source, field, values, tableau_field, tableau_values}]}."""
    sheets = {s['name']: s for s in dash.get('sheets') or []}
    out = []
    for a in _ready(dash):
        source = sheets.get(a.get('source'))
        if source is None:
            continue
        try:
            with engine.field_scope(source):
                expr, T = engine.rdim(a['caption']), engine.tbl(source)
            if a.get('datepart') == 'month':
                df = engine.q(f"SELECT DATE_PART('month', {expr}) AS V, COUNT(*) AS N FROM {T} WHERE {expr} IS NOT NULL "
                              'GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 1')
                v = int(df.iloc[0, 0])
                out.append({'action_id': a['id'], 'source': a['source'], 'field': a['caption'], 'values': [v],
                            'tableau_field': f"MONTH({a['caption']})", 'tableau_values': [MONTHS[v - 1]]})
            elif a.get('datepart') is None:
                df = engine.q(f'SELECT {expr} AS V, COUNT(*) AS N FROM {T} WHERE {expr} IS NOT NULL '
                              'GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 1')
                v = df.iloc[0, 0]
                v = v.item() if hasattr(v, 'item') else v
                out.append({'action_id': a['id'], 'source': a['source'], 'field': a['caption'], 'values': [v],
                            'tableau_field': a['caption'], 'tableau_values': [str(v)]})
            # day-value actions: the app selects an ISO date; the Tableau mark value format is not planned yet
        except Exception:
            continue
    return {'schema': PLAN_SCHEMA, 'dashboard': dash.get('name'), 'actions': out}


def load_capture(data):
    b = json.loads(data.decode('utf-8') if isinstance(data, (bytes, bytearray)) else data)
    if not isinstance(b, dict) or b.get('schema') != CAPTURE_SCHEMA or not isinstance(b.get('actions'), list):
        raise ValueError(f'Not an actions capture (expected schema {CAPTURE_SCHEMA})')
    for part in ('default', 'reset'):
        if not isinstance((b.get(part) or {}).get('sheets'), dict):
            raise ValueError(f'The capture has no {part} state')
    for a in b['actions']:
        if not isinstance(a, dict) or not isinstance(a.get('action_id'), str) or not isinstance(a.get('values'), list) \
                or not isinstance((a.get('capture') or {}).get('sheets'), dict):
            raise ValueError('Malformed action in the capture')
    return b


def _prove_all(dash, sheets_cap, prover, selection):
    cells = {}
    for sheet in dash.get('sheets') or []:
        if not sheet.get('worksheet_semantics'):
            continue
        entry = sheets_cap.get(sheet['name'])
        if entry is None:
            continue
        try:
            cells[sheet['name']] = 'VERIFIED' if (prover(sheet, entry, selection) or {}).get('status') == 'Verified' \
                else 'DIFFERS'
        except Exception as exc:
            cells[sheet['name']] = 'PROOF_FAILED:' + type(exc).__name__
    return cells


def _verdict(cells):
    if not cells:
        return 'NOT_CAPTURED'
    return 'VERIFIED' if all(v == 'VERIFIED' for v in cells.values()) else \
        ('DIFFERS' if any(v == 'DIFFERS' for v in cells.values()) else 'PROOF_FAILED')


def prove_actions(ir, capture, prover):
    """prover(sheet, entry, selection {action_id: values}) -> {'status'}. Returns {'status', 'dashboard', 'actions':
    [{id, source, values, verdict, sheets}]} with one 'reset' row."""
    dash = next((d for d in ir.get('dashboards') or [] if d.get('name') == capture.get('dashboard')), None)
    if dash is None:
        return {'status': 'DASHBOARD_NOT_IN_RUN', 'actions': []}
    caught = {a['action_id']: a for a in capture['actions']}
    rows = []
    for a in dash.get('actions') or []:
        if a.get('status') != 'READY':
            rows.append({'id': a['id'], 'source': a.get('source'), 'values': [], 'verdict': 'NOT_SUPPORTED_IN_APP',
                         'sheets': {}, 'reason': a.get('reason')})
            continue
        c = caught.get(a['id'])
        if c is None or c.get('error'):
            rows.append({'id': a['id'], 'source': a.get('source'), 'values': [], 'verdict': 'NOT_CAPTURED', 'sheets': {}})
            continue
        cells = _prove_all(dash, c['capture']['sheets'], prover, {a['id']: c['values']})
        rows.append({'id': a['id'], 'source': a.get('source'), 'values': c['values'], 'verdict': _verdict(cells),
                     'sheets': cells})
    cells = _prove_all(dash, capture['reset']['sheets'], prover, {})
    rows.append({'id': 'reset', 'source': None, 'values': [], 'verdict': _verdict(cells), 'sheets': cells})
    return {'status': 'OK', 'dashboard': dash['name'], 'actions': rows}


def prove_run_actions(run, capture, snowflake):
    """Production wiring: the app's own action state per selection, v3_exact_loop.prove per sheet, read-only."""
    import dashboard_actions as DA
    import engine
    import parameter_actions
    import v3_evidence as EV
    import v3_exact_loop as X
    from v3_local import preview_context
    ir = run['result']['ir']
    dash = next((d for d in ir.get('dashboards') or [] if d.get('name') == capture.get('dashboard')), {})
    by_id = {a['id']: a for a in dash.get('actions') or []}

    def prover(sheet, entry, selection):
        keys = []
        try:
            for aid, values in selection.items():
                k = DA.state_key(engine.IR, by_id[aid])
                engine.st.session_state[k] = list(values)
                keys.append(k)
            return X.prove(sheet, engine, entry, ir=ir)[1]
        finally:
            for k in keys:
                engine.st.session_state.pop(k, None)
    with preview_context(ir, run.get('bindings') or {}, snowflake=snowflake), EV.offscreen():
        parameter_actions.reset(parameter_actions.context(engine))
        return prove_actions(ir, capture, prover)


# ---------------------------------------------------------------- UI (Validation -> App vs Tableau values)
def render(run, st, snowflake):
    st.markdown('**Interactions (AC02)** · each dashboard action replayed in the app and compared with Tableau')
    st.caption('Code picks one selection per filter action (the most frequent member). Run captureActions(origin, '
               'viewPath, plan) from tools/tableau_sheet_capture.js in a signed-in Tableau page and load its file '
               'here. Reset is checked too: after clearing, every sheet must match Tableau again.')
    ir = run['result']['ir']
    dashboards = [d for d in ir.get('dashboards') or [] if d.get('actions')]
    if not dashboards:
        st.caption('No dashboard action in this run.')
        return
    pick = st.selectbox('Dashboard', [d['name'] for d in dashboards], key='ac02-dash')
    dash = next(d for d in dashboards if d['name'] == pick)
    rec = run.setdefault('ac02_work', {})
    if st.button('Plan the action selections', key='ac02-plan'):
        import engine
        from v3_local import preview_context
        try:
            with preview_context(ir, run.get('bindings') or {}, snowflake=snowflake):
                rec['plan'] = plan_actions(dash, engine)
        except Exception as exc:
            rec['plan'] = {'error': 'PLAN_FAILED:' + type(exc).__name__}
    plan = rec.get('plan')
    if plan and plan.get('error'):
        st.warning('Could not plan: ' + plan['error'])
    elif plan and plan.get('dashboard') == pick:
        st.code(json.dumps(plan, indent=1, default=str), language='json')
        st.download_button('Download the plan for the capture script', json.dumps(plan, default=str),
                           file_name='actions_plan.json', mime='application/json', key='ac02-dl')
    up = st.file_uploader('Actions capture (tableau-sheet-capture-actions/1)', type=['json'], key='ac02-up')
    if up is not None and st.button('Replay every action and compare', key='ac02-run'):
        try:
            res = prove_run_actions(run, load_capture(up.getvalue()), snowflake)
        except ValueError:
            res = {'status': 'NOT_AN_ACTIONS_CAPTURE', 'actions': []}
        except Exception as exc:
            res = {'status': 'AC02_FAILED:' + type(exc).__name__, 'actions': []}
        if res.get('status') == 'OK':
            run.setdefault('ac02', {})[res['dashboard']] = res
        else:
            st.warning('Interaction proof did not run: ' + res['status'])
    res = (run.get('ac02') or {}).get(pick)
    if res:
        ok = sum(a['verdict'] == 'VERIFIED' for a in res['actions'])
        st.markdown(f"**{ok} of {len(res['actions'])} interaction check(s) match Tableau** (including Reset)")
        st.dataframe([{'Action': a['id'], 'Source sheet': a.get('source') or '—', 'Selected': ', '.join(map(str, a['values'])),
                       'Verdict': a['verdict'], 'Sheets': '; '.join(f'{k}: {v}' for k, v in a['sheets'].items())[:300]}
                      for a in res['actions']], hide_index=True, use_container_width=True)
