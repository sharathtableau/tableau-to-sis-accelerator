"""Release 22a (SL03, EN22): rationalisation report per metric, from the estate catalogue.

Every metric of a scope gets exactly one class, with evidence:
- conflicting   calculations with the same name whose numbers differ when code runs both on the same data
- equivalent    different formulas that give the same numbers on the same data (proved by code)
- not_testable  in a group to compare, but not run: data not in Snowflake, not translated, uses a parameter,
                or a level-of-detail / table calculation (compared in a later release); the reason is kept
- stale         used on no sheet, or only in workbooks with no Tableau views
- identical     the same formula (fingerprint) in two or more workbooks
- unique        everything else (one workbook, in use)

Groups to compare come from code (same name, different formula) and from Cortex, which may propose metrics
that mean the same business quantity under different names; it can pick only metric ids from the catalogue.
Code runs every member of a group in ONE read-only SELECT on one Snowflake relation (the table or the
code-built relationship query a migration run reads, or a table this accelerator loaded), at the grand
total, by month of the main date and by one shared dimension, and compares the values. Only those numbers
decide equivalent or conflicting. A Cortex-proposed group whose numbers differ is listed for review and does
not make its metrics conflicting.

For each conflicting group Cortex explains the difference and recommends one of the group's own definitions;
code checks the choice is one of them and that every number it quotes is in the measured results. A person
decides what is certified (release 23).

Expressions are the catalogue's translated SQL (the migration's own translator), never Cortex output. They
run inside a code-built SELECT and are refused if they contain anything but an expression.
"""
import datetime
import json
import math
import re
import uuid

CLASSES = ('conflicting', 'equivalent', 'not_testable', 'stale', 'identical', 'unique')
LABELS = {'conflicting': 'Conflicting', 'equivalent': 'Equivalent', 'not_testable': 'Not testable yet',
          'stale': 'Stale', 'identical': 'Identical', 'unique': 'Unique'}
MAX_ROWS = 20000
GROUP_CALLS, EXPLAIN_CALLS = 6, 15
GROUP_BATCH, MAX_GROUP = 100, 8
LOAD_SCHEMA = ('WBR_DB', 'PIPELINE_DEMO')

S, N, B, T, V = 'STRING', 'NUMBER', 'BOOLEAN', 'TIMESTAMP_TZ', 'VARIANT'
TABLES = {
    'RAT_RUNS': ('One row when a rationalisation run starts and one when it finishes', [
        ('RUN_ID', S), ('RECORDED_AT', T), ('STATUS', S), ('SCOPE', S), ('STARTED_BY', S), ('VIA', S),
        ('METRICS', N), ('GROUPS_COMPARED', N), ('TESTED', N), ('COUNTS', V), ('CORTEX_CALLS', N), ('NOTE', S)]),
    'RAT_GROUPS': ('Groups compared: members, where they were run, the values that differ, verdict, and the '
                   'checked Cortex explanation and recommendation', [
        ('RUN_ID', S), ('RECORDED_AT', T), ('SCOPE', S), ('GROUP_ID', S), ('SOURCE', S), ('NAME', S),
        ('MEMBERS', V), ('VERDICT', S), ('REASON', S), ('RELATION', S), ('GRAINS', V), ('CLUSTERS', V),
        ('DIFFERENCES', V), ('TESTED_SQL', S), ('CORTEX_REASON', S), ('EXPLANATION', S), ('RECOMMENDED_FP', S),
        ('CORTEX_PASSED_CHECK', B), ('CODE_CHECK', S)]),
    'RAT_METRICS': ('Every metric of a scope with its one class and the evidence behind it', [
        ('RUN_ID', S), ('RECORDED_AT', T), ('SCOPE', S), ('METRIC_FP', S), ('NAME', S), ('NAMES', V),
        ('KIND', S), ('CLASS', S), ('GROUP_ID', S), ('REASON', S), ('WORKBOOKS', N), ('WORKBOOK_NAMES', V),
        ('SHEETS', N), ('TABLEAU_VIEWS', N), ('EXPRESSION', S)]),
}
_LATEST = """SELECT RUN_ID FROM {S}.RAT_RUNS WHERE STATUS = 'finished'
QUALIFY ROW_NUMBER() OVER (PARTITION BY SCOPE ORDER BY RECORDED_AT DESC) = 1"""
VIEWS = {
    'V_RATIONALISATION': ('Latest finished rationalisation per scope: every metric with its class', """
SELECT * FROM {S}.RAT_METRICS WHERE RUN_ID IN (""" + _LATEST + ')'),
    'V_RAT_GROUPS': ('Latest finished rationalisation per scope: groups compared and their verdicts', """
SELECT * FROM {S}.RAT_GROUPS WHERE RUN_ID IN (""" + _LATEST + ')'),
    'V_RAT_SUMMARY': ('Latest finished rationalisation per scope: metrics per class', """
SELECT SCOPE, CLASS, COUNT(*) AS METRICS FROM {S}.RAT_METRICS WHERE RUN_ID IN (""" + _LATEST + """)
GROUP BY SCOPE, CLASS"""),
}


