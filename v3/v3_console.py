"""Migration console: the original accelerator's workflow and look, on the V3 engine.

Run locally:  python -m streamlit run v3_console.py --server.port 8514
Hosted entry: v3_sis_app.py.

Pages follow the original pipeline_app.py (Overview, New migration with a five-stage
run, Inventory, Preview, Validation). Every stage calls V3 functions only:
  Discovery   workbook retrieval and datasource inventory (read-only)
  Parsing     v3_local.prepare_payload: scoped IR, assessment, source resolution, Cortex review
  Data model  verified bindings and authored relationship plans already checked at intake
  App build   v3_package.workbook_bundle (a candidate package; not a deployment)
  Validation  structural support/refusal summary; parity stays NOT_VERIFIED
A run never loads or changes workbook data. When the EN01 evidence store is set up
(sql/en01_evidence_store.sql), the run appends its own evidence there - stages, Cortex calls,
proposals and code's rulings, proofs, fixes, approvals, deployments and a saved state used to
reopen it after a refresh (v3_store.py). Deploy (an explicit, reviewed button) creates one Streamlit
app per workbook from the validated package (v3_deploy.py). Validation adds sheet-level
checks, Tableau value and image comparison and a downloadable migration report
(v3_evidence.py). Every console Cortex call is logged by workflow step (v3_ai.py) and shown
on the Cortex AI page; Cortex output is advisory and never executed.
The previous single-page workbench (v3_app.py) stays available as a fallback.
"""
from collections import Counter
import hashlib
import io
import json
import os
import re
from pathlib import Path
import runpy
import xml.etree.ElementTree as ET
import zipfile

import streamlit as st

import backend
import engine
import tableau_server as TS
import v3_store
from coverage_v3 import summarize
from ir_contract import VERSION
from v3_local import prepare_payload, preview_context
from v3_console_style import (STAGES, PAGES, esc, inject_styles, render_header,
                              render_live, render_stepper)

st.set_page_config(page_title='Tableau to SiS accelerator', layout='wide')

RUN, SOURCE, BROWSE = 'v3c_run', 'v3c_source', 'v3c_browse'
INTAKE = 'v3c-in-'          # intake widget keys kept alive while other pages show
CONVERTIBLE = ('UNVERIFIED', 'PARTIAL')
REFUSED = ('UNSUPPORTED', 'INVALID')
DATA_SUFFIXES = ('.csv', '.xls', '.xlsx', '.hyper', '.tde')
# Same pre-filled site as the original accelerator's Tableau intake.
DEFAULT_SITE_URL = os.environ.get('V3_TABLEAU_SITE_URL',
                                  'https://prod-useast-b.online.tableau.com/#/site/b360bi')


def _runtime_sf():
    return backend._active_session() is not None


def _keep_intake_widgets():
    # Streamlit drops state for widgets that are not rendered on a rerun. Re-assigning
    # keeps the Cloud site/project/workbook choices when the user visits other pages.
    for key in list(st.session_state):
        if isinstance(key, str) and key.startswith(INTAKE):
            st.session_state[key] = st.session_state[key]


def _role_needed(page):
    """E10: the role this page needs that the viewer lacks (None = allowed). Resolved once per session."""
    import v3_roles
    if 'v3c_access' not in st.session_state:
        try:
            st.session_state['v3c_access'] = v3_roles.roles_for(_runtime_sf(), v3_store.viewer()[0])
        except Exception:
            st.session_state['v3c_access'] = {'mode': 'OPEN', 'roles': set()}
    return v3_roles.required(page, st.session_state['v3c_access'])


def _go(page):
    st.session_state['v3c_pending'] = page


def _reset():
    for key in (RUN, SOURCE):
        st.session_state.pop(key, None)
    _go('New migration')


# --------------------------------------------------------------------------- #
# Run model
# --------------------------------------------------------------------------- #
def _discover(raw, filename):
    """Read-only inventory of the workbook package and its datasources."""
    normalized = TS.normalize_workbook_download(filename, raw)
    import v3_intake
    v3_intake.check_archive(raw, normalized)          # E15: refused with a code before anything is read
    bundled = []
    if zipfile.is_zipfile(io.BytesIO(raw)):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = archive.namelist()
            twbs = [n for n in names if n.lower().endswith('.twb')]
            if len(twbs) != 1:
                raise ValueError('Workbook package must contain exactly one .twb file')
            xml = archive.read(twbs[0])
            bundled = [Path(n).name for n in names if n.lower().endswith(DATA_SUFFIXES)]
    else:
        xml = raw
    import tableau_parser
    root = tableau_parser.parse_xml(xml)
    rows = []
    for ds in root.findall('./datasources/datasource'):
        if ds.get('name') == 'Parameters':
            continue
        classes = sorted({c.get('class') for c in ds.iter('connection')
                          if c.get('class') and c.get('class') != 'federated'})
        rows.append({'Datasource': ds.get('caption') or ds.get('name'),
                     'Connections': ', '.join(classes) or '—'})
    return {'filename': normalized, 'bytes': len(raw), 'datasources': rows,
            'bundled_files': bundled, 'dashboards': len(root.findall('./dashboards/dashboard'))}


def _tables(plan):
    return [t.get('table', '') for t in (plan.get('tables') or {}).values()]


def _bind(result):
    """Binding status per datasource from the verified intake result."""
    sf = _runtime_sf()
    bindings, rows, missing = dict(result['bindings']), [], []
    for cap, path in bindings.items():
        if isinstance(path, dict):
            once = [t.get('table') for t in (path.get('tables') or {}).values() if t.get('distinct')]
            rows.append({'Datasource': cap, 'Binding': 'Authored relationship model',
                         'Snowflake objects': ', '.join(_tables(path)),
                         'Status': 'Verified: keys, coverage and fact grain' +
                                   (f"; {', '.join(once)} read as one row per key (its key repeats; every column "
                                    'is constant per key)' if once else '')})
        elif sf and isinstance(path, str) and path:
            rows.append({'Datasource': cap, 'Binding': 'Existing Snowflake object',
                         'Snowflake objects': path,
                         'Status': 'Matched from workbook metadata; schema and access checked'})
        elif not sf and path and Path(path).is_file():
            rows.append({'Datasource': cap, 'Binding': 'Bundled extract (local preview)',
                         'Snowflake objects': Path(path).name, 'Status': 'Local data'})
        else:
            missing.append(cap)
            rows.append({'Datasource': cap, 'Binding': '—', 'Snowflake objects': '—',
                         'Status': 'Needs explicit binding' + _binding_reason(result, cap)})
    return bindings, rows, missing


def _binding_reason(result, cap):
    """DM8: why the automatic binding did not pass, as the code-built reason (a code; the detail only when the check
    itself wrote it -- never a Snowflake or Cortex message)."""
    from v3_model_binding import reason_code
    report = next((r for r in result.get('source_reports') or [] if r.get('caption') == cap and r.get('reason')), None)
    if not report:
        return ''
    text = str(report['reason'])
    code = reason_code(ValueError(text))
    own = re.match(r'[A-Z][A-Z_]{3,}: (.{0,220})', text)
    if code == 'DATA_NOT_IN_SNOWFLAKE':
        return f" — {code}: the workbook's own data; load it below, or bind an existing table"
    return f' — {code}' + (f': {own.group(1)}' if own else '')


def _fields_for(result, cap):
    scopes = [d for d, s in result['ir']['field_scopes'].items() if s['caption'] == cap]
    if len(scopes) != 1:
        raise ValueError('Ambiguous datasource identity')
    return [f for f in result['ir']['fields'].values() if f['datasource_id'] == scopes[0]]


def _auto_bind(run):
    """File / extract datasources: reuse a table that already exists in Snowflake (read-only).

    Same naming and search as the original accelerator's onboarding (pipeline.resolve_existing_table:
    WBR_DB.PIPELINE_DEMO first, else a single unambiguous match in WBR_DB). A found table is used only
    after the same column/type and read-access check as an explicit binding. Nothing is written."""
    if not _runtime_sf() or not run.get('missing'):
        return []
    import pipeline
    from calc_translator import to_phys
    from v3_source_binding import validate_table
    session = backend._active_session()
    result, found = run['result'], []
    for cap in list(run['missing']):
        try:
            fqn, note = pipeline.resolve_existing_table(session, pipeline.LOAD_DB, pipeline.LOAD_SCHEMA,
                                                        to_phys(cap))
            if not fqn:
                continue
            fqn = fqn.replace('"', '')
            fields = _fields_for(result, cap)
            try:
                table, how = validate_table(session, fqn, fields), 'columns, types and read access verified'
            except ValueError as exact:
                # release 23: tables made by the original accelerator (and the laptop loader) use the engine's
                # column names (SALES_TARGET for 'Sales Target'); every workbook field must still be present
                # with a compatible type. The data is not compared.
                import v3_data_load as DL
                try:
                    table = validate_table(session, fqn, DL.engine_fields(
                        [f for f in fields if not f.get('is_calculation') and f.get('datatype') != 'table'
                         and not str(f.get('internal') or '').startswith(':')]))
                    how = 'every workbook field found under its engine column name; types and read access verified'
                except Exception as engine:
                    raise ValueError(f'{fqn}: exact names: {exact}. Engine names (e.g. SALES_TARGET): {engine}')
        except Exception as exc:
            run.setdefault('auto_bind_notes', {})[cap] = f'{type(exc).__name__}: {str(exc)[:200]}'
            continue
        run['bindings'][cap] = table
        run['missing'].remove(cap)
        found.append(cap)
        for row in run['binding_rows']:
            if row['Datasource'] == cap:
                row.update({'Binding': 'Existing Snowflake table (auto-detected)', 'Snowflake objects': table,
                            'Status': 'Found in Snowflake; ' + how})
    return found


def _workbook_root(run):
    """The workbook XML, re-read from the bytes kept for this session."""
    import tempfile
    import tableau_parser
    from tableau_server import normalize_workbook_download
    name = normalize_workbook_download(run['result'].get('source_name') or 'workbook.twbx', run['_raw'])
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / ('wb' + Path(name).suffix.lower())
        path.write_bytes(run['_raw'])
        return tableau_parser.load_twb_xml(str(path))


def _complete_binding(run, cap, table, label, status):
    """One datasource is bound: update the table, and build the app candidate when nothing is missing."""
    if isinstance(table, dict):
        run.setdefault('load_checks', {})[cap] = table.pop('checks', None)
    run['bindings'][cap] = table
    if cap in run['missing']:
        run['missing'].remove(cap)
    objects = ', '.join(_tables(table)) if isinstance(table, dict) else table
    for row in run.get('binding_rows') or []:
        if row['Datasource'] == cap:
            row.update({'Binding': label, 'Snowflake objects': objects, 'Status': status})
    if not run['missing'] and run.get('states', {}).get('Data model') == 'review':
        sv_status = None
        if _relationship_plans(run) or _single_tables(run):
            try:     # same as a run that binds everything itself: shared Semantic View created or reused
                sv_status, sv_detail = _auto_semantic(run)
                run['events'].insert(0, ('Semantic View', sv_detail or 'Not created',
                                         'done' if sv_status == 'done' else 'review'))
            except Exception as exc:
                run['events'].insert(0, ('Semantic View', f'Not created ({type(exc).__name__})', 'review'))
        run['states']['Data model'] = 'done' if sv_status in (None, 'done') else 'review'
        status_, detail, run['package'] = _build(run)
        run['states']['App build'] = status_
        run['events'].insert(0, ('Bindings completed', detail, 'done'))


def _auto_loaded(run):
    """Release 21a: bind tables loaded earlier from exactly this workbook's data (fingerprint match, read-only),
    and prepare a load plan for every datasource still missing. Nothing is written here."""
    if not _runtime_sf() or not run.get('missing') or not run.get('_raw'):
        return []
    import v3_data_load as DL
    import v3_data_entities as ENT
    session, result = backend._active_session(), run['result']
    try:
        root = _workbook_root(run)
    except Exception as exc:
        run['load_plans'] = {cap: {'caption': cap, 'kind': 'none', 'ok': False,
                                   'reason': f'Workbook not readable for loading ({type(exc).__name__})'}
                             for cap in run['missing']}
        return []
    found, plans = [], {}
    for cap in list(run['missing']):
        try:
            loc = DL.locate(run['_raw'], root, result['ir'], cap)
            model = None
            if loc.get('sha256') and ENT.multi(root, result['ir'], cap):
                try:
                    model = ENT.find_model(session, root, result['ir'], cap, loc['sha256'])
                except Exception:
                    model = None
            if model:
                notes = '; '.join(model['checks']['notes'])
                _complete_binding(run, cap, model, 'Relationship model over loaded tables',
                                  'Tables loaded earlier from this data; keys and fact rows checked' + (f'. {notes}' if notes else ''))
                found.append(cap)
                continue
            hit = DL.find_loaded(session, cap, loc['sha256']) if loc.get('sha256') else None
            if hit:
                table = DL.check_loaded(session, hit, DL.fields_for(result['ir'], loc['datasource_id']))
                _complete_binding(run, cap, table, "Loaded earlier from this workbook's data",
                                  'Same source bytes (fingerprint); columns, types and read access verified')
                found.append(cap)
                continue
            try:     # release 23: the same data already loaded (from another file or workbook)?
                same = DL.find_reusable(session, run['_raw'], root, result['ir'], cap)
            except Exception:
                same = None
            if isinstance(same, dict):
                notes = '; '.join(same.get('checks', {}).get('notes') or [])
                _complete_binding(run, cap, same, 'Relationship model over loaded tables (same data)',
                                  'The same data was loaded earlier from another file or workbook; found by data '
                                  'fingerprint; keys and fact rows checked' + (f'. {notes}' if notes else ''))
                found.append(cap)
                continue
            if same:
                _complete_binding(run, cap, same, 'Loaded table with the same data',
                                  'The same data was loaded earlier from another file or workbook; found by data '
                                  'fingerprint; columns, types and read access verified')
                found.append(cap)
                continue
            plans[cap] = DL.plan(run['_raw'], root, result['ir'], cap)
        except Exception as exc:
            plans[cap] = {'caption': cap, 'kind': 'none', 'ok': False, 'reason': f'{type(exc).__name__}: {str(exc)[:200]}'}
    run['load_plans'] = plans
    return found


def _missing_summary(run):
    plans = run.get('load_plans') or {}
    n = len(run['missing'])
    can = sum(1 for c in run['missing'] if (plans.get(c) or {}).get('ok'))
    ext = sum(1 for c in run['missing'] if (plans.get(c) or {}).get('kind') == 'extract')
    parts = [f'{n} datasource(s) need a binding']
    if can:
        parts.append(f'{can} can be loaded from the workbook after approval')
    if ext:
        parts.append(f'{ext} are Tableau extracts (load once with the laptop loader)')
    return '; '.join(parts)


def _approve_loads(run, caps):
    """A person approved: load each datasource's data into a new table, bind it, record the approval."""
    import v3_data_load as DL
    plans = run.get('load_plans') or {}
    aid = v3_store.approve(run, 'load_data', ', '.join(caps),
                           {'datasources': caps, 'targets': {c: plans[c].get('target') for c in caps},
                            'sha256': {c: plans[c].get('sha256') for c in caps},
                            'rows': {c: plans[c].get('rows') for c in caps}})
    session, root, who = backend._active_session(), _workbook_root(run), v3_store.viewer()[0]
    done, errors = [], []
    for cap in caps:
        try:
            rec = DL.load(session, run['_raw'], root, run['result']['ir'], cap, who=who, via='app')
            v3_store.log_load(run, DL.log_row(run, rec, 'app', aid))
            model = isinstance(rec['table'], dict)
            notes = '; '.join(rec.get('notes') or [])
            _complete_binding(run, cap, rec['table'],
                              'Relationship model over loaded tables' if model else "Loaded from the workbook's own data",
                              f"{rec['action']}; " + ('keys and fact rows checked' if model else
                                                     'columns, types and read access verified') + (f'. {notes}' if notes else ''))
            run.setdefault('load_plans', {}).pop(cap, None)
            done.append(f"{cap} -> {rec['table']}")
        except Exception as exc:
            errors.append(f'{cap}: {type(exc).__name__}: {str(exc)[:300]}')
    if done:
        run['events'].insert(0, ('Data loaded after approval', '; '.join(done), 'done'))
    return done, errors


def _build(run):
    if not _runtime_sf():
        return 'review', 'Packaging needs Snowflake bindings (local preview only)', None
    if run['missing']:
        return 'review', 'Waiting for bindings: ' + ', '.join(run['missing']), None
    from v3_package import workbook_bundle
    account = None
    if _runtime_sf() is not None:          # E14: the account the bindings were verified in travels with the app
        try:
            from v3_source_binding import account_host
            account = account_host(_runtime_sf())
        except Exception:
            account = None
    data, build = workbook_bundle(run['result']['ir'], run['bindings'], ask=run.get('ask') or None, account=account)
    return 'done', 'Candidate package ' + build[:12] + ' (not deployed)', {'data': data, 'build': build}


def _validate(result):
    states = Counter(r['state'] for r in result['assessment']['sheets'])
    refused = sum(states[s] for s in REFUSED)
    issues = result['assessment']['issues']
    status = 'failed' if issues else (
        'review' if refused or result['assessment']['untranslated_calculations'] else 'done')
    detail = (f"{sum(states[s] for s in CONVERTIBLE)} convertible, {refused} refused; "
              'Tableau parity not yet verified')
    return status, detail, dict(states)


def run_pipeline(source, slot):
    states, events = {}, []
    origin = 'Tableau Cloud' if source['origin'] == 'tableau-cloud' else 'Uploaded file'
    run = {'name': source['name'], 'origin': origin, 'states': states, 'events': events,
           'details': {}, 'result': None, 'failed': None}
    v3_store.start(run, source)          # EN01: no-op when the evidence store is not set up

    def mark(stage, status, title, detail):
        states[stage] = status
        run['details'][stage] = detail
        events.insert(0, (title, detail, 'failed' if status == 'failed' else 'done'))
        v3_store.checkpoint(run, stage, snapshot=False)     # never raises; failures listed on the run
        render_live(slot, source['name'], origin, states, events, current=stage,
                    finished=stage == STAGES[-1] or status == 'failed')

    def start(stage, detail):
        states[stage] = 'active'
        events.insert(0, (stage + ' started', detail, ''))
        render_live(slot, source['name'], origin, states, events, current=stage)

    stage = 'Discovery'
    try:
        start(stage, 'Retrieving the workbook and inventorying datasources')
        fetched = {}
        if source['origin'] == 'tableau-cloud':
            fetched = TS.fetch_workbook_by_id(source['server_url'], source['site_content_url'],
                                              source['workbook_id'], name_hint=source['name'])
            raw, filename = fetched['bytes'], fetched['filename']
            try:   # scope the migration to the dashboards Tableau Cloud publishes
                fetched['published_views'] = TS.list_published_views(
                    source['server_url'], source['site_content_url'], source['workbook_id'])
            except Exception:
                fetched['published_views'] = None
        else:
            raw, filename = source['raw'], source['name']
        v3_store.note_workbook(run, raw)
        run['_raw'] = raw                     # release 21a: loads read the bundled files; not kept in snapshots
        run['discovery'] = _discover(raw, filename)
        d = run['discovery']
        mark(stage, 'done', 'Discovery complete',
             f"{len(d['datasources'])} datasource(s), {d['dashboards']} dashboard(s), "
             f"{len(d['bundled_files'])} bundled data file(s); workbook data not loaded or changed")

        stage = 'Parsing'
        start(stage, 'Scoped workbook model, assessment, source resolution and Cortex review')
        result = prepare_payload(raw, filename, origin=source['origin'],
                                 published_views=fetched.get('published_views'),
                                 tableau_server=source.get('server_url') if source['origin'] == 'tableau-cloud' else None)
        if source['origin'] == 'tableau-cloud':
            result.update(cloud_workbook_id=fetched.get('workbook_id'),
                          cloud_server_url=source['server_url'],
                          cloud_site_content_url=source['site_content_url'],
                          cloud_content_url=source.get('content_url'))
        run['result'] = result
        sheets = len(result['assessment']['sheets'])
        calls = (result['ir'].get('ai_usage') or {}).get('attempted_calls', 0)
        issues = result['assessment']['issues']
        mark(stage, 'failed' if issues else 'done', 'Parsing complete',
             f"{len(result['ir'].get('dashboards', []))} dashboard(s), {sheets} sheet occurrence(s); "
             f"Cortex calls: {calls}" +
             (f"; not published on Tableau Cloud, not migrated: {', '.join(result['ir']['unpublished_dashboards'])}"
              if result['ir'].get('unpublished_dashboards') else ''))
        if issues:
            run['failed'] = stage
            return run

        stage = 'Data model'
        start(stage, 'Checking each datasource binding and authored relationship')
        run['bindings'], run['binding_rows'], run['missing'] = _bind(result)
        auto = _auto_bind(run)
        if auto:
            events.insert(0, ('Existing tables detected', 'Bound read-only: ' + ', '.join(auto), 'done'))
        loaded = _auto_loaded(run)
        if loaded:
            events.insert(0, ('Loaded data found', 'Bound read-only to tables loaded earlier from this '
                                                   "workbook's own data: " + ', '.join(loaded), 'done'))
        if run['missing']:
            mark(stage, 'review', 'Data model needs input', _missing_summary(run))
        else:
            bound = f"{len(run['bindings'])} datasource(s) bound read-only"
            # One Data model stage: binding, then the Semantic View, then a single "complete".
            events.insert(0, ('Datasources bound', bound, 'done'))
            sv_status, sv_detail = None, None
            if _relationship_plans(run) or _single_tables(run):
                events.insert(0, ('Semantic View', 'Generating the Snowflake Semantic View from the Tableau '
                                                   'data model and proving it equal to the source', ''))
                render_live(slot, source['name'], origin, states, events, current=stage)
                sv_status, sv_detail = _auto_semantic(run)
                events[0] = ('Semantic View', sv_detail or 'Not created', 'failed' if sv_status == 'failed' else 'done')
                for title, fn in (('Cortex semantic model', _auto_cortex_model),
                                  ('Cortex calculation translation', _auto_cortex_calcs)):
                    events.insert(0, (title, 'Running in Snowflake (AI_COMPLETE); code verifies the result', ''))
                    render_live(slot, source['name'], origin, states, events, current=stage)
                    ok, text = fn(run)
                    events[0] = (title, text, 'done' if ok else 'review')
            mark(stage, sv_status or 'done', 'Data model complete', sv_detail or bound)

        stage = 'App build'
        start(stage, 'Packaging the application candidate')
        status, detail, run['package'] = _build(run)
        mark(stage, status, 'App build complete' if status == 'done' else 'App build needs input', detail)

        stage = 'Validation'
        start(stage, 'Summarising support, refusals and open verification')
        status, detail, run['sheet_states'] = _validate(result)
        ref = _auto_reference(run)
        if ref:
            events.insert(0, ('Tableau reference loaded automatically', ref, 'done'))
        elif run.get('capture_auto_miss'):
            events.insert(0, ('Tableau reference', run['capture_auto_miss'] + '; upload it on the Validation page', 'review'))
        if not run.get('missing') and _runtime_sf():      # every run draws every sheet (EN07)
            for title, text, ok in _auto_validation(run, lambda t: (
                    events.insert(0, (t, 'Running', '')),
                    render_live(slot, source['name'], origin, states, events, current=stage))):
                events[0] = (title, text, 'done' if ok else 'review')
            detail = _auto_validation_summary(run)
        else:
            detail += '; run sheet, value and image checks on the Validation page'
        mark(stage, status, 'Validation complete' if ref else 'Validation summary ready', detail)
        if run.get('sheet_checks') and any(r['Renders on data'] == 'Error' for r in run['sheet_checks']):
            states[stage] = 'review'
    except Exception as exc:
        states[stage] = 'failed'
        run['failed'] = stage
        run['error'] = f'{type(exc).__name__}: {exc}'
        events.insert(0, (stage + ' failed', run['error'], 'failed'))
        v3_store.checkpoint(run, stage, snapshot=False)
        render_live(slot, source['name'], origin, states, events, current=stage, finished=True)
    return run


