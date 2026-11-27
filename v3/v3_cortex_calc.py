"""Cortex as a second, independent calculation translator; code is the referee (Cortex lab).

For every Tableau calculation used by the migrated sheets:
1. Cortex (AI_COMPLETE, structured output) translates the ORIGINAL Tableau formula into one Snowflake
   SQL expression over the bound columns. It never sees the engine's translation.
2. Code validates the expression against an allowlist: only known columns, allowlisted functions and
   operators, literals; no statements, sub-queries, comments or semicolons. Anything else is rejected
   and shown, never run.
3. Code builds ONE read-only query per calculation that evaluates the engine's translation and the
   Cortex translation on the same bound data and compares them:
   - aggregate calculations: both values over the whole source;
   - row-level calculations: the number of rows where the two results differ (EQUAL_NULL).
4. The console shows a scorecard. Agreement is evidence that two independent translations match;
   disagreement points at a Cortex error or an engine bug. Nothing changes the IR, the app or a verdict.
FIXED with dimensions is compared row by row in its window form (EN04); ordered windows, FIXED inside an outer
aggregate and parameter-driven calculations are reported as not comparable.
"""
import re

ALLOWED_WORDS = set('''
SUM AVG MIN MAX COUNT COUNT_IF MEDIAN DISTINCT CASE WHEN THEN ELSE END AND OR NOT IN IS NULL LIKE ILIKE BETWEEN
IFF IFNULL NVL COALESCE NULLIF ZEROIFNULL NULLIFZERO DIV0 ROUND FLOOR CEIL CEILING ABS SIGN MOD POWER POW SQRT LN LOG
EXP GREATEST LEAST TRY_CAST CAST AS TO_DATE TO_TIMESTAMP TO_CHAR TO_VARCHAR TO_NUMBER TO_DOUBLE TRY_TO_NUMBER
TRY_TO_DOUBLE TRY_TO_DATE DATE TIMESTAMP TIMESTAMP_NTZ VARCHAR STRING TEXT NUMBER NUMERIC DECIMAL FLOAT DOUBLE
INTEGER INT BIGINT BOOLEAN DATEADD DATEDIFF DATE_TRUNC DATE_PART EXTRACT YEAR MONTH DAY QUARTER WEEK HOUR MINUTE
SECOND DAYOFWEEK DAYOFYEAR WEEKOFYEAR DAYNAME MONTHNAME LAST_DAY CURRENT_DATE LEFT RIGHT SUBSTR SUBSTRING UPPER
LOWER TRIM LTRIM RTRIM LENGTH LEN CONTAINS STARTSWITH ENDSWITH CONCAT REPLACE SPLIT_PART POSITION CHARINDEX TRUE
FALSE REGEXP_LIKE YEAROFWEEK EQUAL_NULL ANY_VALUE
'''.split())
DATE_PARTS = {'YEAR', 'MONTH', 'DAY', 'QUARTER', 'WEEK', 'HOUR', 'MINUTE', 'SECOND', 'DAYOFWEEK', 'DAYOFYEAR'}
_TOKEN = re.compile(r"""\s+|'(?:[^']|'')*'|\d+(?:\.\d+)?|[A-Za-z_][A-Za-z0-9_$]*|"[^"]+"|<=|>=|<>|!=|\|\||::|[-+*/%=<>(),.]""")
_AGG = re.compile(r'\b(SUM|AVG|MIN|MAX|COUNT|COUNT_IF|MEDIAN)\s*\(', re.I)
MAX_CALCS = 60
BATCH = 10

SCHEMA_ITEM = {'type': 'object', 'properties': {
    'name': {'type': 'string'}, 'sql_expression': {'type': 'string'},
    'level': {'type': 'string', 'enum': ['row', 'aggregate', 'cannot_translate']},
    'notes': {'type': 'string'}},
    'required': ['name', 'sql_expression', 'level', 'notes'], 'additionalProperties': False}


_TABLE_AGG = re.compile(r'\(\s*SELECT\s+(MIN|MAX|SUM|AVG|COUNT)\s*\(\s*(DISTINCT\s+)?("?[A-Za-z_][A-Za-z0-9_]*"?)\s*\)'
                        r'\s+FROM\s+__TBL__\s*\)', re.I)


