"""SL06 (milestone M7): validation across filter states, not only Tableau's default state.

    plan_states   code picks a few quick-filter states per dashboard (categorical filters, the most frequent members,
                  deterministic) -> a plan the Tableau capture script reads
    capture       tools/tableau_sheet_capture.js captureStates(): Embedding API applyFilterAsync per state on the
                  worksheets that carry the filter, summary data of every sheet, original selection restored
    state_part    the app side of one state: the same clause the engine's own dashboard control builds (build_where),
                  so the dashboard part governs the column and the sheet's saved selection steps aside, as in Tableau
    prove_states  v3_exact_loop.prove for every captured sheet in every state; a sheet is VERIFIED_ALL_STATES only
                  when every state (default included) is Verified; a missing capture is NOT_CAPTURED, never equal

Read-only queries only; no Cortex; error text never leaves this module (codes only).
"""
import hashlib
import json

PLAN_SCHEMA = 'v3-filter-states-plan/1'
CAPTURE_SCHEMA = 'tableau-sheet-capture-states/1'
MAX_FILTERS = 3
VALUES_PER_FILTER = 2


def _q1(text):
    return str(text).replace("'", "''")


def _resolve(dash, f, engine):
    """(table, expression) for one placed categorical filter, resolved exactly as engine.build_where does."""
    by_name = {s.get('name'): s for s in dash.get('sheets') or []}
    source = by_name.get(f.get('scope_sheet')) or next(
        (s for s in dash.get('sheets') or [] if any(x.get('caption') == f.get('caption') for x in s.get('filters') or [])),
        None)
    if source is None and not f.get('datasource_id') and 'field_scopes' in (engine.IR or {}):
        return None, None                                   # E02: never the built-in default table (V3 scoped)
    T = engine.tbl(source) if source else engine.ORDERS
    scope = dict(source or {})
    if f.get('datasource_id'):
        scope['datasource_id'] = f['datasource_id']
        declared = (engine.IR.get('field_scopes') or {}).get(f['datasource_id'])
        if not declared:
            return None, None
        T = engine.tbl({'datasource': declared['caption']})      # DM2: never a default table
    if f.get('field_id'):
        scope['field_bindings'] = {f['caption']: f['field_id']}
    with engine.field_scope(scope):
        return T, engine.rdim(f['caption'])


def _categorical(dash):
    return [f for f in dash.get('filters') or [] if f.get('kind') == 'categorical'][:MAX_FILTERS]


def _state_id(field, values):
    return 's' + hashlib.sha256(json.dumps([field, values]).encode('utf-8')).hexdigest()[:10]


def plan_states(dash, engine, values_per_filter=VALUES_PER_FILTER):
    """{'schema', 'dashboard', 'states': [{'id', 'field', 'values': [one member]}]} from the bound data: for each of
    the first MAX_FILTERS categorical quick filters, its most frequent members (ties by value). Text members only."""
    states = []
    for f in _categorical(dash):
        try:
            T, expr = _resolve(dash, f, engine)
            if not T:
                continue
            df = engine.q(f'SELECT {expr} AS V, COUNT(*) AS N FROM {T} WHERE {expr} IS NOT NULL '
                          f'GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {int(values_per_filter)}')
        except Exception:
            continue
        for v in list(df.iloc[:, 0]) if len(df) else []:
            if isinstance(v, str):
                states.append({'id': _state_id(f['caption'], [v]), 'field': f['caption'], 'values': [v]})
    return {'schema': PLAN_SCHEMA, 'dashboard': dash.get('name'), 'states': states}


