"""Estate catalogue page (release 20b, SL01): harvest a Tableau project and browse what it contains.

Read-only on Tableau. Builds no apps. Writes only the catalogue tables (INSERT). Errors show the
exception class only; Cortex service text never reaches the page.
"""
import streamlit as st

import v3_catalog as C
from v3_console_style import render_header

K = 'v3cat-'
LAPTOP = ('Set-Location "C:\\Users\\SharathKumarKammari\\Downloads\\Tableau to SiS_Cowork - Cortex\\'
          'tableau_accelerator_v3_cortex"; python tools\\harvest_catalogue.py --site "{site}" --project "{project}"')


def _connect(url):
    import tableau_server as TS
    location = TS.parse_site_url(url)
    name, secret = TS._resolve_pat(None, None)
    client = TS.TableauRestClient(location['server_url'])
    client.sign_in_with_pat(name, secret, location['site_content_url'])
    return client, location


def _harvest_panel(sess, default_site):
    url = (st.text_input('Tableau site URL', value=default_site, key=K + 'site') or '').strip()
    st.caption('Uses the configured Tableau credentials (Snowflake secret). Read-only: lists projects, downloads '
               'each workbook without its data, reads view counts.')
    if st.button('Connect to Tableau', disabled=not url, key=K + 'connect'):
        st.session_state.pop(K + 'browse', None)
        try:
            with st.spinner('Connecting to Tableau and listing projects…'):
                client, location = _connect(url)
                try:
                    st.session_state[K + 'browse'] = dict(location, url=url, projects=client.list_projects(),
                                                          workbooks=client.list_workbooks())
                finally:
                    client.sign_out()
        except Exception as exc:
            st.error(f'Tableau connection failed ({type(exc).__name__}).')
    browse = st.session_state.get(K + 'browse')
    if not browse or browse.get('url') != url:
        return
    projects = browse['projects']
    _ids, path = C.project_tree(projects, None, include_sub=False)
    choices = sorted((p['id'] for p in projects), key=lambda i: path(i).casefold())
    pid = st.selectbox('Project', choices, index=None, format_func=path, key=K + 'project')
    sub = st.checkbox('Include sub-projects', value=True, key=K + 'sub')
    cortex = st.checkbox('Describe metrics with Cortex (names, descriptions, intent; checked by code)', value=True,
                         key=K + 'cortex')
    if not pid:
        return
    ids, path = C.project_tree(projects, pid, sub)
    books = [w for w in browse['workbooks'] if w.get('project_id') in ids]
    st.write(f'**{len(books)} workbook(s)** in {path(pid)}' + (' and its sub-projects' if sub else ''))
    if len(books) > C.APP_MAX_WORKBOOKS:
        st.warning(f'More than {C.APP_MAX_WORKBOOKS} workbooks: run this harvest from a laptop (same code, same '
                   'tables, no time limit). In PowerShell:')
        st.code(LAPTOP.format(site=url, project=path(pid)), language='powershell')
        return
    st.caption('Keep this tab open while it runs. Each workbook is saved as soon as it is read; if the harvest '
               'stops, start it again and unchanged workbooks are skipped.')
    if st.button('Start harvest', type='primary', disabled=not books, key=K + 'start'):
        _run(sess, browse, books, path, path(pid), cortex)


def _run(sess, browse, books, path, scope, cortex):
    import v3_store
    who, _src = v3_store.viewer()
    harvest = C.Harvest(sess, 'app', scope, site=browse['url'], who=who)
    bar = st.progress(0.0, text='Starting…')
    log = st.container()
    try:
        client, location = _connect(browse['url'])
    except Exception as exc:
        st.error(f'Tableau sign-in failed ({type(exc).__name__}).')
        return
    try:
        harvest.start(len(books))
        for i, w in enumerate(sorted(books, key=lambda b: (path(b.get('project_id')), b.get('name') or ''))):
            bar.progress(i / len(books), text=f'{i + 1} of {len(books)}: {w.get("name")}')
            row = C.harvest_tableau_workbook(harvest, client, w, path, location['server_url'],
                                             location['site_content_url'])
            mark = {'harvested': '✅', 'unchanged': '↺', 'failed': '⚠️'}.get(row['STATUS'], '•')
            log.caption(f"{mark} {w.get('name')}: {row['STATUS']}"
                        + (f" ({row.get('ERROR_CLASS')})" if row.get('ERROR_CLASS') else ''))
    finally:
        client.sign_out()
    bar.progress(1.0, text='Describing metrics with Cortex…' if cortex else 'Finishing…')
    try:
        counts = harvest.finish(describe_metrics=cortex)
    except Exception as exc:
        st.error(f'Harvest summary not saved ({type(exc).__name__}).')
        return
    bar.empty()
    st.success(f"Harvest finished: {counts['harvested']} read, {counts['unchanged']} unchanged, "
               f"{counts['failed']} failed; {harvest.described} metric description(s) passed code's check.")
    st.session_state[K + 'scope'] = scope