_WINDOW = re.compile(r'\bOVER\s*\(\s*(?:PARTITION\s+BY\s+([^()]*?))?\s*\)', re.I)
_WINDOW_AGGS = {'SUM', 'AVG', 'MIN', 'MAX', 'COUNT', 'MEDIAN'}


def _window_calls(text):
    """[(start, end, partition_text)] of every AGG(...) OVER (PARTITION BY ...) / OVER () call, or None when an
    OVER clause does not follow an aggregate call (EN04: the one window form, nothing else)."""
    out = []
    for m in _WINDOW.finditer(text):
        i = len(text[:m.start()].rstrip()) - 1
        if i < 0 or text[i] != ')':
            return None
        depth, j = 0, i
        while j >= 0:
            depth += {')': 1, '(': -1}.get(text[j], 0)
            if depth == 0:
                break
            j -= 1
        if j < 0:
            return None
        k = j
        while k > 0 and (text[k - 1].isalnum() or text[k - 1] == '_'):
            k -= 1
        if text[k:j].strip().upper() not in _WINDOW_AGGS:
            return None
        out.append((k, m.end(), m.group(1)))
    return out


def _strip_window_calls(text):
    calls = _window_calls(text or '')
    if not calls:
        return text or ''
    out, pos = [], 0
    for a, b, _p in calls:
        out.append(text[pos:a] + '0')
        pos = b
    return ''.join(out) + text[pos:]


def validate_expression(expr, columns, windows=False):
    """(ok, reason). Allowlist tokenizer; any unknown token rejects the expression.
    One subquery form is allowed: a table-level FIXED, (SELECT AGG(COLUMN) FROM __TBL__), the same form the
    engine itself emits for {FIXED : AGG([x])}. __TBL__ is replaced by the bound relation in compare_one."""
    text = str(expr or '').strip()
    cols_up = {c.upper() for c in columns}
    for m in _TABLE_AGG.finditer(text):
        if m.group(3).strip('"').upper() not in cols_up:
            return False, f'unknown column {m.group(3)} in table-level FIXED'
    text = _TABLE_AGG.sub('0', text)
    if windows and _WINDOW.search(text):              # EN04: AGG(x) OVER (PARTITION BY known columns) only
        calls = _window_calls(text)
        if calls is None:
            return False, 'only AGG(...) OVER (PARTITION BY ...) windows are allowed'
        for _a, _b, part in calls:
            for c in [x.strip().strip('"') for x in (part or '').split(',') if x.strip()]:
                if c.upper() not in cols_up:
                    return False, f'unknown column {c} in PARTITION BY'
        text = _WINDOW.sub('', text)
    if not text:
        return False, 'empty'
    if ';' in text or '--' in text or '/*' in text:
        return False, 'statement separators or comments are not allowed'
    cols = {c.upper() for c in columns}
    pos, depth = 0, 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            return False, f'unexpected character at {pos}: {text[pos:pos + 12]!r}'
        tok = m.group(0)
        pos = m.end()
        if tok.isspace() or tok[0] == "'" or tok[0].isdigit():
            continue
        if tok == '(':
            depth += 1
        elif tok == ')':
            depth -= 1
            if depth < 0:
                return False, 'unbalanced parentheses'
        elif tok[0] == '"':
            if tok[1:-1].upper() not in cols:
                return False, f'unknown column {tok}'
        elif tok[0].isalpha() or tok[0] == '_':
            up = tok.upper()
            if up not in cols and up not in ALLOWED_WORDS and up not in DATE_PARTS:
                return False, f'not allowed: {tok}'
    if depth != 0:
        return False, 'unbalanced parentheses'
    return True, 'allowlist passed'


def _is_aggregate(sql):
    """Aggregate at the expression's own level; a table-level FIXED subquery inside a row-level
    expression ((SELECT MAX(X) FROM __TBL__)) is a scalar, not an aggregate of the expression."""
    return bool(_AGG.search(_strip_window_calls(_TABLE_AGG.sub('0', sql or ''))))


