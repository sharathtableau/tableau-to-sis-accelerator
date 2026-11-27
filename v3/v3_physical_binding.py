"""DM3: typed, read-only plans for live Snowflake datasources that are a PHYSICAL JOIN or the author's CUSTOM SQL.

Tableau computes a physical join on the joined rows (duplicates included -- unlike relationships), and a custom-SQL
datasource on the author's query result. Both are rebuilt here from the workbook XML as a typed plan, compiled to ONE
read-only SELECT, and verified against the session before binding (account identity, every table's schema and the
projected columns' types, a zero-row run). The plan is recompiled from its typed fields whenever it is used; no SQL
text is taken from anywhere but the authored workbook, and custom SQL must pass the Ask-the-data read-only guard.
"""
import re

from calc_translator import to_phys
from v3_source_binding import FQN, compatible, rows

JOIN_VERSION = 'physical-join/1'
CUSTOM_VERSION = 'custom-sql/1'
UNION_VERSION = 'live-union/1'
TABLE_NAME = 'Table Name'                  # the column Tableau adds to a union: the member a row came from
_MEMBER = re.compile(r"[A-Za-z0-9_ $.\-]{1,255}\Z")
JOIN_TYPES = {'inner': 'INNER', 'left': 'LEFT', 'right': 'RIGHT', 'full': 'FULL OUTER'}
_IDENT = re.compile(r'[A-Za-z_][A-Za-z_0-9]*\Z')
_OPERAND = re.compile(r'\[([^\[\]]+)\]\.\[([^\[\]]+)\]\Z')


def quote(value):
    if not isinstance(value, str) or not value or len(value) > 255 or any(ord(c) < 32 for c in value):
        raise ValueError('Invalid source column identifier')
    return '"' + value.replace('"', '""') + '"'


def _connection(ds):
    named = {n.get('name'): n.find('connection') for n in ds.findall('.//named-connection')}
    upstream = {k: c for k, c in named.items() if c is not None and c.get('class') == 'snowflake'}
    if len(upstream) != 1:
        raise ValueError('One authored Snowflake connection is required')
    return next(iter(upstream.items()))


def _table(rel, conn):
    parts = re.findall(r'\[([^\]]+)\]', rel.get('table', ''))
    if '.'.join('[' + p + ']' for p in parts) != rel.get('table', ''):
        raise ValueError('Source relation identifier cannot be resolved safely')
    if len(parts) == 1:
        parts = [conn.get('dbname'), conn.get('schema')] + parts
    elif len(parts) == 2:
        parts = [conn.get('dbname')] + parts
    fqn = '.'.join(p or '' for p in parts)
    if len(parts) != 3 or not FQN.fullmatch(fqn) or fqn != fqn.upper():
        raise ValueError('Unquoted uppercase DATABASE.SCHEMA.TABLE source required')
    return fqn


def _fields(ds, owners):
    """local column -> {owner, remote, datatype, output}; records of other owners (the extract's own) are skipped."""
    declared = {(c.get('name') or '').strip('[]'): c.get('datatype') for c in ds.findall('./column')}
    fields = {}
    for mr in ds.findall('.//metadata-record'):
        if mr.get('class') != 'column':
            continue
        parent = (mr.findtext('parent-name') or '').strip('[]')
        if parent not in owners:
            continue
        local = (mr.findtext('local-name') or '').strip('[]')
        remote = mr.findtext('remote-name')
        quote(remote)
        definition = {'owner': parent, 'remote': remote, 'output': to_phys(local),
                      'datatype': mr.findtext('local-type') or declared.get(local)}
        if local in fields and fields[local] != definition:
            raise ValueError('Ambiguous physical field metadata')
        fields[local] = definition
    if not fields:
        raise ValueError('No physical columns are declared for the source')
    output = [f['output'] for f in fields.values()]
    if len(output) != len(set(output)):
        raise ValueError('Normalized physical columns collide')
    return fields