def state_part(dash, state, engine):
    """The engine's dashboard-filter part for one state (categorical, one member), or None when the dashboard has no
    such quick filter."""
    f = next((x for x in _categorical(dash) if x.get('caption') == state.get('field')), None)
    if f is None or not state.get('values'):
        return None
    T, expr = _resolve(dash, f, engine)
    if not T:
        return None
    vals = [str(v) for v in state['values']]
    clause = f"{expr} = '{_q1(vals[0])}'" if len(vals) == 1 else \
        f"{expr} IN ({', '.join(chr(39) + _q1(v) + chr(39) for v in vals)})"
    return {'col': expr, 'clause': clause, 'caption': f['caption'], 'scope': f.get('scope_sheet'), 'table': T,
            'governs': True, 'datasource_id': f.get('datasource_id'), 'field_id': f.get('field_id'),
            'runtime_filter': {'kind': 'categorical', 'value': vals[0] if len(vals) == 1 else None}}


def load_capture(data):
    """Validate a states capture bundle (schema tableau-sheet-capture-states/1). Raises ValueError."""
    b = json.loads(data.decode('utf-8') if isinstance(data, (bytes, bytearray)) else data)
    if not isinstance(b, dict) or b.get('schema') != CAPTURE_SCHEMA or not isinstance(b.get('default'), dict) \
            or not isinstance(b.get('states'), list):
        raise ValueError(f'Not a filter-states capture (expected schema {CAPTURE_SCHEMA})')
    for s in b['states']:
        if not isinstance(s, dict) or not isinstance(s.get('id'), str) or not isinstance(s.get('field'), str) \
                or not isinstance(s.get('values'), list) or not isinstance((s.get('capture') or {}).get('sheets'), dict):
            raise ValueError('Malformed state in the capture')
    if not isinstance(b['default'].get('sheets'), dict):
        raise ValueError('The capture has no default state')
    return b


def _status(prover, sheet, entry, parts):
    try:
        return 'VERIFIED' if (prover(sheet, entry, parts) or {}).get('status') == 'Verified' else 'DIFFERS'
    except Exception as exc:                     # codes only
        return 'PROOF_FAILED:' + type(exc).__name__


def prove_states(ir, capture, prover, part_for):
    """Every captured sheet of the capture's dashboard, in the default state and every filter state.
    prover(sheet, entry, where_parts) -> {'status'}; part_for(dash, state) -> engine part or None."""
    dash = next((d for d in ir.get('dashboards') or [] if d.get('name') == capture.get('dashboard')), None)
    if dash is None:
        return {'status': 'DASHBOARD_NOT_IN_RUN', 'rows': [], 'summary': {}}
    parts = {s['id']: part_for(dash, s) for s in capture['states']}
    rows = []
    for sheet in dash.get('sheets') or []:
        if not sheet.get('worksheet_semantics'):
            continue
        default = (capture['default'].get('sheets') or {}).get(sheet.get('name'))
        if default is None:
            continue
        cells = {'default': _status(prover, sheet, default, None)}
        for s in capture['states']:
            entry = (s['capture'].get('sheets') or {}).get(sheet.get('name'))
            if entry is None:
                cells[s['id']] = 'NOT_CAPTURED'
            elif parts[s['id']] is None:
                cells[s['id']] = 'FILTER_NOT_IN_APP'
            else:
                cells[s['id']] = _status(prover, sheet, entry, [parts[s['id']]])
        checked = [v for v in cells.values() if v != 'NOT_CAPTURED']
        verified = sum(v == 'VERIFIED' for v in checked)
        all_ok = len(checked) == len(cells) and verified == len(checked)
        rows.append({'sheet': sheet.get('name'), 'cells': cells, 'states_checked': len(checked),
                     'states_verified': verified, 'verdict': 'VERIFIED_ALL_STATES' if all_ok else
                     ('DIFFERS_IN_A_STATE' if any(v != 'VERIFIED' for v in checked) else 'INCOMPLETE')})
    return {'status': 'OK', 'dashboard': dash.get('name'), 'rows': rows, 'states': [{k: s[k] for k in ('id', 'field', 'values')} for s in capture['states']],
            'summary': {'sheets': len(rows), 'verified_all_states': sum(r['verdict'] == 'VERIFIED_ALL_STATES' for r in rows),
                        'states': len(capture['states'])}}