def _capture_slug(view):
    """Workbook part of a captured view address: /[t/<site>/]views/<workbook>/<view>."""
    parts = [p for p in str(view or '').split('/') if p]
    return parts[parts.index('views') + 1] if 'views' in parts and parts.index('views') + 1 < len(parts) else ''


def _auto_reference(run):
    """Load the shipped Tableau reference that belongs to this Tableau Cloud workbook (lab).
    Matched only by the workbook address recorded in the capture; anything else leaves the upload."""
    if run.get('capture_bytes'):
        return None
    result = run.get('result') or {}
    if not result.get('cloud_workbook_id'):
        return None
    wanted = str(result.get('cloud_content_url') or '')
    try:
        from tableau_references import REFERENCES
        import v3_sheet_capture as SC
    except Exception:
        return None
    dashboards = {SC.canon(d.get('name')): {s.get('name') for s in d.get('sheets', [])}
                  for d in (result.get('ir') or {}).get('dashboards', [])}
    try:                                            # release 26: whole-workbook bundles (every dashboard)
        from tableau_reference_bundles import BUNDLES
    except Exception:
        BUNDLES = {}
    if wanted and wanted in BUNDLES:
        raw = BUNDLES[wanted].encode('utf-8')
        try:
            cap = SC.load(raw)
            parts = SC.dashboard_captures(cap)
            run.update(capture_bytes=raw, capture_name=f'{wanted} (workbook bundle)',
                       capture_digest=hashlib.sha256(raw).hexdigest(),
                       capture_auto=(f"Tableau's own values for {len(parts)} dashboard(s) of this workbook, captured "
                                     f"{parts[0].get('captured_at')} with Tableau's Embedding API; matched to this "
                                     f'workbook by its Tableau Cloud address ({wanted})'))
            run.pop('sheet_reference', None)
            return run['capture_auto']
        except ValueError:
            pass
    for name, text in REFERENCES.items():
        try:
            cap = SC.load(text.encode('utf-8'))
        except Exception:
            continue
        sheets = set(cap.get('sheets') or {})
        if wanted and _capture_slug(cap.get('view')).lower() == wanted.lower():
            how = f'its Tableau Cloud address ({wanted})'
        elif sheets and sheets <= dashboards.get(SC.canon(cap.get('dashboard')), set()):
            how = (f"dashboard '{cap.get('dashboard')}' and all {len(sheets)} captured sheet names "
                   f"(capture view {cap.get('view')})")
        else:
            continue
        raw = text.encode('utf-8')
        run.update(capture_bytes=raw, capture_name=name, capture_digest=hashlib.sha256(raw).hexdigest(),
                   capture_auto=(f"{name}, captured {cap.get('captured_at')} with Tableau's Embedding API; "
                                 f'matched to this workbook by {how}'))
        run.pop('sheet_reference', None)
        return run['capture_auto']
    run['capture_auto_miss'] = f"no shipped capture matched (workbook address: {wanted or 'not reported'})"
    return None


def _auto_validation(run, announce):
    """Validation as the last stage of the run: render every sheet, compare with Tableau, prove causes.
    Yields (title, text, ok) per step with its duration. The image comparison stays a button."""
    import time
    import v3_evidence as EV
    import v3_prove
    timing = run['validation_timing'] = []

    def step(title, fn):
        announce(title)
        t0 = time.time()
        try:
            text = fn()
            ok = True
        except Exception as exc:
            text, ok = f'{type(exc).__name__}: {str(exc)[:200]}', False
        seconds = round(time.time() - t0, 1)
        timing.append({'Step': title, 'Seconds': seconds, 'Result': text})
        return title, f'{text} ({seconds}s)', ok

    def render():
        EV.sheet_checks(run, True)
        rows = run.get('sheet_checks') or []
        return f"{sum(1 for r in rows if str(r['Renders on data']).startswith('Yes'))} of {len(rows)} sheets render on Snowflake"
    yield step('Rendering every sheet on Snowflake', render)
    if not run.get('capture_bytes'):                   # no Tableau reference yet: drawing is the check
        return

    def compare():
        _compare_capture(run)
        res = [r[0] for r in run['sheet_reference']['results'].values()]
        ok = sum(1 for r in res if r['Status'].startswith('Match'))
        diff = sum(1 for r in res if r['Status'] == 'Difference found')
        return f'{ok} of {len(res)} sheets match Tableau, {diff} with differences'
    yield step("Comparing every sheet with Tableau's values", compare)

    def exact():                                    # release 26: exact plans proven against Tableau
        import v3_exact_loop as X
        rows = X.prove_run(run, True)
        ok = sum(1 for r in rows if r['Result'] == 'Verified')
        part = sum(1 for r in rows if r['Result'] == 'Partly verified')
        return (f'{ok} of {len(rows)} sheets: an exact plan equals Tableau\'s values'
                + (f', {part} partly (Tableau forecast rows named)' if part else '')
                + f'; {len(X.applicable(run))} ready for a person to apply')
    yield step('Proving exact plans against Tableau (every sheet)', exact)

    diffs = [(n, d) for n, (summary, d) in run['sheet_reference']['results'].items()
             if summary['Status'] == 'Difference found'] if run.get('sheet_reference') else []
    for n, detail in diffs:
        sheet = next((s for d in run['result']['ir'].get('dashboards', []) for s in d.get('sheets', [])
                      if s.get('name') == n), None)
        dash, entry = _proof_entry(run, sheet)
        if not dash or not entry:
            continue
        summary = run['sheet_reference']['results'][n][0]

        def proof(n=n, detail=detail, sheet=sheet, dash=dash, entry=entry, summary=summary):
            run.setdefault('proof_tried', []).append(n)
            _memory(run)
            try:
                pr = run.setdefault('proofs', {})[n] = v3_prove.prove(
                    backend._active_session(), run, dict(summary, View=n, Sheet=n), detail or [], sheet, dash, entry, True)
            except Exception as exc:                 # shown next to the sheet, not only in the stage log
                run.setdefault('proof_errors', {})[n] = f'{type(exc).__name__}: {str(exc)[:300]}'
                raise
            (run.get('proof_errors') or {}).pop(n, None)
            good = next((r for r in pr['results'] if r.get('Result') == 'Proven'), None)
            return (f"cause proven: {good['Hypothesis']}; fix ready for reviewer approval" if good
                    else 'no hypothesis proven; needs a person')
        yield step(f'Cause proof — {n}', proof)


def _auto_validation_summary(run):
    sref = run.get('sheet_reference')
    checks = run.get('sheet_checks') or []
    drawn = sum(1 for r in checks if str(r['Renders on data']).startswith('Yes'))
    errors = sum(1 for r in checks if r['Renders on data'] == 'Error')
    refused = sum(1 for r in checks if r['Renders on data'] == 'Refused')
    if not sref:
        return (f'{drawn} of {len(checks)} sheets draw on Snowflake, {errors} with an error, {refused} refused '
                'with a reason; no Tableau reference loaded yet, so values are not compared'
                if checks else 'Validation could not compare values; see the Validation page')
    res = [r[0] for r in sref['results'].values()]
    ok = sum(1 for r in res if r['Status'].startswith('Match'))
    diff = sum(1 for r in res if r['Status'] == 'Difference found')
    proven = sum(1 for p in (run.get('proofs') or {}).values()
                 if any(r.get('Result') == 'Proven' for r in p['results']))
    total = sum(t['Seconds'] for t in run.get('validation_timing') or [])
    return (f'{ok} of {len(res)} sheets match Tableau; {diff} with differences, {proven} with a proven cause '
            f'(fix awaits reviewer approval); {round(total, 1)}s; image comparison on the Validation page')


def _pill(run):
    if not run:
        return 'Ready for migration', ''
    if run.get('failed'):
        return run['failed'] + ' failed', 'warn'
    if run.get('missing'):
        return 'Bindings needed', 'warn'
    return 'Ready for review', 'ok'


def _require_run(title, subtitle):
    run = st.session_state.get(RUN)
    render_header(title, subtitle, *_pill(run))
    if not run or not run.get('result'):
        st.markdown('<div class="v2-empty">Run a migration first. Results appear here once '
                    'the workbook has been parsed.</div>', unsafe_allow_html=True)
        st.button('Go to New migration', type='primary', key='v3c-empty-' + title,
                  on_click=_go, args=('New migration',))
        return None
    return run


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #
def sidebar():
    if 'v3c_pending' in st.session_state:
        st.session_state['v3c_page'] = st.session_state.pop('v3c_pending')
    with st.sidebar:
        st.markdown('<div class="v2-brand"><span class="v2-mark"><i></i><i></i><i></i></span>'
                    '<span class="v2-brand-text">Tableau to SiS<small>Migration workbench</small>'
                    '</span></div>', unsafe_allow_html=True)
        page = st.radio('Navigation', PAGES, label_visibility='collapsed', key='v3c_page')
        st.markdown('---')
        st.caption('TARGET PLATFORM')
        st.write('**Snowflake (hosted)**' if _runtime_sf() else '**Local development preview**')
        st.caption('Datasources are matched to existing Snowflake objects from workbook '
                   'metadata and verified read-only. The only object a run creates is the Semantic '
                   'View for relationship / join datasources.')
        run = st.session_state.get(RUN)
        if run and run.get('result'):
            st.markdown('---')
            st.caption('ACTIVE WORKBOOK')
            st.write(f"**{run['name']}**")
            states = Counter(r['state'] for r in run['result']['assessment']['sheets'])
            st.caption(f"{len(run['result']['ir'].get('dashboards', []))} dashboards · "
                       f"{sum(states[s] for s in CONVERTIBLE)} convertible · "
                       f"{sum(states[s] for s in REFUSED)} refused sheet occurrences")
            st.button('Start another migration', use_container_width=True, key='v3c-reset',
                      on_click=_reset)
        st.markdown('---')
        classic = st.toggle('Classic V3 workbench', key='v3c_classic',
                            help='The previous single-page V3 workbench, kept as a fallback.')
        st.caption('Engine ' + VERSION)
    return page, classic


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
def _architecture_svg_file(svg):
    """Standalone SVG for download: the board plus its white background."""
    i = svg.index('>') + 1
    return (svg[:i] + '<rect width="100%" height="100%" fill="#FFFFFF"/>' + svg[i:]).encode('utf-8')


@st.dialog('Platform architecture', width='large')
def _architecture_dialog(svg, width):
    """Near full-screen view of the architecture board (lab). Esc or the close button returns."""
    st.markdown('<style>div[role="dialog"]{width:95vw !important;max-width:95vw !important;}'
                '.v3c-arch-full{overflow:auto;max-height:80vh;background:#FFFFFF;border:1px solid #B9CDE6;border-radius:8px;padding:12px;}'
                f'.v3c-arch-full svg{{min-width:{int(width)}px;}}</style>', unsafe_allow_html=True)
    st.markdown(f'<div class="v3c-arch-full">{svg}</div>', unsafe_allow_html=True)
    st.download_button('Download diagram (SVG)', _architecture_svg_file(svg), file_name='accelerator_architecture.svg',
                       mime='image/svg+xml', key='v3c-arch-download')


def _access_note():
    """E10: who may use which page; the owner's one-time SQL while the console is in open mode."""
    import v3_roles
    _role_needed('Overview')
    access = st.session_state.get('v3c_access') or {}
    if access.get('mode') == 'TABLE':
        st.caption('Your roles: ' + ', '.join(sorted(access.get('roles') or [])))
        return
    if access.get('mode') in ('OPEN', 'OPEN_NO_ROWS'):
        with st.expander('Who may use this console (E10): open to everyone until roles are set'):
            st.caption('AUTHOR runs migrations, reviews and approves fixes; PUBLISHER deploys apps and portals; '
                       'anyone else sees Overview and the Estate catalogue. One-time SQL for the owner:')
            st.code('\n'.join(v3_roles.setup_sql()), language='sql')


def page_overview():
    render_header('Tableau to SiS Accelerator',
                  'Automated discovery, conversion and evidence for one Tableau workbook at a time, '
                  'running inside Snowflake.', *_pill(st.session_state.get(RUN)))
    _access_note()
    _saved_runs()
    st.markdown(
        """
        <div class="v2-flow">
          <section><small>Source</small><b>Tableau workbook</b>
            <span>Upload one TWB/TWBX or pull it directly from Tableau Cloud.</span></section>
          <i>&rsaquo;</i>
          <section><small>Automated accelerator</small><b>Discover, translate, rebuild</b>
            <span>Resolve governed Snowflake data, check the data model, translate calculations
            and rebuild dashboards, filters, parameters and actions.</span></section>
          <i>&rsaquo;</i>
          <section><small>Outcome</small><b>Streamlit in Snowflake</b>
            <span>A reviewable application with an explicit list of what converted, what was
            refused and what still needs verification.</span></section>
        </div>
        """, unsafe_allow_html=True)
    st.subheader('What the accelerator handles')
    st.markdown(
        """
        <div class="v2-cards">
          <div class="v2-card" style="--accent:#29b5e8"><h3>Connect and understand</h3>
            <p>Upload a workbook or browse a Tableau Cloud site.</p>
            <p>Inventory datasources, dashboards, sheets, filters, parameters, actions and calculations.</p></div>
          <div class="v2-card" style="--accent:#11567f"><h3>Convert on a checked engine</h3>
            <p>Bind to existing Snowflake objects with schema and access checks, and verify authored relationships.</p>
            <p>Translate calculations with typed dependency checks; unsupported semantics are refused, never guessed.</p></div>
          <div class="v2-card" style="--accent:#14a07a"><h3>Prove before you promote</h3>
            <p>Preview the generated dashboards on live Snowflake data.</p>
            <p>Cortex reviews the workbook structure; its output is advisory and never executed as code.</p></div>
        </div>
        <div class="v2-trust">
          <div><b>Snowflake native</b><span>Runs inside the governance boundary</span></div>
          <div><b>Metadata driven</b><span>Sources matched and verified, read-only</span></div>
          <div><b>Live migration status</b><span>Real progress across five stages</span></div>
          <div><b>Evidence first</b><span>Refusals and open checks are visible</span></div>
        </div>
        """, unsafe_allow_html=True)
    st.subheader('One automated workflow')
    render_stepper()
    with st.expander('What this engine corrects compared with the first accelerator'):
        st.markdown(
            '- Unknown datasources are refused instead of falling back to a default table.\n'
            '- Relationship keys, coverage and fact grain are checked before binding; no partial-model fallback.\n'
            '- Unsupported calculation dependencies are refused instead of becoming NULL or SUM.\n'
            '- Field identity is kept per datasource, so same-named fields from different sources stay separate.\n'
            '- Source data is never changed: existing tables are used in place and file data (Excel, CSV, '
            'Hyper) is loaded into new tables. The run records its own evidence (stages, Cortex calls, proofs, '
            'fixes, approvals) in the evidence store.\n'
            '- Cortex proposals are validated against the source; model-generated SQL is never executed.')
    with st.expander('Platform architecture — how the accelerator is wired', expanded=True):
        st.caption('Tableau sources on the left; everything else runs inside one Snowflake account. '
                   'Cortex proposes, code verifies, and every sheet passes three checks before deploy.')
        try:
            from v3_platform_board import build_svg_fragment
            svg, _w, _h = build_svg_fragment()
            if st.button('⛶  Expand diagram', key='v3c-arch-expand', help='Open the diagram full screen'):
                _architecture_dialog(svg, _w)
            st.markdown(f'<div class="v2-arch">{svg}</div>', unsafe_allow_html=True)
        except Exception as exc:
            st.error(f'Architecture board unavailable: {type(exc).__name__}: {exc}')
    with st.expander('Snowflake Cortex across the workflow'):
        st.dataframe(_touchpoint_table(), hide_index=True, use_container_width=True)
        st.caption('Cortex proposes or explains; deterministic code verifies; Tableau evidence accepts. '
                   'Model output is never executed.')
    action, detail = st.columns([0.25, 0.75])
    with action:
        st.button('Start a migration', type='primary', use_container_width=True,
                  key='v3c-overview-start', on_click=_go, args=('New migration',))
    with detail:
        st.caption('Snowflake remains the execution and governance boundary throughout.')


def _saved_runs():
    """EN01: runs saved in Snowflake reopen after a refresh or a redeploy, with no new Cortex calls."""
    sess = v3_store.session()
    if sess is None or not _runtime_sf():
        return
    if not v3_store.ready(sess):
        st.caption('Evidence store not set up: a run is kept only until the page is refreshed. The set-up '
                   f'script sql/en01_evidence_store.sql creates {v3_store.SCHEMA}.')
        return
    try:
        rows = v3_store.recent_runs(sess)
    except Exception as exc:
        st.caption(f'Saved runs could not be listed ({type(exc).__name__}).')
        return
    if not rows:
        return
    can_remove = v3_store.can_hide(sess)
    with st.expander(f'Saved runs ({len(rows)})', expanded=False):
        st.caption('Reopen a run after a refresh or redeploy. Remove takes a run off this list; its evidence '
                   'stays in Snowflake.')
        for r in rows:
            c = st.columns([0.44, 0.32, 0.12, 0.12])
            c[0].markdown(f"**{esc(r['WORKBOOK_NAME'] or '—')}** · {esc(r['ORIGIN'] or '')}"
                          + (f" · {esc(r['FAILED_STAGE'])} failed" if r.get('FAILED_STAGE') else ''))
            c[1].caption(f"Started {str(r['STARTED_AT'])[:16]} by {r['STARTED_BY'] or 'not reported'}; "
                         f"last saved {str(r['LAST_SAVED_AT'])[:16]}")
            if c[2].button('Reopen', key='v3c-reopen-' + str(r['RUN_ID']), use_container_width=True):
                _reopen(r['RUN_ID'])
            if c[3].button('Remove', key='v3c-hide-' + str(r['RUN_ID']), use_container_width=True,
                           disabled=not can_remove):
                _hide([r['RUN_ID']])
        if can_remove:
            if st.button('Remove all from this list', key='v3c-hide-all'):
                _hide([r['RUN_ID'] for r in rows])
        else:
            st.caption('To remove runs from this list, run the release 23b set-up SQL (adds HIDDEN_RUNS).')
        st.caption('A reopened run shows the verdicts recorded for it; no Cortex call is repeated. '
                   'Re-run validation to check it again.')


def _hide(run_ids):
    try:
        v3_store.hide_runs(v3_store.session(), run_ids)
    except Exception as exc:
        st.error(f'Not removed ({type(exc).__name__}).')
        return
    st.rerun()


def _reopen(run_id):
    try:
        run = v3_store.reopen(v3_store.session(), run_id)
    except Exception as exc:
        st.error(f'This run could not be reopened ({type(exc).__name__}).')
        return
    if run.get('result') and not run.get('missing'):
        try:
            _status, _detail, run['package'] = _build(run)
        except Exception as exc:
            run['events'].insert(0, ('Package not rebuilt', type(exc).__name__, 'review'))
    run['events'].insert(0, ('Run reopened', 'Reopened from the evidence store; verdicts as recorded', 'done'))
    st.session_state[RUN] = run
    _go('New migration')
    st.rerun()


def _upload_intake():
    upload = st.file_uploader('Tableau workbook', type=['twb', 'twbx'], key='v3c-upload')
    if upload is not None:
        raw = upload.getvalue()
        digest = hashlib.sha256(raw).hexdigest()
        current = st.session_state.get(SOURCE) or {}
        if current.get('digest') != digest:
            st.session_state[SOURCE] = {'origin': 'upload', 'name': upload.name, 'raw': raw,
                                        'digest': digest}


def _cloud_intake():
    st.session_state.setdefault(INTAKE + 'site', DEFAULT_SITE_URL)   # kept across runs
    url = (st.text_input('Tableau site URL', key=INTAKE + 'site',
                         placeholder='https://<pod>.online.tableau.com/#/site/<site>/home') or '').strip()
    st.caption('Uses the configured Tableau credentials (Snowflake secret). Read-only.')
    if st.button('Connect to Tableau', disabled=not url, key='v3c-connect'):
        st.session_state.pop(BROWSE, None)
        try:
            with st.spinner('Connecting to Tableau and listing workbooks…'):
                location = TS.parse_site_url(url)
                st.session_state[BROWSE] = dict(location, url=url, **TS.list_site_contents(**location))
        except Exception as exc:
            st.error(f'Tableau connection failed: {type(exc).__name__}: {exc}')
    browse = st.session_state.get(BROWSE)
    if not browse or browse.get('url') != url:
        return
    books = {w['id']: w for w in browse.get('workbooks', []) if w.get('id')}
    st.success(f'Connected to Tableau. {len(books)} workbooks available.')
    if not books:
        return
    projects = {w['project_id']: w.get('project_name') or 'Unnamed project'
                for w in books.values() if w.get('project_id')}
    # '' (not None) means all projects: a None value restored into widget state renders as empty.
    project = st.selectbox('Project', [''] + sorted(projects, key=lambda p: (projects[p], p)),
                           format_func=lambda p: 'All projects' if not p else projects[p],
                           key=INTAKE + 'project')
    choices = sorted((i for i, w in books.items() if not project or w.get('project_id') == project),
                     key=lambda i: (books[i].get('name') or '', i))
    selected = st.selectbox('Workbook', choices, index=None,
                            format_func=lambda i: books[i].get('name') or 'Unnamed workbook',
                            key=INTAKE + 'workbook-' + (project or 'all'))
    if selected:
        current = st.session_state.get(SOURCE) or {}
        if current.get('workbook_id') != selected:
            st.session_state[SOURCE] = {'origin': 'tableau-cloud',
                                        'name': books[selected].get('name') or selected,
                                        'workbook_id': selected, 'server_url': browse['server_url'],
                                        'site_content_url': browse['site_content_url'],
                                        'content_url': books[selected].get('content_url')}


def page_migrate():
    run = st.session_state.get(RUN)
    render_header('New migration', 'Choose one Tableau workbook and run the five migration stages.',
                  *_pill(run))
    if run:
        _render_run(run)
        return
    render_stepper()
    left, right = st.columns([1.65, 0.75], gap='large')
    with left:
        st.subheader('Select workbook source')
        mode = st.radio('Workbook source', ['Tableau Cloud', 'Upload file'], horizontal=True,
                        label_visibility='collapsed', key=INTAKE + 'mode')
        wanted = 'tableau-cloud' if mode == 'Tableau Cloud' else 'upload'
        if (st.session_state.get(SOURCE) or {}).get('origin') not in (None, wanted):
            st.session_state.pop(SOURCE, None)
        if mode == 'Tableau Cloud':
            _cloud_intake()
        else:
            _upload_intake()
    with right:
        st.subheader('Automation')
        st.markdown('<div class="v2-note"><b>Target platform</b><br>Streamlit in Snowflake</div>',
                    unsafe_allow_html=True)
        st.markdown('<div class="v2-auto"><div><b>Automatic, read-only source resolution</b>'
                    '<span>Workbook datasources are matched to existing Snowflake objects and '
                    'authored relationships are verified. Source data is never changed; the run '
                    'records its own evidence in Snowflake so it can be reopened.'
                    '</span></div></div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="v2-next"><div class="v2-next-title">What happens next</div>'
            '<div class="v2-next-row"><div class="v2-next-icon">01</div><div><b>Analyze workbook</b>'
            '<span>Inventory content, dependencies and Cortex review</span></div></div>'
            '<div class="v2-next-row"><div class="v2-next-icon">02</div><div><b>Verify Snowflake model</b>'
            '<span>Check bindings, keys and fact grain; build the Semantic View for relationships</span></div></div>'
            '<div class="v2-next-row"><div class="v2-next-icon">03</div><div><b>Build and review</b>'
            '<span>Package the app and list what needs verification</span></div></div></div>',
            unsafe_allow_html=True)
    source = st.session_state.get(SOURCE)
    st.markdown('<div class="v2-section"></div>', unsafe_allow_html=True)
    action, note = st.columns([0.25, 0.75])
    with action:
        start = st.button('Run migration', type='primary',
                          use_container_width=True, disabled=source is None, key='v3c-run')
    with note:
        st.caption(f"Selected: {source['name']}" if source else 'Select a workbook to start.')
    if start and source:
        slot = st.empty()
        st.session_state[RUN] = run_pipeline(source, slot)
        st.rerun()