def _join_tree(rel, conn, tables, joins):
    """Left-deep join tree -> tables {name: {...}} in order, joins [{type, on: [(lt, lc, rt, rc)]}]."""
    if rel.get('type') == 'table':
        name = rel.get('name')
        if not name or name in tables:
            raise ValueError('Join leaf names must be unique')
        tables[name] = {'name': name, 'table': _table(rel, conn)}
        return
    if rel.get('type') != 'join':
        raise ValueError('Unsupported relation inside a physical join')
    kind = rel.get('join')
    if kind not in JOIN_TYPES:
        raise ValueError(f'Join type {kind!r} is not supported')
    children = [c for c in rel if c.tag == 'relation']
    clause = rel.find('clause')
    if len(children) != 2 or clause is None or clause.get('type') != 'join':
        raise ValueError('A join needs one clause and two inputs')
    if children[1].get('type') != 'table':
        raise ValueError('Only left-deep physical joins are supported')
    _join_tree(children[0], conn, tables, joins)
    left_names = set(tables)
    _join_tree(children[1], conn, tables, joins)
    right = children[1].get('name')
    expr = clause.find('expression')
    terms = [expr] if expr is not None and expr.get('op') == '=' else \
        (list(expr) if expr is not None and expr.get('op') == 'AND' else None)
    if not terms:
        raise ValueError('Join clauses must be equalities (optionally AND-ed)')
    on = []
    for t in terms:
        ops = [e.get('op', '') for e in t]
        if t.get('op') != '=' or len(ops) != 2 or len(t) != 2 or any(len(e) for e in t):
            raise ValueError('Join clauses must be equalities between two columns')
        a, b = (_OPERAND.match(o) for o in ops)
        if not a or not b:
            raise ValueError('Join clause operands must be [table].[column]')
        (ta, ca), (tb, cb) = a.groups(), b.groups()
        if tb != right and ta == right:
            (ta, ca), (tb, cb) = (tb, cb), (ta, ca)
        if tb != right or ta not in left_names:
            raise ValueError('Join clause does not connect the joined inputs')
        quote(ca), quote(cb)
        on.append([ta, ca, tb, cb])
    joins.append({'type': kind, 'right': right, 'on': on})



def _read_only(sql):
    import v3_semantic as S
    body = (sql or '').strip().rstrip(';').strip()
    if not S._READ_ONLY.match(body) or S._WRITE.search(body) or S._SIDE_EFFECTS.search(body) or ';' in body:
        raise ValueError('Custom SQL must be one read-only SELECT')
    if re.search(r'\bCALL\b', body, re.I):
        raise ValueError('Custom SQL must be one read-only SELECT')
    if '<[Parameters].' in body or re.search(r'<\[[^\]]+\]\.\[[^\]]+\]>', body):
        raise ValueError('Custom SQL with Tableau parameter placeholders is not supported yet')
    return body


def build_live(ds):
    """A live datasource whose relation is a physical join or custom SQL -> typed plan (else ValueError)."""
    name, conn = _connection(ds)
    top = [r for r in ds.find('connection') if r.tag == 'relation'] if ds.find('connection') is not None else []
    if len(top) != 1:
        raise ValueError('One source relation is required')
    rel = top[0]
    if any(n.tag in ('initial-sql',) for n in ds.iter()):
        raise ValueError('Initial SQL is not supported')
    base = {'datasource_id': ds.get('name'), 'server': conn.get('server', '')}
    if rel.get('type') == 'join':
        tables, joins = {}, []
        _join_tree(rel, conn, tables, joins)
        if len(tables) > 32:
            raise ValueError('Too many joined tables')
        plan = dict(base, version=JOIN_VERSION, kind='physical-join', root=next(iter(tables)), tables=tables,
                    joins=joins, fields=_fields(ds, set(tables)))
        compile_query(plan)
        return plan
    if rel.get('type') == 'text':
        if rel.get('connection') != name:
            raise ValueError('Custom SQL must use the authored Snowflake connection')
        owner = rel.get('name') or 'Custom SQL Query'
        plan = dict(base, version=CUSTOM_VERSION, kind='custom-sql', sql=_read_only(rel.text),
                    tables={owner: {'name': owner}}, fields=_fields(ds, {owner}))
        compile_query(plan)
        return plan
    if rel.get('type') == 'union':
        # 4.2: a union of live Snowflake tables -> UNION ALL of the members (Tableau matches columns by name; a
        # column a member lacks is NULL for its rows -- complete_union() reads which, before validation)
        members = [r for r in rel if r.tag == 'relation']
        if len(members) < 2 or any(m.get('type') != 'table' or m.get('connection') != name for m in members):
            raise ValueError('A live union must stack two or more tables of the authored Snowflake connection')
        tables = {}
        for m in members:
            label = m.get('name') or ''
            if not _MEMBER.fullmatch(label) or label in tables:
                raise ValueError('Union member names must be unique and plain')
            tables[label] = {'name': label, 'table': _table(m, conn)}
        owner = rel.get('name') or 'Union'
        plan = dict(base, version=UNION_VERSION, kind='live-union', union=owner, tables=tables,
                    fields=_fields(ds, {owner}))
        compile_query(plan)
        return plan
    raise ValueError('Not a physical join, custom SQL or live union source')


