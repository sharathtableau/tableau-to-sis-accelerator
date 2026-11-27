"""Shared V3 intake and isolated local/Snowflake preview. Never deploys objects."""
from contextlib import contextmanager
from copy import deepcopy
import io
import hashlib
import json
import tempfile
import os
import re
from pathlib import Path
from threading import RLock
import zipfile

import backend
import config
import engine
import findings
import pipeline
import tableau_parser
from ir_contract import assess_ir

_LOCK = RLock()  # legacy engine configuration is process-global


def published_scope(ir, published_views):
    """Keep the dashboards Tableau Cloud publishes as views (what users actually see).

    Unpublished dashboards in the file are recorded, not migrated. If no dashboard name is
    among the published views (e.g. only worksheets are published) nothing is dropped."""
    if not published_views:
        return ir
    names = {str(v) for v in published_views}
    keep = [d for d in ir.get('dashboards', []) if d.get('name') in names]
    if not keep:
        return ir
    dropped = [d.get('name') for d in ir.get('dashboards', []) if d.get('name') not in names]
    ir['dashboards'] = keep
    if dropped:
        ir['unpublished_dashboards'] = dropped
    return ir


def prefer_model_extracts(root, bindings, files, reports):
    """C7: a datasource whose model spans SEVERAL data files (a relationship / join of CSVs or workbooks) binds to its
    decoded extract -- what Tableau queries, every table of the model -- never to the first raw file, which is one
    table of it (Clara Brazil: the response CSV alone, so the Calendar table's Date1 never existed). A single-file
    datasource keeps its file. Only an extract this intake decoded (state DECODED) is used."""
    import init_workbook as IW
    decoded = {str(r.get('extract') or '').casefold() for r in reports or [] if r.get('state') == 'DECODED'}
    out = dict(bindings)
    for cap, names in IW.datasource_files(root).items():
        data = {n.casefold() for n in names if not n.lower().endswith(('.hyper', '.tde'))}
        extracts = [n for n in names if n.lower().endswith(('.hyper', '.tde')) and n.casefold() in decoded]
        target = files.get(os.path.splitext(extracts[0])[0] + '.csv') if len(extracts) == 1 else None
        if len(data) > 1 and target:
            out[cap] = target
    return out


def safe_intake(raw, filename, tableau_server=None):
    """E15 / EN24: (bytes to parse, intake record). The package is checked before it is written or parsed (codes
    only); a published datasource gets its published definition spliced in (read-only, through the configured
    Tableau client, never another host); the record holds the snapshot lineage and the live / extract policy."""
    import xml.etree.ElementTree as ET
    import v3_intake
    manifest = v3_intake.check_archive(raw, filename)
    xml = v3_intake.workbook_xml(raw)
    published = []
    if v3_intake.published_sources(ET.fromstring(xml)):
        new_xml, published = v3_intake.expand_published(xml, v3_intake.tableau_fetcher(tableau_server))
        if new_xml is not xml:
            raw = v3_intake.rewrite_package(raw, filename, new_xml)
            xml = new_xml
    root = tableau_parser.normalize_feature_flags(ET.fromstring(xml))
    return raw, {'manifest': manifest, 'published': published, 'policy': v3_intake.source_policy(root),
                 'kits': v3_intake.replication_kit(root)}


def prepare_payload(raw, filename, origin='upload', published_views=None, tableau_server=None):
    """One semantic intake for uploads and Cloud downloads; names are labels only."""
    from tableau_server import normalize_workbook_download
    normalized = normalize_workbook_download(filename, raw)
    digest = hashlib.sha256(raw).hexdigest()
    raw, intake = safe_intake(raw, normalized, tableau_server)
    folder = Path(tempfile.mkdtemp(prefix='tableau-v3-'))
    path = folder / (digest + Path(normalized).suffix.lower())
    path.write_bytes(raw)
    result = prepare(path, raw, published_views)
    result['intake'] = intake
    from cortex_assist import assist, DEFAULT_MODEL
    result['ir'] = assist(result['ir'], backend._active_session(),
                          model=os.environ.get('V3_CORTEX_MODEL', DEFAULT_MODEL), review_workbook=True)
    result['assessment'] = assess_ir(result['ir'])
    result.update(digest=digest, folder=str(folder), source_name=filename,
                  origin=origin, parser_mode='scoped')
    return result