def _relationship_plans(run):
    # DM3: physical-join / custom-SQL plans are bound read-only too, but are not relationship models (no shared
    # Semantic View is built from them)
    return {cap: plan for cap, plan in (run.get('bindings') or {}).items()
            if isinstance(plan, dict) and plan.get('kind') not in ('physical-join', 'custom-sql')}


_SF_OBJECT = re.compile(r'[A-Za-z_][A-Za-z_0-9$]*\.[A-Za-z_][A-Za-z_0-9$]*\.[A-Za-z_][A-Za-z_0-9$]*')


def _single_tables(run):
    """Datasources bound to one existing Snowflake table (hosted only; local paths are not tables)."""
    if not _runtime_sf():
        return {}
    return {cap: b for cap, b in (run.get('bindings') or {}).items()
            if isinstance(b, str) and _SF_OBJECT.fullmatch(b)}


def _create_single(run, cap, table, enrich=True):
    """Semantic View over a single bound table, for Ask the data; the dashboard keeps reading the table."""
    import v3_semantic as SV
    import v3_sv_shared as SH
    session = backend._active_session()
    rec = SH.ensure_single(session, table, cap, enrich=SV.cortex_enricher(session, run) if enrich else None)
    v3_store.log_semantic(run, SH.log_rows(run, cap, rec, 'single_table'))
    run.setdefault('semantic', {})[cap] = rec
    if rec['verified'] and rec.get('ask'):
        run.setdefault('ask', {})[cap] = rec['ask']
    else:
        (run.get('ask') or {}).pop(cap, None)
    return rec


def _semantic_section(run, key):
    """Semantic layer for relationship / join datasources: generate, create, verify, attach."""
    import v3_semantic as SV
    plans = _relationship_plans(run)
    if not plans:
        return
    st.subheader('Snowflake Semantic View')
    st.caption('Datasources built from Tableau relationships or joins become a Snowflake Semantic View: the '
               'logical tables, the authored relationship keys (verified above), dimensions and '
               'metrics. Cortex adds business descriptions and synonyms. Once created, the dashboard reads '
               'its data through the Semantic View, but only after the app proves it returns the same rows '
               'and totals as the verified relationship model.')
    result = run['result']
    for cap, plan in plans.items():
        base = __import__('v3_semantic').base_plan(plan)
        try:
            sem = SV.model(base, result['ir'], cap)
        except Exception as exc:
            st.warning(f'{cap}: semantic model unavailable ({type(exc).__name__}: {exc})')
            continue
        record = (run.get('semantic') or {}).get(cap)
        if record and record.get('mode') == 'shared' and record.get('model'):
            sem = record['model']
        in_use = _bound_view(run, cap)
        if in_use:
            st.info(f"**In use by the dashboard and Ask the data:** {in_use} "
                    f"({'designed by Cortex, verified by code' if in_use.endswith('_CORTEX') else 'built by code'})")
        cols = st.columns(5)
        for col, (label, value) in zip(cols, (('Tables', len(sem['tables'])), ('Relationships', len(sem['relationships'])),
                                              ('Dimensions', len(sem['dimensions'])), ('Numeric', sum(1 for d in sem['dimensions'] if d.get('numeric'))),
                                              ('Metrics', len(sem['metrics'])))):
            col.metric(label, value)
        st.dataframe([{'Relationship': r['name'], 'From': f"{r['from']}.{r['from_key']}",
                       'To': f"{r['to']}.{r['to_key']}"} for r in sem['relationships']],
                     hide_index=True, use_container_width=True)
        with st.expander('Metrics in the Semantic View'):
            st.dataframe([{'Metric': m['name'], 'Definition': m['expr'], 'From': m['source']} for m in sem['metrics']],
                         hide_index=True, use_container_width=True)
            if sem['skipped']:
                st.caption(f"{len(sem['skipped'])} Tableau calculations are not metrics in the view (they use "
                           'parameters, table sub-queries or window logic); the dashboard still computes them '
                           'on the same data.')
        with st.expander('Semantic View DDL'):
            st.code((record or {}).get('ddl') or SV.ddl(sem), language='sql')
        if record:
            _decision_note(record)
            if record['verified']:
                if _bound_view(run, cap).endswith('_CORTEX'):
                    st.success(f"Code-built Semantic View {record['name']} created and verified (same rows and totals "
                               'as the relationship model). Kept as the fallback: the dashboard and Ask the data '
                               'read the Cortex-designed view below.')
                else:
                    st.success(f"Semantic View {record['name']} created. The dashboard now reads its data through it "
                               '(same rows and totals as the verified relationship model).')
            else:
                st.error('Semantic View created but not equal to the relationship model; the dashboard keeps the '
                         'verified model query.')
            st.dataframe(record['checks'], hide_index=True, use_container_width=True)
            if any(a['Result'] != 'created' for a in record.get('attempts') or []):
                with st.expander('Definition attempts'):
                    st.dataframe(record['attempts'], hide_index=True, use_container_width=True)
            model_ = record.get('model') or {}
            entries = [dict(e, _type='Metric') for e in model_.get('metrics') or []] + \
                      [dict(e, _type='Dimension') for e in model_.get('dimensions') or []]
            if entries:
                cortex_n = sum(1 for e in entries if str(e.get('comment') or '').endswith('(description by Cortex)'))
                with st.expander(f'Business glossary in the Semantic View — {cortex_n} of {len(entries)} '
                                 'descriptions written by Cortex', expanded=True):
                    st.caption('Stored in the Semantic View itself (COMMENT and WITH SYNONYMS), so Cortex Analyst '
                               'and anyone browsing the view in Snowsight see the same business meaning. '
                               'The deployed dashboard shows this glossary on its Ask the data tab.')
                    st.dataframe([{'Type': e['_type'], 'Name': e['name'], 'Table': e['table'],
                                   'Description': str(e.get('comment') or '').replace(' (description by Cortex)', ''),
                                   'Synonyms': ', '.join(e.get('synonyms') or []),
                                   'Written by': 'Cortex' if str(e.get('comment') or '').endswith(
                                       '(description by Cortex)') else 'Tableau'} for e in entries],
                                 hide_index=True, use_container_width=True)
            if record.get('metrics'):
                with st.expander('Metric values from the Semantic View'):
                    st.dataframe(record['metrics'], hide_index=True, use_container_width=True)
            _cortex_model_block(run, cap, base, key)
            continue
        if not _runtime_sf():
            st.caption('Creating the Semantic View runs in the hosted Snowflake app.')
            continue
        enrich = st.checkbox('Let Cortex write business descriptions and synonyms', value=True,
                             key=f'v3c-sv-enrich-{key}-{cap}')
        err = (run.get('semantic_errors') or {}).get(cap)
        if err:
            st.warning('The migration run could not create the Semantic View: ' + err)
        if st.button('Retry: create Semantic View' if err else 'Create Semantic View in Snowflake', type='primary',
                     key=f'v3c-sv-create-{key}-{cap}'):
            try:
                with st.spinner('Creating the Semantic View and checking it against the relationship model…'):
                    rec = _create_semantic(run, cap, base, enrich)
                if rec['verified']:
                    if not run.get('missing'):
                        status, detail, run['package'] = _build(run)
                        run['events'].insert(0, ('Semantic View created', rec['name'] + '; ' + detail, 'done'))
                st.rerun()
            except Exception as exc:
                st.error(f'Semantic View not created: {type(exc).__name__}: {str(exc)[:400]}')


def _decision_note(record):
    """Release 20a: say whether the shared view was reused, refreshed or created, and what was in the way."""
    import v3_sv_shared as SH
    text = SH.summary(record)
    if text:
        st.info(text)
    blocked = (record.get('decision') or {}).get('blocked') or []
    if blocked:
        with st.expander(f'{len(blocked)} view name(s) could not be used'):
            st.caption('The run did not fail: the next name was used. The statement below, run by the owning role, '
                       'lets this app use the original view next time.')
            st.dataframe(blocked, hide_index=True, use_container_width=True)


def _bound_view(run, cap):
    b = (run.get('bindings') or {}).get(cap)
    return str(b.get('semantic_view') or '') if isinstance(b, dict) else ''


def _cortex_model_block(run, cap, base, key):
    """Cortex designs a second Semantic View from tables and columns only; code proves it (lab)."""
    import v3_cortex_model as CM
    st.markdown('**Cortex-designed semantic model** (AI_COMPLETE designs, code validates, creates and proves)')
    st.caption("Cortex sees the logical tables and their Snowflake columns, not Tableau's relationship keys. It "
               'chooses the fact table, keys, relationships, metrics, descriptions and synonyms as a typed design. '
               'Code rejects anything unknown, creates the view under a separate _CORTEX name and runs the same '
               'equality check. The migration run does this automatically; a verified design is used by the '
               'dashboard and Ask the data, otherwise the code-built view above stays in use.')
    prior = (run.get('cortex_models') or {}).get(cap)
    if (not prior or not prior.get('verified')) and st.button(
            f"{'Retry Cortex design' if prior else 'Let Cortex design the semantic model'} — {cap}",
            disabled=not _runtime_sf(), key=f'v3c-cm-{key}-{cap}'):
        try:
            with st.spinner('Cortex is designing the model; code is validating, creating and proving it…'):
                import v3_sv_shared as SH
                run.setdefault('cortex_models', {})[cap] = SH.cortex_variant(
                    backend._active_session(), run, base, run['result']['ir'], cap, (run.get('semantic') or {}).get(cap))
        except Exception as exc:
            st.error(f'Cortex design not created: {type(exc).__name__}: {str(exc)[:300]}')
    rec = (run.get('cortex_models') or {}).get(cap)
    if not rec:
        return
    if rec.get('reused'):
        with st.container(border=True):
            if rec['verified']:
                st.success(f"{rec['name']}: the existing Cortex-designed shared view for these tables was reused "
                           '(no new Cortex call) and verified equal to the Tableau relationship model.')
            else:
                st.error(f"{rec['name']}: the existing Cortex-designed shared view does not reproduce this "
                         'workbook\'s model; the dashboard keeps the code-built view.')
            if rec.get('checks'):
                st.dataframe(rec['checks'], hide_index=True, use_container_width=True)
            _cortex_switch(run, cap, base, rec, key)
        return
    with st.container(border=True):
        cmp = rec['relationships_vs_tableau']
        sem = rec['model']
        cols = st.columns(4)
        for col, (lbl, val) in zip(cols, (('Relationships matching Tableau', f"{cmp['same']} of {cmp['authored']}"),
                                          ('Fact table', 'Same as Tableau' if cmp['fact_matches_tableau'] else 'Different'),
                                          ('Metrics designed', len(sem.get('metrics') or [])),
                                          ('Items rejected by code', len(rec['rejected'])))):
            col.metric(lbl, val)
        if rec['verified']:
            st.success(f"{rec['name']} created and verified: same rows and totals as the Tableau relationship model.")
        else:
            st.error('Not verified: the Cortex design does not reproduce the Tableau relationship model. '
                     'Nothing is attached; the dashboard is unaffected.')
        if rec.get('proposal_notes'):
            st.caption('Cortex design notes: ' + rec['proposal_notes'])
        if rec.get('checks'):
            st.dataframe(rec['checks'], hide_index=True, use_container_width=True)
        st.dataframe([{'Relationship (Cortex)': r['name'], 'From': f"{r['from']}.{r['from_key']}",
                       'To': f"{r['to']}.{r['to_key']}"} for r in sem.get('relationships') or []],
                     hide_index=True, use_container_width=True)
        if sem.get('metrics'):
            with st.expander('Metrics designed by Cortex'):
                st.dataframe([{'Metric': m['name'], 'Definition': m['expr'],
                               'Description': str(m.get('comment') or '').replace(' (design by Cortex)', '')}
                              for m in sem['metrics']], hide_index=True, use_container_width=True)
            if rec.get('metrics'):
                st.dataframe(rec['metrics'], hide_index=True, use_container_width=True)
        if rec['rejected']:
            with st.expander('Rejected by code'):
                st.dataframe(rec['rejected'], hide_index=True, use_container_width=True)
        with st.expander('DDL generated by code from the Cortex design'):
            st.code(rec.get('ddl') or '', language='sql')
        _cortex_switch(run, cap, base, rec, key)


def _cortex_switch(run, cap, base, rec, key):
    """Point the dashboard and Ask the data at the Cortex-designed view (only when proven equal), or back."""
    import v3_semantic as SV
    current = (run.get('bindings') or {}).get(cap) or {}
    on_cortex = str(current.get('semantic_view') or '').endswith('_CORTEX')
    code_rec = (run.get('semantic') or {}).get(cap)
    if on_cortex:
        st.info('In use: the dashboard and Ask the data read through this Cortex-designed view. Validation '
                'compares every sheet with Tableau on it.')
        if code_rec and code_rec.get('verified') and st.button('Switch back to the code-built view',
                                                               key=f'v3c-cm-back-{key}-{cap}'):
            _rebind(run, cap, __import__('v3_sv_shared').attach(base, code_rec), 'code-built Semantic View', code_rec['name'])
            st.rerun()
        return
    if not rec.get('verified'):
        st.caption('Only a Cortex design proven equal to the Tableau model can be used by the dashboard.')
        return
    if st.button('Use the Cortex-designed view for this dashboard and Ask the data', type='primary',
                 key=f'v3c-cm-use-{key}-{cap}'):
        _rebind(run, cap, __import__('v3_sv_shared').attach(base, rec), 'Cortex-designed Semantic View', rec['name'])
        st.rerun()


def _rebind(run, cap, binding, label, name):
    """Swap one datasource's bound view, rebuild the package, and clear evidence that must be re-proven."""
    v3_store.approve(run, 'switch_model', cap, {'to': label, 'view': name})
    run['bindings'][cap] = binding
    for row in run.get('binding_rows') or []:
        if row.get('Datasource') == cap:
            row.update({'Binding': label, 'Snowflake objects': name,
                        'Status': 'Verified equal to the Tableau relationship model; re-run validation'})
    for k in ('sheet_values', 'sheet_checks', 'sheet_reference', 'reference', 'proofs', 'proof_tried'):
        run.pop(k, None)
    if not run.get('missing'):
        status, detail, run['package'] = _build(run)
        run['events'].insert(0, ('Data model switched', f'{label}: {name}; {detail}', 'done'))


def _single_semantic_section(run, key):
    """Single-table datasources: a Semantic View over the bound table, so Ask the data works too."""
    singles = _single_tables(run)
    if not singles:
        return
    st.subheader('Snowflake Semantic View — single-table data')
    st.caption('This datasource reads one Snowflake table, so there are no relationships to rebuild. The '
               'migration still creates a Semantic View over that table — every column, totals and distinct '
               'counts, plus Cortex descriptions and synonyms — and proves it returns the same rows and totals '
               'as the table. Ask the data (Cortex Analyst) uses it; the dashboard keeps reading the table.')
    for cap, table in singles.items():
        record = (run.get('semantic') or {}).get(cap)
        if record and record.get('mode') == 'single_table':
            sem = record.get('model') or {}
            _decision_note(record)
            cols = st.columns(4)
            for col, (lbl, value) in zip(cols, (('Table', table.split('.')[-1]), ('Dimensions', len(sem.get('dimensions') or [])),
                                                ('Numeric', sum(1 for d in sem.get('dimensions') or [] if d.get('numeric'))),
                                                ('Metrics', len(sem.get('metrics') or [])))):
                col.metric(lbl, value)
            if record['verified']:
                st.success(f"Semantic View {record['name']} created over {table} and verified equal to it. "
                           'Ask the data is enabled here and in the deployed app.')
            else:
                st.error('Semantic View created but not equal to the table; Ask the data is not enabled.')
            if record.get('checks'):
                st.dataframe(record['checks'], hide_index=True, use_container_width=True)
            with st.expander('Semantic View DDL'):
                st.code(record.get('ddl') or '', language='sql')
            entries = [dict(e, _type='Metric') for e in sem.get('metrics') or []] + \
                      [dict(e, _type='Dimension') for e in sem.get('dimensions') or []]
            if entries:
                cortex_n = sum(1 for e in entries if str(e.get('comment') or '').endswith('(description by Cortex)'))
                with st.expander(f'Business glossary in the Semantic View — {cortex_n} of {len(entries)} '
                                 'descriptions written by Cortex'):
                    st.dataframe([{'Type': e['_type'], 'Name': e['name'],
                                   'Description': str(e.get('comment') or '').replace(' (description by Cortex)', ''),
                                   'Synonyms': ', '.join(e.get('synonyms') or [])} for e in entries],
                                 hide_index=True, use_container_width=True)
            if any(a['Result'] not in ('created', 'equal') for a in record.get('attempts') or []):
                with st.expander('Definition attempts'):
                    st.dataframe(record['attempts'], hide_index=True, use_container_width=True)
            continue
        err = (run.get('semantic_errors') or {}).get(cap)
        if err:
            st.warning(f'{cap}: the migration run could not create the Semantic View: ' + err)
        if st.button(('Retry: create' if err else 'Create') + f' Semantic View over {table}', type='primary',
                     key=f'v3c-sv1-create-{key}-{cap}'):
            try:
                with st.spinner('Creating the Semantic View and checking it against the table…'):
                    rec = _create_single(run, cap, table)
                if rec['verified'] and not run.get('missing'):
                    status, detail, run['package'] = _build(run)
                    run['events'].insert(0, ('Semantic View created', rec['name'] + '; ' + detail, 'done'))
                st.rerun()
            except Exception as exc:
                st.error(f'Semantic View not created: {type(exc).__name__}: {str(exc)[:400]}')


def _create_semantic(run, cap, base, enrich=True):
    """Create, verify and (only if equal) attach the Semantic View for one relationship datasource."""
    import v3_semantic as SV
    import v3_sv_shared as SH
    session = backend._active_session()
    rec = SH.ensure(session, base, cap, enrich=SV.cortex_enricher(session, run) if enrich else None)
    v3_store.log_semantic(run, SH.log_rows(run, cap, rec, 'relationship'))
    run.setdefault('semantic', {})[cap] = rec
    if rec['verified']:
        run['bindings'][cap] = SH.attach(base, rec)
        for row in run.get('binding_rows') or []:
            if row.get('Datasource') == cap:
                row.update({'Binding': 'Snowflake Semantic View', 'Snowflake objects': rec['name'],
                            'Status': ('Shared view reused' if (rec.get('decision') or {}).get('action') == 'reused'
                                       else 'Shared view created from the tables and authored keys')
                            + '; verified equal to the model'})
    return rec


def _auto_semantic(run):
    """Migration step: every relationship / join datasource gets its Semantic View automatically."""
    plans = {cap: __import__('v3_semantic').base_plan(p)
             for cap, p in _relationship_plans(run).items()}
    singles = _single_tables(run)
    if not plans and not singles:
        return None, 'no Snowflake-bound datasources'
    if not _runtime_sf() or backend._active_session() is None:
        return 'review', 'Semantic View is created in the hosted Snowflake app'
    made, notes, asks, info = [], [], [], []      # notes = what did not pass; info = what is recorded
    for cap, table in singles.items():
        try:
            rec = _create_single(run, cap, table)
            if rec['verified']:
                asks.append(rec['name'].split('.')[-1])
            else:
                notes.append(f'{cap}: view over {table} not equal to the table; Ask the data not enabled')
        except Exception as exc:
            run.setdefault('semantic_errors', {})[cap] = f'{type(exc).__name__}: {str(exc)[:300]}'
            notes.append(f'{cap}: Semantic View not created ({type(exc).__name__})')
    if not plans:
        detail = (f"Semantic View {', '.join(asks)} created over the dashboard table and verified equal; "
                  'Ask the data enabled (dashboard reads the table directly)' if asks else 'Semantic View not created')
        return ('done' if asks and not notes else 'review'), '; '.join([detail] + notes)
    for cap, base in list(plans.items()):
        import v3_semantic as _SV
        part, left_out = _SV.exact_part(base)
        if not left_out:
            continue
        # DM8: a one-row-per-key table cannot be declared in a Semantic View; the view covers the rest (Ask the
        # data), the dashboard keeps reading the whole verified model
        plans.pop(cap)
        names = ', '.join(t['table'] for t in left_out)
        try:
            session = backend._active_session()
            import v3_sv_shared as SH
            if len(part['tables']) >= 2:
                rec = SH.ensure(session, part, cap, enrich=_SV.cortex_enricher(session, run))
            else:
                rec = SH.ensure_single(session, part['tables'][part['root']]['table'], cap)
            run.setdefault('semantic', {})[cap] = dict(rec, partial=names)
            if rec['verified']:
                asks.append(rec['name'].split('.')[-1])
                info.append(f'{cap}: Semantic View covers the model without {names} (its key repeats; read one row '
                            'per key); dashboards read the whole verified model')
            else:
                notes.append(f'{cap}: view over the model without {names} not equal; Ask the data not enabled')
        except Exception as exc:
            run.setdefault('semantic_errors', {})[cap] = f'{type(exc).__name__}: {str(exc)[:300]}'
            notes.append(f'{cap}: Semantic View not created ({type(exc).__name__})')
    for cap, base in plans.items():
        try:
            rec = _create_semantic(run, cap, base)
            if rec['verified']:
                made.append(rec['name'].split('.')[-1])
            else:
                notes.append(f'{cap}: view not equal to the relationship model, dashboard keeps the model query')
        except Exception as exc:
            run.setdefault('semantic_errors', {})[cap] = f'{type(exc).__name__}: {str(exc)[:300]}'
            notes.append(f'{cap}: Semantic View not created ({type(exc).__name__})')
    detail = (f"Semantic View {', '.join(made)} created and verified equal; dashboards read through it"
              if made else 'Semantic View not attached')
    if asks:
        detail += f"; Ask-the-data view {', '.join(asks)}"
    if not made and asks:
        detail = f"Semantic View {', '.join(asks)} created and verified equal (Ask the data)"
    return ('done' if (made or asks) and not notes else 'review'), '; '.join([detail] + notes + info)