def single_table_plan(ds, rel, conn, table):
    """6.3: one live table with a reserved-word column (ORDER, GROUP, ...) -> a one-table projection plan: every
    column read quoted, exposed under its engine name (ORDER_)."""
    name = rel.get('name')
    if not name:
        raise ValueError('Source relation has no name')
    plan = {'datasource_id': ds.get('name'), 'server': conn.get('server', ''), 'version': JOIN_VERSION,
            'kind': 'projection', 'root': name, 'tables': {name: {'name': name, 'table': table}}, 'joins': [],
            'fields': _fields(ds, {name})}
    compile_query(plan)
    return plan


def needs_projection(ds, rel):
    """True when a column of this relation is a reserved word (ORDER, GROUP, ...): it cannot be read unquoted, and
    its engine name (ORDER_) differs from the column. Other names keep the direct binding (case is corrected by
    the engine at query time)."""
    from calc_translator import RESERVED
    for mr in ds.findall('.//metadata-record'):
        if mr.get('class') == 'column' and (mr.findtext('parent-name') or '').strip('[]') == rel.get('name'):
            remote = mr.findtext('remote-name') or ''
            if re.sub(r"[^0-9a-zA-Z]+", "_", remote).strip("_").upper() in RESERVED:
                return True
    return False


def _projection(plan, alias):
    selected, outputs = [], set()
    if len(plan['fields']) > 1000:
        raise ValueError('Too many projected fields')
    for f in plan['fields'].values():
        out = f['output']
        if not _IDENT.fullmatch(out) or out in outputs or f['owner'] not in alias:
            raise ValueError('Invalid projected field')
        outputs.add(out)
        selected.append(alias[f['owner']] + '.' + quote(f['remote']) + ' AS ' + out)
    return selected


def compile_query(plan):
    if plan.get('version') == JOIN_VERSION:
        names = list(plan['tables'])
        if not names or plan.get('root') != names[0] or len(plan['joins']) != len(names) - 1:
            raise ValueError('Invalid physical join plan')
        alias = {n: f't{i}' for i, n in enumerate(names)}
        for t in plan['tables'].values():
            if not FQN.fullmatch(t['table']) or t['table'] != t['table'].upper():
                raise ValueError('Invalid source table')
        sql = 'SELECT ' + ', '.join(_projection(plan, alias)) + ' FROM ' + plan['tables'][names[0]]['table'] + ' t0'
        for j in plan['joins']:
            if j['type'] not in JOIN_TYPES or j['right'] not in alias or not j['on']:
                raise ValueError('Invalid join step')
            conds = []
            for ta, ca, tb, cb in j['on']:
                if ta not in alias or tb != j['right']:
                    raise ValueError('Invalid join clause')
                conds.append(f'{alias[ta]}.{quote(ca)} = {alias[tb]}.{quote(cb)}')
            sql += (f" {JOIN_TYPES[j['type']]} JOIN {plan['tables'][j['right']]['table']} {alias[j['right']]}"
                    f" ON {' AND '.join(conds)}")
        return sql
    if plan.get('version') == UNION_VERSION:
        missing = plan.get('missing') or {}
        cols = [f['remote'] for f in plan['fields'].values() if f['remote'] != TABLE_NAME]
        if len(plan['tables']) < 2 or len(set(cols)) != len(cols):
            raise ValueError('Invalid live union plan')
        parts = []
        for label, t in plan['tables'].items():
            if not FQN.fullmatch(t['table']) or t['table'] != t['table'].upper() or not _MEMBER.fullmatch(label):
                raise ValueError('Invalid union member')
            gone = set(missing.get(label) or [])
            sel = [('NULL AS ' + quote(c)) if c in gone else quote(c) for c in cols]
            sel.append("'" + label.replace("'", "''") + "' AS " + quote(TABLE_NAME))
            parts.append('SELECT ' + ', '.join(sel) + ' FROM ' + t['table'])
        return ('SELECT ' + ', '.join(_projection(plan, {plan['union']: 'q'})) + ' FROM (' +
                ' UNION ALL '.join(parts) + ') q')
    if plan.get('version') == CUSTOM_VERSION:
        body = _read_only(plan.get('sql'))
        owner = next(iter(plan['tables']))
        return 'SELECT ' + ', '.join(_projection(plan, {owner: 'q'})) + ' FROM (' + body + ') q'
    raise ValueError('Invalid live source plan')