def prepare_cloud(url):
    """Read from Tableau through the existing PAT client; never onboard/deploy."""
    from tableau_server import fetch_workbook
    fetched = fetch_workbook(url)
    result = prepare_payload(fetched['bytes'], fetched['filename'], origin='tableau-cloud')
    result['cloud_workbook_id'] = fetched['workbook_id']
    return result


def prepare_cloud_workbook(server_url, site_content_url, workbook_id, name_hint=None):
    """Download the selected REST identity through the original credential path."""
    from tableau_server import fetch_workbook_by_id
    fetched = fetch_workbook_by_id(server_url, site_content_url, workbook_id, name_hint=name_hint)
    result = prepare_payload(fetched['bytes'], fetched['filename'], origin='tableau-cloud',
                             published_views=fetched.get('published_views'), tableau_server=server_url)
    result.update(cloud_workbook_id=fetched['workbook_id'],
                  cloud_server_url=server_url, cloud_site_content_url=site_content_url)
    return result


def _mark_left_out(ir, root, reports):
    """E04: a relationship table the flatten left out (its keys repeat) has no columns in the loaded data. Mark
    its fields -- found by the column metadata's <parent-name> (the table's object id, or its caption inside the
    same datasource) and the table's own record count -- and every calculation built on one, so a sheet using
    them refuses with the reason instead of a database error. Sheets that do not use them are unaffected."""
    import re
    from urllib.parse import quote
    import pipeline as _pl
    out, links = {}, {}
    for rep in reports or []:
        for e in rep.get('left_out') or []:
            out[e['table']] = e.get('reason', '')
            links[e['table']] = e
    if not out:
        return {}
    related_files = {}
    fields = ir.get('fields') or {}
    for ds in root.iter('datasource'):
        did = ds.get('name')
        records = [(m.findtext('parent-name') or '', m.findtext('local-name') or '')
                   for m in ds.iter('metadata-record') if m.get('class') == 'column']
        owners = {o for o in out if any(p.strip('[]') == o for p, _l in records)}
        if not owners:
            continue
        names = {o: {o, re.sub(r'_[0-9A-F]{16,}$', '', o, flags=re.I)} for o in owners}
        gone = set()
        for parent, local in records:
            for o in owners:
                if parent.strip('[]') in names[o]:
                    gone.add((local.strip('[]'), o))
        remote = {}
        for m in ds.iter('metadata-record'):
            if m.get('class') == 'column':
                remote[((m.findtext('local-name') or '').strip('[]'), (m.findtext('parent-name') or '').strip('[]'))] = \
                    m.findtext('remote-name')
        scope = (ir.get('field_scopes') or {}).get(did) or {}
        for o in owners:
            link = links.get(o) or {}
            tcap = next(iter(names[o] - {o}), o)
            # DM4: a table left out directly off the flattened table, with its own rows, is queried per table
            if link.get('file') and link.get('parent') not in out and scope.get('caption'):
                source = f"{scope['caption']} [{tcap}]"
                related_files[source] = link['file']
                entry = {'table': tcap, 'parent_key': _pl.to_phys(link['parent_key']), 'key': _pl.to_phys(link['key'])}
                if link.get('role') == 'fact':
                    # DM4: another fact -- its measures break down only by its own or the shared table's columns
                    shared = {link['parent'], re.sub(r'_[0-9A-F]{16,}$', '', link['parent'], flags=re.I)}
                    entry.update(role='fact', shared_columns=sorted(
                        _pl.to_phys(ln) for (ln, pn) in remote if pn in shared))
                scope.setdefault('related_tables', {})[source] = entry
        for local, o in gone | {(f'__tableau_internal_object_id__].[{o}', o) for o in owners}:
            entry = fields.get(f'{did}::{quote(local)}')
            if entry is not None:
                cap = names[o] - {o}
                entry['unavailable'] = (f"'{entry.get('caption')}' comes from the related table "
                                        f"{next(iter(cap), o)}, left out of the data model ({out[o]}): joining it "
                                        "would multiply the other tables' rows")
                source = next((k for k, v in (scope.get('related_tables') or {}).items()
                               if v['table'] == next(iter(cap), o)), None)
                if source:
                    if local.startswith('__tableau_internal_object_id__'):
                        entry['related'] = {'source': source, 'count': True}
                    else:
                        r = next((rv for (ln, pn), rv in remote.items() if ln == local and pn in names[o]), None)
                        if r:
                            entry['related'] = {'source': source, 'column': _pl.to_phys(r)}
        phys = {_pl.to_phys(e['physical']): (e['unavailable'], (e.get('related') or {}).get('source'))
                for e in fields.values()
                if e.get('datasource_id') == did and e.get('unavailable') and e.get('physical')}
        for e in fields.values():
            sql = ((e.get('calculation') or {}).get('sql') or '') if e.get('datasource_id') == did else ''
            hits = [(why, src) for col, (why, src) in phys.items() if re.search(rf'\b{re.escape(col)}\b', sql)]
            if hits and not e.get('unavailable'):
                e['unavailable'] = f"'{e.get('caption')}' uses a field that is not available -- {hits[0][0]}"
                sources = {src for _w, src in hits}
                if len(sources) == 1 and None not in sources:
                    e['uses_related'] = next(iter(sources))   # C9: every missing column is from this one table
    return related_files