def ddl(schema):
    q = lambda s: s.replace("'", "''")
    out = ['-- Release 22a: rationalisation report (SL03). Runs only INSERT.']
    for name, (comment, cols) in TABLES.items():
        body = ',\n'.join(f'  {c} {t}' for c, t in cols)
        out.append(f"CREATE TABLE IF NOT EXISTS {schema}.{name} (\n{body}\n) COMMENT = '{q(comment)}';")
    for name, (comment, sql) in VIEWS.items():
        out.append(f"CREATE OR REPLACE VIEW {schema}.{name} COMMENT = '{q(comment)}' AS" + sql.format(S=schema) + ';')
    return out


# --------------------------------------------------------------------------- #
# Expressions (the catalogue's translated SQL; never model output)
# --------------------------------------------------------------------------- #
AGG_SQL = {'SUM': 'SUM({})', 'AVG': 'AVG({})', 'COUNT': 'COUNT({})', 'COUNTD': 'COUNT(DISTINCT {})',
           'MIN': 'MIN({})', 'MAX': 'MAX({})', 'MEDIAN': 'MEDIAN({})', 'STDEV': 'STDDEV_SAMP({})',
           'STDEVP': 'STDDEV_POP({})', 'VAR': 'VAR_SAMP({})', 'VARP': 'VAR_POP({})'}
_FORBIDDEN = re.compile(r';|\b(SELECT|INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|GRANT|REVOKE|CALL|COPY|PUT|GET|'
                        r'EXECUTE|USE|SHOW|DESCRIBE|TRUNCATE|UNDROP|COMMENT)\b|--|/\*', re.I)
_COL = re.compile(r'COL\(([A-Z0-9_]+)\)')
IDENT = re.compile(r'[A-Z_][A-Z0-9_]*\Z')


def phys(name):
    from calc_translator import to_phys
    return to_phys(name)


def _blocked(sql):
    if not sql or not str(sql).strip():
        return 'not translated'
    if '__PARAM_' in sql:
        return 'uses a parameter'
    if '__WIN_' in sql or re.search(r'\bOVER\s*\(', sql, re.I):
        return 'table calculation or level of detail (compared in a later release)'
    if _strip_literals(sql) and _FORBIDDEN.search(_strip_literals(sql)):
        return 'expression not allowed'
    return None


def _strip_literals(sql):
    return re.sub(r"'(?:[^']|'')*'", "''", sql or '')


def expression(m, calcs, fields):
    """(sql, reason) for one metric row. `calcs`/`fields`: dicts keyed by (WORKBOOK_KEY, DATASOURCE_ID, FIELD)."""
    key = (m['WORKBOOK_KEY'], m['DATASOURCE_ID'], m['FIELD'])
    if m['KIND'] in ('lod', 'table_calc'):
        return None, 'table calculation or level of detail (compared in a later release)'
    if m['ORIGIN_KIND'] == 'calculation':
        sql = (calcs.get(key) or {}).get('TRANSLATED_SQL')
    else:
        agg = str(m['NORMALISED'] or '').split('(', 1)[0]
        if agg not in AGG_SQL:
            return None, f'aggregation {agg or "?"} is not compared'
        calc = calcs.get(key)
        if calc is not None:
            if calc.get('KIND') != 'row':
                return None, 'table calculation or level of detail (compared in a later release)'
            inner = calc.get('TRANSLATED_SQL')
            sql = AGG_SQL[agg].format('(' + inner + ')') if inner else None
        else:
            f = fields.get(key)
            sql = AGG_SQL[agg].format(phys(f['PHYSICAL'])) if f and f.get('PHYSICAL') else None
    why = _blocked(sql)
    return (None, why) if why else (sql, None)


def needed_columns(normalised):
    return sorted(set(_COL.findall(normalised or '')))


# --------------------------------------------------------------------------- #
# Relations (where groups are run) — read-only
# --------------------------------------------------------------------------- #
def _q(name):
    return name if IDENT.fullmatch(name) else '"' + name.replace('"', '""') + '"'


