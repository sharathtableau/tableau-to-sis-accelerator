"""Release 21b-A: load each logical table once, as its own Snowflake table, and model the relationships.

21a loads a relationship datasource (Orders / People / Returns) as ONE joined table per workbook, so three
workbooks on the same data make three copies. Here each logical table is loaded once:

- table name = the logical table's name (ORDERS, PEOPLE, RETURNS); _V2.._V9 when different data is there;
- fingerprint = the DATA (shaped rows, any order), so the same Orders from an .xls or a .hyper extract lands in
  the same table; the table comment also lists every source file fingerprint that produced it, so the hosted
  app finds tables the laptop loader made from an extract it cannot decode itself;
- the relationship model over those tables is built from the authored keys, proven in memory before anything
  is written (the same planner as 21a), then checked in Snowflake: dimension keys unique, fact rows unchanged.
  A fact row with no matching dimension row is allowed and counted (its dimension fields are empty, as in
  Tableau); the binding plan says so;
- the plan is an ordinary relationship plan (source = 'loaded'), so the release 20a shared Semantic View is
  created or reused over these tables like any Snowflake model.

If any step fails, the caller falls back to the 21a joined table and shows the reason.
"""
import hashlib

import v3_data_load as DL

SOURCE = 'loaded'


def multi(root, ir, cap):
    """Logical tables of a datasource with more than one (relationship model), else {}."""
    from semantic_layer import _objects
    _did, ds = DL._ds_by_caption(root, ir, cap)
    objs = _objects(ds)
    return objs if len(objs) > 1 else {}


def IW_object_id(record):
    import init_workbook as IW
    return IW.metadata_object_id(record)


def owners(ds, ir, did):
    """Workbook field -> owning logical table, source column, type and the engine's physical name."""
    irf = {f['internal']: f for f in DL.fields_for(ir, did)}
    from semantic_layer import _objects
    by_caption = {}
    for oid, o in _objects(ds).items():
        by_caption.setdefault(o['caption'], []).append(oid)
    out = {}
    for mr in ds.findall('.//metadata-record'):
        if mr.get('class') != 'column':
            continue
        local = (mr.findtext('local-name') or '').strip('[]')
        oid = IW_object_id(mr)
        if not oid:
            # R47 (as DM3 in v3_model_binding): a record that names its table only by caption (<parent-name>)
            # belongs to the one logical table with that caption
            same = by_caption.get((mr.findtext('parent-name') or '').strip('[]')) or []
            oid = same[0] if len(same) == 1 else ''
        remote = mr.findtext('remote-name')
        if not local or not oid or remote is None:
            continue
        f = irf.get(local) or {}
        out[local] = {'owner': oid, 'remote': remote, 'datatype': f.get('datatype') or mr.findtext('local-type'),
                      'physical': f.get('physical') or local}
    return out


def frames_for(raw, loc, decoded=None):
    """{logical table id: DataFrame} for a relationship datasource, else None."""
    if isinstance(decoded, dict):
        return dict(decoded) if len(decoded) > 1 else None
    if decoded is None and loc.get('kind') == 'file' and len(loc.get('files') or []) > 1:
        data = {}                       # R47: each logical table from its own file (read once per file)
        for p in loc['files']:
            if p['member'] not in data:
                data[p['member']] = DL._read_member(raw, p['member'])
        return {p['object']: DL._read(data[p['member']], p['member'], p['sheet']) for p in loc['files']}
    return None


def data_sha(df):
    import pandas as pd
    h = hashlib.sha256('|'.join(f'{c}:{df[c].dtype}' for c in df.columns).encode())
    h.update(pd.util.hash_pandas_object(df, index=False).sort_values().values.tobytes())
    return h.hexdigest()


def shape(root, ir, loc, frames, extract):
    """Each logical table shaped to its own workbook fields; dimension duplicates removed; proven joinable."""
    import init_workbook as IW
    from semantic_layer import _objects
    did, ds = DL._ds_by_caption(root, ir, loc['caption'])
    own, objs = owners(ds, ir, did), _objects(ds)
    rels = [r for r in IW.parse_relationships(root, strict=True) if r['first'] in frames and r['second'] in frames]
    dims = {r['second'] for r in rels}
    if set(frames) != set(objs):
        raise ValueError('The data does not hold exactly the logical tables of the workbook')
    out, notes, raw_frames = {}, [], {}
    for oid, df in frames.items():
        cap = objs[oid]['caption']
        if oid in dims:
            n = len(df)
            df = df.drop_duplicates().reset_index(drop=True)
            if len(df) < n:
                notes.append(f'{cap}: {n - len(df)} exact duplicate rows removed')
        raw_frames[oid] = df.copy()
        flds = [{'physical': o['remote'], 'datatype': o['datatype'], 'internal': local}
                for local, o in own.items() if o['owner'] == oid]
        shaped, fixes, kept, absent = DL.shape(df, flds, extract=extract)
        if absent:
            notes.append(f"{cap}: not in the extract (Tableau keeps only the fields it uses): {', '.join(absent)}")
        out[oid] = {'caption': cap, 'frame': shaped, 'fixes': fixes, 'kept': kept, 'sha': data_sha(shaped),
                    'target': DL.target_base(cap), 'rows': len(shaped), 'columns': len(shaped.columns)}
    DL.flatten(raw_frames, root)          # in-memory proof on the source names: keys unique, tree joinable
    return out, notes, own, ds


