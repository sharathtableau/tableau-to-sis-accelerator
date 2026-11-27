"""Run: python -m streamlit run v3_app.py --server.port 8513"""
import hashlib
import json
from pathlib import Path
import tempfile

import streamlit as st
import engine
import backend
import findings
from ir_contract import VERSION
from v3_local import prepare_payload, prepare_cloud, preview_context
from coverage_v3 import summarize

st.set_page_config(page_title='Tableau accelerator V3', layout='wide')
def _dashboard_navigation(dashboards, digest, collapsed=False):
    # Keep selected-view execution: hidden tabs must not issue queries or
    # register controls for every dashboard on each rerun.
    with st.container(key='v3_dashboard_navigation'):
        st.markdown("<style>.st-key-v3_dashboard_navigation [role=radiogroup]{gap:18px;border-bottom:1px solid #e5e7eb;}"
                    ".st-key-v3_dashboard_navigation label[data-baseweb=radio]{padding:8px 0;border-bottom:2px solid transparent;}"
                    ".st-key-v3_dashboard_navigation label[data-baseweb=radio]>div:first-child{display:none;}"
                    ".st-key-v3_dashboard_navigation label:has(input:checked){border-bottom-color:#3366ff;}"
                    ".st-key-v3_dashboard_navigation label:has(input:checked) p{color:#3366ff;}"
                    ".st-key-v3_dashboard_navigation label:focus-within{outline:2px solid #a6bdff;outline-offset:2px;}"
                    "</style>",unsafe_allow_html=True)
        return st.radio('Dashboard', list(range(len(dashboards))),
                        format_func=lambda i: dashboards[i].get('title') or dashboards[i].get('name','Dashboard'),
                        horizontal=True,key='v3-dashboard-'+digest,label_visibility='collapsed' if collapsed else 'visible')

def _open_dashboard(result, bindings):
    intake = {key:st.session_state[key] for key in list(st.session_state)
              if key in ('v3-source-mode','v3-cloud-mode') or key.startswith(('v3-cloud-url-','v3-project-','v3-workbook-','sf-'))}
    # A non-widget snapshot survives cleanup of the hidden intake widgets.
    st.session_state['v3_focused_dashboard'] = {'result':result,'bindings':dict(bindings),'intake_widgets':intake}

def _back_to_workspace():
    focused = st.session_state.pop('v3_focused_dashboard', None)
    if focused:
        for key,value in focused.get('intake_widgets',{}).items():
            st.session_state[key] = value

focused = st.session_state.get('v3_focused_dashboard')
if focused:
    # Retain the explicit intake widgets across this separate dashboard view.
    # Normal parameters and actions use their existing workbook-scoped store.
    for key in list(st.session_state):
        if key in ('v3-source-mode','v3-cloud-mode') or key.startswith(('v3-cloud-url-','v3-project-','v3-workbook-','sf-')):
            st.session_state[key] = st.session_state[key]
    result=focused['result'];digest=result['digest']
    st.button('Back to migration workspace',on_click=_back_to_workspace)
    st.title('Tableau → Streamlit (Snowflake)')
    st.caption(result['source_name'])
    try:
        from ir_contract import require_valid_ir
        require_valid_ir(result['ir'])
        with preview_context(result['ir'],focused['bindings'],snowflake=backend._active_session() is not None):
            import parameter_actions
            st.button('Reset workbook controls',key='v3-reset-'+digest,on_click=parameter_actions.reset,args=(parameter_actions.context(engine),))
            dashboards=result['ir'].get('dashboards',[])
            if dashboards:
                selected=_dashboard_navigation(dashboards,digest,collapsed=True)
                engine.render_dashboard(dashboards[selected],presentation='native')
            engine._render_findings()
    except Exception as exc:
        st.error(f'Dashboard unavailable: {type(exc).__name__}: {exc}')
    st.stop()

st.title('Tableau accelerator · V3')
st.caption(('Snowflake deployment candidate' if backend._active_session() is not None else 'Local development candidate') + ' · ' + VERSION)
st.info('Upload a workbook to assess its structure and preview available data. '
        'Data and visual parity remain unverified until compared against Tableau.')