_PARAM_REF = re.compile(r'\[Parameters\]\.\[([^\[\]]+)\]')


def with_parameter_values(formula, params):
    """Replace [Parameters].[X] with its default as a literal (internal or display name), so Cortex never
    has to guess a parameter's value. Unknown parameters are left as written."""
    def lit(m):
        v = (params or {}).get(m.group(1))
        if v is None:
            return m.group(0)
        v = str(v).strip()
        if len(v) >= 2 and v[0] == v[-1] == '"':
            v = "'" + v[1:-1].replace("'", "''") + "'"
        return v
    return _PARAM_REF.sub(lit, formula or '')


_CONST = re.compile(r'^\s*(MIN|MAX|SUM|AVG)?\s*\(?\s*[-+]?\d+(\.\d+)?\s*\)?\s*$', re.I)


def param_defaults(ir):
    """{token: SQL literal} for every workbook parameter at its default value."""
    from calc_translator import param_token, param_sql_literal
    out = {}
    for cap, v in ((ir or {}).get('params') or {}).items():
        try:
            out[param_token(cap)] = param_sql_literal(v)
        except Exception:
            pass
    return out


def with_defaults(code_sql, defaults):
    for tok, lit in defaults.items():
        code_sql = code_sql.replace(tok, str(lit))
    return code_sql


def classify(name, code_sql, formula=None):
    if not code_sql:
        return 'Not comparable', 'no engine translation'
    if _CONST.match(code_sql) or (formula is not None and _CONST.match(formula)):
        return 'Skipped', 'constant'
    if re.search(r'\bOVER\s*\(', code_sql, re.I):
        if '__WIN_ORDER' in code_sql or _window_calls(code_sql) is None or re.search(
                r'\bOVER\s*\([^()]*\bORDER\s+BY', code_sql, re.I):
            return 'Not comparable', 'table calculation / ordered window'
        if _is_aggregate(code_sql):
            return 'Not comparable', 'aggregate over FIXED (next version)'
    if '__PARAM' in code_sql:
        return 'Not comparable', 'parameter without a default value'
    if '__WIN_ORDER' in code_sql:
        return 'Not comparable', 'table calculation (view order)'
    return None, None


def relation(binding, plan_compile):
    """Read-only SQL relation for the bound source and its column names."""
    if isinstance(binding, dict):
        from v3_semantic import base_plan
        base = base_plan(binding)
        return f'({plan_compile(base)})', sorted({f['output'] for f in base['fields'].values()}), \
            {local: f['output'] for local, f in base['fields'].items()}
    return binding, None, None


_REF = re.compile(r'\[([^\[\]]+)\]')
MAX_DEPS, MAX_DEP_CHARS = 15, 800


FIELD_KEYS = {'internal', 'field_id', 'field', 'calc', 'calculation', 'column', 'source_field', 'caption'}
SKIP_KEYS = {'worksheet_semantics', 'dashboard_actions', 'parameter_actions', 'action_dashboard'}


def sheet_refs(sheet):
    """Field names a sheet actually uses (axes, colour, size, label, detail, tooltip, filters, bindings).
    Workbook-wide metadata carried on the sheet (worksheet_semantics, actions) is skipped: it names every
    calculation in the workbook, so substring matching against the whole sheet picked them all."""
    from urllib.parse import unquote
    out, stack = set(), [(None, sheet)]
    while stack:
        key, v = stack.pop()
        if isinstance(v, dict):
            stack.extend((k, x) for k, x in v.items() if k not in SKIP_KEYS)
        elif isinstance(v, (list, tuple)):
            stack.extend((key, x) for x in v)
        elif isinstance(v, str) and key in FIELD_KEYS and v.strip():
            out.add(v.strip())
            if '::' in v:
                out.add(unquote(v.split('::', 1)[1]).strip())
            out.update(_REF.findall(v))
    return out