def plan_for(ds, own, tables, present):
    """Relationship plan (v3_model_binding format) over the loaded tables."""
    import init_workbook as IW
    from calc_translator import to_phys
    from semantic_layer import _objects, join_plan
    from v3_model_binding import VERSION, compile_query
    objs = _objects(ds)
    graph = join_plan(objs, IW.parse_relationships(ds, strict=True))
    if not graph['steps'] or len(graph['steps']) != len(objs) - 1:
        raise ValueError('A complete single-fact relationship tree is required')
    fields, outputs = {}, set()
    for local, o in own.items():
        col = to_phys(o['remote'])
        if o['owner'] not in tables or col not in present[o['owner']]:
            continue
        out = to_phys(o['physical'])
        if out in outputs:
            raise ValueError('Two workbook fields map to the same column name: ' + out)
        outputs.add(out)
        fields[local] = {'owner': o['owner'], 'remote': col, 'datatype': o['datatype'], 'output': out}
    keys = []
    for rel in ds.findall('.//relationship'):
        expr = rel.find('expression')
        left, right = [e.get('op').strip('[]') for e in expr]
        a, b = rel.find('first-end-point').get('object-id'), rel.find('second-end-point').get('object-id')
        if left not in fields or right not in fields:
            raise ValueError('A relationship key column is not in the loaded data')
        keys.append({'first': a, 'second': b, 'left': fields[left]['remote'], 'right': fields[right]['remote']})
    plan = {'version': VERSION, 'source': SOURCE, 'datasource_id': ds.get('name'), 'server': '',
            'root': graph['root'], 'tables': {oid: {'table': fqn, 'caption': objs[oid]['caption']}
                                              for oid, fqn in tables.items()},
            'fields': fields, 'keys': keys}
    compile_query(plan)
    return plan


def check(session, plan):
    """Snowflake check for a loaded-tables plan (v3_model_binding.validate routes here)."""
    from v3_model_binding import compile_query, quote
    from v3_source_binding import rows
    from v3_model_binding import one_row_per_key, _source
    compile_query(plan)
    notes, edges = [], []
    t = plan['tables']
    for x in t.values():
        x.pop('distinct', None)
    for k in plan['keys']:
        a, b = t[k['first']]['table'], t[k['second']]['table']
        l, r = quote(k['left']), quote(k['right'])
        c = rows(session, f'SELECT COUNT(*) AS N, COUNT({r}) AS NONNULL, COUNT(DISTINCT {r}) AS UNIQUE_KEYS FROM {b}')[0]
        if c['UNIQUE_KEYS'] < c['NONNULL']:
            # DM8: the key repeats -- one row per key when every column is constant per key (else refused)
            one_row_per_key(session, plan, k['second'], (), c['UNIQUE_KEYS'])
            notes.append(f"{t[k['second']]['caption']}: its key repeats; every column is constant per key, read as "
                         'one row per key')
        m = rows(session, f'SELECT COUNT(*) AS N FROM {a} a LEFT JOIN {_source(plan, k["second"])} b ON a.{l} = b.{r} '
                          f'WHERE a.{l} IS NOT NULL AND b.{r} IS NULL')[0]['N']
        if m:
            notes.append(f"{int(m):,} {t[k['first']]['caption']} rows have no {t[k['second']]['caption']} row: "
                         'its fields are empty for them, as in Tableau')
        edges.append({'dimension_rows': c['N'], 'unmatched_keys': m})
    sql = compile_query(plan)
    fact = rows(session, 'SELECT COUNT(*) AS N FROM ' + t[plan['root']]['table'])[0]['N']
    count = rows(session, 'SELECT COUNT(*) AS N FROM (' + sql + ')')[0]['N']
    if fact != count:
        raise ValueError('Fact row count changed')
    return {'fact_rows': fact, 'output_rows': count, 'edges': edges, 'grain': 'fact', 'notes': notes,
            'tableau_relationship_equivalence': 'NOT_VERIFIED'}


# --------------------------------------------------------------------------- #
# Snowflake: find, load
# --------------------------------------------------------------------------- #
def _by_comment(session, base, needle):
    tables = DL._tables(session, base)
    for name in [base] + [f'{base}_V{n}' for n in DL.VERSIONS]:
        c = tables.get(name)
        if c is not None and DL.TAG in c and needle in c:
            return f'{DL.LOAD_DB}.{DL.LOAD_SCHEMA}.{name}', c
    return None, None


