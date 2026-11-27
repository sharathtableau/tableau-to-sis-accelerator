"""Tableau site browsing uses the shared PAT transport and REST identities."""
import hashlib
import streamlit as st
import tableau_server as TS
from v3_local import prepare_cloud, prepare_cloud_workbook


def render_cloud_intake():
    mode = st.radio('Cloud connection', ['Browse site', 'Direct workbook or view link'], horizontal=True, key='v3-cloud-mode')
    label = 'Tableau site URL' if mode == 'Browse site' else 'Tableau Cloud workbook or view URL'
    url = st.text_input(label, key='v3-cloud-url-' + mode).strip()
    st.caption('Uses the configured Tableau credentials. Data and visual parity remain unverified.')
    scope = (mode, url)
    if st.session_state.get('v3_cloud_scope') != scope:
        for key in ('v3_cloud_browse', 'v3_cloud_result', 'v3_export'):
            st.session_state.pop(key, None)
        st.session_state['v3_cloud_scope'] = scope
    if mode == 'Direct workbook or view link':
        if st.button('Pull and assess workbook', type='primary', disabled=not url):
            st.session_state.pop('v3_cloud_result', None)
            st.session_state.pop('v3_export', None)
            try:
                with st.spinner('Pulling workbook and checking contracts…'):
                    st.session_state['v3_cloud_result'] = prepare_cloud(url)
            except Exception as exc:
                st.error(f'Cloud intake blocked: {type(exc).__name__}: {exc}')
        return st.session_state.get('v3_cloud_result')
    st.caption('Connect to your site, then choose a project and workbook.')
    if st.button('Connect to Tableau', type='primary', disabled=not url):
        for key in ('v3_cloud_browse', 'v3_cloud_result', 'v3_export'):
            st.session_state.pop(key, None)
        st.session_state['v3_cloud_generation'] = st.session_state.get('v3_cloud_generation', 0) + 1
        try:
            with st.spinner('Connecting to Tableau and listing workbooks…'):
                location = TS.parse_site_url(url)
                contents = TS.list_site_contents(**location)
                st.session_state['v3_cloud_browse'] = dict(location, **contents)
        except Exception as exc:
            st.error(f'Tableau connection failed: {type(exc).__name__}: {exc}')
    browse = st.session_state.get('v3_cloud_browse')
    if browse is None:
        return None
    books = {w['id']: w for w in browse['workbooks'] if w.get('id')}
    st.success(f"Connected to Tableau. {len(books)} workbooks available.")
    if not books:
        st.info('No workbooks are visible to the configured Tableau credentials on this site.')
        return None
    projects = {w['project_id']: w.get('project_name') or 'Unnamed project'
                for w in books.values() if w.get('project_id')}
    widget_scope = hashlib.sha256(repr((scope, st.session_state.get('v3_cloud_generation'))).encode()).hexdigest()[:16]
    project = st.selectbox('Project', [None] + sorted(projects, key=lambda p: (projects[p], p)),
                           format_func=lambda p: 'All projects' if p is None else f'{projects[p]} ({p})',
                           key='v3-project-' + widget_scope)
    choices = sorted((i for i, w in books.items() if project is None or w.get('project_id') == project),
                     key=lambda i: (books[i].get('name') or '', i))
    selected = st.selectbox('Workbook', choices, index=None,
                            format_func=lambda i: f"{books[i].get('name') or 'Unnamed workbook'} — {books[i].get('project_name') or 'No project'} ({i})",
                            key='v3-workbook-' + widget_scope + str(project))
    selection = (scope, project, selected)
    if st.session_state.get('v3_cloud_selection') != selection:
        st.session_state.pop('v3_cloud_result', None)
        st.session_state.pop('v3_export', None)
        st.session_state['v3_cloud_selection'] = selection
    if st.button('Fetch and assess selected workbook', type='primary', disabled=selected is None):
        st.session_state.pop('v3_cloud_result', None)
        st.session_state.pop('v3_export', None)
        try:
            with st.spinner('Pulling workbook and checking contracts…'):
                st.session_state['v3_cloud_result'] = prepare_cloud_workbook(
                    browse['server_url'], browse['site_content_url'], selected, books[selected].get('name'))
        except Exception as exc:
            st.error(f'Cloud intake blocked: {type(exc).__name__}: {exc}')
    return st.session_state.get('v3_cloud_result')
