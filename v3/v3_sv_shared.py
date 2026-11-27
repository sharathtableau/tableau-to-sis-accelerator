"""Shared Semantic Views: one view per set of tables and keys, reused by every workbook (release 20a).

Why: until release 19 every migration created its own Semantic View, named from the fields that one
workbook used. Two workbooks on the same Snowflake tables got two views (four for the ECommerce model).
Leadership asked for one trusted semantic layer instead (master plan 6.0, SL02, EN40).

How (code decides; Cortex never decides a match):
1. Fingerprint = the physical tables, the fact table and the join keys. Field selection, Tableau
   captions and workbook names are not part of it.
2. The shared view is built from the tables themselves: every modellable column of every table, the
   authored keys, and generated metrics (totals, distinct keys). Workbook calculations are not put in
   the shared view; they belong to the catalogue and rationalisation (SL01, SL03).
3. Name = <FACT TABLE>_SV<LAB>_F<fingerprint>. Before anything is created, code looks for that name:
   - it exists and reproduces this workbook's model (same rows and totals)  -> reused, no DDL;
   - it exists, is ours, not certified, but no longer matches (table columns changed) -> refreshed in place;
   - it exists but belongs to another role and cannot be used (22 Oct DS_BASIC case) -> the next name
     (_R2 .. _R9) is used and the owner plus the GRANT that clears it are recorded; the run never fails;
   - it does not exist -> created.
4. The workbook reads the view through a mapping (its own output column -> view dimension). The mapping
   is checked by code and the equality proof runs before the dashboard is attached.
A view whose COMMENT contains [CERTIFIED] is never replaced (certification arrives in release 23).
"""
import hashlib
import json
import re

import v3_semantic as SV
from v3_model_binding import compile_query, IDENT

RETRY = range(2, 10)
CERTIFIED_TAG = '[CERTIFIED]'
CORTEX = '_CORTEX'
SHARED_MODE = 'shared'
_BLOCKED = re.compile(r'already exists|insufficient privileges|not authorized|does not exist or not authorized', re.I)
_REF = re.compile(r'[A-Za-z_][A-Za-z_0-9]*\.[A-Za-z_][A-Za-z_0-9]*\Z')
_SKIP_TOTAL = re.compile(r'LATITUDE|LONGITUDE|_RATE$')


# --------------------------------------------------------------------------- #
# Fingerprint and names
# --------------------------------------------------------------------------- #
def _digest(core):
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()[:10].upper()


def fingerprint(plan):
    """Physical tables, fact table and join keys only."""
    t = plan['tables']
    tables = sorted(x['table'] for x in t.values())
    if len(set(tables)) != len(tables):
        raise ValueError('The same table appears twice in the relationship tree; a shared view is not built')
    keys = sorted([t[k['first']]['table'], k['left'], t[k['second']]['table'], k['right']]
                  + ([json.dumps(k['pairs'])] if k.get('pairs') else []) for k in plan['keys'])
    return _digest({'root': t[plan['root']]['table'], 'tables': tables, 'keys': keys})


def fingerprint_table(fqn):
    return _digest({'table': fqn})


def _short(fqn):
    return SV.label(fqn.split('.')[-1]) or 'T'


def base_name(plan):
    root = plan['tables'][plan['root']]['table']
    return f'{SV.DB}.{SV.SCHEMA}.{_short(root)}_SV{SV.LAB}_F{fingerprint(plan)}'


def base_name_table(fqn):
    return f'{SV.DB}.{SV.SCHEMA}.{_short(fqn)}_SV{SV.LAB}_T{fingerprint_table(fqn)}'


def candidates(base):
    return [base] + [f'{base}_R{n}' for n in RETRY]


def allowed(base):
    names = candidates(base)
    return set(names) | {n + CORTEX for n in names}


# --------------------------------------------------------------------------- #
# Shared model (from the tables, not from the workbook)
# --------------------------------------------------------------------------- #
def table_columns(session, fqn):
    return SV.single_spec(session, fqn)['columns']