def find_model(session, root, ir, cap, src_sha):
    """Hosted: tables loaded earlier from exactly this source (any of its logical tables), as a checked plan."""
    from calc_translator import to_phys
    from semantic_layer import _objects
    did, ds = DL._ds_by_caption(root, ir, cap)
    objs = _objects(ds)
    tables, present = {}, {}
    for oid, o in objs.items():
        fqn, _c = _by_comment(session, DL.target_base(o['caption']), f'src_sha256={src_sha}')
        if not fqn:
            return None
        tables[oid] = fqn
        present[oid] = {str((r.as_dict() if hasattr(r, 'as_dict') else dict(r)).get('name')).upper()
                        for r in session.sql('DESCRIBE TABLE ' + fqn).collect()}
    plan = plan_for(ds, owners(ds, ir, did), tables, present)
    plan['checks'] = check(session, plan)
    return plan


def load(session, raw, root, ir, cap, loc, frames, extract, who=None, via='app'):
    """Load each logical table once (reuse by data fingerprint), build and check the plan. Returns the record."""
    import datetime
    shaped, notes, own, ds = shape(root, ir, loc, frames, extract)
    DL._ensure_schema(session)
    tables, actions = {}, []
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
    for oid, e in shaped.items():
        fqn, comment = _by_comment(session, e['target'], f"data_sha256={e['sha']}")
        if fqn:
            if f"src_sha256={loc['sha256']}" not in comment:
                new = comment + f" src_sha256={loc['sha256']}"
                session.sql(f"COMMENT ON TABLE {fqn} IS '{new.replace(chr(39), chr(39) * 2)}'").collect()
            tables[oid] = fqn
            actions.append(f"{e['caption']}: reused {fqn.split('.')[-1]}")
            continue
        taken = DL._tables(session, e['target'])
        name = next((n for n in [e['target']] + [f"{e['target']}_V{v}" for v in DL.VERSIONS] if n not in taken), None)
        if not name:
            raise RuntimeError(f"{e['target']} and versions _V2.._V9 all exist; retire old versions first")
        fqn = f'{DL.LOAD_DB}.{DL.LOAD_SCHEMA}.{name}'
        session.write_pandas(e['frame'], name, database=DL.LOAD_DB, schema=DL.LOAD_SCHEMA, auto_create_table=True,
                             overwrite=False, quote_identifiers=False)
        DL._fix_types(session, fqn, e['fixes'])
        n = int(session.sql(f'SELECT COUNT(*) AS N FROM {fqn}').collect()[0][0])
        if n != e['rows']:
            raise RuntimeError(f"{fqn}: {n} rows in Snowflake, {e['rows']} in the data")
        note = (f"{DL.TAG} data_sha256={e['sha']} src_sha256={loc['sha256']} table={e['caption']} rows={n} "
                f"via={via} by={who or 'unknown'} at={stamp}")
        session.sql(f"COMMENT ON TABLE {fqn} IS '{note.replace(chr(39), chr(39) * 2)}'").collect()
        tables[oid] = fqn
        actions.append(f"{e['caption']}: loaded {name}")
    plan = plan_for(ds, own, tables, {oid: set(e['frame'].columns) for oid, e in shaped.items()})
    checks = check(session, plan)
    plan['checks'] = checks
    root_rows = shaped[plan['root']]['rows']
    return dict(loc, table=plan, bound=True, action='; '.join(actions), rows=root_rows,
                columns=sum(e['columns'] for e in shaped.values()), notes=notes + checks['notes'],
                entities=[{'table': tables[o], 'rows': e['rows'], 'caption': e['caption']} for o, e in shaped.items()])


def find_by_data(session, root, ir, loc, frames, extract=False):
    """Read-only (release 23): a checked plan over loaded tables that already hold exactly this data (every logical
    table found by its data fingerprint, under any name), or None. Nothing is written."""
    shaped, _notes, own, ds = shape(root, ir, loc, frames, extract)
    tables = {}
    for oid, e in shaped.items():
        hit = DL.find_by_data(session, e['sha'], e['caption'])
        if not hit:
            return None
        tables[oid] = hit[0]
    plan = plan_for(ds, own, tables, {oid: set(e['frame'].columns) for oid, e in shaped.items()})
    plan['checks'] = check(session, plan)
    return plan


def plan_summary(root, ir, loc, frames, extract):
    """What the person sees before approving: one line per logical table."""
    shaped, notes, _own, _ds = shape(root, ir, loc, frames, extract)
    return [{'caption': e['caption'], 'target': e['target'], 'rows': e['rows'], 'columns': e['columns']}
            for e in shaped.values()], notes