def _auto_cortex_model(run):
    """Lab: Cortex designs the semantic model during the run; used automatically only if proven equal."""
    import v3_cortex_model as CM
    import v3_semantic as SV
    if not _runtime_sf() or backend._active_session() is None:
        return False, 'Runs in the hosted Snowflake app'
    out = []
    for cap, plan in _relationship_plans(run).items():
        base = SV.base_plan(plan)
        code_rec = (run.get('semantic') or {}).get(cap)
        try:
            import v3_sv_shared as SH
            rec = SH.cortex_variant(backend._active_session(), run, base, run['result']['ir'], cap, code_rec)
        except Exception as exc:
            out.append((False, f'{cap}: Cortex design not created ({type(exc).__name__}); dashboard keeps the code-built view'))
            continue
        run.setdefault('cortex_models', {})[cap] = rec
        cmp = rec['relationships_vs_tableau']
        if rec['verified'] and rec.get('reused'):
            run['bindings'][cap] = SH.attach(base, rec)
            for row in run.get('binding_rows') or []:
                if row.get('Datasource') == cap:
                    row.update({'Binding': 'Cortex-designed Semantic View', 'Snowflake objects': rec['name'],
                                'Status': 'Existing Cortex-designed shared view reused; verified equal to the Tableau relationship model'})
            out.append((True, f"{cap}: existing Cortex-designed shared view reused (no new Cortex call); verified equal; "
                              'dashboard and Ask the data read through it'))
        elif rec['verified']:
            run['bindings'][cap] = SH.attach(base, rec)
            for row in run.get('binding_rows') or []:
                if row.get('Datasource') == cap:
                    row.update({'Binding': 'Cortex-designed Semantic View', 'Snowflake objects': rec['name'],
                                'Status': 'Designed by Cortex; verified equal to the Tableau relationship model'})
            out.append((True, f"{cap}: Cortex designed the model ({cmp['same']} of {cmp['authored']} relationships "
                              f"match Tableau, {len(rec['model'].get('metrics') or [])} metrics); verified equal; "
                              'dashboard and Ask the data read through it'))
        else:
            out.append((False, f"{cap}: Cortex design not equal to the Tableau model; dashboard keeps "
                               f"{'the code-built view' if code_rec and code_rec.get('verified') else 'the model query'}"))
    if not out:
        return True, 'No relationship datasource; not needed'
    return all(ok for ok, _t in out), '; '.join(t for _o, t in out)


def _auto_cortex_calcs(run):
    """Lab: Cortex translates the original formulas during the run; code compares with the engine."""
    import v3_cortex_calc as CC
    from v3_model_binding import compile_query
    if not _runtime_sf() or backend._active_session() is None:
        return False, 'Runs in the hosted Snowflake app'
    ir = run['result']['ir']
    used, formulas, binding = _calc_inputs(run, ir)
    if not used or not binding:
        return True, 'No calculations on the migrated sheets'
    try:
        run['calc_xl'] = out = CC.run_check(backend._active_session(), run, used, formulas, binding, compile_query, ir)
    except Exception as exc:
        return False, f'Not run ({type(exc).__name__}); the engine translation is unaffected'
    sc = out['score']
    return sc['Disagree'] == 0, (f"Cortex translated the Tableau formulas independently: {sc['Agree']} of "
                                 f"{sc['Compared']} compared agree with the engine"
                                 + (f", {sc['Disagree']} disagree (listed for review)" if sc['Disagree'] else '')
                                 + '; details on Inventory, Calculations')


def _semantic_badge(run):
    names = [p['semantic_view'] for p in _relationship_plans(run).values() if p.get('semantic_view')]
    if names:
        st.markdown('<div class="v2-auto"><div><b>Data source: Snowflake Semantic View</b><span>'
                    + esc(', '.join(names)) + ' — every sheet on this dashboard queries the Semantic View '
                    '(verified identical to the authored relationship model).</span></div></div>',
                    unsafe_allow_html=True)


def _usage_tiles(rows):
    """Tokens and cost of this run's Cortex calls (Snowflake-reported where available)."""
    import v3_usage
    t = v3_usage.totals(rows)
    if not t['calls'] and not t['analyst_messages']:
        return
    st.markdown('<div class="v2-summary">'
                f'<div><span>Tokens in</span><b>{t["input"]:,}</b></div>'
                f'<div><span>Tokens out</span><b>{t["output"]:,}</b></div>'
                f'<div><span>AI credits</span><b>{t["credits"]:.4f}</b></div>'
                f'<div><span>AI cost (USD)</span><b>${t["usd"]:.2f}</b></div>'
                f'<div><span>Ask the data</span><b>{t["analyst_messages"]} · ${t["analyst_usd"]:.2f}</b></div>'
                '</div>', unsafe_allow_html=True)
    note = v3_usage.RATE_NOTE
    if t['estimated_calls']:
        note += f' {t["estimated_calls"]} vision call(s) are estimates: image tokens are not reported by that call.'
    st.caption(note)


def _stage_costs(run, rows):
    """EN03 / CF04: cost per stage and the run's ceilings."""
    import v3_ai
    import v3_usage
    per = v3_usage.stage_totals(rows)
    if per:
        st.dataframe(per, hide_index=True, use_container_width=True)
    lim, u = v3_ai.limits(run), v3_ai.used(run)
    st.caption(f"Ceilings for this run: {u['tokens']:,} of {lim['tokens']:,} tokens, {u['credits']:.4f} of "
               f"{lim['credits']:g} AI credits (V3_CORTEX_MAX_TOKENS_PER_RUN, V3_CORTEX_MAX_CREDITS_PER_RUN). "
               f"Model per job: light jobs use {v3_ai.model_for('Report', 'Executive summary')}, the rest "
               f"{v3_ai.model_for('Parsing', 'calculation')}.")
    gone = v3_ai.unavailable_models(run)              # EN32: retired / not offered models, and what was used
    if gone:
        used = sorted({r.get('Model') for r in rows if r.get('Status') == 'Completed' and r.get('Model') not in gone})
        st.warning('Cortex model(s) refused by Snowflake in this run: ' + ', '.join(gone) + '. Used instead: '
                   + (', '.join(m for m in used if m) or 'none') + '. Set V3_CORTEX_MODEL / V3_CORTEX_MODEL_LIGHT / '
                   'V3_CORTEX_FALLBACK_MODELS to models your region offers.')


def _agent_section():
    """AG01 / EN06 / AG02: the migration agent over read-only procedures (setup SQL for the owner; ask it here)."""
    import v3_agent_tools as AT
    with st.expander('Migration agent in Snowflake (AG01 / AG02): assess and report only'):
        st.caption('Three read-only procedures (assess one workbook, assess a batch, run status) and a Cortex Agent '
                   'that can only call them. Nothing deploys, loads data or applies a fix from the agent: those stay '
                   'buttons for a person here. Upload workbooks to @WBR_DB.V3_AGENT.WORKBOOKS.')
        z = st.text_input('Stage path of this release\'s candidate.zip (owner uploads it once per release)',
                          key='v3c-ag-zip', placeholder='@WBR_DB.V3_AGENT.RELEASES/candidate.zip')
        if z.strip():
            try:
                st.code('\n\n'.join(AT.setup_sql(z.strip()) + AT.agent_setup_sql()), language='sql')
            except ValueError:
                st.warning('STAGE_PATH_REFUSED: use @DATABASE.SCHEMA.STAGE/path/candidate.zip')
        q = st.text_input('Ask the migration agent', key='v3c-ag-q',
                          placeholder='e.g. Assess @WBR_DB.V3_AGENT.WORKBOOKS/Sales.twbx and list what is not supported')
        if st.button('Ask', disabled=not _runtime_sf() or not q.strip(), key='v3c-ag-go'):
            with st.spinner('The agent is calling the read-only procedures…'):
                st.session_state['v3c-ag-last'] = AT.ask_agent(backend._active_session(), q)
        last = st.session_state.get('v3c-ag-last')
        if last:
            if last['status'] != 'OK':
                st.warning('Agent: ' + last['status'])
            else:
                st.markdown(esc(last['text']).replace('\n', '  \n'))
                st.caption('Tools called: ' + (', '.join(last['tool_calls']) or 'none') +
                           '. Advisory: verdicts come from the procedures\' summaries.')


def _recheck_section(run):
    """CC03: record a source baseline for this migration; a scheduled task reports changed sources."""
    import datetime
    import v3_deploy
    import v3_recheck as RC
    tables = RC.tables((run or {}).get('bindings'))
    with st.expander('Scheduled re-check of the deployed app (CC03): source drift'):
        st.caption('Records the row count and HASH_AGG(*) of every Snowflake table this app reads. A daily task '
                   'recomputes them; a changed source means: re-validate the app against Tableau here.')
        z = st.text_input('Stage path of this release\'s candidate.zip', key='v3c-rc-zip',
                          placeholder='@WBR_DB.V3_AGENT.RELEASES/candidate.zip')
        if z.strip():
            try:
                st.code('\n\n'.join(RC.setup_sql(z.strip())), language='sql')
            except ValueError:
                st.warning('STAGE_PATH_REFUSED')
        try:
            app = v3_deploy.app_identifier((run or {}).get('name') or 'APP')
        except ValueError:
            app = None
        if run and run.get('result') and tables and app:
            st.caption(f'{len(tables)} table(s): ' + ', '.join(tables))
            if st.button(f'Record the baseline for {app}', disabled=not _runtime_sf(), key='v3c-rc-base'):
                who = v3_store.viewer()[0] or 'Reviewer in the console'
                v3_store.approve(run, 'recheck_baseline', app, {'tables': tables})
                run['cc03'] = RC.record_baseline(backend._active_session(), app, run.get('bindings'), approved_by=who)
                run['cc03']['at'] = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
            if run.get('cc03'):
                st.caption(f"Baseline: {run['cc03']['status']}, {run['cc03']['tables']} table(s) at {run['cc03']['at']}")
        if _runtime_sf():
            rows, status = RC.latest(backend._active_session(), 50)
            if rows:
                st.dataframe(rows, hide_index=True, use_container_width=True)


def _spend_section():
    """EN33: spend read-only from ACCOUNT_USAGE (where the role may read it) + monitor SQL for an account admin."""
    import v3_spend as SP
    with st.expander('Warehouse and Cortex spend (EN33)'):
        if _runtime_sf() and st.button('Show the last 30 days', key='v3c-spend'):
            st.session_state['v3c-spend-last'] = SP.spend(backend._active_session(), 30)
        last = st.session_state.get('v3c-spend-last')
        if last:
            if last['status'] != 'OK':
                st.caption('Spend: ' + last['status'] + ' (ACCOUNT_USAGE needs a role an admin grants).')
            else:
                st.dataframe(last['by_day'], hide_index=True, use_container_width=True)
                if last.get('cortex'):
                    st.dataframe(last['cortex'], hide_index=True, use_container_width=True)
                elif last.get('cortex_status'):
                    st.caption('Cortex by function: ' + last['cortex_status'])
        q = st.number_input('Monthly credit quota for the app warehouse', min_value=1, value=100, step=10,
                            key='v3c-spend-quota')
        st.caption('Resource monitors need ACCOUNTADMIN: give this SQL to an account admin.')
        st.code('\n'.join(SP.admin_sql('POWERHOUSE', int(q))), language='sql')


def _accuracy_section():
    """EN05 / AI08: Cortex accuracy on the fixed seeded set, per release, with the regression gate."""
    import v3_accuracy as AC
    with st.expander('Cortex accuracy per release (EN05): seeded causes and translations'):
        st.caption(f'{len(AC.CAUSE_SEEDS)} seeded differences (one per cause hypothesis, asked twice) and '
                   f'{len(AC.TRANSLATION_SEEDS)} seeded translations compared by code over inline rows. About '
                   f'{2 * len(AC.CAUSE_SEEDS) + len(AC.TRANSLATION_SEEDS)} Cortex calls. A drop of more than 10 points '
                   'against the previous release is a regression.')
        label = st.text_input('Release label', key='v3c-acc-release', placeholder='e.g. release38')
        session = backend._active_session() if _runtime_sf() else None
        hist, status = AC.history(session) if session is not None else ([], 'NO_SNOWFLAKE_SESSION')
        if status.startswith('NO_ACCURACY_TABLE'):
            st.caption('One-time SQL for an owner (results are kept per release):')
            st.code('\n'.join(AC.setup_sql()), language='sql')
        if st.button('Run the accuracy check', disabled=session is None or not label.strip(), key='v3c-acc-run'):
            with st.spinner('Cortex is answering the seeded set; code is scoring…'):
                row = AC.run(session, label.strip())
            g = AC.gate(row, next((h for h in hist if h['release'] != row['release']), None))
            row['save'] = AC.save(session, row) if not status.startswith('NO_') else status
            st.session_state['v3c-acc-last'] = (row, g)
        last = st.session_state.get('v3c-acc-last')
        if last:
            row, g = last
            st.markdown(f"**{g['status']}** · top-1 {row['cause_top1']:.0%} · top-3 {row['cause_top3']:.0%} · "
                        f"translations {row['translation_agree']:.0%} · repeatable {row['repeatable']:.0%} · "
                        f"{row['calls']} calls, {row['credits']:.4f} AI credits"
                        + (' · ' + '; '.join(g['reasons']) if g['reasons'] else ''))
            st.dataframe(row['cause_rows'], hide_index=True, use_container_width=True)
            st.dataframe(row['translation_rows'], hide_index=True, use_container_width=True)
        if hist:
            st.dataframe(hist, hide_index=True, use_container_width=True)


def _memory(run):
    """TM01 / EN02: proven translations and fixes (loaded once per console session; codes only)."""
    import v3_memory
    if 'tm_memory' not in st.session_state:
        st.session_state['tm_memory'], st.session_state['tm_status'] = v3_memory.Memory.from_table(
            backend._active_session() if _runtime_sf() else None)
    run['_memory'] = st.session_state['tm_memory']
    return run['_memory']


def _scorecard(run):
    """CF01: what Cortex proposed vs what passed code's check (from the run's stored results)."""
    import v3_scorecard
    rows = v3_scorecard.scorecard(run)
    if rows:
        st.markdown('**Cortex contribution scorecard**')
        t = v3_scorecard.totals(rows)
        st.dataframe(rows + [dict(t, Stage='All', Decision='Total')], hide_index=True, use_container_width=True)
        st.caption('Counted from the stored results of each step; Cortex proposes, code checks, Tableau values '
                   'accept. Awaiting evidence = passed the code rules but not yet compared with Tableau.')
    import v3_ai
    rep = v3_ai.repeatability(run)
    if rep:
        st.markdown('**Answer repeatability (EN38)**: the same prompt asked of the same model more than once')
        st.dataframe(rep, hide_index=True, use_container_width=True)


def _analyst_section(run):
    """Same 'Ask the data' tab the deployed app gets (v3_ask), logged as a Cortex call here."""
    import v3_ai
    import v3_ask
    st.subheader('Ask the data — Cortex Analyst on the Semantic View')
    if not v3_ask.semantic_plans(run.get('bindings'), run.get('ask')):
        st.info('Available once the migration has created the Semantic View for the dashboard\'s data '
                '(Data model stage). The deployed dashboard gets the same "Ask the data" tab.')
        return
    st.caption('This is the tab business users get in the deployed dashboard app.')

    def log(status, note):
        entry = v3_ai.record(run, 'Cortex AI', 'Cortex Analyst on the Semantic View', 'Cortex Analyst', status,
                             note=note or '')
        if status == 'Completed':
            import v3_usage
            credits, usd = v3_usage.analyst_cost(1)
            entry.update({'Messages': 1, 'Platform credits': credits, 'Cost USD': usd,
                          'Token source': 'Billed per message'})
    v3_ask.render(backend._active_session(), run.get('bindings'), key='v3c-ask', on_answer=log, ask=run.get('ask'))
    _verified_queries_block(run)
    _ask_agent_setup(run)


def _ask_agent_setup(run):
    """AG03: one-time SQL for an ask agent per Semantic View; Ask the data uses it once it exists."""
    import v3_ask
    views = sorted({p['semantic_view'] for p in v3_ask.semantic_plans(run.get('bindings'), run.get('ask')).values()})
    if not views:
        return
    with st.expander('Ask the data through a Cortex Agent (AG03): one-time SQL for an owner'):
        st.caption('Snowflake recommends Cortex Agents over direct Cortex Analyst calls. Once this agent exists, '
                   'Ask the data goes through it (here and in the deployed app); otherwise it calls Cortex Analyst '
                   'directly. Either way the SQL is shown and run read-only.')
        for v in views:
            try:
                st.code('\n\n'.join(v3_ask.ask_agent_setup_sql(v)), language='sql')
            except ValueError:
                st.caption(f'{v}: name rejected')


def _verified_queries_block(run):
    """CF02: sheets proven equal to Tableau become verified queries of the Semantic View (a person approves)."""
    import v3_sheet_plan as P
    import v3_verified_queries as VQ
    sems = {cap: rec for cap, rec in (run.get('semantic') or {}).items() if rec.get('verified') and rec.get('model')}
    proven = VQ.proven_sheets(run)
    if not sems or not proven:
        return
    st.markdown('**Verified queries from sheets proven equal to Tableau (CF02)**')
    ir = run['result']['ir']
    sheets = {s.get('name'): s for d in ir.get('dashboards', []) for s in d.get('sheets', [])}
    results = run['sheet_reference']['results']
    if st.button('Prepare verified queries', disabled=not _runtime_sf(), key='v3c-vq-prep'):
        session = backend._active_session()
        out = []
        for cap, rec in sems.items():
            for n in proven:
                sheet = sheets.get(n)
                if not sheet or sheet.get('datasource') not in (cap, None):
                    continue
                try:
                    c = VQ.candidate(n, sheet, P.from_sheet(sheet, ir), rec['model'])
                except Exception as exc:                       # codes only
                    c = {'sheet': n, 'state': 'SKIPPED', 'reason': 'PLAN_FAILED:' + type(exc).__name__}
                if c['state'] == 'CANDIDATE':
                    c = VQ.prove(session, c, results[n][1])
                out.append(dict(c, datasource=cap))
        run['verified_queries'] = out
    rows = run.get('verified_queries') or []
    if not rows:
        st.caption('Code writes a question and SQL for each proven sheet it can state exactly, then proves it through '
                   "the Semantic View against Tableau's values before offering it.")
        return
    st.dataframe([{'Sheet': r['sheet'], 'State': r['state'], 'Question': r.get('question', ''),
                   'Evidence': r.get('reason', ''), 'SQL': r.get('sql', '')} for r in rows],
                 hide_index=True, use_container_width=True)
    verified = [r for r in rows if r['state'] == 'VERIFIED']
    if verified and st.button(f'Evaluate Ask the data on these {len(verified)} question(s) (CF03)',
                              disabled=not _runtime_sf(), key='v3c-cf03'):
        import v3_analyst_eval as AE
        session = backend._active_session()
        out = {'rows': []}
        for cap in {r['datasource'] for r in verified}:
            view = run['semantic'][cap]['model']['name']
            res = AE.evaluate(session, view, [r for r in verified if r['datasource'] == cap])
            out['rows'] += [dict(x, datasource=cap) for x in res['rows']]
        decided = [x for x in out['rows'] if x['Result'] in ('Correct', 'Incorrect', 'SQL refused')]
        out['sql_correctness'] = (sum(x['Result'] == 'Correct' for x in decided) / len(decided)) if decided else None
        run['cf03'] = out
    if run.get('cf03'):
        sc = run['cf03'].get('sql_correctness')
        st.markdown('**Ask the data vs verified queries (CF03):** ' +
                    (f'{sc:.0%} correct' if sc is not None else 'nothing decided'))
        st.dataframe(run['cf03']['rows'], hide_index=True, use_container_width=True)
        st.caption("Cortex Analyst's SQL ran read-only; its numbers were compared with the Semantic View proof that "
                   "equals Tableau's values.")
    ready = [r for r in rows if r['state'] == 'VERIFIED' and not r.get('added')]
    if ready and st.button(f'Add {len(ready)} verified quer{"y" if len(ready) == 1 else "ies"} to the Semantic View',
                           type='primary', key='v3c-vq-add'):
        who = v3_store.viewer()[0] or 'Reviewer in the console'
        v3_store.approve(run, 'add_verified_queries', ', '.join(r['sheet'] for r in ready)[:500],
                         {'sheets': [r['sheet'] for r in ready]})
        session = backend._active_session()
        for cap in {r['datasource'] for r in ready}:
            rec = run['semantic'][cap]
            mine = [r for r in ready if r['datasource'] == cap]
            have = list(rec['model'].get('verified_queries') or [])
            status, rec['model'] = VQ.apply(session, rec['model'], have + [VQ.entry(r) for r in mine], approved_by=who)
            for r in mine:
                r['added'] = status == 'ADDED'
                r['reason'] = status if status != 'ADDED' else 'added to ' + rec['model']['name']
        st.rerun()


def _render_run(run):
    info, action = st.columns([0.75, 0.25])
    with info:
        st.markdown(f"**{esc(run['name'])}** · {esc(run['origin'])}")
    with action:
        st.button('Start new migration', type='primary', use_container_width=True,
                  on_click=_reset, key='v3c-new-run')
    render_stepper(run['states'])
    with st.expander('Run log', expanded=bool(run.get('failed'))):
        for title, detail, _kind in run['events']:
            st.markdown(f'**{title}** — {detail}')
    saved = v3_store.status(run)
    if saved:
        (st.caption if saved[0] == 'ok' else st.warning)(saved[1])
    if run.get('failed'):
        st.error(f"{run['failed']} failed. " +
                 (run.get('error') or 'See the assessment issues in Validation.'))
        if not run.get('result'):
            return
    result = run['result']
    states = Counter(r['state'] for r in result['assessment']['sheets'])
    st.markdown(
        '<div class="v2-summary">'
        f"<div><span>Dashboards</span><b>{len(result['ir'].get('dashboards', []))}</b></div>"
        f"<div><span>Sheet occurrences</span><b>{len(result['assessment']['sheets'])}</b></div>"
        f"<div><span>Convertible</span><b>{sum(states[s] for s in CONVERTIBLE)}</b></div>"
        f"<div><span>Refused (listed)</span><b>{sum(states[s] for s in REFUSED)}</b></div>"
        '</div>', unsafe_allow_html=True)
    review = result['ir'].get('ai_migration_review')
    if review:
        with st.container(border=True):
            st.markdown('**Cortex migration review** — ' + esc(review.get('title') or
                        review.get('status', '').replace('_', ' ').title()))
            st.write(review.get('reason', ''))
            if review.get('components'):                  # AI01: components attached by code
                st.caption('Concerns: ' + '; '.join(map(str, review['components'][:20])))
            calls = (result['ir'].get('ai_usage') or {}).get('attempted_calls', 0)
            st.caption(f'Cortex calls in the Parsing stage: {calls}. Advisory only: it does not approve '
                       'execution or establish Tableau parity. Every call is listed on the Cortex AI page.')
    if run.get('binding_rows'):
        st.subheader('Snowflake data model')
        st.dataframe(run['binding_rows'], hide_index=True, use_container_width=True)
    if run.get('missing'):
        _explicit_bindings(run)
    elif run.get('result'):
        _semantic_section(run, 'run')
        _single_semantic_section(run, 'run')
    if run.get('package'):
        st.download_button('Download application candidate', run['package']['data'],
                           file_name='v3_workbook_' + run['package']['build'][:12] + '.zip',
                           mime='application/zip', key='v3c-package')
        st.caption('Candidate build ' + run['package']['build'] +
                   '. A package is not a deployment or acceptance.')
    if not run.get('failed'):
        st.success('Migration run complete. Use Preview to open the dashboards and Validation '
                   'to review the evidence.')
    nav = st.columns(5)
    for col, target in zip(nav, ['Inventory', 'Preview', 'Validation', 'Cortex AI', 'Deploy']):
        with col:
            st.button(target, use_container_width=True, key='v3c-go-' + target,
                      on_click=_go, args=(target,))