def sheet_calc_names(sheets, names, formulas, with_dependencies=True):
    """Calculations the given sheets use (exact name match on their fields), then the calculations those
    reference (formula chain). `names` = candidate calculation names, in workbook order."""
    refs = set()
    for sh in sheets or []:
        refs |= sheet_refs(sh)
    direct = [n for n in names if n in refs]
    out = list(direct)
    if with_dependencies:
        for n in direct:
            for dep in dependencies(formulas.get(n) or '', formulas):
                if dep not in out:
                    out.append(dep)
    return out


def dependencies(formula, formulas, limit=MAX_DEPS):
    """Formulas of the calculations a formula references, following the chain (code-side, deterministic).
    Lets Cortex expand nested calculations instead of declining; parameters and columns are not calculations."""
    out, queue = {}, [formula or '']
    while queue and len(out) < limit:
        for ref in _REF.findall(queue.pop(0)):
            if ref in out or ref == 'Parameters' or ref not in formulas:
                continue
            out[ref] = formulas[ref][:MAX_DEP_CHARS]
            queue.append(formulas[ref])
            if len(out) >= limit:
                break
    return out


MEMORY = {}          # formula fingerprint -> Cortex proposal (repeatable reruns in this app process; TM01 seed)


def _fingerprint(name, formula, formulas, column_map, params):
    import hashlib
    import json
    import v3_ai as A
    blob = json.dumps([formula, dependencies(formula, formulas or {}), sorted((column_map or {}).items()),
                       sorted((params or {}).items()), A.DEFAULT_MODEL], default=str)
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()


def translate(session, run, items, column_map, params=None, formulas=None):
    """Cortex translations; a formula already translated in this app process (same formula, references,
    columns, parameter defaults and model) reuses the stored answer, so reruns give the same scorecard."""
    keys = {n: _fingerprint(n, f, formulas, column_map, params) for n, f in items}
    out = {n: dict(MEMORY[k], name=n, reused=True) for n, k in keys.items() if k in MEMORY}
    todo = [(n, f) for n, f in items if n not in out]
    if todo:
        fresh = _translate_cortex(session, run, todo, column_map, params, formulas)
        for n, p in fresh.items():
            MEMORY[keys[n]] = p
        out.update(fresh)
    return out


def _translate_cortex(session, run, items, column_map, params=None, formulas=None):
    import v3_ai as A
    names = [n for n, _f in items]
    schema = {'type': 'object', 'properties': {'items': {'type': 'array', 'items': dict(SCHEMA_ITEM, properties=dict(
        SCHEMA_ITEM['properties'], name={'type': 'string', 'enum': names}))}},
        'required': ['items'], 'additionalProperties': False}
    ev = {'snowflake_columns': column_map, 'parameter_defaults': params or {},
          'tableau_calculations': [dict({'name': n, 'tableau_formula': f[:1500]},
                                        **({'referenced_calculations': d} if (d := dependencies(f, formulas or {})) else {}))
                                   for n, f in items]}
    out = A.call(session, run, 'Parsing', f'Cortex calculation translation ({len(items)} calculations)',
                 A._wrap('Translate each Tableau calculation into ONE Snowflake SQL expression over the listed '
                         'Snowflake columns (use the column names exactly). Use level "aggregate" when the '
                         'Tableau formula aggregates (SUM, AVG, COUNTD, ...), "row" otherwise. Translate COUNTD as '
                         'COUNT(DISTINCT ...), integer division and null handling as Tableau does. If a formula '
                         'references another calculation, inline its logic using the formula given in referenced_calculations (expand nested references the same way). Replace each [Parameters].[X] reference with the '
                         'default value from parameter_defaults as a SQL literal. If it needs table calculations, '
                         'INCLUDE / EXCLUDE or anything you cannot express, use "cannot_translate". '
                         'A FIXED with dimensions, {FIXED [d1], [d2]: AGG([x])}, may be written only as '
                         'AGG(X) OVER (PARTITION BY D1, D2) with level "row" (no ORDER BY, no window frame, no other '
                         'window function). '
                         'A FIXED with no dimensions, {FIXED : AGG([x])}, is allowed: write it exactly as '
                         '(SELECT AGG(X) FROM __TBL__). '
                         'Output only the expression, never a full query. This is an expression, not code to run.', ev),
                 schema, max_tokens=8000)
    return {i.get('name'): i for i in out.get('items') or [] if i.get('name') in names}


