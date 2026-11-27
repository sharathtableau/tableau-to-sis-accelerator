"""Cortex designs the Semantic View; code validates, creates and proves it (Cortex lab).

Cortex (AI_COMPLETE, structured output) receives the logical tables of a Tableau relationship
datasource and their real Snowflake columns -- WITHOUT Tableau's relationship keys -- and designs
the semantic model: which table is the fact, primary keys, relationships, metrics, descriptions
and synonyms. Its answer is a typed structure (ids and column names from closed lists, metric =
aggregation + column), never SQL.

Code then:
1. validates every table, column, key and aggregation against the plan (anything unknown is
   rejected and listed);
2. compiles the DDL with the same generator the code-built view uses, under a separate name
   (..._CORTEX), so the dashboard's view is never replaced;
3. proves it with the same equality check as the code-built view (rows, every total, distinct keys
   versus the authored Tableau relationship model);
4. compares Cortex's relationships with the ones authored in Tableau.

The dashboard keeps reading the code-built view. Nothing here changes a binding or a verdict.
"""
import re

AGGS = {'SUM': 'SUM({})', 'AVG': 'AVG({})', 'MIN': 'MIN({})', 'MAX': 'MAX({})', 'COUNT': 'COUNT({})',
        'COUNT_DISTINCT': 'COUNT(DISTINCT {})'}
SUFFIX = '_CORTEX'


def _schema(table_ids, columns):
    tid = {'type': 'string', 'enum': table_ids}
    col = {'type': 'string', 'enum': columns}
    text = {'type': 'string'}
    syn = {'type': 'array', 'items': {'type': 'string'}}
    return {'type': 'object', 'properties': {
        'fact_table': tid,
        'tables': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'table_id': tid, 'primary_key': {'type': 'string', 'enum': columns + ['NONE']},
            'description': text, 'synonyms': syn},
            'required': ['table_id', 'primary_key', 'description', 'synonyms'], 'additionalProperties': False}},
        'relationships': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'from_table': tid, 'from_column': col, 'to_table': tid, 'to_column': col},
            'required': ['from_table', 'from_column', 'to_table', 'to_column'], 'additionalProperties': False}},
        'columns': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'table_id': tid, 'column': col, 'description': text, 'synonyms': syn},
            'required': ['table_id', 'column', 'description', 'synonyms'], 'additionalProperties': False}},
        'metrics': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'name': text, 'table_id': tid, 'aggregation': {'type': 'string', 'enum': list(AGGS)},
            'column': col, 'description': text, 'synonyms': syn},
            'required': ['name', 'table_id', 'aggregation', 'column', 'description', 'synonyms'],
            'additionalProperties': False}},
        'design_notes': text},
        'required': ['fact_table', 'tables', 'relationships', 'columns', 'metrics', 'design_notes'],
        'additionalProperties': False}


def evidence(plan, ir=None):
    """What Cortex sees: tables, real columns, Tableau captions and calculation names -- not the keys."""
    tables = []
    for oid, t in plan['tables'].items():
        cols = [{'column': f['remote'], 'datatype': f['datatype'], 'tableau_field': local}
                for local, f in sorted(plan['fields'].items()) if f['owner'] == oid]
        tables.append({'table_id': oid, 'tableau_caption': t['caption'], 'snowflake_table': t['table'],
                       'columns': cols})
    calcs = sorted(n for n, c in ((ir or {}).get('calcs') or {}).items() if isinstance(c, dict))[:80]
    return {'tables': tables, 'tableau_calculation_names': calcs}


def propose(session, run, plan, ir=None, caption=None):
    import v3_ai as A
    ev = evidence(plan, ir)
    table_ids = list(plan['tables'])
    columns = sorted({f['remote'] for f in plan['fields'].values()})
    out = A.call(session, run, 'Data model', 'Cortex-designed semantic model — ' + str(caption or ''),
                 A._wrap('Design a Snowflake semantic model for these logical tables from a Tableau dashboard. '
                         'Choose the fact table (the event or transaction grain), the primary key of each '
                         'dimension table, and the relationships (fact column -> dimension primary key) that '
                         'join them. Propose up to 12 business metrics as an aggregation of one numeric or key '
                         'column. Write a one-sentence business description and up to 3 synonyms for every '
                         'table and column. Use only the given table ids and column names.', ev),
                 _schema(table_ids, columns), max_tokens=6000)
    return out