def relations(sess, bindings):
    """Pool of relations with their columns: bound tables and models of the scope, plus loaded tables."""
    pool, tables = [], {}
    for b in bindings:
        if b['KIND'] == 'model' and b.get('RELATION'):
            cols = b.get('COLUMNS') or []
            if isinstance(cols, str):
                cols = json.loads(cols)
            pool.append({'kind': 'model', 'relation': b['RELATION'], 'label': 'relationship model of ' + str(b.get('WORKBOOK_NAME') or b['DATASOURCE_ID']),
                         'columns': {c: None for c in cols}, 'owners': {(b['WORKBOOK_KEY'], b['DATASOURCE_ID'])}})
        elif b['KIND'] == 'table' and b.get('RELATION'):
            t = tables.setdefault(b['RELATION'], {'kind': 'table', 'relation': b['RELATION'], 'label': b['RELATION'],
                                                  'columns': {}, 'owners': set()})
            t['owners'].add((b['WORKBOOK_KEY'], b['DATASOURCE_ID']))
    db, sch = LOAD_SCHEMA
    try:
        for r in sess.sql(f'SELECT TABLE_NAME FROM {db}.INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = ? AND '
                          "(COMMENT LIKE 'v3load%' OR COMMENT LIKE 'v3map%')", params=[sch]).collect():
            fqn = f'{db}.{sch}.{r[0]}'
            tables.setdefault(fqn, {'kind': 'loaded', 'relation': fqn, 'label': fqn + ' (loaded)', 'columns': {},
                                    'owners': set()})
    except Exception:
        pass
    by_db = {}
    for fqn in tables:
        by_db.setdefault(fqn.split('.')[0], []).append(fqn)
    for dbn, names in by_db.items():
        try:
            rows = sess.sql(f'SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE FROM {dbn}.INFORMATION_SCHEMA.COLUMNS '
                            'WHERE TABLE_SCHEMA || \'.\' || TABLE_NAME IN (' + ','.join('?' * len(names)) + ')',
                            params=['.'.join(n.split('.')[1:]) for n in names]).collect()
            for r in rows:
                fqn = f'{dbn}.{r[0]}.{r[1]}'
                if fqn in tables:
                    tables[fqn]['columns'][str(r[2])] = str(r[3])
        except Exception:
            pass
    pool += [t for t in tables.values() if t['columns']]
    return pool


def choose(pool, needed, owners):
    """A relation that has every needed column; bound to a member first, then loaded, then any."""
    fits = [r for r in pool if set(needed) <= set(r['columns'])]
    if not fits:
        return None
    rank = lambda r: (0 if r['owners'] & owners else 1 if r['kind'] == 'loaded' else 2, len(r['columns']), r['label'])
    return sorted(fits, key=rank)[0]


DATE_TYPES = ('DATE', 'TIMESTAMP', 'TIMESTAMP_NTZ', 'TIMESTAMP_LTZ', 'TIMESTAMP_TZ', 'DATETIME')
TEXT_TYPES = ('TEXT', 'VARCHAR', 'STRING', 'CHAR')


def _types(rel, field_types):
    out = {}
    for c, t in rel['columns'].items():
        t = (t or '').upper()
        if not t:
            ft = field_types.get(c)
            t = 'DATE' if ft in ('date', 'datetime') else 'TEXT' if ft == 'string' else ''
        out[c] = t
    return out


def grains(rel, field_types, use_counts):
    """(date column, dimension column) for the month and dimension comparisons: the most used in the scope."""
    types = _types(rel, field_types)
    rank = lambda c: (-use_counts.get(c, 0), c)
    dates = sorted((c for c, t in types.items() if t.split('(')[0] in DATE_TYPES), key=rank)
    dims = sorted((c for c, t in types.items() if t.split('(')[0] in TEXT_TYPES and use_counts.get(c)), key=rank)
    return (dates[0] if dates else None), (dims[0] if dims else None)


def test_sql(rel, exprs, date=None, dim=None):
    src = rel['relation'] if rel['kind'] != 'model' else '(' + rel['relation'] + ')'
    cols = ', '.join(f'{e} AS M{i}' for i, e in enumerate(exprs))
    parts = [f"SELECT 'total' AS GRAIN, NULL::STRING AS K, {cols} FROM {src} r"]
    if date:
        parts.append(f"SELECT 'month', TO_CHAR(DATE_TRUNC('MONTH', r.{_q(date)}), 'YYYY-MM'), {cols} FROM {src} r GROUP BY 2")
    if dim:
        parts.append(f"SELECT 'dimension', TO_VARCHAR(r.{_q(dim)}), {cols} FROM {src} r GROUP BY 2")
    return 'SELECT * FROM (' + ' UNION ALL '.join(parts) + f') ORDER BY 1, 2 LIMIT {MAX_ROWS}'


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def same(a, b):
    if a is None or b is None:
        return a is None and b is None
    x, y = _num(a), _num(b)
    if x is not None and y is not None:
        return abs(x - y) <= 1e-6 + 1e-9 * max(abs(x), abs(y))
    return str(a) == str(b)