def shared_model(session, plan, columns=None, caption=None):
    """Every modellable column of every table, the authored keys, generated metrics."""
    compile_query(plan)
    t = plan['tables']
    fingerprint(plan)
    order = [plan['root']] + sorted((o for o in t if o != plan['root']), key=lambda o: t[o]['table'])
    cols = columns or {o: table_columns(session, t[o]['table']) for o in order}
    used_alias, alias = set(), {}
    for o in order:
        alias[o] = SV._alias(t[o]['table'].split('.')[-1], used_alias)
    counts = {}
    for o in order:
        for c in cols[o]:
            counts[c['column'].upper()] = counts.get(c['column'].upper(), 0) + 1
    used, dims = set(), []
    for o in order:
        for c in cols[o]:
            col = c['column']
            wanted = col if (o == plan['root'] or counts[col.upper()] == 1) else f"{alias[o].upper()}_{col}"
            dims.append({'table': alias[o], 'name': SV._dim_name(wanted, used), 'column': col,
                         'caption': re.sub(r'[_\s]+', ' ', col).strip().title(), 'datatype': c['datatype'],
                         'numeric': c['datatype'] in SV.NUMERIC})
    pk = {}
    for k in plan['keys']:
        pk.setdefault(k['second'], SV.plan_key(k, 'right'))
    tables = [{'alias': alias[o], 'table': t[o]['table'], 'caption': t[o]['table'].split('.')[-1],
               'role': 'fact' if o == plan['root'] else 'dimension', 'primary_key': pk.get(o)} for o in order]
    rels = [{'name': f"{alias[k['first']]}_to_{alias[k['second']]}", 'from': alias[k['first']],
             'from_key': SV.plan_key(k, 'left'), 'to': alias[k['second']], 'to_key': SV.plan_key(k, 'right')}
            for k in sorted(plan['keys'], key=lambda k: (t[k['first']]['table'], t[k['second']]['table'], k['left']))]
    have = {(d['table'], d['column']) for d in dims}
    for r in rels:
        if any((r['from'], c) not in have for c in SV.key_cols(r['from_key'])) or \
                any((r['to'], c) not in have for c in SV.key_cols(r['to_key'])):
            raise ValueError('A join key column is not a modellable column of its table')
    fact = alias[plan['root']]
    names, metrics = {d['name'] for d in dims}, []
    for d in dims:
        if d['table'] == fact and d['numeric'] and not SV._ID_LIKE.search(d['name']) and not _SKIP_TOTAL.search(d['name']):
            n = 'TOTAL_' + d['name']
            if n not in names:
                names.add(n)
                metrics.append({'table': fact, 'name': n, 'expr': f"SUM({fact}.{d['name']})",
                                'caption': 'Total ' + d['caption'], 'source': 'generated'})
    for r in rels:
        if isinstance(r['from_key'], list):
            continue                       # a composite key has no single distinct-count metric
        d = next(d for d in dims if d['table'] == r['from'] and d['column'] == r['from_key'])
        n = 'DISTINCT_' + d['name']
        if n not in names:
            names.add(n)
            metrics.append({'table': r['from'], 'name': n, 'expr': f"COUNT(DISTINCT {r['from']}.{d['name']})",
                            'caption': 'Distinct ' + d['caption'].lower(), 'source': 'generated'})
    return {'name': base_name(plan), 'caption': caption, 'tables': tables, 'relationships': rels, 'facts': [],
            'dimensions': dims, 'metrics': metrics, 'skipped': [],
            'origin': 'Snowflake tables and the Tableau relationship keys (shared view, one per set of tables and keys)'}


def mapping(sem, plan):
    """Each workbook output column -> 'alias.DIMENSION' in the shared view; missing columns listed."""
    by_table = {t['table']: t['alias'] for t in sem['tables']}
    dims = {(d['table'], d['column']): d['name'] for d in sem['dimensions']}
    out, missing = {}, []
    for f in plan['fields'].values():
        a = by_table.get(plan['tables'][f['owner']]['table'])
        name = dims.get((a, f['remote']))
        if name is None:
            missing.append(f['remote'])
        else:
            out[f['output']] = f'{a}.{name}'
    return out, sorted(set(missing))