def _explicit_bindings(run):
    st.warning('Bind these datasources to existing Snowflake tables or views before preview.')
    if not _runtime_sf():
        st.caption('Explicit Snowflake bindings are available in the hosted app.')
        return
    result = run['result']
    _load_cards(run)
    for cap in list(run['missing']):
        key = 'v3c-bind-' + hashlib.sha256((result['digest'] + cap).encode()).hexdigest()[:16]
        note = (run.get('auto_bind_notes') or {}).get(cap)
        if note:
            st.caption(f'{cap}: an existing table was found but did not pass the check — {note}')
        value = (st.text_input(f'Snowflake table/view for {cap}', key=key,
                               placeholder='DATABASE.SCHEMA.OBJECT') or '').strip()
        if value and st.button('Verify binding', key=key + '-verify'):
            try:
                from v3_source_binding import validate_table
                run['bindings'][cap] = validate_table(backend._active_session(), value, _fields_for(result, cap))
                v3_store.approve(run, 'bind_table', cap, {'table': value})
                run['missing'].remove(cap)
                for row in run['binding_rows']:
                    if row['Datasource'] == cap:
                        row.update({'Binding': 'Explicit Snowflake object', 'Snowflake objects': value,
                                    'Status': 'Physical schema and read access verified'})
                if not run['missing']:
                    run['states']['Data model'] = 'done'
                    status, detail, run['package'] = _build(run)
                    run['states']['App build'] = status
                    run['events'].insert(0, ('Bindings completed', detail, 'done'))
                st.rerun()
            except Exception as exc:
                st.error(f'Binding unavailable for {cap}: {exc}')
    plans = run.get('load_plans') or {}
    _mapping_cards(run, [c for c in run['missing'] if not (plans.get(c) or {}).get('ok')])


def _load_cards(run):
    """Release 21a: what can be loaded from the workbook's own data, what needs the laptop loader, and why."""
    import v3_data_load as DL
    plans = run.get('load_plans') or {}
    shown = [c for c in run['missing'] if c in plans]
    if not shown:
        return
    st.markdown('**Load the workbook\'s own data into Snowflake**')
    st.caption(f'Nothing is written until you approve. Each table goes to {DL.LOAD_DB}.{DL.LOAD_SCHEMA} as a new '
               'table tagged with a fingerprint of its source file; the next run finds it again without loading. '
               'A changed file is loaded as a new version (_V2); existing tables are never replaced.')
    key = 'v3c-load-' + run['result']['digest'][:12]
    ready = [c for c in shown if plans[c].get('ok')]
    for cap in shown:
        p = plans[cap]
        with st.container(border=True):
            if p.get('ok') and p.get('mode') == 'entities':
                files = ', '.join(sorted({Path(f['member']).name for f in p.get('files') or []}))
                st.markdown(f"**{esc(cap)}** — {esc(files)} · one table per logical table, shared by every "
                            'workbook with the same data:')
                st.dataframe([{'Logical table': e['caption'], 'Snowflake table': f"{DL.LOAD_DB}.{DL.LOAD_SCHEMA}.{e['target']}",
                               'Rows': e['rows'], 'Columns': e['columns']} for e in p['entities']],
                             hide_index=True, use_container_width=True)
                st.caption('Tables that already hold exactly this data are reused, not loaded again. The relationship '
                           'keys from Tableau join them; a shared Semantic View is then created or reused over them.')
                for note in p.get('notes') or []:
                    st.caption(note)
                if st.button(f'Approve and load — {cap}', key=f'{key}-{cap}', disabled=not run.get('_raw')):
                    _run_loads(run, [cap])
            elif p.get('ok'):
                files = ', '.join(sorted({Path(f['member']).name for f in p.get('files') or []}))
                st.markdown(f"**{esc(cap)}** — {esc(files)} → `{DL.LOAD_DB}.{DL.LOAD_SCHEMA}.{p['target']}` · "
                            f"{p['rows']:,} rows · {p['columns']} columns")
                for note in p.get('notes') or []:
                    st.caption(note)
                if st.button(f'Approve and load — {cap}', key=f'{key}-{cap}', disabled=not run.get('_raw')):
                    _run_loads(run, [cap])
            elif p.get('kind') == 'extract':
                st.markdown(f'**{esc(cap)}** — Tableau extract (.hyper)')
                st.caption('The hosted app cannot decode Tableau extracts. Load this workbook once from a laptop '
                           '(it shows the plan and asks before loading), then run the migration again: the loaded '
                           'table is found automatically.')
                st.code(DL.LAPTOP_COMMAND, language='powershell')
            else:
                st.markdown(f'**{esc(cap)}**')
                st.caption(p.get('reason') or 'Nothing to load from the workbook')
    if len(ready) > 1 and st.button(f'Approve and load all {len(ready)}', type='primary', key=key + '-all',
                                    disabled=not run.get('_raw')):
        _run_loads(run, ready)
    if not run.get('_raw') and ready:
        st.caption('This run was reopened from a saved state, which does not keep the workbook file. '
                   'Run the migration again to load.')


def _mapping_cards(run, caps):
    """Release 21b-B: Cortex proposes the existing Snowflake table for datasources no code route could bind."""
    if not caps or not _runtime_sf():
        return
    recs = run.get('cortex_mappings') or {}
    st.markdown('**Find the data in Snowflake with Cortex**')
    st.caption('Code lists the tables in WBR_DB whose columns look like these fields; Cortex picks one and maps every '
               'field to a column, from those lists only; code checks every column and type. Nothing is bound until '
               'you approve. Renamed columns are bound through a view in WBR_DB.PIPELINE_DEMO; nothing is replaced.')
    key = 'v3c-map-' + run['result']['digest'][:12]
    ask = [c for c in caps if c not in recs]
    if len(ask) > 1 and st.button(f'Ask Cortex for all {len(ask)}', key=key + '-all'):
        _ask_mappings(run, ask)
        st.rerun()
    for cap in caps:
        rec = recs.get(cap)
        with st.container(border=True):
            st.markdown(f'**{esc(cap)}**')
            if not rec:
                if st.button(f'Ask Cortex which Snowflake table holds this data — {cap}', key=f'{key}-{cap}'):
                    _ask_mappings(run, [cap])
                    st.rerun()
                continue
            if not rec.get('table') or rec.get('error'):
                st.caption(rec.get('verdict') or 'No proposal')
                if rec.get('candidates'):
                    st.caption('Candidates looked at: ' + ', '.join(rec['candidates']))
                continue
            st.markdown(f"Cortex proposes `{rec['table']}`" + (f" ({rec['rows']:,} rows)" if rec.get('rows') else '')
                        + (f" — {esc(rec['reason'])}" if rec.get('reason') else ''))
            st.dataframe(rec.get('pairs') or [], hide_index=True, use_container_width=True)
            if not rec.get('ok'):
                st.warning('Not bindable: ' + str(rec.get('verdict')))
                continue
            import v3_cortex_mapping as MAP
            label = (f"Approve: bind to {rec['table']}" if rec.get('identity') else
                     f"Approve: create the mapping view {MAP.DL.LOAD_DB}.{MAP.DL.LOAD_SCHEMA}.{MAP.DL.target_base(cap)}_MAP and bind")
            if st.button(label, type='primary', key=f'{key}-{cap}-apply'):
                try:
                    _apply_mapping(run, cap)
                    st.rerun()
                except Exception as exc:
                    st.error(f'Not bound — {type(exc).__name__}: {str(exc)[:300]}')


def _ask_mappings(run, caps):
    import v3_cortex_mapping as MAP
    session = backend._active_session()
    with st.spinner('Listing candidate tables; Cortex is proposing; code is checking…'):
        for cap in caps:
            try:
                fields = MAP.DL.fields_for(run['result']['ir'], _scope_id(run, cap))
                run.setdefault('cortex_mappings', {})[cap] = MAP.suggest(session, run, cap, fields)
            except Exception as exc:
                run.setdefault('cortex_mappings', {})[cap] = {'caption': cap, 'ok': False,
                                                              'verdict': f'Not checked ({type(exc).__name__})'}


def _scope_id(run, cap):
    ids = [d for d, s in run['result']['ir']['field_scopes'].items() if s['caption'] == cap]
    if len(ids) != 1:
        raise ValueError('Ambiguous datasource identity')
    return ids[0]


def _apply_mapping(run, cap):
    """A person approved Cortex's checked proposal: bind the table or create the mapping view."""
    import v3_cortex_mapping as MAP
    rec = run['cortex_mappings'][cap]
    fields = MAP.DL.fields_for(run['result']['ir'], _scope_id(run, cap))
    v3_store.approve(run, 'map_table', cap, {'table': rec['table'], 'identity': rec.get('identity'),
                                            'pairs': [(p['Field'], p['Column']) for p in rec['pairs']]})
    fqn, action = MAP.apply(backend._active_session(), cap, rec, fields, who=v3_store.viewer()[0])
    rec['bound_to'] = fqn
    _complete_binding(run, cap, fqn, 'Existing Snowflake table (proposed by Cortex, checked by code)',
                      f'{action}; every field mapped with a compatible type; approved')
    run['events'].insert(0, ('Cortex table mapping applied', f'{cap} -> {fqn} ({action})', 'done'))


def _run_loads(run, caps):
    with st.spinner('Loading into Snowflake and checking columns, types and access…'):
        done, errors = _approve_loads(run, caps)
    for e in errors:
        st.error('Not loaded — ' + e)
    if done and not errors:
        st.rerun()


def _calc_cortex(run, ir):
    """Cortex AISQL over the calculations the migrated sheets use (advisory labels and summary)."""
    import v3_ai
    import v3_cortex_calc as CC
    sheets = [s for d in ir.get('dashboards', []) for s in d.get('sheets', [])]
    calcs = ir.get('calcs') or {}
    used = [(n, calcs[n].get('sql')) for n in CC.sheet_calc_names(sheets, list(calcs), run['result'].get('calc_formulas') or {})
            if isinstance(calcs.get(n), dict) and calcs[n].get('sql')]
    if not used:
        return
    if st.button(f'Classify and summarise {min(len(used), 60)} of {len(used)} calculations with Cortex (AI_CLASSIFY + AI_AGG)',
                 disabled=not _runtime_sf(), key='v3c-calc-ai'):
        try:
            with st.spinner('AI_CLASSIFY and AI_AGG are running in Snowflake…'):
                run['calc_ai'] = v3_ai.classify_calculations(backend._active_session(), run, used)
        except Exception as exc:
            st.error(str(exc))
    out = run.get('calc_ai')
    if out:
        with st.container(border=True):
            st.markdown('**Cortex calculation inventory** (AI_CLASSIFY per calculation, AI_AGG summary)')
            if out.get('summary'):
                st.markdown(out['summary'])
            counts = {}
            for r in out['rows']:
                counts[r['Cortex category']] = counts.get(r['Cortex category'], 0) + 1
            st.dataframe([{'Category': k, 'Calculations': v} for k, v in sorted(counts.items(), key=lambda p: -p[1])],
                         hide_index=True, use_container_width=True)
            with st.expander('Every calculation and its label'):
                st.dataframe(out['rows'], hide_index=True, use_container_width=True)
            st.caption('Advisory labels. Translations, support states and verdicts come from code.')


FEATURE = {'AI_CLASSIFY': 'AI_CLASSIFY', 'AI_AGG': 'AI_AGG', 'claude-opus-5': 'AI_COMPLETE multimodal (TO_FILE)'}


def _cortex_strip(run, steps, title):
    """Inline: the Cortex calls made in these workflow steps, the Snowflake feature, status and cost."""
    import v3_ai
    rows = [r for r in v3_ai.activity(run) if r.get('Step') in steps]
    if not rows:
        return
    view = [{'Step': r.get('Step'), 'What Cortex did': r.get('Purpose'),
             'Snowflake feature': FEATURE.get(str(r.get('Model')), 'AI_COMPLETE') if r.get('Model') else '—',
             'Model': r.get('Model'), 'Status': r.get('Status'),
             'Tokens in/out': (f"{r['Input tokens']:,} / {r.get('Output tokens') or 0:,}"
                               if r.get('Input tokens') is not None else '—'),
             'Cost USD': r.get('Cost USD')} for r in rows]
    with st.expander(f'{title}: {len(view)} call(s)', expanded=False):
        st.dataframe(view, hide_index=True, use_container_width=True)
        st.caption('Cortex proposes, code verifies. No call here changes a verdict, binding or value.')


def _calc_inputs(run, ir):
    """(calculations used by migrated sheets, original formulas, binding of their main datasource)."""
    import v3_cortex_calc as CC
    formulas = run['result'].get('calc_formulas') or {}
    sheets = [s for d in ir.get('dashboards', []) for s in d.get('sheets', [])]
    calcs = ir.get('calcs') or {}
    used = [(n, calcs[n].get('sql')) for n in CC.sheet_calc_names(sheets, list(calcs), formulas)
            if isinstance(calcs.get(n), dict)]
    caps = [s.get('datasource') for s in sheets if s.get('datasource') in (run.get('bindings') or {})]
    cap = max(set(caps), key=caps.count) if caps else next(iter(run.get('bindings') or {}), None)
    return used, formulas, (run.get('bindings') or {}).get(cap)


def _cortex_calc_block(run, ir):
    """Cortex translates the original Tableau formulas independently; code validates and compares (lab)."""
    import v3_cortex_calc as CC
    from v3_model_binding import compile_query
    used, formulas, binding = _calc_inputs(run, ir)
    if not used or not binding:
        return
    st.markdown('**Cortex calculation translation** (second, independent translator; code is the referee)')
    st.caption("Cortex translates each original Tableau formula without seeing the engine's translation. Code "
               'checks every expression against an allowlist, then one read-only query per calculation evaluates '
               'both translations on the same Snowflake data and compares them. Parameters are compared at their '
               'default values; nested calculations are sent with the formulas they reference; constants and '
               'duplicate formulas are left out; up to 60 calculations per run, translated automatically during '
               'the migration run.')
    if st.button(('Re-run Cortex translation' if run.get('calc_xl') else 'Translate with Cortex and compare')
                 + f' ({len(used)} calculations on the migrated sheets)',
                 disabled=not _runtime_sf(), key='v3c-calc-xl'):
        try:
            with st.spinner('Cortex is translating; code is validating and comparing on Snowflake…'):
                run['calc_xl'] = CC.run_check(backend._active_session(), run, used, formulas, binding, compile_query, ir)
        except Exception as exc:
            st.error(f'Cortex translation check failed: {type(exc).__name__}: {str(exc)[:300]}')
    out = run.get('calc_xl')
    if not out:
        return
    sc = out['score']
    with st.container(border=True):
        cols = st.columns(5)
        for col, (lbl, val) in zip(cols, (('Compared', f"{sc['Compared']} of {sc['Eligible']}"), ('Cortex agrees with engine', sc['Agree']),
                                          ('Disagree', sc['Disagree']),
                                          ('Rejected or declined', sc['Rejected by allowlist'] + sc['Cortex declined']),
                                          ('Not comparable yet', sc['Not comparable'] + sc['Query failed']))):
            col.metric(lbl, val)
        order = {'Disagree': 0, 'Agree': 1, 'Rejected by allowlist': 2, 'Cortex declined': 3, 'Query failed': 4}
        if out.get('reasons'):
            with st.expander('Why the others were not compared'):
                st.dataframe(out['reasons'], hide_index=True, use_container_width=True)
        st.dataframe(sorted(out['rows'], key=lambda r: order.get(r['Result'], 9)), hide_index=True,
                     use_container_width=True)
        st.caption('Agreement means two independent translations give the same values on this data. A disagreement '
                   'is either a Cortex error or an engine bug, and needs a person. Nothing here changes the app.')


def _cf05_block(run, ir):
    """CF05: Cortex is the FIRST translator of every calculation the sheets use; code is the referee (the engine
    where it translates, Tableau's values where the rules refuse). Applying needs a person's approval."""
    import datetime
    import v3_cortex_calc as CC
    import v3_first_translator as FT
    import cx3_calc as X3
    from v3_model_binding import compile_query
    items = FT.calculations(ir)
    if not items:
        return
    refused = sum(1 for i in items if not i['engine_sql'])
    st.markdown(f'**Cortex first translation (CF05)** · {len(items)} calculation(s) used, {refused} refused by the rules')
    st.caption('Cortex translates every original formula first. Code checks each expression against the allowlist; '
               "where the rules translate it too, both run on the same rows in one read-only query (the engine is "
               "the referee and is never replaced); where the rules refuse it, the translation is accepted only "
               "when every captured sheet using it equals Tableau's values. Applying needs your approval.")
    _used, _formulas, binding = _calc_inputs(run, ir)
    if st.button('Translate with Cortex and referee', disabled=not _runtime_sf() or not binding, key='v3c-cf05'):
        session = backend._active_session()
        try:
            rel, columns, _map = CC.relation(binding, compile_query)
            if columns is None:
                columns = list(session.sql(f'SELECT * FROM {rel} LIMIT 0').columns)
            verify = ((lambda fid, sql: X3.verify_tableau(run, run.get('capture_bytes'), fid, sql, snowflake=True))
                      if run.get('capture_bytes') else None)
            with st.spinner('Cortex is translating; code is refereeing…'):
                run['cf05'] = {'rows': FT.review(ir, columns, FT.cortex_ask(session, run),
                                                 engine_check=FT.engine_referee(session, rel), verify=verify,
                                                 memory=_memory(run)),
                               'with_tableau': bool(verify)}
        except Exception as exc:                          # codes only
            run['cf05'] = {'rows': [], 'error': 'CF05_FAILED:' + type(exc).__name__}
    out = run.get('cf05')
    if not out:
        return
    if out.get('error'):
        st.warning('CF05 did not run: ' + out['error'])
        return
    counts = Counter(r['state'] for r in out['rows'])
    st.markdown(' · '.join(f'{k} {counts[k]}' for k in FT.STATES if counts.get(k)) +
                ('' if out['with_tableau'] else ' · no Tableau values loaded: refused calculations stay PROPOSED'))
    st.dataframe([{'Calculation': r['caption'], 'Rules': r['engine'], 'State': r['state'],
                   'Cortex expression (allowlist-checked)': r.get('sql', ''), 'Evidence': r.get('reason', ''),
                   'Tableau formula': r['formula'][:160]} for r in out['rows']],
                 hide_index=True, use_container_width=True)
    ready = [r for r in out['rows'] if r['state'] == 'VERIFIED' and not r.get('applied')]
    if ready and st.button(f'Apply {len(ready)} Tableau-verified translation(s)', type='primary', key='v3c-cf05-apply'):
        who = v3_store.viewer()[0] or 'Reviewer in the console'
        at = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        approval = v3_store.approve(run, 'apply_cf05_translations', ', '.join(r['caption'] for r in ready)[:500],
                                    {'calculations': [r['caption'] for r in ready]})
        new = ir
        import v3_memory
        for r in ready:
            new = FT.apply(new, r, approved_by=who, at=at)
            r['applied'] = True
            r['memory'] = v3_memory.save(backend._active_session(), 'cf05', r.get('formula_full') or r['formula'],
                                         {'sql_expression': r['sql'], 'level': r.get('level') or (
                                             'aggregate' if CC._is_aggregate(r['sql']) else 'row')},
                                         caption=r['caption'], workbook=run.get('name', ''),
                                         evidence=r.get('evidence'), run_id=run.get('run_id', ''), proven=True)
        run['result']['ir'] = new
        run.setdefault('applied_cf05', []).append({'At': at, 'By': who, 'Calculations': len(ready),
                                                   'Approval': approval or 'not recorded (store off)'})
        if not run.get('missing'):
            status, detail, run['package'] = _build(run)
            run['events'].insert(0, ('Cortex translations applied', f'{len(ready)} calculation(s); {detail}', 'done'))
        st.rerun()
    if run.get('applied_cf05'):
        st.caption('Applied: ' + '; '.join(f"{a['Calculations']} calculation(s) by {a['By']} at {a['At']}"
                                           for a in run['applied_cf05']))
    _memory_note()


def _memory_note():
    import v3_memory
    status = st.session_state.get('tm_status', '')
    if status.startswith('NO_MEMORY_TABLE'):
        with st.expander('Translation memory (TM01): one-time SQL for an owner'):
            st.caption('Keeps translations and fixes proven against Tableau so later workbooks get them as examples. '
                       'Until the table exists, nothing is remembered.')
            st.code('\n'.join(v3_memory.setup_sql()), language='sql')
    elif status == 'OK':
        n = len(st.session_state['tm_memory'].entries)
        st.caption(f'Translation memory: {n} proven entr{"y" if n == 1 else "ies"} given to Cortex as examples.')


def _cx3_block(run, ir):
    """CX3 (release 27): Cortex proposes typed plans for LOD and table calculations; code compiles them and
    accepts one only when the sheets using it equal Tableau's captured values. Nothing is applied here."""
    import cx2_safety as S
    import cx3_calc as X3
    import v3_cortex_calc as CC
    from v3_model_binding import compile_query
    hard = X3.hard_calcs(ir)
    if not hard:
        return
    st.markdown(f'**Cortex plans for hard calculations (CX3)** · {len(hard)} LOD / table calculation(s)')
    st.caption('Cortex returns a typed plan from closed lists, never SQL; code checks every field and function, '
               'writes the SQL itself and runs it read-only. A plan is VERIFIED only when every sheet that uses '
               'the calculation shows the same values as Tableau (load the Tableau values in Validation first). '
               'Similar plans verified earlier (kept in WBR_DB.CX_CALC.CX3_LIBRARY) are given to Cortex as '
               'examples. INCLUDE / EXCLUDE / nested FIXED are accepted only where one query computes them exactly.')
    _used, _formulas, binding = _calc_inputs(run, ir)
    if st.button('Ask Cortex for plans and verify against Tableau', disabled=not _runtime_sf() or not binding,
                 key='v3c-cx3'):
        session = backend._active_session()
        try:
            _rel, columns, _map = CC.relation(binding, compile_query)
            if columns is None:
                columns = list(session.sql(f'SELECT * FROM {_rel} LIMIT 0').columns)
            if 'cx3_library' not in st.session_state:          # verified translations kept in Snowflake (CX3-02)
                st.session_state['cx3_library'], st.session_state['cx3_library_status'] = X3.Library.from_table(session)
            lib = st.session_state['cx3_library']
            known = {X3._sig(e['formula']) for e in lib.verified()}
            budget = S.Budget(S.DEFAULT_BUDGET_TOKENS)
            verify = ((lambda fid, sql: X3.verify_tableau(run, run.get('capture_bytes'), fid, sql, snowflake=True))
                      if run.get('capture_bytes') else None)
            with st.spinner('Cortex is proposing plans; code is compiling and proving them against Tableau…'):
                run['cx3'] = {'rows': X3.review(ir, columns, X3.cortex_ask(session, run), lib, budget=budget,
                                                session=session, verify=verify),
                              'budget': budget.summary(), 'with_tableau': bool(verify)}
            saved = [X3.save_verified(session, e, run_id=(run.get('_store') or {}).get('run_id', '')) for e in lib.verified()
                     if X3._sig(e['formula']) not in known]
            run['cx3']['library'] = dict(Counter(saved))
            run['cx3']['eval_store'] = S.record_evaluation(session, 'CX3', S.cx3_evaluation_rows(
                (run.get('_store') or {}).get('run_id', ''), run.get('name'), run['cx3']['rows']), [])
        except Exception as exc:                          # codes only
            run['cx3'] = {'rows': [], 'error': 'CX3_FAILED:' + type(exc).__name__}
    out = run.get('cx3')
    if not out:
        return
    if out.get('error'):
        st.warning('CX3 did not run: ' + out['error'])
        return
    counts = Counter(r['state'] for r in out['rows'])
    st.markdown(' · '.join(f'{k} {counts[k]}' for k in X3.STATES if counts.get(k)) +
                f" · tokens used ≈ {out['budget']['used_tokens_est']:,}" +
                ('' if out['with_tableau'] else ' · no Tableau values loaded: proposals stay PROPOSED'))
    st.dataframe([{'Calculation': r['caption'], 'Kind': r['kind'], 'State': r['state'],
                   'Plan SQL (written by code)': r.get('sql', ''), 'Reason / evidence': (
                       '; '.join(f"{s['sheet']}: {s['status']}" for s in (r.get('evidence') or {}).get('sheets', []))
                       or r.get('reason', ''))[:240], 'Tableau formula': r['formula'][:160]} for r in out['rows']],
                 hide_index=True, use_container_width=True)
    st.caption('A VERIFIED plan is evidence, not a change: applying it goes through the verified-plan approval.')
    lib_status = st.session_state.get('cx3_library_status', '')
    if out.get('library'):
        st.caption('Verified-translation library: ' + ', '.join(f'{k} {v}' for k, v in out['library'].items()))
    if lib_status.startswith('NO_LIBRARY_TABLE'):
        with st.expander('Keep verified translations across sessions (one-time SQL for an owner to run)'):
            st.code('\n'.join(X3.library_setup_sql()), language='sql')
    with st.expander('Find similar verified translations by meaning (Cortex Search, one-time SQL for an owner)'):
        st.caption('Without the service, similar examples come from AI_EMBED similarity, then word overlap. Search '
                   'results are used only when they match a translation this library holds as VERIFIED.')
        st.code('\n'.join(X3.library_search_setup_sql()), language='sql')