def compare(rows, k):
    """rows: [(grain, key, v0..vk-1)]. Returns (clusters as lists of member indexes, differing rows)."""
    parent = list(range(k))

    def find(i):
        while parent[i] != i:
            i = parent[i]
        return i
    for i in range(k):
        for j in range(i + 1, k):
            if find(i) != find(j) and all(same(r[2 + i], r[2 + j]) for r in rows):
                parent[find(j)] = find(i)
    clusters = {}
    for i in range(k):
        clusters.setdefault(find(i), []).append(i)
    diffs = [r for r in rows if not all(same(r[2], r[2 + i]) for i in range(1, k))]
    return sorted(clusters.values()), diffs


def _plain(v):
    if v is None or isinstance(v, (bool, int, str)):
        return v
    f = _num(v)
    return f if f is not None else str(v)


# --------------------------------------------------------------------------- #
# Groups to compare
# --------------------------------------------------------------------------- #
def name_groups(metrics):
    """Calculations with the same name (case and spacing ignored) and more than one fingerprint."""
    by = {}
    for m in metrics:
        if m['ORIGIN_KIND'] == 'calculation':
            by.setdefault(re.sub(r'\s+', ' ', str(m['NAME']).strip()).casefold(), {}).setdefault(m['METRIC_FP'], m['NAME'])
    return [{'source': 'same name', 'name': next(iter(fps.values())), 'members': sorted(fps), 'reason': None}
            for _k, fps in sorted(by.items()) if len(fps) > 1]


GROUP_TASK = ('Each item is a metric found in Tableau workbooks (names, formula, business name, intent). Find groups '
              'of items that are meant to measure the SAME business quantity but are written as different formulas '
              '(for example a margin computed two ways). Only return groups of two or more ids. Do not group metrics '
              'that clearly measure different things (sales versus profit, a sum versus an average of the same '
              'field). Give one short reason per group. Return no groups if none apply.')


def cortex_groups(sess, run, info, desc, calls=GROUP_CALLS):
    """Cortex proposes groups from closed lists of metric ids; code keeps valid groups only."""
    import v3_ai
    items = sorted(info.values(), key=lambda i: ((desc.get(i['fp']) or {}).get('INTENT') or 'other', i['fp']))
    batches, cur, intent = [], [], None
    for it in items:
        i = (desc.get(it['fp']) or {}).get('INTENT') or 'other'
        if cur and (i != intent or len(cur) >= GROUP_BATCH):
            batches.append(cur)
            cur = []
        cur.append(it)
        intent = i
    if cur:
        batches.append(cur)
    out, done = [], 0
    for batch in batches:
        if len(batch) < 2:
            continue
        if done >= calls:
            break
        ids = {f'x{n + 1}': it['fp'] for n, it in enumerate(batch)}
        evidence = [{'id': k, 'names': info[fp]['names'][:4], 'formula': str(info[fp]['expression'])[:400],
                     'business_name': (desc.get(fp) or {}).get('BUSINESS_NAME'),
                     'intent': (desc.get(fp) or {}).get('INTENT')} for k, fp in ids.items()]
        schema = {'type': 'object', 'properties': {'groups': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'ids': {'type': 'array', 'items': {'type': 'string', 'enum': sorted(ids)}}, 'reason': {'type': 'string'}},
            'required': ['ids', 'reason'], 'additionalProperties': False}}},
            'required': ['groups'], 'additionalProperties': False}
        done += 1
        try:
            answer = v3_ai.call(sess, run, 'Rationalisation', 'Metrics with the same meaning', v3_ai._wrap(GROUP_TASK, evidence),
                                schema, max_tokens=4000)
        except v3_ai.CortexUnavailable:
            break
        for g in answer.get('groups') or []:
            members = sorted({ids[i] for i in (g.get('ids') or []) if i in ids})
            if 2 <= len(members) <= MAX_GROUP:
                out.append({'source': 'cortex', 'name': info[members[0]]['name'], 'members': members,
                            'reason': str(g.get('reason') or '')[:500]})
    return out


EXPLAIN_TASK = ('Two or more Tableau definitions of the same metric give different numbers on the same data. Using '
                'the formulas and the measured values, explain in at most two sentences what makes them differ, then '
                'recommend the one definition a governed semantic layer should keep (or NONE if it cannot be decided) '
                'with a one-sentence reason (for example the one used on most sheets, or the one that matches the '
                'business name). Quote numbers only as they appear in the evidence.')