def _plan_related_sheets(ir, path):
    """DM4: a sheet that uses a field of a related table (queried per table) is drawn from its exact plan
    (v3_sheet_plan + v3_relationship_query), the path the dashboard uses for planned sheets."""
    related = {fid for fid, e in (ir.get('fields') or {}).items() if e.get('related')}
    if not related:
        return
    hit = False
    for d in ir.get('dashboards') or []:
        for s in d.get('sheets') or []:
            occ = (s.get('worksheet_semantics') or {}).get('occurrences') or []
            if s.get('kind') != 'planned' and any(o.get('field_id') in related for o in occ):
                s['support'] = {'status': 'UNSUPPORTED', 'reason': 'uses a related table (per-table relationship query)'}
                hit = True
    if hit:
        import v3_exact_loop
        v3_exact_loop.attach_plans(ir, path)


def prepare(path, raw, published_views=None):
    ir = published_scope(tableau_parser.build_ir(str(path), scoped=True), published_views)
    import v3_exact_loop            # release 26: exact plan from the shelves for every refused sheet
    v3_exact_loop.attach_plans(ir, path)
    import v3_fallback              # release 25a: original engine (labelled approximated) for what is left
    v3_fallback.approximate(ir, path)
    assessment = assess_ir(ir)
    from v3_source_binding import resolve_sources
    root = tableau_parser.load_twb_xml(str(path))
    session = backend._active_session()
    source_bindings, source_reports = resolve_sources(root, ir, session)
    files, hypers, errors = {}, [], []
    data_model_reports = []
    related_files = {}                     # DM4: related tables' own rows, by related source caption
    if str(path).lower().endswith('.twbx'):
        # DM1: same-named files in different archive folders are kept apart and matched by the archive path each
        # connection names (pipeline.bundled_data_files / match_files_to_datasources); an ambiguous reference binds
        # nothing
        files, hypers = pipeline.bundled_data_files(raw, str(Path(path).parent))
        if hypers and set(source_bindings) != set(ir.get('datasources', [])):
            import init_workbook
            root = tableau_parser.load_twb_xml(str(path))
            blocked = pipeline.decode_hypers_locally(
                hypers, files, str(Path(path).parent), init_workbook.parse_relationships(root, strict=True, composite=True),
                strict=True, reports=data_model_reports)
            errors.extend(f'Cannot decode bundled extract: {name}' for name in blocked)
            related_files = _mark_left_out(ir, root, data_model_reports) or {}
            _plan_related_sheets(ir, path)
    root = tableau_parser.load_twb_xml(str(path))
    bindings = pipeline.match_files_to_datasources(root, files)
    bindings = prefer_model_extracts(root, bindings, files, data_model_reports)
    import init_workbook as _iw
    dup = {Path(k).name.casefold(): [] for k in files if '/' in k}
    for k in files:
        if '/' in k:
            dup[Path(k).name.casefold()].append(k)
    for cap, names in _iw.datasource_files(root).items():
        clash = [dup[n.casefold()] for n in names if n.casefold() in dup]
        if clash and not bindings.get(cap):
            errors.append(f"Bundled data for {cap} is ambiguous: {names[0]} is in "
                          f"{' and '.join(sorted(clash[0]))}; explicit binding is required.")
    import init_workbook
    for cap, members in tableau_parser.union_members(root).items():
        paths = [files[m] for m in members if m in files]
        if len(paths) != len(members):
            bindings[cap] = None
            errors.append(f'Incomplete union data for {cap}; explicit complete data is required.')
        elif len(paths) >= 2:
            target = str(Path(path).parent / (pipeline.to_phys(cap) + '__union.csv'))
            if init_workbook.materialize_union(paths, target):
                bindings[cap] = target
            else:
                bindings[cap] = None
                errors.append(f'Union materialization failed: {cap}')
    for datasource in ir.get('datasources', []):
        bindings.setdefault(datasource, None)
    if session is None:                                  # DM4: related tables' own rows (local preview)
        bindings.update(related_files)
    if session is not None:
        bindings = {cap: source_bindings.get(cap) for cap in ir.get('datasources', [])}
    try:     # Cortex lab: original Tableau formulas, kept OUTSIDE the IR (engine and package unchanged)
        _defs, _p, _internal, _pi = tableau_parser.extract_calcs_params(root, {})
        calc_formulas = dict(_internal)      # internal IDs (Calculation_..., X (copy)_...) used by the IR and nested refs
        calc_formulas.update(_defs)          # display names
        calc_params = {str(k): str(v) for k, v in (_p or {}).items()}   # defaults under internal AND display names
    except Exception:
        calc_formulas, calc_params = {}, {}
    try:        # release 26c: every authored zone vs where the app draws it (runs for every migration)
        import v3_layout_check
        layout_rows, layout_summary = v3_layout_check.check_ir(ir)
    except Exception as exc:
        layout_rows, layout_summary = [], {'error': type(exc).__name__}
    return {'ir': ir, 'assessment': assessment, 'bindings': bindings, 'intake_errors': errors, 'source_reports': source_reports,
            'data_model_reports': data_model_reports, 'calc_formulas': calc_formulas, 'calc_params': calc_params,
            'layout_check': {'rows': layout_rows, 'summary': layout_summary}}