def prove_run_states(run, capture, snowflake):
    """Production wiring: the run's IR and bindings, read-only, v3_exact_loop.prove per sheet and state."""
    import engine
    import parameter_actions
    import v3_evidence as EV
    import v3_exact_loop as X
    from v3_local import preview_context
    ir = run['result']['ir']
    with preview_context(ir, run.get('bindings') or {}, snowflake=snowflake), EV.offscreen():
        parameter_actions.reset(parameter_actions.context(engine))
        return prove_states(ir, capture,
                            lambda sheet, entry, parts: X.prove(sheet, engine, entry, where_parts=parts, ir=ir)[1],
                            lambda dash, state: state_part(dash, state, engine))


# ---------------------------------------------------------------- UI (Validation -> App vs Tableau values)
def render(run, st, snowflake, session_ok):
    st.markdown('**Filter states (SL06)** · the same comparison in several quick-filter states, not only the default')
    st.caption('Code picks the states (the most frequent members of up to three quick filters). Run '
               'captureStates(origin, viewPath, plan) from tools/tableau_sheet_capture.js in a signed-in Tableau '
               'page, then load its file here. A sheet is verified across states only when every state matches.')
    ir = run['result']['ir']
    dashboards = [d for d in ir.get('dashboards') or [] if _categorical(d)]
    if not dashboards:
        st.caption('No dashboard in this run has a categorical quick filter.')
        return
    pick = st.selectbox('Dashboard', [d['name'] for d in dashboards], key='sl06-dash')
    dash = next(d for d in dashboards if d['name'] == pick)
    if st.button('Plan the filter states', key='sl06-plan'):
        import engine
        from v3_local import preview_context
        try:
            with preview_context(ir, run.get('bindings') or {}, snowflake=snowflake):
                run.setdefault('sl06', {})['plan'] = plan_states(dash, engine)
        except Exception as exc:
            run.setdefault('sl06', {})['plan_error'] = 'PLAN_FAILED:' + type(exc).__name__
    rec = run.get('sl06') or {}
    if rec.get('plan_error'):
        st.warning('Could not plan states: ' + rec['plan_error'])
    plan = rec.get('plan')
    if plan and plan.get('dashboard') == pick:
        st.code(json.dumps(plan, indent=1), language='json')
        st.download_button('Download the plan for the capture script', json.dumps(plan), file_name='filter_states_plan.json',
                           mime='application/json', key='sl06-dl')
    up = st.file_uploader('Filter-states capture (tableau-sheet-capture-states/1)', type=['json'], key='sl06-up')
    if up is not None and st.button('Prove every sheet in every state', key='sl06-run', disabled=not session_ok):
        try:
            rec['result'] = prove_run_states(run, load_capture(up.getvalue()), snowflake)
        except ValueError:
            rec['result'] = {'status': 'NOT_A_STATES_CAPTURE', 'rows': []}
        except Exception as exc:
            rec['result'] = {'status': 'SL06_FAILED:' + type(exc).__name__, 'rows': []}
        run['sl06'] = rec
    res = rec.get('result')
    if not res:
        return
    if res.get('status') != 'OK':
        st.warning('Filter-state proof did not run: ' + res['status'])
        return
    s = res['summary']
    st.markdown(f"**{s['verified_all_states']} of {s['sheets']} sheet(s) verified in every state** "
                f"({s['states']} filter state(s) plus the default)")
    labels = {x['id']: f"{x['field']} = {', '.join(x['values'])}" for x in res['states']}
    st.dataframe([{'Sheet': r['sheet'], 'Verdict': r['verdict'], 'Default': r['cells'].get('default'),
                   **{labels[k]: v for k, v in r['cells'].items() if k != 'default'}} for r in res['rows']],
                 hide_index=True, use_container_width=True)