def explain(sess, run, group, info):
    """Cortex explanation and recommendation for a conflicting group; returns fields checked by code."""
    import v3_ai
    ids = {f'd{n + 1}': fp for n, fp in enumerate(group['members'])}
    evidence = {'members': [{'id': k, 'names': info[fp]['names'][:4], 'formula': str(info[fp]['expression'])[:600],
                             'workbooks': info[fp]['workbooks'], 'sheets': info[fp]['sheets'],
                             'tableau_views': info[fp]['views']} for k, fp in ids.items()],
                'tested_on': group.get('relation_label'), 'grains': group.get('grains'),
                'differences': [{'grain': d['grain'], 'key': d['key'],
                                 'values': {k: d['values'].get(fp) for k, fp in ids.items()}}
                                for d in (group.get('differences') or [])[:10]]}
    schema = {'type': 'object', 'properties': {'explanation': {'type': 'string'},
                                               'recommended': {'type': 'string', 'enum': sorted(ids) + ['NONE']},
                                               'reason': {'type': 'string'}},
              'required': ['explanation', 'recommended', 'reason'], 'additionalProperties': False}
    answer = v3_ai.call(sess, run, 'Rationalisation', 'Conflict explanation — ' + str(group['name'])[:80],
                        v3_ai._wrap(EXPLAIN_TASK, evidence), schema, max_tokens=1500)
    text = str(answer.get('explanation') or '').strip()
    reason = str(answer.get('reason') or '').strip()
    rec = answer.get('recommended')
    bad = v3_ai.ungrounded_numbers(text + ' ' + reason, evidence)
    if rec not in ids and rec != 'NONE':
        return {'CORTEX_PASSED_CHECK': False, 'CODE_CHECK': 'recommendation is not one of the group'}
    if not text or len(text) > 800:
        return {'CORTEX_PASSED_CHECK': False, 'CODE_CHECK': 'explanation empty or too long'}
    if bad:
        return {'CORTEX_PASSED_CHECK': False, 'CODE_CHECK': 'numbers not in the evidence: ' + ', '.join(bad[:5])}
    return {'EXPLANATION': text[:800], 'CORTEX_REASON': reason[:500], 'RECOMMENDED_FP': ids.get(rec),
            'CORTEX_PASSED_CHECK': True, 'CODE_CHECK': 'recommendation in group; numbers in evidence'}


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #
def _records(df):
    if df is None:
        return []
    out = []
    for r in df.to_dict('records'):
        out.append({k: (None if (isinstance(v, float) and math.isnan(v)) else v) for k, v in r.items()})
    return out


def _views(v):
    f = _num(v)
    return None if f is None else int(f)


def build_info(metrics, usage, workbooks):
    views = {w['WORKBOOK_KEY']: _views(w.get('TOTAL_VIEWS')) for w in workbooks}
    sheets = {}
    for u in usage:
        if u.get('METRIC_FP'):
            sheets.setdefault(u['METRIC_FP'], set()).add((u['WORKBOOK_KEY'], u['WORKSHEET']))
    info = {}
    for m in metrics:
        i = info.setdefault(m['METRIC_FP'], {'fp': m['METRIC_FP'], 'names': [], 'kind': m['KIND'], 'rows': [],
                                             'books': {}, 'expression': m['EXPRESSION'], 'normalised': m['NORMALISED']})
        if m['NAME'] not in i['names']:
            i['names'].append(m['NAME'])
        i['rows'].append(m)
        i['books'][m['WORKBOOK_KEY']] = m['WORKBOOK_NAME']
    for fp, i in info.items():
        i['name'] = max(i['names'], key=lambda n: sum(r['NAME'] == n for r in i['rows']))
        i['workbooks'] = len(i['books'])
        i['sheets'] = len(sheets.get(fp, ()))
        vs = [views.get(k) for k in i['books']]
        i['views'] = sum(v for v in vs if v) if any(v is not None for v in vs) else None
        i['no_views'] = bool(vs) and all(v == 0 for v in vs)
    return info