_VALIDATED = {}            # hosted only: binding fingerprint -> time its read-only model check passed
VALIDATION_TTL = 600       # seconds; a different binding (e.g. the Cortex view) is checked at once


def _validate_once(active, table, ir):
    import time
    from v3_model_binding import validate
    key = hashlib.sha256(json.dumps(table, sort_keys=True, default=str).encode()).hexdigest()
    now = time.time()
    if now - _VALIDATED.get(key, 0) < VALIDATION_TTL:
        return
    validate(active, table, ir)
    _VALIDATED[key] = now


def _validate_table_once(active, table, ir, cap):
    import time
    from v3_source_binding import validate_table
    key = 'table:' + table
    now = time.time()
    if now - _VALIDATED.get(key, 0) < VALIDATION_TTL:
        return
    did = next((d for d, sc in (ir.get('field_scopes') or {}).items() if sc.get('caption') == cap), None)
    validate_table(active, table, [f for f in ir['fields'].values() if f['datasource_id'] == did])
    _VALIDATED[key] = now


def _drifted(drift, cap, check):
    """Run one binding's runtime check; on failure record why (schema problems named; a Snowflake error only by
    its type -- its text never reaches the UI) and report the datasource as drifted."""
    try:
        check()
        return False
    except ValueError as exc:
        drift.append((cap, str(exc)[:300]))
    except Exception as exc:
        drift.append((cap, 'access check failed: ' + type(exc).__name__))
    return True