def _check_map(m, plan):
    outputs = {f['output'] for f in plan['fields'].values()}
    if not isinstance(m, dict) or set(m) != outputs:
        raise ValueError('Shared view mapping does not cover the workbook columns')
    for k, v in m.items():
        if not IDENT.fullmatch(k) or not isinstance(v, str) or not _REF.fullmatch(v):
            raise ValueError('Invalid shared view mapping')
    dims = {}
    for v in m.values():
        a, d = v.split('.')
        if dims.setdefault(d, a) != a:
            raise ValueError('Ambiguous shared view dimension')


def relation(name, m):
    """Row-level relation with the workbook's column names, read through the shared view."""
    dims = sorted(set(m.values()))
    select = ', '.join(f"{v.split('.')[1]} AS {k}" for k, v in sorted(m.items()))
    return f"SELECT {select} FROM SEMANTIC_VIEW({name} DIMENSIONS {', '.join(dims)})"


def bound_relation(plan):
    """Runtime: the shared view name must derive from the plan's own tables and keys."""
    base = SV.base_plan(plan)
    name, m = plan.get('semantic_view'), plan.get('semantic_map')
    if name not in allowed(base_name(base)):
        raise ValueError('Semantic View name does not match the tables and keys of this model')
    _check_map(m, base)
    return relation(name, m)


def attach(plan, rec):
    """Binding plan that reads through the verified view (shared or, for older records, code-built)."""
    if rec.get('mode') != SHARED_MODE:
        return SV.attach(plan, rec)
    if not rec.get('verified'):
        raise ValueError('Semantic View was not verified equal to the relationship model')
    out = SV.base_plan(plan)
    _check_map(rec['map'], out)
    out.update({'semantic_view': rec['name'], 'semantic_mode': SHARED_MODE, 'semantic_map': dict(rec['map'])})
    return out


# --------------------------------------------------------------------------- #
# Snowflake look-ups
# --------------------------------------------------------------------------- #
def _dict(r):
    d = r.as_dict() if hasattr(r, 'as_dict') else dict(r)
    return {str(k).lower(): v for k, v in d.items()}


def current_role(session):
    try:
        return str(session.sql('SELECT CURRENT_ROLE()').collect()[0][0]).upper()
    except Exception:
        return None


def existing(session, base):
    """Views visible to this role whose name starts with the base name: {short name: {owner, comment}}."""
    short = base.split('.')[-1]
    try:
        rows = session.sql(f"SHOW SEMANTIC VIEWS LIKE '{short}%' IN SCHEMA {SV.DB}.{SV.SCHEMA}").collect()
    except Exception:
        return {}
    out = {}
    for r in rows:
        d = _dict(r)
        if d.get('name'):
            out[str(d['name']).upper()] = {'owner': str(d.get('owner') or '').upper(),
                                           'comment': str(d.get('comment') or '')}
    return out


def _profile(session, sql, plan):
    return SV._profile(session, sql, plan)


def verify(session, plan, name, m):
    """Same rows, totals and distinct keys as the authored relationship model."""
    base = _profile(session, compile_query(plan), plan)
    through = _profile(session, relation(name, m), plan)
    checks, ok = [], True
    for k, v in base.items():
        w = through.get(k)
        same = (v == w) if not isinstance(v, (int, float)) or v is None or w is None else \
            abs(float(v) - float(w)) <= 1e-6 * max(1.0, abs(float(v)))
        ok &= bool(same)
        checks.append({'Check': 'Row count' if k == 'ROWS_' else ('Total ' if k.startswith('S_') else 'Distinct ') + k[2:],
                       'Relationship model': v, 'Semantic View': w, 'Equal': bool(same)})
    return ok, checks


def _grant(name, owner):
    return (f'USE ROLE {owner}; GRANT OWNERSHIP ON SEMANTIC VIEW {name} TO ROLE WBR_OWNER COPY CURRENT GRANTS;'
            if owner else f'-- {name} exists but is not visible to this role; its owner must grant access')


