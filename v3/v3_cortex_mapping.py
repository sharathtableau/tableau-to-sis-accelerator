"""Release 21b-B (CF06, AI02): Cortex proposes which existing Snowflake table holds a datasource's data.

For a datasource that is still unbound after every code route (authored Snowflake table, same-named table,
data loaded from the workbook, laptop extract load), the data may already be in Snowflake under other names.

1. Code lists candidate tables in WBR_DB (INFORMATION_SCHEMA, read-only) and ranks them by how many of the
   workbook's field names match their column names (exact after normalising, then close spellings).
2. Cortex (AI_COMPLETE, typed output) picks ONE candidate table and maps every workbook field to one of its
   columns. Table and column names come from closed lists in the response schema; Cortex writes no SQL.
3. Code checks every pair: the table is a candidate, the column exists in it, the type is compatible with the
   workbook field, nothing is left unmapped; it counts the rows (read-only).
4. A person approves. Same names as the engine uses: the table is bound as it is. Renamed columns: code creates a
   view in WBR_DB.PIPELINE_DEMO (SELECT "column" AS FIELD ...) over the table — never replacing an object —
   and binds it after the same column and type check.
Cortex never binds anything; a failed Cortex call shows only that it failed (no error text).
"""
import difflib
import hashlib
import json
import re

import v3_data_load as DL

SCAN_DB = 'WBR_DB'
SKIP_SCHEMAS = ('INFORMATION_SCHEMA', 'TABLEAU_ACCELERATOR_V3_EVIDENCE')
MAX_CANDIDATES = 6
MAX_COLUMNS = 300
TAG = 'v3map'
_PLAIN = re.compile(r'[A-Z_][A-Z0-9_$]*\Z')


def _norm(name):
    return re.sub(r'[^A-Z0-9]', '', str(name).upper())


def _sf_type(data_type, scale):
    t = str(data_type or '').upper()
    if t in ('NUMBER', 'DECIMAL', 'NUMERIC'):
        return f'NUMBER(38,{int(scale or 0)})'
    if t == 'TEXT':
        return 'VARCHAR'
    return t


def catalog(session, db=SCAN_DB):
    """{DB.SCHEMA.TABLE: [{'column', 'type'}]} for tables and views this role can see (unquoted names only)."""
    skip = ', '.join(f"'{s}'" for s in SKIP_SCHEMAS)
    rows = session.sql(f'SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE, NUMERIC_SCALE '
                       f'FROM {db}.INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA NOT IN ({skip}) '
                       'ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION').collect()
    out = {}
    for r in rows:
        d = {str(k).upper(): v for k, v in (r.as_dict() if hasattr(r, 'as_dict') else dict(r)).items()}
        sch, tab = str(d['TABLE_SCHEMA']), str(d['TABLE_NAME'])
        if not (_PLAIN.fullmatch(sch) and _PLAIN.fullmatch(tab)):
            continue                                     # quoted / mixed-case objects need an explicit binding
        cols = out.setdefault(f'{db}.{sch}.{tab}', [])
        if len(cols) < MAX_COLUMNS:
            cols.append({'column': str(d['COLUMN_NAME']), 'type': _sf_type(d['DATA_TYPE'], d.get('NUMERIC_SCALE'))})
    return out


def rank(fields, cat, limit=MAX_CANDIDATES):
    """Candidate tables, best first: exact normalised name matches count 1, close spellings 0.5."""
    want = [_norm(f['physical']) for f in fields]
    scored = []
    for fqn, cols in cat.items():
        names = [_norm(c['column']) for c in cols]
        have = set(names)
        score = 0.0
        for w in want:
            if w in have:
                score += 1
            elif difflib.get_close_matches(w, names, n=1, cutoff=0.85):
                score += 0.5
        if score:
            scored.append((score, -abs(len(cols) - len(want)), fqn))
    scored.sort(reverse=True)
    return {fqn: cat[fqn] for _s, _d, fqn in scored[:limit]}


def _schema(fields, cands):
    columns = sorted({c['column'] for cols in cands.values() for c in cols}) + ['NONE']
    return {'type': 'object', 'properties': {
        'table': {'type': 'string', 'enum': sorted(cands) + ['NONE']},
        'mappings': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'field': {'type': 'string', 'enum': sorted({f['physical'] for f in fields})},
            'column': {'type': 'string', 'enum': columns}},
            'required': ['field', 'column'], 'additionalProperties': False}},
        'reason': {'type': 'string'}},
        'required': ['table', 'mappings', 'reason'], 'additionalProperties': False}