@contextmanager
def preview_context(ir, bindings, snowflake=False):
    """Serialize and restore legacy process state; never reuse corpus bindings."""
    with _LOCK:
        active = backend._active_session()
        if active is not None and not snowflake:
            raise RuntimeError('V3 local preview refuses an active Snowflake session')
        if snowflake and active is None:
            raise RuntimeError('Snowflake preview requires an active session')
        old_sources = deepcopy(config.DATASOURCES)
        old_default, old_orders, old_engine_orders = config.DEFAULT_DATASOURCE, config.ORDERS, engine.ORDERS
        old_ir, old_params = engine.IR, deepcopy(engine.PARAMS)
        old_findings = findings.all_findings()
        try:
            drift = []
            if snowflake:
                available = {}
                for cap, table in bindings.items():
                    if not table: continue
                    if isinstance(table, dict):
                        from v3_model_binding import table_name
                        if cap not in ir.get('datasources', []) or ir['field_scopes'].get(table.get('datasource_id'), {}).get('caption') != cap:
                            raise ValueError('Relationship binding datasource identity mismatch')
                        if _drifted(drift, cap, lambda: _validate_once(active, table, ir)):
                            continue
                        available[cap] = {'table': table_name(table), 'local_file': None, 'model_plan': table}
                        continue
                    if cap not in ir.get('datasources', []) or not re.fullmatch(
                            r'[A-Za-z_][A-Za-z_0-9]*\.[A-Za-z_][A-Za-z_0-9]*\.[A-Za-z_][A-Za-z_0-9]*', table):
                        raise ValueError('Snowflake binding requires a declared datasource and DATABASE.SCHEMA.OBJECT')
                    # DM6 (E14): a single bound table is re-checked too (columns, types, access), same cadence
                    if _drifted(drift, cap, lambda: _validate_table_once(active, table, ir, cap)):
                        continue
                    available[cap] = {'table':table,'local_file':None}
                config.DATASOURCES.clear()
                config.DATASOURCES.update(available)
                config.DEFAULT_DATASOURCE = next(iter(available), None)
                config.ORDERS = engine.ORDERS = next((v['table'] for v in available.values()), '')
            else:
                available = {cap: path for cap, path in bindings.items() if path and Path(path).is_file()}
                pipeline.configure_datasources(available, db='V3_LOCAL', schema='PREVIEW')
                # DM6: the workbook's declared physical types travel with each local file (lossless preview load)
                from calc_translator import to_phys
                for did, scope in (ir.get('field_scopes') or {}).items():
                    spec = config.DATASOURCES.get(scope.get('caption'))
                    if spec is not None and spec.get('local_file'):
                        spec['types'] = {to_phys(f['physical']): f.get('datatype') for f in ir['fields'].values()
                                         if f.get('datasource_id') == did and not f.get('is_calculation')
                                         and f.get('physical') and f.get('datatype') in
                                         ('string', 'integer', 'real', 'date', 'datetime', 'boolean')}
            engine.configure(ir)
            findings.clear()
            for cap, why in drift:
                findings.record('BLOCKER', cap, 'binding-drift',
                                f"Datasource '{cap}' no longer matches its Snowflake binding ({why}); its sheets are "
                                'not shown until it is re-bound')
            yield
        finally:
            config.DATASOURCES.clear()
            config.DATASOURCES.update(old_sources)
            config.DEFAULT_DATASOURCE, config.ORDERS, engine.ORDERS = old_default, old_orders, old_engine_orders
            engine.configure(old_ir)
            engine.PARAMS = old_params
            backend._LOCAL_CON = None
            if not snowflake:
                # local previews: identical SQL text can name different local files -> never reuse.
                # Hosted: queries name real Snowflake objects; keep the 10-minute query cache.
                engine.table_columns.clear()
                engine._q_exec.clear()
            findings.clear()
            for item in old_findings:
                findings.record(**item)