# --------------------------------------------------------------------------- #
# Decide: reuse, refresh, create, or move past a blocked name
# --------------------------------------------------------------------------- #
def _place(session, base, build, check, enrich=None):
    """Shared decision loop. build(name) -> sem for that name; check(sem) -> (ok, checks)."""
    role, seen = current_role(session), existing(session, base)
    attempts, blocked = [], []
    for name in candidates(base):
        short = name.split('.')[-1]
        info = seen.get(short)
        if info is not None:
            sem = build(name)
            try:
                ok, checks = check(sem)
            except Exception as exc:
                ok, checks = False, []
                attempts.append({'Attempt': f'read existing {short}', 'Result': type(exc).__name__ + ': ' + str(exc)[:160]})
            if ok:
                attempts.append({'Attempt': f'read existing {short}', 'Result': 'equal'})
                return {'action': 'reused', 'sem': sem, 'checks': checks, 'attempts': attempts, 'owner': info['owner'],
                        'blocked': blocked, 'statement': None}
            certified = CERTIFIED_TAG in info['comment']
            if info['owner'] == role and not certified:
                action = 'refreshed'
            else:
                blocked.append({'View': short, 'Owner': info['owner'] or 'unknown',
                                'Reason': 'certified view; never replaced' if certified else 'owned by another role',
                                'Grant that clears it': '' if certified else _grant(name, info['owner'])})
                continue
        else:
            action = 'created'
            sem = build(name)
        if enrich:
            try:
                enrich(sem)
            except Exception:
                pass
        try:
            sem, statement, tries = SV._define(session, sem)
        except RuntimeError as exc:
            if _BLOCKED.search(str(exc)):
                blocked.append({'View': short, 'Owner': 'not visible to ' + str(role or 'this role'),
                                'Reason': 'exists but cannot be replaced by this role', 'Grant that clears it': _grant(name, None)})
                attempts.append({'Attempt': f'create {short}', 'Result': 'blocked: ' + str(exc)[:160]})
                continue
            raise
        attempts += tries
        ok, checks = check(sem)
        attempts.append({'Attempt': f'read through {short}', 'Result': 'equal' if ok else 'not equal'})
        return {'action': action, 'sem': sem, 'checks': checks, 'attempts': attempts, 'owner': role,
                'blocked': blocked, 'statement': statement}
    raise RuntimeError('No usable shared view name: every candidate is owned by another role or certified')


def _record(placed, base, m=None, ask=None):
    sem = placed['sem']
    ok = all(c['Equal'] for c in placed['checks']) and bool(placed['checks'])
    return {'name': sem['name'], 'ddl': placed['statement'] or SV.ddl(sem), 'verified': ok, 'checks': placed['checks'],
            'mode': SHARED_MODE, 'map': m, 'metrics': [], 'skipped': [], 'model': sem, 'attempts': placed['attempts'],
            'ask': ask if ok else None,
            'decision': {'action': placed['action'], 'base': base, 'owner': placed['owner'], 'blocked': placed['blocked']}}


def ensure(session, plan, caption=None, enrich=None):
    """Relationship datasource: reuse, refresh or create the shared view; prove it; never attach unproven."""
    plan = SV.base_plan(plan)
    base = base_name(plan)
    shared = shared_model(session, plan, caption=caption)
    m, missing = mapping(shared, plan)
    if missing:
        raise ValueError('Columns cannot be modelled in a shared view: ' + ', '.join(missing[:5]))

    def build(name):
        return dict(json.loads(json.dumps(shared)), name=name)

    placed = _place(session, base, build, lambda sem: verify(session, plan, sem['name'], m), enrich)
    rec = _record(placed, base, m)
    if rec['verified']:
        rec['metrics'] = SV.metric_check(session, rec['model'])
    return rec


def ensure_single(session, table, caption=None, enrich=None):
    """Single-table datasource (Ask the data): one shared view per table."""
    spec = SV.single_spec(session, table)
    base = base_name_table(table)
    short = table.split('.')[-1]

    def build(name):
        sem = SV.single_model(spec, None, short)
        sem.update({'name': name, 'caption': caption or short})
        return sem

    def check(sem):
        return SV.verify_single(session, sem)

    placed = _place(session, base, build, check, enrich)
    core = SV._single_core(spec)
    ask = dict(core, semantic_view=placed['sem']['name'])
    rec = _record(placed, base, ask=ask)
    rec['mode'] = SV.SINGLE_MODE
    if rec['verified']:
        rec['metrics'] = SV.metric_check(session, rec['model'])
    return rec