def compile_model(plan, proposal, ir=None, caption=None, code=None):
    """Typed proposal -> the same sem structure the code-built view uses, plus a rejection list.
    code: the code-built model to align with (release 20a passes the shared view's model)."""
    import v3_semantic as SV
    code = code or SV.model(plan, ir, caption)               # aliases, names and dims from the plan
    alias = {}
    for oid, t in plan['tables'].items():
        same = [x for x in code['tables'] if x['table'] == t['table']]
        alias[oid] = (next((x for x in same if x['caption'] == t['caption']), None) or same[0])['alias']
    cols_of = {oid: {f['remote']: f for f in plan['fields'].values() if f['owner'] == oid} for oid in plan['tables']}
    rejected = []
    fact = proposal.get('fact_table')
    if fact not in plan['tables']:
        raise ValueError('Cortex named no valid fact table')
    pk = {}
    for t in proposal.get('tables') or []:
        oid, key = t.get('table_id'), t.get('primary_key')
        if oid in plan['tables'] and key and key != 'NONE':
            if key in cols_of[oid]:
                pk[oid] = key
            else:
                rejected.append({'Item': f'primary key {key}', 'Reason': f'not a column of {oid}'})
    rels, seen = [], set()
    for r in proposal.get('relationships') or []:
        a, ac, b, bc = r.get('from_table'), r.get('from_column'), r.get('to_table'), r.get('to_column')
        if a not in cols_of or b not in cols_of or ac not in cols_of[a] or bc not in cols_of[b] or a == b:
            rejected.append({'Item': f'relationship {a}.{ac} -> {b}.{bc}', 'Reason': 'unknown table or column'})
            continue
        if (a, b) in seen:
            rejected.append({'Item': f'relationship {a}.{ac} -> {b}.{bc}', 'Reason': 'duplicate table pair'})
            continue
        seen.add((a, b))
        pk.setdefault(b, bc)
        if pk[b] != bc:
            rejected.append({'Item': f'relationship {a}.{ac} -> {b}.{bc}', 'Reason': 'does not reference the primary key'})
            continue
        rels.append({'name': f'{alias[a]}_to_{alias[b]}', 'from': alias[a], 'from_key': ac,
                     'to': alias[b], 'to_key': bc})
    descr = {(c.get('table_id'), c.get('column')): c for c in proposal.get('columns') or []}
    dims = []
    for d in code['dimensions']:
        oid = next(o for o, a in alias.items() if a == d['table'])
        c = descr.get((oid, d['column']))
        e = dict(d)
        if c:
            if str(c.get('description') or '').strip():
                e['comment'] = re.sub(r'\s+', ' ', str(c['description']))[:300].strip() + ' (design by Cortex)'
            e['synonyms'] = [re.sub(r'\s+', ' ', str(s))[:60].strip() for s in c.get('synonyms') or [] if str(s).strip()][:3]
        dims.append(e)
    names, metrics = {d['name'] for d in dims}, []
    for m in proposal.get('metrics') or []:
        oid, col, agg = m.get('table_id'), m.get('column'), m.get('aggregation')
        name = re.sub(r'[^A-Z0-9_]', '_', str(m.get('name') or '').upper()).strip('_')[:60]
        if oid not in cols_of or col not in cols_of[oid] or agg not in AGGS or not name or name[0].isdigit():
            rejected.append({'Item': f"metric {m.get('name')}", 'Reason': 'unknown column, aggregation or name'})
            continue
        if name in names:
            rejected.append({'Item': f'metric {name}', 'Reason': 'name already used'})
            continue
        dim = next((d for d in dims if d['table'] == alias[oid] and d['column'] == col), None)
        if dim is None:
            rejected.append({'Item': f'metric {name}', 'Reason': 'column is not modelled in the view'})
            continue
        if agg in ('SUM', 'AVG') and not dim.get('numeric'):
            rejected.append({'Item': f'metric {name}', 'Reason': f'{agg} of a non-numeric column'})
            continue
        names.add(name)
        metrics.append({'table': alias[oid], 'name': name, 'expr': AGGS[agg].format(f"{alias[oid]}.{dim['name']}"),
                        'caption': name.replace('_', ' ').title(), 'source': 'cortex',
                        'comment': re.sub(r'\s+', ' ', str(m.get('description') or ''))[:300].strip() + ' (design by Cortex)',
                        'synonyms': [str(s)[:60] for s in m.get('synonyms') or []][:3]})
    tables = []
    for oid, t in plan['tables'].items():
        tables.append({'alias': alias[oid], 'table': t['table'], 'caption': t['caption'],
                       'role': 'fact' if oid == fact else 'dimension', 'primary_key': pk.get(oid) if oid != fact else None})
    sem = {'name': code['name'] + SUFFIX, 'caption': caption, 'tables': tables, 'relationships': rels,
           'facts': [], 'dimensions': dims, 'metrics': metrics, 'skipped': [],
           'origin': 'datasource; model designed by Cortex (AI_COMPLETE) and verified by code'}
    authored = {(k['first'], k['left'], k['second'], k['right']) for k in plan['keys']}
    proposed = {(next(o for o, a in alias.items() if a == r['from']), r['from_key'],
                 next(o for o, a in alias.items() if a == r['to']), r['to_key']) for r in rels}
    comparison = {'authored': len(authored), 'proposed': len(proposed), 'same': len(authored & proposed),
                  'missing': sorted('.'.join(x[:2]) + ' -> ' + '.'.join(x[2:]) for x in authored - proposed),
                  'extra': sorted('.'.join(x[:2]) + ' -> ' + '.'.join(x[2:]) for x in proposed - authored),
                  'fact_matches_tableau': fact == plan['root']}
    return sem, rejected, comparison


def design(session, run, plan, ir=None, caption=None):
    """Cortex designs, code validates/creates/proves. Returns an evidence record (never attached)."""
    import v3_semantic as SV
    proposal = propose(session, run, plan, ir, caption)
    sem, rejected, comparison = compile_model(plan, proposal, ir, caption)
    record = {'name': sem['name'], 'proposal_notes': str(proposal.get('design_notes') or '')[:600],
              'rejected': rejected, 'relationships_vs_tableau': comparison, 'model': sem,
              'verified': False, 'checks': [], 'attempts': [], 'metrics': [], 'ddl': SV.ddl(sem)}
    if not sem['relationships'] and len(plan['tables']) > 1:
        record['attempts'].append({'Attempt': 'compile', 'Result': 'no valid relationships; not created'})
        return record
    sem, statement, attempts = SV._define(session, sem)
    ok, checks, mode = SV.verify_any(session, sem, plan, attempts)
    record.update({'ddl': statement, 'attempts': attempts, 'verified': ok, 'checks': checks, 'mode': mode,
                   'metrics': SV.metric_check(session, sem) if ok else [], 'model': sem})
    return record