def _intake_block(run):
    """E15 / EN24: how each datasource is read, the snapshot lineage, published sources and source views."""
    import v3_intake
    intake = (run.get('result') or {}).get('intake') or {}
    with st.expander('Sources and intake (E15 / EN24)'):
        if intake.get('policy'):
            st.markdown('**Live, extract and published sources — what the app reads**')
            st.dataframe(intake['policy'], hide_index=True, use_container_width=True)
        m = intake.get('manifest') or {}
        if m:
            st.markdown('**Snapshot lineage** (recorded with this run)')
            st.dataframe([{'Package sha256': m.get('sha256', '')[:16] + '…', 'Workbook XML sha256':
                           m.get('twb_sha256', '')[:16] + '…', 'Members': len(m.get('members') or []),
                           'Tableau revision': (m.get('repository') or {}).get('revision') or '—'}],
                         hide_index=True, use_container_width=True)
            if m.get('extracts'):
                st.dataframe([{'Datasource': e['datasource'], 'Extract': e['file'], 'Refreshed': e.get('refreshed')
                               or '—'} for e in m['extracts']], hide_index=True, use_container_width=True)
        if intake.get('published'):
            st.markdown('**Published datasources** (definition fetched read-only from Tableau)')
            st.dataframe([{'Datasource': p['caption'], 'Published source': p['content_url'], 'Revision':
                           p.get('revision') or '—', 'State': p['state']} for p in intake['published']],
                         hide_index=True, use_container_width=True)
        for kit in intake.get('kits') or []:
            st.markdown(f"**Replication kit — {esc(kit['Datasource'])}** ({esc(kit['Source'])} → "
                        f"`{kit['Landing table']}`)")
            for i, step in enumerate(kit['steps'], 1):
                st.caption(f'{i}. {step}')
            st.code(kit['ddl'] + ';', language='sql')
        plans = {cap: plan for cap, plan in (run.get('bindings') or {}).items() if isinstance(plan, dict)}
        if not plans:
            st.caption('No custom SQL, physical join or relationship source to turn into a view.')
            return
        st.markdown('**Source views (EN24)** — custom SQL, joins and relationship models as shared Snowflake views')
        schema = st.text_input('Schema for the views (DATABASE.SCHEMA)', value='WBR_DB.V3_SOURCES', key='en24-schema')
        try:
            ddl = [v3_intake.view_sql(plan, cap, schema) for cap, plan in plans.items()]
        except ValueError:
            st.warning('Schema rejected: use DATABASE.SCHEMA.')
            return
        st.caption('One-time SQL for an owner (never OR REPLACE). Each view is the verified read-only query the app '
                   'already uses; binding to it changes no value.')
        st.code(f'CREATE SCHEMA IF NOT EXISTS {schema.upper()};\n' + ';\n'.join(ddl) + ';', language='sql')
        if st.button('Bind to the views', key='en24-bind', disabled=_runtime_sf() is None):
            session, ir, rows = _runtime_sf(), run['result']['ir'], []
            for cap, plan in plans.items():
                fields = [f for f in ir['fields'].values() if f['datasource_id'] == plan.get('datasource_id')]
                out = v3_intake.bind_view(session, v3_intake.view_name(plan, cap, schema), fields)
                if out['state'] == 'BOUND':
                    run['bindings'][cap] = out['table']
                rows.append({'Datasource': cap, 'View': v3_intake.view_name(plan, cap, schema), 'State': out['state']})
            st.dataframe(rows, hide_index=True, use_container_width=True)


def page_inventory():
    run = _require_run('Inventory', 'Everything the workbook contains, and how each item is handled.')
    if not run:
        return
    result, ir = run['result'], run['result']['ir']
    _cortex_strip(run, ('Parsing', 'Data model'), 'Cortex in Parsing and Data model')
    tabs = st.tabs(['Dashboards', 'Data model', 'Calculations', 'Filters & parameters',
                    'Interactions', 'Cortex review'])
    with tabs[0]:
        st.dataframe(result['assessment']['sheets'], hide_index=True, use_container_width=True)
        st.caption('UNVERIFIED means a supported contract exists and the sheet renders; numeric and '
                   'visual parity with Tableau are still to be verified. Refused sheets list their reason.')
    with tabs[1]:
        st.dataframe((run.get('discovery') or {}).get('datasources') or [{'Datasource': 'None found'}],
                     hide_index=True, use_container_width=True)
        if run.get('binding_rows'):
            st.dataframe(run['binding_rows'], hide_index=True, use_container_width=True)
        for cap, plan in (run.get('bindings') or {}).items():
            if isinstance(plan, dict) and plan.get('kind') == 'custom-sql':
                with st.expander(f'Source detail — {cap}'):
                    st.code(str(plan.get('sql') or ''), language='sql')        # the author's own custom SQL
                    st.caption('Read as one read-only SELECT; columns and types verified.')
            elif isinstance(plan, dict) and plan.get('keys'):
                with st.expander(f'Relationship detail — {cap}'):
                    tabs_ = plan.get('tables') or {}
                    st.dataframe([{'From': (tabs_.get(k.get('first')) or {}).get('table', k.get('first')),
                                   'To': (tabs_.get(k.get('second')) or {}).get('table', k.get('second')),
                                   'Left key': k.get('left'), 'Right key': k.get('right'),
                                   'Read as': 'one row per key' if (tabs_.get(k.get('second')) or {}).get('distinct')
                                   else 'table'}
                                  for k in plan.get('keys', [])],
                                 hide_index=True, use_container_width=True)
                    st.caption('Root (fact) table: ' + str((tabs_.get(plan.get('root')) or {}).get('table', plan.get('root'))) +
                               '. Joined read-only (unmatched keys kept, as Tableau); fact row count preserved.')
        if result.get('source_reports'):
            with st.expander('Datasource resolution evidence'):
                st.dataframe(result['source_reports'], hide_index=True, use_container_width=True)
        _intake_block(run)
    with tabs[2]:
        rows = [{'Calculation': name, 'Translated SQL': (c.get('sql') or '')[:200],
                 'Aggregate-ready': c.get('agg_ready')}
                for name, c in (ir.get('calcs') or {}).items() if isinstance(c, dict)]
        st.dataframe(rows or [{'Calculation': 'No calculations'}], hide_index=True,
                     use_container_width=True)
        _calc_cortex(run, ir)
        _cf05_block(run, ir)
        _cortex_calc_block(run, ir)
        _cx3_block(run, ir)
        if result['assessment']['untranslated_calculations']:
            st.warning('Not translated (refused, never substituted): ' +
                       ', '.join(map(str, result['assessment']['untranslated_calculations'])))
    with tabs[3]:
        filters = [{'Dashboard': d.get('name'), 'Sheet': s.get('name'),
                    'Filters': len(s.get('filters') or [])}
                   for d in ir.get('dashboards', []) for s in d.get('sheets', []) if s.get('filters')]
        st.dataframe(filters or [{'Filter': 'No sheet filters detected'}], hide_index=True,
                     use_container_width=True)
        params = [{'Parameter': k, 'Default': str(v)} for k, v in (ir.get('params') or {}).items()]
        st.dataframe(params or [{'Parameter': 'No parameters'}], hide_index=True,
                     use_container_width=True)
    with tabs[4]:
        rows = [{'Dashboard': d['name'], 'Source': a.get('source'), 'Status': a.get('status'),
                 'Note': a.get('reason', 'Selection and clear available; interaction acceptance pending')}
                for d in ir.get('dashboards', []) for a in d.get('actions', [])]
        rows += [{'Dashboard': d['name'], 'Source': s['name'], 'Status': a.get('status'),
                  'Note': a.get('reason', 'Parameter assignment available; interaction acceptance pending')}
                 for d in ir.get('dashboards', []) for s in d.get('sheets', [])
                 for a in s.get('parameter_actions', [])]
        st.dataframe(rows or [{'Interaction': 'No dashboard actions'}], hide_index=True,
                     use_container_width=True)
        st.caption('Chart selections use explicit controls where hosted Streamlit cannot select '
                   'layered charts.')
    with tabs[5]:
        review = ir.get('ai_migration_review')
        if review:
            st.write('**' + (review.get('title') or review.get('status', '')) + '**')
            st.write(review.get('reason', ''))
            if review.get('components'):                  # AI01: components attached by code
                st.caption('Concerns: ' + '; '.join(map(str, review['components'][:20])))
        else:
            st.info('No Cortex review was produced for this run (Cortex unavailable or budget exhausted).')
        st.caption('All Cortex activity for this run, by workflow step, is on the Cortex AI page.')
        st.caption('Actual Cortex calls: ' + str((ir.get('ai_usage') or {}).get('attempted_calls', 0)))
        if ir.get('ai_assistance'):
            st.dataframe(ir['ai_assistance'], hide_index=True, use_container_width=True)
        if ir.get('ai_include_plans'):
            st.dataframe([{k: r.get(k) for k in ('dashboard', 'sheet', 'status', 'reason')}
                          for r in ir['ai_include_plans']], hide_index=True, use_container_width=True)
    st.download_button('Download assessment', json.dumps(result['assessment'], indent=2),
                       file_name='v3_assessment.json', mime='application/json',
                       key='v3c-dl-assessment')
    import cx8_documents                    # CX8 (release 27): requirements from supporting documents, advisory
    cx8_documents.render(run, st, backend._active_session())


def _dashboard_navigation(dashboards, digest):
    # Selected-view execution: only the chosen dashboard issues queries on a rerun.
    engine.DASH_NAV = ('v3-dashboard-' + digest, [d.get('name') for d in dashboards])
    with st.container(key='v3_dashboard_navigation'):
        st.markdown("<style>.st-key-v3_dashboard_navigation [role=radiogroup]{gap:18px;border-bottom:1px solid #e5e7eb;}"
                    ".st-key-v3_dashboard_navigation label[data-baseweb=radio]{padding:8px 0;border-bottom:2px solid transparent;}"
                    ".st-key-v3_dashboard_navigation label[data-baseweb=radio]>div:first-child{display:none;}"
                    ".st-key-v3_dashboard_navigation label:has(input:checked){border-bottom-color:#3366ff;}"
                    ".st-key-v3_dashboard_navigation label:has(input:checked) p{color:#3366ff;}"
                    "</style>", unsafe_allow_html=True)
        return st.radio('Dashboard', list(range(len(dashboards))),
                        format_func=lambda i: dashboards[i].get('name') or engine.display_title(dashboards[i].get('title')) or 'Dashboard',
                        horizontal=True, key='v3-dashboard-' + digest, label_visibility='collapsed')


def page_preview():
    run = _require_run('Preview', 'The generated dashboards, running on ' +
                      ('Snowflake data.' if _runtime_sf() else 'bundled local data (development preview).'))
    if not run:
        return
    result = run['result']
    if run.get('missing') or result['assessment']['issues']:
        st.warning('Preview needs every datasource bound and no structural issues. See New migration.')
        return
    try:
        from ir_contract import require_valid_ir
        require_valid_ir(result['ir'])
        with preview_context(result['ir'], run['bindings'], snowflake=_runtime_sf()):
            import parameter_actions
            import dashboard_actions
            dashboard_actions.compact_controls(st)
            _semantic_badge(run)
            st.button('Reset workbook controls', key='v3-reset-' + result['digest'],
                      on_click=parameter_actions.reset, args=(parameter_actions.context(engine),))
            dashboards = result['ir'].get('dashboards', [])
            if dashboards:
                selected = _dashboard_navigation(dashboards, result['digest'])
                engine.render_dashboard(dashboards[selected], presentation='native')
            engine._render_findings()
    except Exception as exc:
        st.error(f'Dashboard unavailable: {type(exc).__name__}: {exc}')


def _reference_check(run):
    ref = run.get('reference')
    if not ref:
        return ('Numeric parity with Tableau', 'Not run',
                'Run "Compare Tableau values" above (Tableau Cloud workbooks)')
    rows = ref['rows']
    matched = sum(1 for r in rows if r['Status'] == 'Match')
    differ = sum(1 for r in rows if r['Status'] == 'Difference found')
    status = ('Matched' if matched and not differ else 'Differences found' if differ else 'Not comparable')
    return ('Numeric parity with Tableau (published views)', status,
            f"{matched} view(s) matched, {differ} with differences, "
            f"{len(rows) - matched - differ} unavailable or not comparable; default state, {ref['at']}")


def _capture_check(run):
    sref = run.get('sheet_reference')
    if not sref:
        return ('Per-sheet parity with Tableau (Embedding API)', 'Not run', 'Load the Tableau reference values and run validation')
    res = [r[0] for r in sref['results'].values()]
    ok = sum(1 for r in res if r['Status'].startswith('Match'))
    diff = sum(1 for r in res if r['Status'] == 'Difference found')
    values = sum(r.get('Matched values', 0) for r in res)
    return ('Per-sheet parity with Tableau (Embedding API)', 'Differences found' if diff else 'Matched' if ok else 'Not comparable',
            f'{ok} of {len(res)} sheets match ({values} values), {diff} with differences')


def _sheet_check(run):
    rows = run.get('sheet_checks')
    if not rows:
        return ('Sheet-level rendering on data', 'Not run', 'Run "Sheet checks" above')
    ok = sum(1 for r in rows if r['Renders on data'].startswith('Yes'))
    err = sum(1 for r in rows if r['Renders on data'] == 'Error')
    refused = sum(1 for r in rows if r['Renders on data'] == 'Refused')
    return ('Sheet-level rendering on data', 'Errors found' if err else 'Complete',
            f'{ok} of {len(rows)} sheets render on the bound data, {err} errors, {refused} refused; '
            f"default state, {run.get('sheet_checks_at')}")


def _visual_check(run):
    visual = run.get('visual')
    if not visual:
        return ('Visual comparison (Tableau image vs app image)', 'Not run', 'Run "Compare images" above')
    from v3_evidence import VERDICT_LABEL
    labels = Counter(VERDICT_LABEL.get(v.get('verdict'), str(v.get('verdict'))) for v in visual.values())
    return ('Visual comparison (Tableau image vs app image)', 'Reviewed by Cortex (advisory)'
            if any(v.get('verdict') for v in visual.values()) else 'Images only',
            ', '.join(f'{n} {k.lower()}' for k, n in labels.items()) + '; a person decides visual parity')


def _can_compare(result, run):
    return bool(result.get('cloud_workbook_id')) and not run.get('missing') and not result['assessment']['issues']