def complete_union(session, plan):
    """A live-union plan with, per member, the union columns it lacks (NULL for its rows, as Tableau). A column no
    member has, or one with types that do not agree, refuses."""
    if plan.get('version') != UNION_VERSION:
        return plan
    cols = [f for f in plan['fields'].values() if f['remote'] != TABLE_NAME]
    schemas = {label: {r['NAME']: r['TYPE'] for r in rows(session, 'DESCRIBE TABLE ' + t['table'])}
               for label, t in plan['tables'].items()}
    missing = {}
    for f in cols:
        have = [lab for lab, sch in schemas.items() if f['remote'] in sch]
        if not have:
            raise ValueError('A union column is in no member table')
        if any(not compatible(f['datatype'], schemas[lab][f['remote']]) for lab in have):
            raise ValueError('Union member column types differ from the workbook')
        for lab in schemas:
            if lab not in have:
                missing.setdefault(lab, []).append(f['remote'])
    return dict(plan, missing={k: sorted(v) for k, v in missing.items()})


def validate(session, plan, ir=None):
    if plan.get('version') == UNION_VERSION:
        plan = complete_union(session, plan)
    sql = compile_query(plan)
    identity = rows(session, 'SELECT CURRENT_ACCOUNT() AS LOCATOR, CURRENT_ACCOUNT_NAME() AS ACCOUNT_NAME, '
                             'CURRENT_ORGANIZATION_NAME() AS ORG, CURRENT_REGION() AS REGION')[0]
    expected = (str(identity['ORG']) + '-' + str(identity['ACCOUNT_NAME'])).lower().replace('_', '-') + \
        '.snowflakecomputing.com'
    if plan['server'].lower().rstrip('.') != expected:
        raise ValueError('Source account identity mismatch')
    if plan['version'] == UNION_VERSION:
        # complete_union() checked every member's columns and types; the projected field types stand for them
        names = {'string': 'VARCHAR', 'integer': 'NUMBER(38,0)', 'real': 'FLOAT', 'date': 'DATE',
                 'datetime': 'TIMESTAMP_NTZ', 'boolean': 'BOOLEAN'}
        schemas = {plan['union']: {f['remote']: 'VARCHAR' if f['remote'] == TABLE_NAME else
                                   names.get(f['datatype'], 'VARCHAR') for f in plan['fields'].values()}}
    elif plan['version'] == JOIN_VERSION:
        schemas = {n: {r['NAME']: r['TYPE'] for r in rows(session, 'DESCRIBE TABLE ' + t['table'])}
                   for n, t in plan['tables'].items()}
        for j in plan['joins']:
            for ta, ca, tb, cb in j['on']:
                if schemas[ta].get(ca) is None or schemas[ta].get(ca) != schemas[tb].get(cb):
                    raise ValueError('Join key columns are missing or their types differ')
    else:
        owner = next(iter(plan['tables']))
        rows(session, 'SELECT * FROM (' + _read_only(plan['sql']) + ') q LIMIT 0')
        schemas = {owner: {r['NAME']: r['TYPE'] for r in rows(session, 'DESCRIBE RESULT LAST_QUERY_ID()')}}
    for f in plan['fields'].values():
        actual = schemas[f['owner']].get(f['remote'])
        if not actual or not compatible(f['datatype'], actual):
            raise ValueError('Source physical field schema mismatch')
    if ir is not None:
        fields = {f['internal']: f for f in ir['fields'].values()
                  if f['datasource_id'] == plan['datasource_id'] and not f['is_calculation']
                  and f.get('datatype') != 'table' and not str(f.get('internal') or '').startswith(':')}
        for internal, f in fields.items():
            if internal not in plan['fields'] or plan['fields'][internal]['output'] != to_phys(f['physical']):
                raise ValueError('Live source plan does not match the IR physical fields')
    rows(session, 'SELECT * FROM (' + sql + ') LIMIT 0')
    count = rows(session, 'SELECT COUNT(*) AS N FROM (' + sql + ')')[0]['N']
    grain = {JOIN_VERSION: 'physical join (row level, as Tableau)', UNION_VERSION: 'union of the member tables',
             CUSTOM_VERSION: 'custom SQL result'}[plan['version']]
    out = {'output_rows': count, 'grain': grain, 'tableau_equivalence': 'NOT_VERIFIED'}
    if plan['version'] == UNION_VERSION:
        out['missing_columns'] = plan.get('missing') or {}
    return out