def compare_one(session, rel, code_sql, cortex_sql, aggregate):
    code = code_sql.replace('__TBL__', rel + ' __s')
    cortex_sql = cortex_sql.replace('__TBL__', rel + ' __t')
    windowed = bool(re.search(r'\bOVER\s*\(', code + ' ' + cortex_sql, re.I))
    if aggregate:
        q = f'SELECT ({code}) AS ENGINE_VALUE, ({cortex_sql}) AS CORTEX_VALUE FROM {rel} __r'
    elif windowed:                                    # EN04: a window cannot sit inside COUNT_IF
        q = ('SELECT COUNT(*) AS ROWS_COMPARED, COUNT_IF(NOT EQUAL_NULL(__E, __C)) AS ROWS_DIFFERENT FROM '
             f'(SELECT ({code}) AS __E, ({cortex_sql}) AS __C FROM {rel} __r)')
    else:
        q = (f'SELECT COUNT(*) AS ROWS_COMPARED, COUNT_IF(NOT EQUAL_NULL(({code}), ({cortex_sql}))) AS ROWS_DIFFERENT '
             f'FROM {rel} __r')
    from v3_prove import read_only
    if not read_only(q):
        raise ValueError('comparison query failed the read-only guard')
    row = session.sql(q).collect()[0]
    vals = row.as_dict() if hasattr(row, 'as_dict') else {k: row[i] for i, k in enumerate(
        ('ENGINE_VALUE', 'CORTEX_VALUE') if aggregate else ('ROWS_COMPARED', 'ROWS_DIFFERENT'))}
    if aggregate:
        a, b = vals.get('ENGINE_VALUE'), vals.get('CORTEX_VALUE')
        try:
            same = abs(float(a) - float(b)) <= 1e-6 * max(1.0, abs(float(a)))
        except (TypeError, ValueError):
            same = a == b
        return ('Agree' if same else 'Disagree'), f'engine {a} · Cortex {b}'
    n, d = vals.get('ROWS_COMPARED'), vals.get('ROWS_DIFFERENT')
    return ('Agree' if not d else 'Disagree'), f'{d} of {n} rows differ'