def _run_reference(run):
    import datetime
    import parameter_actions
    from v3_reference import compare_view, headless_capture
    result = run['result']
    views = TS.pull_all_view_csvs(result['cloud_server_url'], result['cloud_site_content_url'],
                                  result['cloud_workbook_id'])
    rows, details = [], {}
    from v3_evidence import offscreen
    with preview_context(result['ir'], run['bindings'], snowflake=_runtime_sf()), offscreen():
        parameter_actions.reset(parameter_actions.context(engine))
        for view in views:
            row, detail = compare_view(result['ir'], view, headless_capture)
            rows.append(row)
            details[row['View']] = detail
    run['reference'] = {'rows': rows, 'details': details,
                        'at': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
    from v3_evidence import merge_statuses
    merge_statuses(run)


def _capture_upload(run):
    """Tableau reference: Tableau's own per-sheet values. Loaded automatically when one ships for this
    workbook (lab); the upload stays available as an override. Kept on the run for every rerun."""
    if run.get('capture_auto'):
        st.success('**Tableau reference loaded automatically** — ' + run['capture_auto'])
    elif run.get('capture_auto_miss'):
        st.info('Tableau reference not loaded automatically: ' + run['capture_auto_miss'] + '. Upload it below.')
    with st.expander('Use a different Tableau reference' if run.get('capture_auto') else
                     'Tableau reference values (sheet capture JSON)', expanded=not run.get('capture_bytes')):
        upload = st.file_uploader('Tableau reference values (sheet capture JSON)', type=['json'],
                                  key='v3c-sheet-capture',
                                  help="Tableau's own computed values for every sheet, captured from the signed-in "
                                       'Tableau view (tools/tableau_sheet_capture.js).')
    if upload is not None:
        digest = hashlib.sha256(upload.getvalue()).hexdigest()
        if run.get('capture_digest') != digest:
            run.update(capture_bytes=upload.getvalue(), capture_name=upload.name, capture_digest=digest)
            run.pop('capture_auto', None)
            run.pop('sheet_reference', None)
    if run.get('capture_name') and not run.get('capture_auto'):
        st.caption(f"Tableau reference loaded: {run['capture_name']}")
    if run.get('validation_timing'):
        with st.expander('Validation steps run by the migration (with timing)'):
            st.dataframe(run['validation_timing'], hide_index=True, use_container_width=True)


def _compare_capture(run):
    if run.get('capture_bytes'):
        _load_sheet_capture(run, run['capture_bytes'], 'file ' + run['capture_name'])


def _validation_actions(run):
    """One button runs the whole validation in order; single steps stay available for re-runs."""
    import v3_evidence as EV
    result = run['result']
    blocked = bool(run.get('missing') or result['assessment']['issues'])
    cloud = _can_compare(result, run)
    st.markdown('<div class="v2-auto"><div><b>How validation works</b><span>'
                '1 · Every sheet is rendered on the Snowflake data (through the Semantic View when one exists). '
                "2 · Its values are compared with Tableau's own values for the same sheet. "
                "3 · The dashboard image is compared with Tableau's image; Cortex vision describes both. "
                '4 · Everything goes into one migration report.</span></div></div>', unsafe_allow_html=True)
    _capture_upload(run)
    cols = st.columns([0.3, 0.7])
    run_all = cols[0].button('Run validation', type='primary', use_container_width=True,
                             disabled=blocked, key='v3c-val-all')
    vision = cols[1].toggle('Use Cortex vision for the image comparison', value=_runtime_sf(), key='v3c-val-vision',
                            help='Cortex (multimodal) describes the Tableau image and the app image and compares '
                                 'them. Advisory; it never changes a value verdict.')
    with st.expander('Run a single step'):
        c = st.columns(3)
        do_sheets = c[0].button('Sheet checks', use_container_width=True, disabled=blocked, key='v3c-val-sheets')
        do_values = c[1].button('Compare Tableau values', use_container_width=True, disabled=blocked,
                                key='v3c-compare-' + result['digest'])
        do_images = c[2].button('Compare images', use_container_width=True, disabled=not cloud,
                                key='v3c-val-images')
    if blocked:
        st.caption('Bind every datasource first (New migration).')
    if run_all or do_sheets or (do_values and not run.get('sheet_values')):
        try:
            with st.spinner('1 · Rendering every sheet on the Snowflake data…'):
                EV.sheet_checks(run, _runtime_sf())
            run.pop('sheet_reference', None)
        except Exception as exc:
            st.error(f'Sheet checks failed: {type(exc).__name__}: {exc}')
    if run_all or do_values:
        try:
            with st.spinner("2 · Comparing every sheet with Tableau's values…"):
                _compare_capture(run)
        except Exception as exc:
            st.error(f'Tableau reference comparison failed: {type(exc).__name__}: {exc}')
        if cloud:
            try:
                with st.spinner('2 · Comparing the published view data (REST)…'):
                    _run_reference(run)
            except Exception as exc:
                st.error(f'Published view comparison failed: {type(exc).__name__}: {exc}')
    elif run.get('capture_bytes') and run.get('sheet_values') and not run.get('sheet_reference'):
        try:
            _compare_capture(run)
        except Exception as exc:
            st.error(f'Tableau comparison failed: {type(exc).__name__}: {exc}')
    if (run_all and cloud) or do_images:
        try:
            with st.spinner('3 · Pulling the Tableau image, rendering the app and asking Cortex vision…'
                            if vision else '3 · Pulling the Tableau image and rendering the app…'):
                EV.visual_compare(run, backend._active_session(), _runtime_sf(), use_cortex=vision)
        except Exception as exc:
            st.error(f'Image comparison failed: {type(exc).__name__}: {exc}')


def _tab_summary(run):
    result = run['result']
    assessment = result['assessment']
    states = Counter(r['state'] for r in assessment['sheets'])
    calls = (result['ir'].get('ai_usage') or {}).get('attempted_calls', 0)
    missing = run.get('missing')
    checks = [
        ('Structural assessment', 'Failed' if assessment['issues'] else 'Complete',
         f"{len(assessment['sheets'])} sheet occurrences inventoried"),
        ('Source binding', 'Needs input' if missing else ('Verified' if _runtime_sf() else 'Local data'),
         ('Missing: ' + ', '.join(missing)) if missing
         else ('Read-only; schema, access and relationship grain checked' if _runtime_sf()
               else 'Bundled extract; Snowflake binding happens in the hosted app')),
        ('Calculation translation',
         'Refusals listed' if assessment['untranslated_calculations'] else 'Complete',
         'Unsupported dependencies are refused, never substituted'),
        ('Cortex structural review', 'Ran' if calls else 'Not run', f'{calls} call(s) at Parsing; advisory only'),
        _sheet_check(run),
        _reference_check(run),
        _capture_check(run),
        _visual_check(run),
        ('Interaction parity', 'Not verified', 'Controls are Streamlit adaptations of Tableau interactions'),
        ('Whole-workbook acceptance', 'Not verified', 'Needs all components verified and reviewed'),
    ]
    st.dataframe([{'Check': c, 'Status': s, 'Note': n} for c, s, n in checks],
                 hide_index=True, use_container_width=True)
    st.subheader('Review queue')
    queue = [r for r in assessment['sheets'] if r['state'] != 'UNVERIFIED']
    if queue:
        st.dataframe(queue, hide_index=True, use_container_width=True)
    else:
        st.success('No refused or partial sheets in this workbook.')
    with st.expander('Sheet support summary'):
        st.write(dict(states))
        coverage = summarize([dict(assessment, file=result['source_name'])])
        if coverage['families']:
            st.dataframe([{k: r[k] for k in ('family', 'sheet_occurrences')} for r in coverage['families']],
                         hide_index=True, use_container_width=True)
        st.download_button('Download coverage diagnostics', json.dumps(coverage, indent=2),
                           file_name='v3_coverage.json', mime='application/json', key='v3c-dl-coverage')
    for error in result.get('intake_errors') or []:
        st.warning(error)
    for issue in assessment['issues']:
        st.error(f"{issue['path']}: {issue['message']}")


def _alt_visual_block(run):
    """RL3 / AI05: an alternate visual for every sheet that fails to draw -- Cortex proposes from a closed menu
    (automatically, once per validation), code checks it draws with the same values; a person applies it."""
    import datetime
    import v3_alt_visual as AV
    failed = AV.targets(run)
    if not failed:
        return
    st.markdown(f'**Alternate visuals for {len(failed)} sheet(s) that did not draw (RL3)**')
    st.caption('Cortex proposes a supported chart for the same fields (it cannot add, drop or re-aggregate a field); '
               'code checks it draws on the sheet\'s own result, else falls back to a table. Values stay the '
               'workbook plan\'s; the drawing is labelled approximated. Nothing changes until you apply it.')
    key = run.get('sheet_checks_at')
    auto = _runtime_sf() and backend._active_session() is not None and run.get('rl3_for') != key
    if auto or st.button('Propose alternate visuals again', key='v3c-rl3'):
        run['rl3_for'] = key
        try:
            with st.spinner('Cortex is proposing alternate visuals; code is drawing them…'):
                run['rl3_rows'] = AV.improve_run(run, _runtime_sf(), AV.cortex_ask(backend._active_session(), run))
        except Exception as exc:                              # codes only
            run['rl3_rows'] = [{'Sheet': '—', 'Result': 'RL3_FAILED:' + type(exc).__name__}]
    if run.get('rl3_rows'):
        st.dataframe(run['rl3_rows'], hide_index=True, use_container_width=True)
    ready = {k: v for k, v in (run.get('rl3') or {}).items()
             if v.get('state') in ('ALTERNATE', 'TABLE_FALLBACK') and not v.get('applied')}
    if ready and st.button(f'Apply {len(ready)} alternate visual(s)', type='primary', key='v3c-rl3-apply'):
        who = v3_store.viewer()[0] or 'Reviewer in the console'
        at = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        names = [k.replace('\u241f', ' / ') for k in ready]
        v3_store.approve(run, 'apply_alternate_visuals', ', '.join(names)[:500], {'sheets': names})
        for k, res in ready.items():
            dash_name, name = k.split('\u241f', 1)
            for d in run['result']['ir'].get('dashboards') or []:
                if d.get('name') != dash_name:
                    continue
                for sh in d.get('sheets') or []:
                    if sh.get('name') == name:
                        AV.apply(sh, res, approved_by=who, at=at)
            res['applied'] = True
        for k in ('sheet_values', 'sheet_checks', 'sheet_reference', 'reference'):
            run.pop(k, None)
        if not run.get('missing'):
            status, detail, run['package'] = _build(run)
            run['events'].insert(0, ('Alternate visuals applied', f'{len(ready)} sheet(s); {detail}', 'done'))
        st.rerun()


def _tab_sheets(run):
    rows = run.get('sheet_checks')
    st.caption('Each sheet is rendered headlessly by the same engine as Preview, on the bound Snowflake data, '
               'in the published default state. Tableau values and the visual verdict are attached when '
               'those comparisons have run.')
    if not rows:
        st.info('Not run yet. Click "Run validation".')
        return
    ok = sum(1 for r in rows if r['Renders on data'].startswith('Yes'))
    st.markdown(
        '<div class="v2-summary">'
        f"<div><span>Sheets</span><b>{len(rows)}</b></div>"
        f"<div><span>Render on data</span><b>{ok}</b></div>"
        f"<div><span>Sheets matching Tableau</span><b>{sum(1 for r in rows if str(r['Tableau values']).startswith('Match'))}</b></div>"
        f"<div><span>Errors</span><b>{sum(1 for r in rows if r['Renders on data'] == 'Error')}</b></div>"
        '</div>', unsafe_allow_html=True)
    st.dataframe(rows, hide_index=True, use_container_width=True)
    _alt_visual_block(run)
    st.markdown('**App values per sheet**')
    st.caption('What each migrated sheet displays on Snowflake (default state). The side-by-side comparison '
               'with Tableau is in tab 2.')
    values = run.get('sheet_values') or {}
    for row in rows:
        app = values.get((row['Dashboard'], row['Sheet'])) or {}
        with st.expander(f"{row['Sheet']} — {app.get('total', 0)} row(s)"):
            if app.get('rows'):
                st.dataframe(app['rows'], hide_index=True, use_container_width=True)
                if app.get('total', 0) > 60:
                    st.caption('Showing the first 60 rows.')
            else:
                st.caption('No tabular values captured for this sheet (text or HTML output).')


def _load_sheet_capture(run, data, source):
    import v3_evidence as EV
    import v3_sheet_capture as SC
    capture = SC.load(data)
    if not run.get('sheet_values'):
        EV.sheet_checks(run, _runtime_sf())
    run['sheet_reference'] = {
        'meta': (f"{capture.get('method')} · view {capture.get('view')} · captured {capture.get('captured_at')} "
                 f"· {source}"),
        'results': SC.compare_capture(capture, run['result']['ir'], run.get('sheet_values'))}
    EV.merge_statuses(run)


def _sheet_capture_section(run):
    st.markdown("**Every sheet: the app's values vs Tableau's own values**")
    st.caption("Tableau's values come from the Tableau reference loaded above: Tableau's Embedding API reads what "
               'Tableau itself computed for every worksheet in the dashboard (the REST export returns only the '
               'first sheet). Values must agree to the precision Tableau displays.')
    sref = run.get('sheet_reference')
    if not sref:
        st.info('Load the Tableau reference values above and click "Run validation".')
        return
    st.caption(sref['meta'])
    rows = [dict({'Sheet': n}, **summary) for n, (summary, _d) in sref['results'].items()]
    st.dataframe(rows, hide_index=True, use_container_width=True)
    for n, (summary, detail) in sref['results'].items():
        if detail:
            with st.expander(f"Tableau vs app — {n}: {summary['Status']}",
                             expanded=summary['Status'] == 'Difference found'):
                st.dataframe([dict(d, Match='Yes' if d['Match'] else 'No') for d in detail],
                             hide_index=True, use_container_width=True)
        if summary['Status'] == 'Difference found':
            sheet = next((s for d in run['result']['ir'].get('dashboards', []) for s in d.get('sheets', [])
                          if s.get('name') == n), None)
            _diagnosis_block(run, dict(summary, View=n, Sheet=n), detail or [], sheet)



def _diagnosis_block(run, row, detail, sheet):
    """'Diagnose with Cortex' for one Tableau/app difference (advisory; never changes a verdict)."""
    import v3_ai
    key = 'v3c-diag-' + hashlib.sha256(str(row['View']).encode()).hexdigest()[:12]
    if st.button(f"Diagnose with Cortex — {row['View']}", key=key):
        try:
            with st.spinner('Asking Cortex for likely causes…'):
                run.setdefault('diagnoses', {})[row['View']] = v3_ai.diagnose(
                    backend._active_session(), run, row, detail or [], sheet)
        except Exception as exc:
            st.error(str(exc))
    diag = (run.get('diagnoses') or {}).get(row['View'])
    if diag:
        with st.container(border=True):
            conf = diag.get('confidence')
            st.markdown(f"**Cortex diagnosis (hypothesis, {conf} confidence, unconfirmed)**")
            st.markdown('\n'.join('- ' + c for c in diag.get('likely_causes', [])))
            st.markdown('**Checks to run**\n' + '\n'.join('- ' + c for c in diag.get('checks_to_run', [])))
            if diag.get('ungrounded_numbers'):
                st.caption('Numbers not found in the evidence: ' + ', '.join(diag['ungrounded_numbers']))
            st.caption('Advisory. The verdict above stays as computed.')
    _proof_block(run, row, detail, sheet)


def _apply_fix(run, view, sheet, proven):
    """Record a proven, reviewer-approved fix on ONE sheet, rebuild the package and re-validate."""
    import datetime
    proof_ref = v3_store.proof_key((run.get('proofs') or {}).get(view))
    approval = v3_store.approve(run, 'apply_fix', view, {'hypothesis': proven.get('Hypothesis'),
                                                         'fix_key': proven.get('fix_key'), 'proof_key': proof_ref})
    who = v3_store.viewer()[0]
    for copy in _sheet_copies(run, view, sheet):     # every dashboard that shows this sheet
        copy['query_fix'] = proven['id']
        if proven.get('fix_key'):
            copy['query_fix_key'] = proven['fix_key']      # the fix does exactly what Cortex proposed and code proved
        else:
            copy.pop('query_fix_key', None)
    run.setdefault('applied_fixes', {})[view] = {
        'Sheet': view, 'Fix': proven['Hypothesis'], 'Proof': proven['Evidence'],
        'Proposed by': (f"Cortex (ranked #{proven.get('Cortex rank')}; proposed: count each {proven['fix_key']} "
                        "once per chart point); proven by code"
                        if proven.get('fix_key') and proven.get('Cortex rank') not in (None, 'Not ranked') else
                        f"Code proof (Cortex ranked it #{proven.get('Cortex rank')})"
                        if proven.get('Cortex rank') not in (None, 'Not ranked')
                        else 'Code proof (Cortex did not rank it)'),
        'Approved': (who + ' in the console') if who else 'Reviewer in the console',
        'At': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
    for k in ('sheet_values', 'sheet_checks', 'sheet_reference', 'reference', 'proofs', 'proof_tried'):
        run.pop(k, None)
    if not run.get('missing'):
        status, detail, run['package'] = _build(run)
        run['events'].insert(0, ('Proven fix applied', f"{view}: {proven['Hypothesis']}; {detail}", 'done'))
    if run.get('capture_bytes'):
        try:
            with st.spinner('Re-validating every sheet against Tableau with the fix applied…'):
                _compare_capture(run)
        except Exception as exc:
            st.warning(f'Re-run validation: {type(exc).__name__}')
    v3_store.fix(run, approval, view, 'applied', proven.get('id'), proven.get('fix_key'), proof_ref,
                 run['applied_fixes'][view])
    try:                                              # EN02: remember the proven fix for the same formulas
        import v3_memory
        import v3_prove
        formulas = (run.get('result') or {}).get('calc_formulas') or {}
        for n in v3_prove.sheet_calcs(run, sheet):
            if formulas.get(n) and 'FIXED' in formulas[n].upper():
                v3_memory.save(backend._active_session() if _runtime_sf() else None, 'proven_fix', formulas[n],
                               {'hypothesis': proven.get('id'), 'lod_key': proven.get('fix_key')}, caption=n,
                               workbook=run.get('name', ''), evidence={'proof': proven.get('Evidence')},
                               proven=True)
    except Exception:
        pass                                          # memory is best-effort; the fix itself is applied


def _sheet_copies(run, view, sheet=None):
    copies = [s for d in run['result']['ir'].get('dashboards', []) for s in d.get('sheets', [])
              if s.get('name') == view]
    return copies + ([sheet] if sheet is not None and not any(c is sheet for c in copies) else [])


def _undo_fix(run, view, sheet):
    approval = v3_store.approve(run, 'undo_fix', view)
    record = (run.get('applied_fixes') or {}).get(view)
    for copy in _sheet_copies(run, view, sheet):
        copy.pop('query_fix', None)
        copy.pop('query_fix_key', None)
    (run.get('applied_fixes') or {}).pop(view, None)
    for k in ('sheet_values', 'sheet_checks', 'sheet_reference', 'reference', 'proofs', 'proof_tried'):
        run.pop(k, None)
    if not run.get('missing'):
        status, detail, run['package'] = _build(run)
        run['events'].insert(0, ('Proven fix removed', view, 'done'))
    v3_store.fix(run, approval, view, 'removed', record=record)


def _applied_fixes_panel(run):
    fixes = run.get('applied_fixes') or {}
    if not fixes:
        return
    with st.container(border=True):
        st.markdown('**Fixes applied after a cause proof** (causes ranked by Cortex, every cause tested by code '
                    "against Tableau's values, applied by a reviewer)")
        st.dataframe(list(fixes.values()), hide_index=True, use_container_width=True)
        import engine
        seen = [engine.LAST_FIX_RENDER[v] for v in fixes if v in engine.LAST_FIX_RENDER]
        if seen:
            st.markdown('**What the fix did in the last render** (counted by the engine while the sheet rendered)')
            st.dataframe(seen, hide_index=True, use_container_width=True)
            if not any(r['Queries rewritten'] for r in seen):
                st.warning('No query was rewritten in the last render, so the values are unchanged. '
                           'The table above shows why (not applicable, or failed with the error shown).')
        else:
            st.caption('The fixed sheet has not been rendered since the fix was applied. Click "Run validation".')
        for view in list(fixes):
            sheet = next((s for d in run['result']['ir'].get('dashboards', []) for s in d.get('sheets', [])
                          if s.get('name') == view), None)
            if sheet is not None and st.button(f'Undo fix — {view}', key='v3c-unfix-' + hashlib.sha256(view.encode()).hexdigest()[:12]):
                _undo_fix(run, view, sheet)
                st.rerun()


def _proof_entry(run, sheet):
    """(dashboard name, Tableau capture entry) for a sheet, when a per-sheet capture is loaded."""
    import v3_sheet_capture as SC
    if not sheet or not run.get('capture_bytes'):
        return None, None
    try:
        entry = SC.load(run['capture_bytes'])['sheets'].get(sheet.get('name'))
    except Exception:
        return None, None
    dash = next((d.get('name') for d in run['result']['ir'].get('dashboards', [])
                 if any(s is sheet or s.get('name') == sheet.get('name') for s in d.get('sheets', []))), None)
    return dash, entry


def _proof_block(run, row, detail, sheet):
    """'Find and prove the cause': Cortex ranks typed hypotheses, code tests them (advisory)."""
    import v3_prove
    dash, entry = _proof_entry(run, sheet)
    if not dash or not entry:
        return
    _memory(run)                                      # EN02: proven fixes reach the proof as a code fact
    key = 'v3c-proof-' + hashlib.sha256(str(row['View']).encode()).hexdigest()[:12]
    if st.button(f"Find and prove the cause — {row['View']}", key=key,
                 help='Cortex ranks hypotheses from a fixed menu; code tests each one with a read-only query '
                      "and compares the result with Tableau's values."):
        try:
            with st.spinner('Cortex is ranking hypotheses; code is testing them on Snowflake…'):
                run.setdefault('proofs', {})[row['View']] = v3_prove.prove(
                    backend._active_session(), run, row, detail or [], sheet, dash, entry, _runtime_sf())
            (run.get('proof_errors') or {}).pop(row['View'], None)
        except Exception as exc:
            run.setdefault('proof_errors', {})[row['View']] = f'{type(exc).__name__}: {str(exc)[:300]}'
            st.error(str(exc))
    import cx4_agent                       # CX4 (release 27): the diagnosis Cortex Agent, advisory
    cx4_agent.render_block(st, run, row, detail, sheet, dash, entry, backend._active_session(), _runtime_sf(),
                           _bound_view(run, (sheet or {}).get('datasource')), v3_prove.prove)
    proof = (run.get('proofs') or {}).get(row['View'])
    tried = run.setdefault('proof_tried', [])
    if not proof and row['View'] not in tried and _runtime_sf() and backend._active_session() is not None:
        tried.append(row['View'])                     # automatic once per difference; the button re-runs it
        try:
            with st.spinner('Cortex is ranking hypotheses; code is testing them on Snowflake…'):
                proof = run.setdefault('proofs', {})[row['View']] = v3_prove.prove(
                    backend._active_session(), run, row, detail or [], sheet, dash, entry, True)
        except Exception as exc:
            st.caption(f'Automatic cause proof not run ({type(exc).__name__}); use the button.')
    if not proof:
        err = (run.get('proof_errors') or {}).get(row['View'])
        if err:
            st.warning(f'The automatic cause proof did not complete: {err}. Click "Find and prove the cause" to run it again.')
        return
    with st.container(border=True):
        st.markdown('**Cause proof: Cortex proposes, code proves**')
        if proof['chosen'].get('summary'):
            st.caption('Cortex: ' + proof['chosen']['summary'])
        fix = proof['chosen'].get('proposed_fix')
        if fix:
            st.caption(f"Cortex's proposed fix: {fix.get('hypothesis')} on {fix.get('lod_key')} — {fix.get('explanation')}")
        g = (proof.get('facts') or {}).get('grain') or {}
        if g.get('text'):
            st.caption('Fact computed by code: ' + g['text'])
        st.dataframe([{k: r.get(k) for k in ('Cortex rank', 'Hypothesis', 'Cortex reason', 'Test', 'Result', 'Evidence')}
                      for r in proof['results']], hide_index=True, use_container_width=True)
        h = proof['harness']
        st.caption('Harness check: the as-migrated query, re-run by the same code, '
                   + ('reproduces the app values.' if h.get('reproduces_app') else
                      'does not exactly reproduce the app values; treat results with care.'))
        good = next((r for r in proof['results'] if r.get('Result') == 'Proven'), None)
        (st.success if good else st.info)(proof['conclusion'])
        if good and good.get('sql'):
            with st.expander('Read-only query that proved it'):
                st.code(good['sql'], language='sql')
        if good and good.get('id') in v3_prove.TRANSFORMS and not sheet.get('query_fix'):
            st.markdown('**Reviewer approval:** apply this proven fix to the migrated app? It changes only this '
                        'sheet, then validation re-runs against Tableau.')
            if st.button(f"Apply the proven fix — {row['View']}", type='primary',
                         key='v3c-fix-' + hashlib.sha256(str(row['View']).encode()).hexdigest()[:12]):
                _apply_fix(run, row['View'], sheet, good)
                st.rerun()
        st.caption('Advisory. The verdict above stays as computed; the fix ships in the engine and is validated again.')


def _exact_section(run):
    """Release 26: every sheet's exact plan (from the workbook shelves) against Tableau's own values.
    A person applies the verified plans; nothing changes in the app before that."""
    import datetime
    import v3_exact_loop as X
    st.markdown("**Exact plans vs Tableau's values** (release 26)")
    st.caption('Each sheet is planned from its Tableau shelves: every dimension on Rows, Columns and the Marks '
               'card sets the level of detail, measures keep their aggregation, table calculations and blends '
               'follow the workbook. Code runs the plan read-only and compares every row with what Tableau '
               'computed. "Verified" means every value equals Tableau to the precision Tableau displays.')
    if not run.get('capture_bytes'):
        st.info('Load a Tableau reference above to prove the exact plans.')
        return
    if st.button('Prove exact plans against Tableau', key='v3c-exact-prove'):
        try:
            with st.spinner('Running every exact plan read-only and comparing with Tableau…'):
                X.prove_run(run, _runtime_sf())
        except Exception as exc:
            st.error(f'Exact plan proof failed: {type(exc).__name__}')
    rows = X.summary_rows(run)
    if not rows:
        st.caption('Not run yet for this migration. Click the button above (it also runs with "Run validation").')
        return
    ok = sum(1 for r in rows if r['Result'] == 'Verified')
    st.markdown(f"**{ok} of {len(rows)} sheets Verified** · "
                f"{sum(1 for r in rows if r['Result'] == 'Partly verified')} partly verified · "
                f"{sum(1 for r in rows if r['Result'] not in X.APPLICABLE)} not matched or not planned")
    st.dataframe(rows, hide_index=True, use_container_width=True)
    import v3_plan_proposals as PP                    # CF07 / RL1 / RL3: Cortex edits for sheets not matched
    if PP.targets(run, 10) and st.button(f'Ask Cortex for plan corrections ({len(PP.targets(run, 10))} sheet(s), '
                                         'proven against Tableau)', disabled=not _runtime_sf(), key='v3c-cf07'):
        try:
            with st.spinner('Cortex proposes edits from a fixed menu; code proves each against Tableau…'):
                PP.improve_run(run, _runtime_sf(), PP.cortex_ask(backend._active_session(), run))
        except Exception as exc:                              # codes only
            run['cf07'] = [{'Sheet': '—', 'Status': 'CF07_FAILED:' + type(exc).__name__}]
        st.rerun()
    if run.get('cf07'):
        st.caption('Cortex plan corrections (CF07): a candidate counts only when the exact loop proves it equal to '
                   "Tableau's values; a changed mark is drawn as Approximated. Apply below with your approval.")
        st.dataframe(run['cf07'], hide_index=True, use_container_width=True)
    ready = X.applicable(run)
    if ready:
        st.markdown(f'**Reviewer approval:** draw these {len(ready)} sheet(s) from the plan that equals Tableau? '
                    'Sheets not verified keep what they show today. The package is rebuilt and validation re-runs.')
        if st.button(f'Apply {len(ready)} verified exact plan(s)', type='primary', key='v3c-exact-apply'):
            names = [f'{d} / {n}' for (d, n), _r in ready]
            approval = v3_store.approve(run, 'apply_exact_plans', ', '.join(names)[:500],
                                        {'sheets': names, 'reference': run.get('capture_name')})
            who = v3_store.viewer()[0] or 'Reviewer in the console'
            at = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
            done = X.apply_verified(run, who, run.get('capture_name'), at)
            run.setdefault('applied_exact', []).append({'At': at, 'By': who, 'Sheets': len(done),
                                                        'Approval': approval or 'not recorded (store off)'})
            for k in ('sheet_values', 'sheet_checks', 'sheet_reference', 'reference'):
                run.pop(k, None)
            if not run.get('missing'):
                status, detail, run['package'] = _build(run)
                run['events'].insert(0, ('Verified exact plans applied', f'{len(done)} sheet(s); {detail}', 'done'))
            st.rerun()
    if run.get('applied_exact'):
        st.caption('Applied: ' + '; '.join(f"{a['Sheets']} sheet(s) by {a['By']} at {a['At']}"
                                           for a in run['applied_exact']))


def _layout_section(run):
    """Release 26c: the layout check that runs with every migration (no screenshot, no Tableau call)."""
    check = (run.get('result') or {}).get('layout_check') or {}
    summary, rows = check.get('summary') or {}, check.get('rows') or []
    st.markdown('**Layout vs Tableau (every dashboard zone)**')
    st.caption('Each zone of the workbook (charts, filters, parameters, legends, text, images, buttons) has an '
               'authored rectangle. The app draws the dashboard from those rectangles; this check computes where '
               'each zone lands with the app\'s own sizing rules and compares it with Tableau\'s. It checks '
               'position and size, not fonts or the pixels inside a chart; the image comparison below does that.')
    if summary.get('error'):
        st.warning('The layout check could not run for this migration.')
        return
    if not rows:
        st.caption('No dashboard zones to check.')
        return
    st.markdown(f"**{summary.get('Placed', 0)} of {summary.get('zones', 0)} zones placed as in Tableau** · "
                f"{summary.get('Moved', 0)} moved · {summary.get('Not drawn', 0)} not drawn")
    shown = [r for r in rows if r.get('Status') != 'Placed'] or rows
    st.dataframe(shown, hide_index=True, use_container_width=True)


def _tab_values(run):
    import v3_ai
    _exact_section(run)
    _sheet_capture_section(run)
    import v3_filter_states as F           # SL06 (release 30): the comparison in several quick-filter states
    with st.container(border=True):
        F.render(run, st, snowflake=_runtime_sf(), session_ok=True)
    import v3_actions_proof as AP          # AC02 (release 30): dashboard actions and Reset compared with Tableau
    with st.container(border=True):
        AP.render(run, st, snowflake=_runtime_sf())
    st.markdown('**Published view data (REST)**')
    result = run['result']
    st.caption('Tableau computes each published view itself; the app computes the same sheet on '
               'Snowflake. Values must agree to the precision Tableau displays. Both sides use the '
               'published default state, so workbook controls are reset first.')
    if not result.get('cloud_workbook_id'):
        st.info('Available for workbooks pulled from Tableau Cloud.')
        return
    ref = run.get('reference')
    if not ref:
        st.info('Not run yet. Click "Run validation".')
        return
    st.dataframe(ref['rows'], hide_index=True, use_container_width=True)
    for row in ref['rows']:
        detail = ref['details'].get(row['View'])
        if detail:
            with st.expander(f"Values compared — {row['View']}", expanded=row['Status'] == 'Difference found'):
                st.dataframe(detail, hide_index=True, use_container_width=True)
        if row['Status'] == 'Difference found':
            sheet = next((s for d in result['ir'].get('dashboards', []) for s in d.get('sheets', [])
                          if s.get('name') == row.get('Sheet')), None)
            _diagnosis_block(run, row, detail or [], sheet)
    st.caption('Only views Tableau publishes can be compared. Matching published views is evidence for '
               'those sheets, not acceptance of the whole workbook.')


def _tab_visual(run):
    from v3_evidence import VERDICT_LABEL
    st.caption("Left: Tableau's own REST-rendered view image. Right: the migrated app's dashboard rendered "
               'headlessly from its chart objects (default state; zone heights approximate, filter cards not drawn). '
               'Cortex vision describes each image separately, then compares the descriptions. Advisory only.')
    visual = run.get('visual')
    if not visual:
        st.info('Not run yet. Click "Run validation" (needs a workbook pulled from Tableau Cloud).')
        return
    for name, v in visual.items():
        st.markdown(f"#### {esc(name)} — {esc(VERDICT_LABEL.get(v.get('verdict'), str(v.get('verdict'))))}")
        left, right = st.columns(2)
        with left:
            st.caption('Tableau')
            if v.get('tableau_png'):
                st.image(v['tableau_png'], use_container_width=True)
            else:
                st.info('Tableau image unavailable.')
        with right:
            st.caption('Migrated app')
            if v.get('app_png'):
                st.image(v['app_png'], use_container_width=True)
            else:
                st.info('App image unavailable.')
        if v.get('explanation'):
            with st.container(border=True):
                st.markdown('**Cortex vision (advisory)** — ' + v['explanation'])
        if v.get('tableau_description') or v.get('app_description'):
            with st.expander('What Cortex saw in each image'):
                st.markdown('**Tableau image**\n\n' + (v.get('tableau_description') or '—'))
                st.markdown('**App image**\n\n' + (v.get('app_description') or '—'))
        if v.get('omitted'):
            st.caption('Not drawn in the static image: ' + ', '.join(map(str, v['omitted'])))
            reasons = [f"{n.get('sheet')}: {n.get('reason')}" for n in v.get('notes') or []
                       if not n.get('rendered') and n.get('reason')]
            if reasons:
                with st.expander('Why these sheets are missing from the image'):
                    st.code('\n'.join(r[:300] for r in reasons))
        if v.get('error'):
            st.caption(v['error'])


def _tab_report(run):
    import v3_ai
    import v3_evidence as EV
    st.caption('A self-contained HTML report of this run: stages, bindings, sheet-level results, Tableau value '
               'comparison, image pairs, Cortex activity and what is not verified. Open it and print to PDF.')
    left, right = st.columns([0.4, 0.6])
    with left:
        if st.button('Write executive summary with Cortex', key='v3c-report-summary',
                     disabled=not _runtime_sf()):
            try:
                with st.spinner('Cortex is summarising the evidence…'):
                    run['report_summary'] = v3_ai.executive_summary(backend._active_session(), run)
            except Exception as exc:
                st.error(str(exc))
    summary = run.get('report_summary')
    if summary:
        with st.container(border=True):
            st.markdown('**Executive summary (Cortex, grounded in this run)**')
            st.write(summary.get('summary', ''))
            flagged = summary.get('ungrounded_numbers')
            st.caption('Grounding check: ' + ('every number cited appears in the evidence.' if not flagged
                                              else 'not found in the evidence — ' + ', '.join(flagged)))
    import cx6_review                      # CX6 (release 27): Cortex review of the run's evidence, advisory
    cx6_review.render(run, st, backend._active_session())
    import cx5_repair                      # CX5 (release 27): repair plan, advisory; nothing is applied from here
    cx5_repair.render(run, st, backend._active_session())
    data = EV.report_html(run, run.get('sheet_checks'), VERSION, v3_ai.activity(run), summary)
    with right:
        st.download_button('Download migration report', data, type='primary',
                           file_name='migration_report_' + hashlib.sha256(run['name'].encode()).hexdigest()[:8] + '.html',
                           mime='text/html', key='v3c-report')
    missing = [n for n, k in (('sheet checks', 'sheet_checks'), ('Tableau value comparison', 'reference'),
                              ('image comparison', 'visual')) if not run.get(k)]
    if missing:
        st.caption('The report marks these as not run: ' + ', '.join(missing) + '.')


def page_validation():
    run = _require_run('Validation', 'Sheet-level checks, Tableau values, images and the migration report.')
    if not run:
        return
    _validation_actions(run)
    _applied_fixes_panel(run)
    _cortex_strip(run, ('Validation', 'Report'), 'Cortex in Validation')
    tabs = st.tabs(['Summary', '1 · Sheets on Snowflake', '2 · App vs Tableau values',
                    '3 · Image comparison (Cortex vision)', '4 · Visual review (Cortex)', '5 · Migration report',
                    '6 · Acceptance'])
    with tabs[0]:
        _tab_summary(run)
    with tabs[1]:
        _tab_sheets(run)
    with tabs[2]:
        _tab_values(run)
    with tabs[3]:
        _layout_section(run)
        _tab_visual(run)
    with tabs[4]:
        # CX2 in the product (release 27): the same core module as the laptop runner; advisory only
        import cx2_panel
        cx2_panel.render_tab(run, st, engine, backend._active_session(), _runtime_sf())
    with tabs[5]:
        _tab_report(run)
    with tabs[6]:
        # AC01 (release 30): one acceptance verdict per dashboard; the owner signs last (APPROVALS)
        import v3_acceptance
        v3_acceptance.render(run, st, lambda action, subject, detail: v3_store.approve(run, action, subject, detail))


SUGGESTED = ['Which sheets were refused and why?', 'Which Snowflake objects does this app read?',
             'What has been verified against Tableau so far?', 'What should be reviewed before deployment?']


def _touchpoint_table(run=None):
    import v3_ai
    return [{'Workflow step': step, 'Cortex element': name, 'What it does': what,
             'Status': {'live': 'LIVE', 'built': 'BUILT (live proof pending)'}.get(status, 'PLANNED'),
             'Trigger': trigger}
            for step, name, what, status, trigger in v3_ai.TOUCHPOINTS]


def page_cortex():
    import v3_ai
    run = st.session_state.get(RUN)
    render_header('Cortex AI', 'Where Snowflake Cortex works in this migration, every call it made, and '
                  'questions answered from the run evidence.', *_pill(run))
    st.markdown('<div class="v2-auto"><div><b>Guardrail: Cortex proposes, code verifies, evidence accepts</b>'
                '<span>Every Cortex call runs in your account through AI_COMPLETE with structured outputs. Output is '
                'shown to people, never executed, and never changes a verdict, binding or value.</span></div></div>',
                unsafe_allow_html=True)
    st.subheader('Cortex across the workflow')
    st.dataframe(_touchpoint_table(), hide_index=True, use_container_width=True)
    if not run or not run.get('result'):
        st.info('Run a migration to see Cortex activity and ask questions about it.')
        return
    st.subheader('Cortex activity in this run')
    rows = v3_ai.activity(run)
    by_step = Counter(r['Step'] for r in rows if r['Status'] not in ('Not run', 'Not attempted'))
    st.markdown('<div class="v2-summary">' + ''.join(
        f'<div><span>{esc(step)}</span><b>{by_step.get(step, 0)}</b></div>'
        for step in ('Parsing', 'Data model', 'Validation', 'Report', 'Cortex AI')) + '</div>', unsafe_allow_html=True)
    _usage_tiles(rows)
    _stage_costs(run, rows)
    _scorecard(run)
    _accuracy_section()
    _agent_section()
    _recheck_section(run)
    _spend_section()
    st.dataframe(rows or [{'Step': '—', 'Purpose': 'No Cortex calls yet'}], hide_index=True,
                 use_container_width=True)
    st.caption('Parsing calls come from the redacted intake trace (no prompts or responses stored). '
               f'Console calls are capped at {v3_ai.MAX_CONSOLE_CALLS} per run.')
    _analyst_section(run)
    st.subheader('Ask Cortex about this migration')
    st.caption('Answers use only this run\'s evidence (stages, bindings, assessment, comparisons). '
               'Numbers not found in the evidence are flagged.')
    picks = st.columns(len(SUGGESTED))
    chosen = None
    for col, q in zip(picks, SUGGESTED):
        if col.button(q, use_container_width=True, key='v3c-ask-' + hashlib.sha256(q.encode()).hexdigest()[:8]):
            chosen = q
    question = st.text_input('Your question', key='v3c-ask-text', placeholder='e.g. Why is this sheet refused?')
    if st.button('Ask Cortex', type='primary', disabled=not _runtime_sf(), key='v3c-ask-go'):
        chosen = question
    if not _runtime_sf():
        st.caption('Ask Cortex runs in the hosted Snowflake app.')
    if chosen and _runtime_sf():
        try:
            with st.spinner('Cortex is reading the run evidence…'):
                answer = v3_ai.ask(backend._active_session(), run, chosen)
            run.setdefault('ai_qa', []).insert(0, (chosen, answer))
        except Exception as exc:
            st.error(str(exc))
    for q, a in run.get('ai_qa') or []:
        with st.container(border=True):
            st.markdown(f'**Q: {esc(q)}**')
            st.write(a.get('answer', ''))
            note = []
            if not a.get('answerable_from_evidence', True):
                note.append('not answerable from the run evidence')
            if a.get('evidence_used'):
                note.append('evidence: ' + ', '.join(map(str, a['evidence_used']))[:200])
            if a.get('ungrounded_numbers'):
                note.append('numbers not found in the evidence: ' + ', '.join(a['ungrounded_numbers']))
            if a.get('citation_status') == 'UNCITED':                 # AI07
                note.append('UNCITED: no citation matches the run evidence — treat as unverified')
            if a.get('unknown_citations'):
                note.append('citations not in the evidence: ' + ', '.join(map(str, a['unknown_citations']))[:200])
            if note:
                st.caption('; '.join(note))


def _user_filters_block(run):
    """EN27: Tableau user filters -> row access policies on CURRENT_USER() (SQL for the owner; nothing applied)."""
    import v3_user_filters as UF
    rows = UF.policies(run['result']['ir'], run.get('bindings'))
    if not rows:
        return
    with st.expander(f'Tableau user filters ({len(rows)}) -> Snowflake row access policies (EN27)', expanded=True):
        st.caption('Tableau user names and groups do not exist in Snowflake, and this app runs with the owner\'s role, '
                   'so each policy keeps a row when the VIEWER (CURRENT_USER()) is entitled to its value in '
                   'WBR_DB.V3_PORTAL.ENTITLEMENTS, which you maintain. Run the SQL yourself; nothing is applied here.')
        st.dataframe([{'Calculation': r['calculation'], 'Form': r['form'], 'State': r['state'],
                       'Formula': r['formula'][:160]} for r in rows], hide_index=True, use_container_width=True)
        ready = [r for r in rows if r['state'] == 'READY']
        if ready:
            st.code('\n'.join(UF.setup_sql()) + '\n\n' + '\n\n'.join('\n'.join(r['sql']) for r in ready), language='sql')


def page_deploy():
    run = _require_run('Deploy', 'Publish the reviewed migration as its own Streamlit in Snowflake app.')
    if not run:
        return
    _user_filters_block(run)
    result = run['result']
    blockers = []
    if run.get('failed'):
        blockers.append(run['failed'] + ' failed')
    if run.get('missing'):
        blockers.append('datasources without a binding: ' + ', '.join(run['missing']))
    if result['assessment']['issues']:
        blockers.append('structural issues in the assessment')
    if not _runtime_sf():
        blockers.append('deployment runs in the hosted Snowflake app')
    from v3_deploy import DB, SCHEMA, STAGE, app_identifier, app_exists
    import v3_semantic as SV
    dashboards = result['ir'].get('dashboards', [])
    default_title = (dashboards[0].get('title') if dashboards else None) or run['name']
    default_name = (SV.label(run['name']) or 'MIGRATED_WORKBOOK')[:56] + '_LAB_DASHBOARD'   # lab: never collide with stable apps

    st.subheader('1 · Name the app')
    left, right = st.columns(2)
    with left:
        raw_name = st.text_input('App name in Snowflake', value=default_name, key='v3c-app-name-' + result['digest'],
                                 help='Letters, digits and underscores. This is the Streamlit object name.')
    with right:
        title = st.text_input('Title shown to users', value=default_title, key='v3c-app-title-' + result['digest'])
    name_error, ident, exists = None, None, False
    try:
        ident = app_identifier(raw_name)
    except ValueError as exc:
        name_error = str(exc)
    if name_error:
        st.error(name_error)
    else:
        st.caption(f'Will be created as **{DB}.{SCHEMA}.{ident}**')
        if _runtime_sf():
            try:
                exists = app_exists(backend._active_session(), ident)
            except Exception:
                exists = False
    replace = False
    edits_confirmed, edit_record = True, None
    if exists:
        replace = st.checkbox(f'An app named {ident} already exists — replace it with this migration',
                              key='v3c-app-replace-' + result['digest'])
        if replace:
            edits_confirmed, edit_record = _replace_check(ident, result)

    st.subheader('2 · What gets deployed')
    st.markdown(
        '<div class="v2-next">'
        f'<div class="v2-next-row"><div class="v2-next-icon">01</div><div><b>One Streamlit in Snowflake app</b>'
        f'<span>{esc(DB)}.{esc(SCHEMA)}.{esc(ident or "…")} — never replaces the console or another app '
        'unless you confirm it above</span></div></div>'
        f'<div class="v2-next-row"><div class="v2-next-icon">02</div><div><b>The reviewed package</b>'
        f'<span>Staged at @{esc(DB)}.{esc(SCHEMA)}.{esc(STAGE)} in a build-specific folder — exactly what '
        'Preview and Validation ran</span></div></div>'
        '<div class="v2-next-row"><div class="v2-next-icon">03</div><div><b>No data changes</b>'
        '<span>The app reads the Snowflake objects below (the Semantic View for relationship data); '
        'no table is created or changed</span></div></div>'
        '<div class="v2-next-row"><div class="v2-next-icon">04</div><div><b>Ask the data</b>'
        '<span>When the data model is a Semantic View, the app includes an Ask the data tab '
        '(Cortex Analyst) and its business glossary</span></div></div></div>',
        unsafe_allow_html=True)
    if run.get('binding_rows'):
        st.dataframe(run['binding_rows'], hide_index=True, use_container_width=True)
    if blockers:
        st.warning('Deployment is not available: ' + '; '.join(blockers) + '.')
        return

    st.subheader('3 · Sheet decisions')
    review, review_error = _review_editor(run, result)

    st.subheader('4 · Review and deploy')
    reviewed = st.checkbox('I have reviewed the Preview and the Validation results for this workbook',
                           key='v3c-reviewed-' + result['digest'])
    deployed = run.get('deployment')
    ready = reviewed and not name_error and (replace or not exists) and not review_error and edits_confirmed
    if st.button('Deploy to Snowflake', type='primary', disabled=not ready, key='v3c-deploy'):
        approval = v3_store.approve(run, 'deploy', ident, {'title': title or default_title, 'replace': bool(replace),
                                                           'reviewed_preview_and_validation': bool(reviewed),
                                                           'held_back': review['held_back'],
                                                           'known_differences': review['notes'],
                                                           'replaced_app_edits': edit_record})
        try:
            from v3_deploy import deploy_workbook
            with st.spinner(f'Staging the package and creating {ident}…'):
                import v3_review
                deployed_ir = v3_review.apply(result['ir'], review)
                run['deployment'] = deployed = deploy_workbook(backend._active_session(), deployed_ir,
                                                              run['bindings'], (title or default_title),
                                                              app_name=ident, replace=replace,
                                                              ask=run.get('ask') or None)
                run['deployed_package'] = {'ir': deployed_ir, 'title': title or default_title,
                                           'approval': approval}               # MA01: exactly what was approved
            v3_store.deployed(run, approval, deployed)
        except Exception as exc:
            v3_store.deployed(run, approval, error=exc)
            st.error(f'Deployment failed: {type(exc).__name__}: {exc}')
    if deployed:
        st.success('Deployed ' + deployed['identifier'] + ' from build ' + deployed['build_sha256'][:12] + '.')
        if deployed.get('url'):
            st.link_button('Open the deployed app', deployed['url'], type='primary')
        else:
            st.caption('Open it in Snowsight: Projects › Streamlit › ' + deployed['identifier'])
        st.caption(f"{deployed['files']} files staged at {deployed['location']}. Deployment is not "
                   'acceptance: Tableau parity remains as shown on the Validation page.')
        for note in deployed.get('notes') or []:
            st.caption('Note: ' + note)
        _portal_add(run, deployed)
    _portal_section(run)


def _portal_add(run, deployed):
    """MA01: an approved, deployed migration can be added to a portal (1:1 first)."""
    pk = st.session_state.setdefault('v3c-portal-packages', {})
    pid = deployed['identifier']
    if pid in pk:
        st.caption(f'{pid} is in the portal list.')
    elif st.button('Add this migration to a portal (MA01)', key='v3c-portal-add'):
        dp = run.get('deployed_package')
        if not dp:
            st.warning('PACKAGE_NOT_KEPT: deploy again from this page to add it to a portal.')
            return
        pk[pid] = {'id': pid, 'title': dp['title'], 'ir': dp['ir'], 'bindings': run['bindings'],
                   'ask': run.get('ask') or None, 'build': deployed['build_sha256'], 'approval': dp.get('approval')}
        st.rerun()


def _portal_section(run):
    """MA01: deploy a portal (Analytics Home + one page per approved package); access by viewer user name."""
    import v3_portal as PO
    pk = st.session_state.get('v3c-portal-packages') or {}
    with st.expander(f'Portal app (MA01): {len(pk)} approved package(s) in the list'):
        st.caption('A portal composes migrations already approved and deployed as single apps; nothing is re-migrated. '
                   'Who sees which page comes from WBR_DB.V3_PORTAL.ACCESS by viewer user name (\'*\' = everyone). '
                   'Hiding a page is navigation, not security: separate data with row access policies (EN27).')
        st.code('\n'.join(PO.setup_sql()), language='sql')
        if not pk:
            return
        st.dataframe([{'Package': p['id'], 'Title': p['title'], 'Build': p['build'][:12]} for p in pk.values()],
                     hide_index=True, use_container_width=True)
        name = st.text_input('Portal name', key='v3c-portal-name', placeholder='e.g. Sales')
        replace = st.checkbox('Replace the existing portal with this name', key='v3c-portal-replace')
        try:
            ident = PO.portal_identifier(name) if name.strip() else None
        except ValueError as exc:
            ident = None
            st.caption(str(exc))
        if ident and st.button(f'Deploy portal {ident}', type='primary', disabled=not _runtime_sf(),
                               key='v3c-portal-deploy'):
            who = v3_store.viewer()[0] or 'Reviewer in the console'
            v3_store.approve(run, 'deploy_portal', ident, {'packages': list(pk)})
            try:
                with st.spinner(f'Staging the portal {ident}…'):
                    st.session_state['v3c-portal-last'] = PO.deploy_portal(backend._active_session(), name,
                                                                           list(pk.values()), approved_by=who,
                                                                           replace=replace)
            except Exception as exc:                      # codes only
                st.error(f'Portal deployment failed: {type(exc).__name__}')
        last = st.session_state.get('v3c-portal-last')
        if last:
            st.success(f"Deployed {last['identifier']} with {len(last['packages'])} dashboard(s), build "
                       f"{last['build_sha256'][:12]}.")


def _replace_check(ident, result):
    """X15: before replacing an app, list files of its deployed build that were changed by hand -> (confirmed,
    record). Nothing changed: confirmed. Changes, or the check could not run: an explicit second confirmation."""
    from v3_deploy import edited_files
    changes, reason = edited_files(backend._active_session(), ident)
    if changes == []:
        st.caption('No hand edits found in the deployed app: its files match its own build.')
        return True, {'checked': True, 'changes': []}
    if changes:
        st.warning(f'{len(changes)} file(s) of {ident} differ from the build that was deployed — hand edits '
                   'that replacing the app would overwrite:')
        st.dataframe(changes, hide_index=True, use_container_width=True)
    else:
        st.warning(f'Hand edits could not be checked: {reason}.')
    ok = st.checkbox('Replace anyway — the changes listed above are lost' if changes else
                     'Replace anyway without the check', key='v3c-app-edits-' + result['digest'])
    return bool(ok), {'checked': changes is not None, 'changes': changes or [], 'reason': reason,
                      'confirmed': bool(ok)}


def _review_editor(run, result):
    """X13 / X14: the reviewer's per-sheet decisions for the deployed app -> (review, error)."""
    import v3_review
    ir = result['ir']
    status = v3_review.differences(run)
    rows = [{'Sheet': n, 'Validation': status.get(n) or 'Not compared', 'Hold back': False, 'Reason': '',
             'Note for app users': ''} for n in v3_review.sheet_names(ir)]
    st.caption('Hold back a sheet to deploy the rest of the workbook without it (the app shows the reason in its '
               'place), or add a short note app users see under a sheet, e.g. a known difference you have signed '
               'off. Both are recorded with the deploy approval.')
    edited = st.data_editor(rows, hide_index=True, use_container_width=True, key='v3c-review-' + result['digest'],
                            disabled=['Sheet', 'Validation'],
                            column_config={'Hold back': st.column_config.CheckboxColumn(),
                                           'Reason': st.column_config.TextColumn(max_chars=v3_review.MAX_TEXT),
                                           'Note for app users': st.column_config.TextColumn(
                                               max_chars=v3_review.MAX_TEXT)})
    records = edited.to_dict('records') if hasattr(edited, 'to_dict') else list(edited)
    raw = {'held_back': {r['Sheet']: r.get('Reason') or '' for r in records if r.get('Hold back')},
           'notes': {r['Sheet']: r.get('Note for app users') or '' for r in records
                     if not r.get('Hold back') and str(r.get('Note for app users') or '').strip()}}
    try:
        review = v3_review.validate(raw, ir)
    except ValueError as exc:
        st.warning(str(exc))
        return {'held_back': {}, 'notes': {}}, str(exc)
    if review['held_back'] or review['notes']:
        st.caption(f"{len(review['held_back'])} held back, {len(review['notes'])} with a note.")
    return review, None


def _migrate_from_catalogue(source):
    """Estate catalogue -> New migration with that Tableau Cloud workbook selected."""
    st.session_state.pop(RUN, None)
    st.session_state[SOURCE] = source
    st.session_state[INTAKE + 'mode'] = 'Tableau Cloud'
    _go('New migration')
    st.rerun()


def page_catalogue():
    import v3_catalog_ui
    v3_catalog_ui.render(backend._active_session(), _migrate_from_catalogue,
                         st.session_state.get(INTAKE + 'site') or DEFAULT_SITE_URL)


def main():
    _keep_intake_widgets()
    inject_styles()
    page, classic = sidebar()
    if classic:
        runpy.run_path(str(Path(__file__).with_name('v3_app.py')), run_name='__main__')
        return
    pages = {'Overview': page_overview, 'New migration': page_migrate, 'Inventory': page_inventory,
             'Preview': page_preview, 'Validation': page_validation, 'Cortex AI': page_cortex,
             'Deploy': page_deploy, 'Estate catalogue': page_catalogue}
    if st.session_state.get('v3c_last_page') != page:
        # Page switch: hide what is left of the previous page while this one draws (lab).
        st.markdown('<style>[data-stale="true"]{display:none !important;}</style>', unsafe_allow_html=True)
        st.session_state['v3c_last_page'] = page
    # One container per page: a different key is a different element, so the previous page's
    # content is dropped at once instead of lingering until the new page has drawn past it.
    with st.container(key='v3c-page-' + re.sub(r'[^a-z]+', '-', page.lower())):
        need = _role_needed(page)
        if need:
            render_header(page, 'This page needs a role you do not have.')
            st.warning(f'ROLE_REQUIRED:{need} — ask the app owner to add you to the access table.')
        else:
            pages[page]()
    run = st.session_state.get(RUN)
    if run:
        v3_store.checkpoint(run)          # EN01: appends only what is new; never raises


if __name__ == '__main__':
    main()