def _describe_more(sess, scope):
    try:
        left = len(C.pending(sess, scope))
    except Exception:
        return
    if not left:
        return
    st.caption(f'{left} metric(s) in this scope have no checked description yet.')
    if st.button('Describe remaining metrics with Cortex', key=K + 'describe'):
        import v3_store
        harvest = C.Harvest(sess, 'app', scope, who=v3_store.viewer()[0])
        with st.spinner('Cortex is describing metrics…'):
            try:
                harvest.describe_only()
                st.success(f'{harvest.described} description(s) passed code\'s check.')
            except Exception as exc:
                st.error(f'Descriptions not written ({type(exc).__name__}).')


def _tiles(cols, values):
    for col, (label, value) in zip(cols, values):
        col.metric(label, value)


def _results(sess, go_migrate):
    try:
        names = C.scopes(sess)
    except Exception as exc:
        st.error(f'Catalogue could not be read ({type(exc).__name__}).')
        return
    if not names:
        st.info('Nothing catalogued yet. Harvest a project above.')
        return
    want = st.session_state.get(K + 'scope')
    scope = st.selectbox('Scope (the project that was harvested)', names,
                         index=names.index(want) if want in names else 0, key=K + 'view-scope')
    try:
        wb = C.frame(sess, 'V_CAT_WORKBOOKS', scope)
        ds = C.frame(sess, 'V_CAT_DATASOURCES', scope)
        mt = C.frame(sess, 'V_CAT_METRICS', scope)
        us = C.frame(sess, 'V_CAT_USAGE', scope)
        calcs = C.frame(sess, 'V_CAT_CALCULATIONS', scope)
        desc = C.descriptions(sess)
    except Exception as exc:
        st.error(f'Catalogue could not be read ({type(exc).__name__}).')
        return
    table = C.metric_table(mt, us, desc)
    variants = C.name_variants(mt)
    routed = ds['ROUTE'].isin(['bind_snowflake', 'bind_model', 'load', 'laptop']).sum() if not ds.empty else 0
    _tiles(st.columns(4), [('Workbooks', len(wb)), ('Tableau views', int(wb['TOTAL_VIEWS'].fillna(0).sum()) if not wb.empty else 0),
                           ('Datasources with a route', f'{routed} of {len(ds)}'), ('Calculations', len(calcs))])
    _tiles(st.columns(4), [('Distinct metrics', len(table)), ('Shared by 2+ workbooks', int((table['Workbooks'] > 1).sum())),
                           ('Same name, different formula', len(variants)),
                           ('Described by Cortex', int(table['Business name'].notna().sum()) if 'Business name' in table else 0)])
    st.caption('Formulas, fingerprints, routes and lineage come from the workbooks. Business names and descriptions '
               'are written by Cortex and shown only when they pass code\'s check. Routes are what a migration run '
               'does for that datasource; the run still checks Snowflake read-only.')
    _describe_more(sess, scope)
    tabs = st.tabs(['Metrics', 'Datasources and routes', 'Workbooks', 'Same name, different formula', 'Harvests',
                    'Rationalisation'])
    with tabs[0]:
        q = (st.text_input('Search metrics', key=K + 'q') or '').strip().casefold()
        shown = table
        if q:
            hay = table[[c for c in ('Name', 'Names', 'Business name', 'Description', 'Example formula') if c in table]]
            shown = table[hay.astype(str).apply(lambda r: q in ' '.join(r).casefold(), axis=1)]
        cols = [c for c in ('Name', 'Business name', 'Intent', 'Kind', 'Workbooks', 'Sheets', 'Names', 'Example formula',
                            'Description', 'Datasources', 'METRIC_FP') if c in shown]
        st.dataframe(shown[cols], use_container_width=True, hide_index=True)
        pick = st.selectbox('Where is this metric used?', list(shown['METRIC_FP']), index=None,
                            format_func=lambda f: str(table.loc[table['METRIC_FP'] == f, 'Name'].iloc[0]) + '  ·  ' + f,
                            key=K + 'pick')
        if pick:
            use = us[us['METRIC_FP'] == pick]
            st.dataframe(use[['WORKBOOK_NAME', 'WORKSHEET', 'DASHBOARDS', 'CAPTION', 'SHELF', 'AGGREGATION']],
                         use_container_width=True, hide_index=True)
            forms = mt[mt['METRIC_FP'] == pick][['WORKBOOK_NAME', 'NAME', 'EXPRESSION', 'DATASOURCE']].drop_duplicates()
            st.caption('Definitions (same fingerprint)')
            st.dataframe(forms, use_container_width=True, hide_index=True)
    with tabs[1]:
        if ds.empty:
            st.info('No datasources.')
        else:
            counts = ds.groupby('ROUTE').size().rename('Datasources').reset_index()
            counts['Meaning'] = counts['ROUTE'].map(C.ROUTES)
            st.dataframe(counts, use_container_width=True, hide_index=True)
            st.dataframe(ds[['WORKBOOK_NAME', 'DATASOURCE', 'SHAPE', 'ROUTE', 'ROUTE_REASON', 'CONNECTIONS', 'TABLES',
                             'FIELDS', 'CALCULATIONS', 'SOURCE_KEY']], use_container_width=True, hide_index=True)
    with tabs[2]:
        st.dataframe(wb[['WORKBOOK_NAME', 'PROJECT', 'TOTAL_VIEWS', 'DATASOURCES', 'WORKSHEETS', 'DASHBOARDS',
                         'CALCULATIONS', 'METRICS', 'HARVESTED_AT', 'LAST_SEEN']].sort_values(
                             'TOTAL_VIEWS', ascending=False, na_position='last'),
                     use_container_width=True, hide_index=True)
        cloud = wb[wb['ORIGIN'] == 'tableau-cloud']
        if not cloud.empty:
            key = st.selectbox('Migrate a workbook', list(cloud['WORKBOOK_KEY']), index=None,
                               format_func=lambda k: str(cloud.loc[cloud['WORKBOOK_KEY'] == k, 'WORKBOOK_NAME'].iloc[0]),
                               key=K + 'migrate')
            if key and st.button('Open in New migration', key=K + 'go'):
                row = cloud[cloud['WORKBOOK_KEY'] == key].iloc[0]
                ref = C.as_dict(row['SOURCE_REF'])
                go_migrate({'origin': 'tableau-cloud', 'name': row['WORKBOOK_NAME'],
                            'workbook_id': ref.get('workbook_id'), 'server_url': ref.get('server_url'),
                            'site_content_url': ref.get('site_content_url'), 'content_url': ref.get('content_url')})
    with tabs[3]:
        st.caption('Calculations with the same name and a different formula. Release 22 runs them to say whether '
                   'they give the same numbers; nothing here is proven yet.')
        st.dataframe(variants, use_container_width=True, hide_index=True)
    with tabs[4]:
        try:
            st.dataframe(C.harvests(sess, scope), use_container_width=True, hide_index=True)
            failed = C.failures(sess, scope)
            if not failed.empty:
                st.caption('Workbooks whose latest harvest failed')
                st.dataframe(failed, use_container_width=True, hide_index=True)
        except Exception as exc:
            st.error(f'Harvest history could not be read ({type(exc).__name__}).')
    with tabs[5]:
        import v3_rationalise_ui
        v3_rationalise_ui.render(sess, scope)


def render(sess, go_migrate, default_site):
    render_header('Estate catalogue', 'Every workbook in a Tableau project: datasources, calculations and metrics, '
                  'without building apps.', 'Read-only on Tableau', 'ok')
    if sess is None:
        st.info('The estate catalogue needs the hosted Snowflake session. On a laptop, use '
                'tools\\harvest_catalogue.py (same code, same tables).')
        return
    if not C.ready(sess):
        st.warning('The catalogue tables are not set up yet. Re-run sql/en01_evidence_store.sql in Snowsight '
                   '(Run All, role WBR_OWNER); it adds the CAT_ tables and views and changes nothing else.')
        return
    with st.expander('Harvest a Tableau project', expanded=not st.session_state.get(K + 'scope')):
        _harvest_panel(sess, default_site)
    _results(sess, go_migrate)