def run_check(session, run, used_calcs, formulas, binding, plan_compile, ir=None):
    """used_calcs: [(name, engine_sql)]; formulas: {name: tableau formula}. Returns scorecard + rows."""
    rel, columns, column_map = relation(binding, plan_compile)
    if columns is None:
        frame = session.sql(f'SELECT * FROM {rel} LIMIT 0')
        columns = list(frame.columns)
        column_map = {c: c for c in columns}
    rows, todo, seen = [], [], set()
    defaults = param_defaults(ir)
    for name, code_sql in used_calcs:
        formula = formulas.get(name)
        if formula is not None and re.sub(r'\s+', ' ', formula).strip() in seen:
            continue                                   # same formula already in the list
        if formula is not None:
            seen.add(re.sub(r'\s+', ' ', formula).strip())
        code_sql = with_defaults(code_sql or '', defaults)
        status, reason = classify(name, code_sql, formula)
        if status == 'Skipped':
            continue
        if not status and not (set(re.findall(r'[A-Z_][A-Z0-9_]*', code_sql.upper())) & {c.upper() for c in columns}):
            continue                                   # no column at all (labels, parameter flags): not a translation test
        base = {'Calculation': name, 'Tableau formula': (formula or '')[:300], 'Engine translation': (code_sql or '')[:300]}
        if formula is None:
            rows.append(dict(base, **{'Cortex translation': '', 'Result': 'Not comparable', 'Evidence': 'formula not captured'}))
        elif status:
            rows.append(dict(base, **{'Cortex translation': '', 'Result': status, 'Evidence': reason}))
        else:
            todo.append((name, formula, code_sql, base))
    todo = todo[:MAX_CALCS]
    proposals = {}
    unread = {}                                   # name -> why Cortex's answer for its batch could not be used
    params = dict((run or {}).get('result', {}).get('calc_params') or {})
    params.update({k: str(v) for k, v in ((ir or {}).get('params') or {}).items() if k not in params})
    sent = {n: with_parameter_values(f, params) for n, f in formulas.items()}   # values, not names, reach Cortex

    def ask(chunk):
        return translate(session, run, [(n, sent.get(n, with_parameter_values(f, params))) for n, f in chunk],
                         column_map, params, sent)

    for i in range(0, len(todo), BATCH):          # batches of 10: long nested formulas stay inside max_tokens
        chunk = [(n, f) for n, f, _c, _b in todo[i:i + BATCH]]
        try:
            proposals.update(ask(chunk))
        except Exception as exc:                  # one failed batch keeps the others
            cut = any(k in str(exc) for k in ('ValueError', 'JSONDecodeError'))
            if cut and len(chunk) > 1:            # cut-off answer: retry once in two smaller halves
                for half in (chunk[:len(chunk) // 2], chunk[len(chunk) // 2:]):
                    try:
                        proposals.update(ask(half))
                    except Exception:
                        unread.update({n: 'Cortex answer could not be read (likely cut off)' for n, _f in half})
                continue
            why = ('Cortex answer could not be read (likely cut off)' if cut
                   else f'Cortex call failed: {str(exc)[:120]}')
            unread.update({n: why for n, _f in chunk})
    # an expression that came back with unbalanced parentheses is asked for once more on its own
    retry = [(n, f) for n, f, _c, _b in todo
             if n in proposals and n not in unread and str(proposals[n].get('level')) != 'cannot_translate'
             and validate_expression(str(proposals[n].get('sql_expression') or ''), columns)[1] == 'unbalanced parentheses']
    for n, f in retry:
        key = _fingerprint(n, sent.get(n, f), sent, column_map, params)
        MEMORY.pop(key, None)
        try:
            proposals.update(ask([(n, f)]))
        except Exception:
            pass
    for name, _formula, code_sql, base in todo:
        p = proposals.get(name) or {}
        expr = str(p.get('sql_expression') or '').strip()
        row = dict(base, **{'Cortex translation': expr[:300]})
        if name in unread:
            rows.append(dict(row, **{'Result': 'Not comparable', 'Evidence': unread[name]}))
            continue
        if not p or p.get('level') == 'cannot_translate':
            rows.append(dict(row, **{'Result': 'Cortex declined', 'Evidence': str(p.get('notes') or '')[:200]}))
            continue
        ok, why = validate_expression(expr, columns, windows=True)
        if not ok:
            rows.append(dict(row, **{'Result': 'Rejected by allowlist', 'Evidence': why}))
            continue
        engine_agg = _is_aggregate(code_sql)
        if engine_agg != (p.get('level') == 'aggregate') or engine_agg != _is_aggregate(expr):
            rows.append(dict(row, **{'Result': 'Disagree', 'Evidence': 'different level (row vs aggregate)'}))
            continue
        try:
            result, evidence = compare_one(session, rel, code_sql, expr, engine_agg)
        except Exception as exc:
            result, evidence = 'Query failed', f'{type(exc).__name__}: {str(exc)[:160]}'
        rows.append(dict(row, **{'Result': result, 'Evidence': evidence}))
    score = {k: sum(1 for r in rows if r['Result'] == k) for k in
             ('Agree', 'Disagree', 'Rejected by allowlist', 'Cortex declined', 'Query failed', 'Not comparable')}
    score['Compared'] = score['Agree'] + score['Disagree']
    score['Eligible'] = len(rows)
    reasons = {}
    for r in rows:
        if r['Result'] not in ('Agree', 'Disagree'):
            key = f"{r['Result']}: {re.sub(r'[0-9]+', '#', str(r['Evidence']))[:90]}"
            reasons[key] = reasons.get(key, 0) + 1
    return {'score': score, 'rows': rows,
            'reasons': sorted(({'Reason': k, 'Calculations': v} for k, v in reasons.items()), key=lambda x: -x['Calculations'])}