def classify(info, groups):
    """Exactly one class per metric fingerprint: (class, group id, reason)."""
    out = {}
    order = {c: n for n, c in enumerate(CLASSES)}

    def put(fp, cls, gid, why):
        cur = out.get(fp)
        if cur is None or order[cls] < order[cur[0]]:
            out[fp] = (cls, gid, why)
    for g in groups:
        v = g['verdict']
        for fp in g['members']:
            if v == 'conflicting' and g['source'] == 'same name':
                put(fp, 'conflicting', g['id'], 'same name, different numbers')
            elif v == 'equivalent':
                put(fp, 'equivalent', g['id'], 'different formula, same numbers')
            elif v == 'not_testable' and g['source'] == 'same name':
                put(fp, 'not_testable', g['id'], g.get('reason'))
    for fp, i in info.items():
        if i['sheets'] == 0:
            put(fp, 'stale', None, 'used on no sheet')
        elif i['no_views']:
            put(fp, 'stale', None, 'only in workbooks with no Tableau views')
        elif i['workbooks'] > 1:
            put(fp, 'identical', None, f"same formula in {i['workbooks']} workbooks")
        else:
            put(fp, 'unique', None, 'one workbook')
    return out


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class Run:
    def __init__(self, sess, scope, via='app', who=None, cortex=True, write_rows=True):
        self.sess, self.scope, self.via, self.who, self.cortex = sess, scope, via, who, cortex
        self.write_rows = write_rows and sess is not None
        self.id = 'R' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d%H%M%S') + uuid.uuid4().hex[:6]
        self.run = {'name': 'Rationalisation ' + scope, 'ai_log': []}
        self.groups, self.classes, self.info = [], {}, {}
        self.note = None

    def _write(self, table, rows):
        if self.write_rows and rows:
            _writer(self.sess, table, rows)

    def _run_row(self, status, counts=None):
        return {'RUN_ID': self.id, 'RECORDED_AT': _now(), 'STATUS': status, 'SCOPE': self.scope, 'STARTED_BY': self.who,
                'VIA': self.via, 'METRICS': len(self.info), 'GROUPS_COMPARED': len(self.groups),
                'TESTED': sum(1 for g in self.groups if g['verdict'] in ('equivalent', 'conflicting', 'differs')),
                'COUNTS': counts, 'CORTEX_CALLS': sum(1 for e in self.run['ai_log'] if e.get('attempted')),
                'NOTE': self.note}

    def execute(self, frames=None, progress=None):
        """frames: catalogue rows of the scope (read here when not given)."""
        say = progress or (lambda text: None)
        frames = frames or read_scope(self.sess, self.scope)
        metrics = _records(frames['metrics'])
        usage = _records(frames['usage'])
        workbooks = _records(frames['workbooks'])
        calcs = {(c['WORKBOOK_KEY'], c['DATASOURCE_ID'], c['FIELD']): c for c in _records(frames['calculations'])}
        fields = {(f['WORKBOOK_KEY'], f['DATASOURCE_ID'], f['FIELD']): f for f in _records(frames['fields'])}
        desc = {d['METRIC_FP']: d for d in _records(frames.get('descriptions'))}
        self._write('RAT_RUNS', [self._run_row('started')])
        self.info = info = build_info(metrics, usage, workbooks)
        for fp, i in info.items():
            for m in i['rows']:
                sql, why = expression(m, calcs, fields)
                if sql:
                    i['sql'], i['owner'] = sql, (m['WORKBOOK_KEY'], m['DATASOURCE_ID'])
                    break
                i.setdefault('why', why)
        say('Finding groups to compare…')
        groups = name_groups(metrics)
        if self.cortex and self.sess is not None:
            try:
                groups += cortex_groups(self.sess, self.run, info, desc)
            except Exception as exc:
                self.note = 'Cortex grouping not used: ' + type(exc).__name__
        seen, unique = set(), []
        for g in groups:
            key = frozenset(g['members'])
            if key not in seen:
                seen.add(key)
                unique.append(g)
        field_types = {phys(f['PHYSICAL']): f.get('DATATYPE') for f in fields.values() if f.get('PHYSICAL')}
        use = {}
        for u in usage:
            if u.get('SHELF') in ('rows', 'cols', 'color', 'filter', 'detail', 'label'):
                f = fields.get((u['WORKBOOK_KEY'], u['DATASOURCE_ID'], u['FIELD']))
                if f and f.get('PHYSICAL'):
                    use[phys(f['PHYSICAL'])] = use.get(phys(f['PHYSICAL']), 0) + 1
        pool = relations(self.sess, _records(frames['bindings'])) if (unique and self.sess is not None) else []
        for n, g in enumerate(unique):
            g['id'] = f'G{n + 1}'
            say(f"Comparing {g['id']} of {len(unique)}: {g['name']}")
            self._test(g, info, pool, field_types, use)
            self.groups.append(g)
        if self.cortex and self.sess is not None:
            for g in [g for g in self.groups if g['verdict'] == 'conflicting'][:EXPLAIN_CALLS]:
                try:
                    g.update(explain(self.sess, self.run, g, info))
                except Exception as exc:
                    g.update(CORTEX_PASSED_CHECK=False, CODE_CHECK='Cortex not available: ' + type(exc).__name__)
        self.classes = classify(info, self.groups)
        at = _now()
        self._write('RAT_GROUPS', [self._group_row(g, at) for g in self.groups])
        self._write('RAT_METRICS', [self._metric_row(fp, at) for fp in sorted(info)])
        counts = {c: sum(1 for v in self.classes.values() if v[0] == c) for c in CLASSES}
        self._calls()
        self._write('RAT_RUNS', [self._run_row('finished', counts)])
        return counts

    def _test(self, g, info, pool, field_types, use):
        members = g['members']
        missing = [fp for fp in members if not info[fp].get('sql')]
        runnable = [fp for fp in members if info[fp].get('sql')]
        g['verdict'], g['differences'], g['grains'] = 'not_testable', [], []
        if len(runnable) < 2:
            g['reason'] = '; '.join(sorted({f"{info[fp]['name']}: {info[fp].get('why') or 'not runnable'}" for fp in missing}))[:500]
            return
        needed = sorted({c for fp in runnable for c in needed_columns(info[fp]['normalised'])})
        rel = choose(pool, needed, {info[fp].get('owner') for fp in runnable})
        if rel is None:
            g['reason'] = 'no Snowflake table or model holds all of: ' + ', '.join(needed[:12]) + \
                ' (bind or load the data, then run again)'
            return
        date, dim = grains(rel, field_types, use)
        sql = test_sql(rel, [info[fp]['sql'] for fp in runnable], date, dim)
        g.update(relation=rel['relation'] if rel['kind'] != 'model' else 'model', relation_label=rel['label'],
                 tested_sql=sql, grains=['total'] + (['month of ' + date] if date else []) + (['by ' + dim] if dim else []))
        try:
            rows = [tuple(_plain(v) for v in r) for r in self.sess.sql(sql).collect()]
        except Exception as exc:
            g['reason'] = f'query failed on {rel["label"]} ({type(exc).__name__})'
            return
        clusters, diffs = compare(rows, len(runnable))
        g['clusters'] = [[runnable[i] for i in c] for c in clusters]
        g['differences'] = [{'grain': d[0], 'key': d[1], 'values': {runnable[i]: d[2 + i] for i in range(len(runnable))}}
                            for d in diffs[:20]]
        if len(clusters) == 1:
            g['verdict'] = 'equivalent'
        else:
            g['verdict'] = 'conflicting' if g['source'] == 'same name' else 'differs'
        if missing:
            g['reason'] = 'not run: ' + ', '.join(info[fp]['name'] for fp in missing)

    def _group_row(self, g, at):
        return {'RUN_ID': self.id, 'RECORDED_AT': at, 'SCOPE': self.scope, 'GROUP_ID': g['id'], 'SOURCE': g['source'],
                'NAME': g['name'], 'MEMBERS': g['members'], 'VERDICT': g['verdict'], 'REASON': g.get('reason'),
                'RELATION': g.get('relation_label'), 'GRAINS': g.get('grains'), 'CLUSTERS': g.get('clusters'),
                'DIFFERENCES': g.get('differences'), 'TESTED_SQL': g.get('tested_sql'),
                'CORTEX_REASON': g.get('CORTEX_REASON') if g['source'] != 'cortex' else (g.get('reason') or None),
                'EXPLANATION': g.get('EXPLANATION'), 'RECOMMENDED_FP': g.get('RECOMMENDED_FP'),
                'CORTEX_PASSED_CHECK': g.get('CORTEX_PASSED_CHECK'), 'CODE_CHECK': g.get('CODE_CHECK')}

    def _metric_row(self, fp, at):
        i = self.info[fp]
        cls, gid, why = self.classes[fp]
        return {'RUN_ID': self.id, 'RECORDED_AT': at, 'SCOPE': self.scope, 'METRIC_FP': fp, 'NAME': i['name'],
                'NAMES': i['names'], 'KIND': i['kind'], 'CLASS': cls, 'GROUP_ID': gid, 'REASON': why,
                'WORKBOOKS': i['workbooks'], 'WORKBOOK_NAMES': sorted(set(i['books'].values())), 'SHEETS': i['sheets'],
                'TABLEAU_VIEWS': i['views'], 'EXPRESSION': str(i['expression'] or '')[:2000]}

    def _calls(self):
        import v3_store
        if not self.write_rows or not self.run['ai_log'] or not v3_store.ready(self.sess):
            return
        try:
            rows = [dict(r, RUN_ID=self.id, CALL_NO=n + 1) for n, r in enumerate(v3_store.call_rows(self.run))]
            v3_store._write(self.sess, 'CORTEX_CALLS', rows)
        except Exception:
            pass