def single_allowed(entry):
    """Ask-the-data entry naming a shared single-table view of exactly its own table."""
    return entry.get('semantic_view') in allowed(base_name_table(entry.get('table') or ''))


def cortex_variant(session, run, plan, ir, caption, code_rec):
    """Cortex-designed alternative of the shared view: reused when it exists and is proven, else designed once."""
    import v3_cortex_model as CM
    plan = SV.base_plan(plan)
    if not code_rec or code_rec.get('mode') != SHARED_MODE or not code_rec.get('verified'):
        # No verified shared view to align with: do not create a per-workbook view (that is how duplicates began).
        raise ValueError('The Cortex design needs the verified shared view of these tables first')
    name, m = code_rec['name'] + CORTEX, code_rec['map']
    seen = existing(session, code_rec['name'])
    short = name.split('.')[-1]
    if short in seen:
        try:
            ok, checks = verify(session, plan, name, m)
        except Exception:
            ok, checks = False, []
        return {'name': name, 'verified': ok, 'checks': checks, 'mode': SHARED_MODE, 'map': m, 'reused': True,
                'model': {'name': name, 'relationships': [], 'metrics': []}, 'rejected': [], 'attempts': [],
                'relationships_vs_tableau': {'authored': len(plan['keys']), 'proposed': None, 'same': None,
                                             'missing': [], 'extra': [], 'fact_matches_tableau': None},
                'proposal_notes': 'Existing Cortex-designed shared view reused (no new Cortex call).',
                'metrics': [], 'ddl': '', 'decision': {'action': 'reused', 'owner': seen[short]['owner'], 'blocked': []}}
    proposal = CM.propose(session, run, plan, ir, caption)
    sem, rejected, comparison = CM.compile_model(plan, proposal, ir, caption, code=code_rec['model'])
    sem['name'] = name
    record = {'name': name, 'proposal_notes': str(proposal.get('design_notes') or '')[:600], 'rejected': rejected,
              'relationships_vs_tableau': comparison, 'model': sem, 'verified': False, 'checks': [], 'attempts': [],
              'metrics': [], 'ddl': SV.ddl(sem), 'mode': SHARED_MODE, 'map': m,
              'decision': {'action': 'created', 'owner': current_role(session), 'blocked': []}}
    if not sem['relationships'] and len(plan['tables']) > 1:
        record['attempts'].append({'Attempt': 'compile', 'Result': 'no valid relationships; not created'})
        return record
    sem, statement, attempts = SV._define(session, sem)
    ok, checks = verify(session, plan, name, m)
    record.update({'ddl': statement, 'attempts': attempts, 'verified': ok, 'checks': checks, 'model': sem,
                   'metrics': SV.metric_check(session, sem) if ok else []})
    return record


# --------------------------------------------------------------------------- #
# What the person sees
# --------------------------------------------------------------------------- #
def summary(rec):
    d = (rec or {}).get('decision') or {}
    name = str((rec or {}).get('name') or '').split('.')[-1]
    text = {'reused': f'Reused the existing shared view {name}: no new view was created.',
            'refreshed': f'Refreshed the shared view {name} in place (its tables changed); no new view was created.',
            'created': f'Created the shared view {name}. Any later workbook on the same tables and keys reuses it.'}.get(d.get('action'))
    if text and d.get('blocked'):
        text += f" {len(d['blocked'])} earlier name(s) could not be used (owned by another role); see below."
    return text


def log_rows(run, cap, rec, kind):
    """Registry event rows (SEMANTIC_VIEW_EVENTS)."""
    d = rec.get('decision') or {}
    return [{'VIEW_NAME': rec.get('name'), 'BASE_NAME': d.get('base'), 'KIND': kind, 'ACTION': d.get('action'),
             'VERIFIED': bool(rec.get('verified')), 'OWNER_ROLE': d.get('owner'), 'DATASOURCE': cap,
             'WORKBOOK_NAME': ((run.get('result') or {}).get('ir') or {}).get('workbook')
             or run.get('workbook_name') or run.get('name'),
             'TABLES': [t.get('table') for t in (rec.get('model') or {}).get('tables') or []],
             'BLOCKED': d.get('blocked') or []}]