def propose(session, run, cap, fields, cands):
    import v3_ai as A
    evidence = {'tableau_datasource': cap,
                'workbook_fields': [{'field': f['physical'], 'caption': f.get('caption'), 'type': f.get('datatype')}
                                    for f in fields],
                'candidate_tables': [{'table': t, 'columns': cols} for t, cols in cands.items()]}
    task = ('A Tableau datasource must be bound to the Snowflake table that holds its data. Choose the ONE candidate '
            'table that holds this data (or NONE), and map every workbook field to the column of that table that '
            'holds it (or NONE). Use only the given table and column names. Prefer the table where the most fields '
            'map with compatible types. Explain the choice in one sentence.')
    return A.call(session, run, 'Data model', 'Cortex table mapping — ' + str(cap), A._wrap(task, evidence),
                  _schema(fields, cands), max_tokens=4000)


def check(session, proposal, fields, cands):
    """Code's ruling on Cortex's proposal. Nothing here writes."""
    from calc_translator import to_phys
    from v3_source_binding import compatible
    table = proposal.get('table')
    out = {'table': table, 'reason': str(proposal.get('reason') or '')[:400], 'pairs': [], 'missing': [],
           'ok': False, 'identity': False, 'rows': None}
    if table not in cands:
        out['verdict'] = 'Cortex chose no candidate table' if table in (None, 'NONE') else 'Table is not a candidate'
        return out
    cols = {c['column']: c['type'] for c in cands[table]}
    chosen = {}
    for m in proposal.get('mappings') or []:
        if isinstance(m, dict) and m.get('field') not in chosen:
            chosen[m.get('field')] = m.get('column')
    bad = 0
    for f in fields:
        col = chosen.get(f['physical'])
        if not col or col == 'NONE':
            out['missing'].append(f['physical'])
            out['pairs'].append({'Field': f['physical'], 'Type': f.get('datatype'), 'Column': None,
                                 'Column type': None, 'Check': 'not mapped'})
            continue
        if col not in cols:
            bad += 1
            out['pairs'].append({'Field': f['physical'], 'Type': f.get('datatype'), 'Column': col,
                                 'Column type': None, 'Check': 'column is not in the table'})
            continue
        ok = compatible(f.get('datatype'), cols[col])
        bad += 0 if ok else 1
        out['pairs'].append({'Field': f['physical'], 'Type': f.get('datatype'), 'Column': col,
                             'Column type': cols[col], 'Check': 'ok' if ok else 'type does not fit'})
    out['ok'] = not out['missing'] and not bad and bool(fields)
    out['identity'] = out['ok'] and all(p['Column'] == to_phys(p['Field']) for p in out['pairs'])
    out['verdict'] = ('Every field maps with a compatible type' if out['ok'] else
                      f"{len(out['missing'])} field(s) not mapped, {bad} with a wrong column or type")
    if out['ok']:
        out['rows'] = int(session.sql(f'SELECT COUNT(*) AS N FROM {table}').collect()[0][0])
    return out


def suggest(session, run, cap, fields):
    """Rank, ask Cortex, check. Returns the record kept on the run (never raises Cortex error text)."""
    import v3_ai as A
    cands = rank(fields, catalog(session))
    rec = {'caption': cap, 'candidates': list(cands), 'fields': len(fields)}
    if not cands:
        rec.update(verdict='No table in WBR_DB has columns like these fields', ok=False)
        return rec
    try:
        proposal = propose(session, run, cap, fields, cands)
    except A.CortexUnavailable as exc:
        rec.update(verdict=str(exc), ok=False, error=True)
        return rec
    rec['proposal'] = proposal
    rec.update(check(session, proposal, fields, cands))
    return rec


# --------------------------------------------------------------------------- #
# Apply (after a person approves)
# --------------------------------------------------------------------------- #
def _describe(session, fqn):
    try:
        rows = session.sql('DESCRIBE TABLE ' + fqn).collect()
    except Exception:
        rows = session.sql('DESCRIBE VIEW ' + fqn).collect()
    out = {}
    for r in rows:
        d = {str(k).lower(): v for k, v in (r.as_dict() if hasattr(r, 'as_dict') else dict(r)).items()}
        if str(d.get('kind') or 'COLUMN').upper() == 'COLUMN' and d.get('name'):
            out[str(d['name'])] = str(d.get('type') or '')
    return out