def _writer(sess, table, rows):
    import v3_store
    cols = TABLES[table][1]
    for i in range(0, len(rows), 200):
        chunk = rows[i:i + 200]
        sql = (f'INSERT INTO {v3_store.SCHEMA}.{table} ({", ".join(c for c, _t in cols)}) SELECT '
               + ', '.join(v3_store._cast(c, t) for c, t in cols)
               + ' FROM (SELECT PARSE_JSON(column1) AS v FROM VALUES ' + ', '.join(['(?)'] * len(chunk)) + ')')
        params = [json.dumps({c: v3_store.jsonable(r.get(c)) for c, _t in cols}, allow_nan=False) for r in chunk]
        sess.sql(sql, params=params).collect()


def read_scope(sess, scope):
    import v3_catalog as C
    return {'metrics': C.frame(sess, 'V_CAT_METRICS', scope), 'usage': C.frame(sess, 'V_CAT_USAGE', scope, 200000),
            'workbooks': C.frame(sess, 'V_CAT_WORKBOOKS', scope), 'calculations': C.frame(sess, 'V_CAT_CALCULATIONS', scope),
            'fields': C.frame(sess, 'V_CAT_FIELDS', scope, 200000), 'bindings': C.frame(sess, 'V_CAT_BINDINGS', scope),
            'descriptions': C.descriptions(sess)}