source_mode = st.radio('Workbook source', ['Upload', 'Tableau Cloud'], horizontal=True, key='v3-source-mode')
result = None
if source_mode == 'Upload':
    upload = st.file_uploader('Tableau workbook', type=['twb', 'twbx'])
    if upload is not None:
        raw = upload.getvalue()
        digest = hashlib.sha256(raw).hexdigest()
        if st.button('Assess workbook', type='primary'):
            st.session_state.pop('v3_result', None)
            try:
                with st.spinner('Parsing workbook and checking contracts…'):
                    st.session_state['v3_result'] = prepare_payload(raw, upload.name)
            except Exception as exc:
                st.error(f'Intake blocked: {type(exc).__name__}: {exc}')
        cached = st.session_state.get('v3_result')
        if cached and cached['digest'] == digest and cached.get('origin') == 'upload':
            result = cached
else:
    from v3_cloud_ui import render_cloud_intake
    result = render_cloud_intake()
if result:
    digest = result['digest']
    assessment = result['assessment']
    st.subheader(f"Assessment: {assessment['state'].replace('_', ' ')}")
    st.write(result['source_name'])
    review = result['ir'].get('ai_migration_review')
    if review:
        with st.expander('Cortex migration review', expanded=True):
            st.write(review.get('title') or review['status'].replace('_', ' ').title())
            st.write(review['reason'])
            st.caption('AI recommendation based on structural evidence. It does not approve execution or establish Tableau parity.')
            st.caption('Actual Cortex calls this intake: ' + str(result['ir'].get('ai_usage', {}).get('attempted_calls', 0)))
    action_rows = [{'dashboard': d['name'], 'source': a['source'], 'status': a['status'],
                    'reason': a.get('reason', 'Selection and clear adapter available; interaction acceptance pending')}
                   for d in result['ir'].get('dashboards', []) for a in d.get('actions', [])]
    action_rows += [{'dashboard':d['name'],'source':sheet['name'],'status':a['status'],'reason':a.get('reason','Authored parameter assignment available; interaction acceptance pending')} for d in result['ir'].get('dashboards',[]) for sheet in d.get('sheets',[]) for a in sheet.get('parameter_actions',[])]
    if action_rows:
        with st.expander('Dashboard interactions'):
            st.dataframe(action_rows, hide_index=True, use_container_width=True)
            st.caption('Use the selection controls below layered charts to filter the dashboard; remove all values to clear. Single-layer charts also support click, Shift-click and double-click.')

    if result['ir'].get('ai_assistance'):
        with st.expander('AI substitutions and retained refusals'):
            st.dataframe(result['ir']['ai_assistance'], hide_index=True, use_container_width=True)
            st.caption('Validated substitutions are automatic when Cortex is available. Visual parity remains unverified.')
    if result['ir'].get('ai_include_plans'):
        with st.expander('AI calculation planning'):
            st.caption('INCLUDE plans are checked against the source formula. Execution and worksheet acceptance are still pending.')
            st.dataframe([{k: row.get(k) for k in ('dashboard', 'sheet', 'status', 'reason')}
                          for row in result['ir']['ai_include_plans']], hide_index=True, use_container_width=True)
    st.dataframe(assessment['sheets'], hide_index=True, use_container_width=True)
    with st.expander('Coverage and verification gaps'):
        coverage = summarize([dict(assessment, file=result['source_name'])])
        st.write('Reported sheet states:', coverage['states'])
        st.caption('These are structural assessment results. Numeric, visual, interaction, '
                   'Snowflake runtime and held-out workbook verification are not evaluated here.')
        if coverage['families']:
            st.dataframe([{k: row[k] for k in ('family', 'sheet_occurrences')}
                          for row in coverage['families']], hide_index=True, use_container_width=True)
        st.caption('A sheet can appear in several families. Counts do not predict how many '
                   'sheets one change will fix or establish demo readiness.')
        st.download_button('Download coverage diagnostics', json.dumps(coverage, indent=2),
                           file_name='v3_coverage.json', mime='application/json')
    for issue in assessment['issues']:
        st.error(f"{issue['path']}: {issue['message']}")
    for error in result['intake_errors']:
        st.warning(error)
    if assessment['untranslated_calculations']:
        st.warning('Untranslated calculations: ' + ', '.join(assessment['untranslated_calculations']))
    st.download_button('Download assessment', json.dumps(assessment, indent=2),
                       file_name='v3_assessment.json', mime='application/json')
    runtime_sf = backend._active_session() is not None
    st.subheader('Snowflake data' if runtime_sf else 'Local data')
    bindings = dict(result['bindings'])
    if result.get('source_reports'):
        with st.expander('Datasource resolution evidence'):
            st.dataframe(result['source_reports'], hide_index=True, use_container_width=True)
    for cap, path in bindings.items():
        if runtime_sf:
            if isinstance(path, dict):
                st.caption(cap + ': authored relationship model bound as a read-only query. Fact grain verified; Tableau equivalence remains unverified.')
                continue
            key = digest + hashlib.sha256(cap.encode()).hexdigest()[:16]
            binding_key = 'sf-' + key
            selected = (st.text_input(f'Snowflake table/view for {cap}', value=None if binding_key in st.session_state else path or '', placeholder='DATABASE.SCHEMA.OBJECT', key=binding_key) or '').strip()
            bindings[cap] = path if selected == path else None
            if path and selected == path:
                st.caption('Automatically bound from authored source metadata; live data and Tableau parity remain unverified.')
            elif selected:
                try:
                    from v3_source_binding import validate_table
                    scopes = [d for d, scope in result['ir']['field_scopes'].items() if scope['caption'] == cap]
                    if len(scopes) != 1:
                        raise ValueError('Ambiguous datasource identity')
                    fields = [f for f in result['ir']['fields'].values() if f['datasource_id'] == scopes[0]]
                    bindings[cap] = validate_table(backend._active_session(), selected, fields)
                    st.caption('Explicit binding: physical schema and read access verified; source equivalence remains unverified.')
                except Exception as exc:
                    st.warning(f'Binding unavailable for {cap}: {exc}')
        elif path and Path(path).is_file():
            st.caption(f'{cap}: bundled data available')
        else:
            key = digest + hashlib.sha256(cap.encode()).hexdigest()[:16]
            data = st.file_uploader(f'Data for {cap}', type=['csv', 'xls', 'xlsx'], key=key)
            if data:
                target = Path(result['folder']) / (key + Path(data.name).suffix.lower())
                target.write_bytes(data.getvalue())
                bindings[cap] = str(target)
    missing = [cap for cap, path in bindings.items() if not path or (not runtime_sf and not Path(path).is_file())]
    if missing:
        st.warning('No data binding for: ' + ', '.join(missing) + '. Those sheets cannot be verified.')
    if runtime_sf:
        st.subheader('Standalone migration candidate')
        st.caption('Create a package using this validated workbook and its explicit bindings. Export is not deployment or migration acceptance.')
        if st.button('Build standalone candidate', disabled=bool(missing)):
            try:
                from v3_package import workbook_bundle
                data, build = workbook_bundle(result['ir'], bindings)
                st.session_state['v3_export'] = {'digest':digest,'bindings':dict(bindings),'data':data,'build':build}
            except Exception as exc:
                st.session_state.pop('v3_export',None)
                st.error(f'Candidate export blocked: {type(exc).__name__}: {exc}')
        exported=st.session_state.get('v3_export')
        if exported and exported['digest']==digest and exported['bindings']==bindings:
            st.download_button('Download standalone candidate',exported['data'],
                               file_name='v3_workbook_'+exported['build'][:12]+'.zip',mime='application/zip')
            st.caption('Candidate build: '+exported['build'])
    st.button('Open dashboard', type='primary', disabled=bool(missing) or bool(assessment['issues']), on_click=_open_dashboard, args=(result,bindings))
    if st.checkbox('Show available visual previews', value=True, key='preview-' + digest):
        try:
            with preview_context(result['ir'], bindings, snowflake=runtime_sf):
                import parameter_actions
                st.button('Reset workbook controls', key='v3-reset-'+digest, on_click=parameter_actions.reset, args=(parameter_actions.context(engine),))
                engine._render_param_controls()
                dashboards = result['ir'].get('dashboards', [])
                if dashboards:
                    selected = _dashboard_navigation(dashboards,digest)
                    engine.render_dashboard(dashboards[selected], presentation="native")
                observed = findings.all_findings()
                if observed:
                    st.subheader('Preview findings')
                    st.dataframe(observed, use_container_width=True, hide_index=True)
        except Exception as exc:
            st.error(f'Preview unavailable: {type(exc).__name__}: {exc}')