def check_object(session, fqn, fields):
    """The bound table or view has every field under the engine's name with a compatible type."""
    from calc_translator import to_phys
    from v3_source_binding import compatible
    schema = _describe(session, fqn)
    problems = [f"{to_phys(f['physical'])}: {'missing' if to_phys(f['physical']) not in schema else 'type'}"
                for f in fields if not (to_phys(f['physical']) in schema
                                        and compatible(f.get('datatype'), schema[to_phys(f['physical'])]))]
    if problems:
        raise ValueError('Binding check failed: ' + '; '.join(problems[:8]))
    session.sql(f'SELECT * FROM {fqn} LIMIT 0').collect()
    return fqn


def view_sql(fqn, rec, fields, note):
    from calc_translator import to_phys
    from v3_model_binding import quote
    from v3_source_binding import FQN
    if not FQN.fullmatch(rec['table']) or not FQN.fullmatch(fqn):
        raise ValueError('Invalid object name')
    by = {p['Field']: p['Column'] for p in rec['pairs']}
    select = ', '.join(f"{quote(by[f['physical']])} AS {to_phys(f['physical'])}" for f in fields)
    return f"CREATE VIEW {fqn} COMMENT = '{note.replace(chr(39), chr(39) * 2)}' AS SELECT {select} FROM {rec['table']}"


def _views(session, base):
    rows = session.sql(f"SHOW VIEWS LIKE '{base}%' IN SCHEMA {DL.LOAD_DB}.{DL.LOAD_SCHEMA}").collect()
    out = {}
    for r in rows:
        d = {str(k).lower(): v for k, v in (r.as_dict() if hasattr(r, 'as_dict') else dict(r)).items()}
        if d.get('name'):
            out[str(d['name']).upper()] = str(d.get('comment') or '')
    return out


def apply(session, cap, rec, fields, who=None):
    """Bind the checked proposal: the table itself (same names) or a mapping view. Returns (fqn, action)."""
    if not rec.get('ok'):
        raise ValueError('The proposal did not pass the checks')
    if rec.get('identity'):
        return check_object(session, rec['table'], fields), 'bound to the table as it is'
    sha = hashlib.sha256(json.dumps({'table': rec['table'], 'pairs': [(p['Field'], p['Column']) for p in rec['pairs']]},
                                    sort_keys=True).encode()).hexdigest()
    base = DL.target_base(cap) + '_MAP'
    views = _views(session, base)
    taken = set(views) | set(DL._tables(session, base))
    for name in [base] + [f'{base}_V{n}' for n in DL.VERSIONS]:
        fqn = f'{DL.LOAD_DB}.{DL.LOAD_SCHEMA}.{name}'
        if name in views and f'map_sha256={sha}' in views[name]:
            return check_object(session, fqn, fields), 'reused the mapping view'
        if name in taken:
            continue
        DL._ensure_schema(session)
        note = f"{TAG} map_sha256={sha} source={rec['table']} by={who or 'unknown'}"
        session.sql(view_sql(fqn, rec, fields, note)).collect()
        return check_object(session, fqn, fields), 'created the mapping view'
    raise RuntimeError(f'{base} and versions _V2.._V9 all exist; retire old versions first')


def proposal_row(cap, rec, bound):
    return {'STAGE': 'Data model', 'KIND': 'table_mapping', 'SUBJECT': cap,
            'PROPOSAL': {'table': rec.get('table'), 'mappings': (rec.get('proposal') or {}).get('mappings'),
                         'reason': rec.get('reason')},
            'CODE_CHECK': 'Table is a candidate; every column exists with a compatible type; rows counted read-only',
            'CODE_VERDICT': rec.get('verdict'), 'PASSED_CODE_CHECK': bool(rec.get('ok')) if 'ok' in rec else None,
            'CHANGES_APP': bool(bound and bound == rec.get('bound_to')),
            'EVIDENCE': {'candidates': rec.get('candidates'), 'pairs': rec.get('pairs'), 'rows': rec.get('rows')}}