def ready(sess):
    import v3_store
    if sess is None:
        return False
    try:
        db, sch = v3_store.SCHEMA.split('.')
        rows = sess.sql(f'SELECT COUNT(*) FROM {db}.INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = ? AND TABLE_NAME IN ('
                        + ','.join('?' * (len(TABLES) + 1)) + ')',
                        params=[sch] + list(TABLES) + ['CAT_BINDINGS']).collect()
        return int(rows[0][0]) == len(TABLES) + 1
    except Exception:
        return False


def latest(sess, scope):
    """The latest finished run of a scope: (run row, metrics frame, groups frame) or None."""
    import v3_store
    S = v3_store.SCHEMA
    runs = sess.sql(f"SELECT * FROM {S}.RAT_RUNS WHERE SCOPE = ? AND STATUS = 'finished' ORDER BY RECORDED_AT DESC LIMIT 1",
                    params=[scope]).to_pandas()
    if runs.empty:
        return None
    rid = runs.iloc[0]['RUN_ID']
    m = sess.sql(f'SELECT * FROM {S}.RAT_METRICS WHERE RUN_ID = ?', params=[rid]).to_pandas()
    g = sess.sql(f'SELECT * FROM {S}.RAT_GROUPS WHERE RUN_ID = ?', params=[rid]).to_pandas()
    return runs.iloc[0].to_dict(), m, g


def excel(run, metrics, groups):
    """The report as an .xlsx (bytes): Summary, Metrics, Groups, Differences."""
    import io
    import pandas as pd
    buf = io.BytesIO()
    m = metrics.copy()
    m['CLASS'] = m['CLASS'].map(LABELS).fillna(m['CLASS'])
    summary = m.groupby('CLASS').size().rename('Metrics').reset_index()
    diffs = []
    for _i, g in groups.iterrows():
        for d in _json(g.get('DIFFERENCES')) or []:
            for fp, v in (d.get('values') or {}).items():
                diffs.append({'Group': g['GROUP_ID'], 'Name': g['NAME'], 'Grain': d.get('grain'), 'Key': d.get('key'),
                              'Metric': fp, 'Value': v})
    with pd.ExcelWriter(buf, engine='openpyxl') as xl:
        pd.DataFrame([{'Scope': run.get('SCOPE'), 'Run': run.get('RUN_ID'), 'Finished': str(run.get('RECORDED_AT')),
                       'Metrics': run.get('METRICS'), 'Groups compared': run.get('GROUPS_COMPARED')}]).to_excel(
            xl, sheet_name='Summary', index=False)
        summary.to_excel(xl, sheet_name='Summary', index=False, startrow=3)
        m.drop(columns=[c for c in ('RUN_ID', 'RECORDED_AT') if c in m]).to_excel(xl, sheet_name='Metrics', index=False)
        groups.drop(columns=[c for c in ('RUN_ID', 'RECORDED_AT', 'TESTED_SQL') if c in groups]).astype(str).to_excel(
            xl, sheet_name='Groups', index=False)
        pd.DataFrame(diffs).to_excel(xl, sheet_name='Differences', index=False)
    return buf.getvalue()


def _json(v):
    if v is None or isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return None
