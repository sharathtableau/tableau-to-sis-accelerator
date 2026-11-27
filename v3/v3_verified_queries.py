"""CF02: sheets proven equal to Tableau become verified queries (AI_VERIFIED_QUERIES) of the run's Semantic View.

Code writes the question and the SQL, only for shapes it can state exactly (raw dimensions, SUM of raw fact columns,
no filters, no date parts, no calculations, no table calcs, no Measure Names / Values). Before a query is offered,
code proves it: a read-only SEMANTIC_VIEW() query over the same dimensions and metrics must give Tableau's displayed
values for every mark of the sheet. A person approves adding them; the view is recreated with the clause, and if
Snowflake rejects the clause it is recreated as it was. Status codes only, never Snowflake error text."""
import re
import time

_IDENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_$]*$')
_FQN = re.compile(r'^[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*){2}$')
MAX_DIMS = 3


def _norm(text):
    return re.sub(r'[^A-Z0-9]', '', str(text or '').upper())


def proven_sheets(run):
    res = ((run or {}).get('sheet_reference') or {}).get('results') or {}
    return [n for n, v in res.items() if str((v[0] or {}).get('Status', '')).startswith('Match')]


def _lookup(sem, caption, root):
    hits = [d for d in sem.get('dimensions') or []
            if d['table'] == root and _norm(caption) in (_norm(d['caption']), _norm(d['column']), _norm(d['name']))]
    return hits[0] if len(hits) == 1 else None


def candidate(sheet_name, sheet, plan, sem):
    """{'state': CANDIDATE | SKIPPED, 'reason', 'question', 'sql', 'proof_sql', 'dims', 'measures'}."""
    def skip(why):
        return {'sheet': sheet_name, 'state': 'SKIPPED', 'reason': why}
    root = next((t['alias'] for t in sem.get('tables') or [] if t.get('role') == 'fact'), None)
    if not root or not _FQN.match(str(sem.get('name') or '')):
        return skip('no fact table in the Semantic View')
    if (sheet or {}).get('filters') or (plan or {}).get('filters'):
        return skip('the sheet has a filter')
    if not (plan or {}).get('aggregate', True):
        return skip('the sheet shows disaggregated rows')
    dims, measures = [], []
    for f in (plan or {}).get('fields') or []:
        if f.get('kind') in ('measure_names',) or f.get('measure_values'):
            return skip('Measure Names / Values on the sheet')
        if f.get('kind') == 'generated':
            continue
        if f.get('table_calc') or f.get('window'):
            return skip(f"{f.get('caption')}: table calculation")
        if f.get('calculation'):
            return skip(f"{f.get('caption')}: calculation")
        if f.get('date_part', 'none') != 'none':
            return skip(f"{f.get('caption')}: date part")
        d = _lookup(sem, f.get('caption'), root)
        if d is None:
            return skip(f"{f.get('caption')}: not in the Semantic View fact table")
        if f.get('kind') == 'dim':
            if d not in dims:
                dims.append(d)
        elif f.get('kind') == 'measure':
            if f.get('agg') != 'sum':
                return skip(f"{f.get('caption')}: aggregation {f.get('agg')} (only SUM is stated exactly)")
            metric = next((m for m in sem.get('metrics') or [] if m['table'] == root
                           and m['name'] == 'TOTAL_' + d['name']), None)
            if metric is None:
                return skip(f"{f.get('caption')}: no TOTAL metric in the Semantic View")
            measures.append((d, metric, f.get('caption')))
        else:
            return skip(f"{f.get('caption')}: field kind {f.get('kind')}")
    if not measures:
        return skip('no measure')
    if len(dims) > MAX_DIMS:
        return skip(f'more than {MAX_DIMS} dimensions')
    names = [d['name'] for d in dims] + [m['name'] for _d, m, _c in measures]
    if not all(_IDENT.match(n) for n in names + [root]):
        return skip('a name needs quoting')
    sel = [d['name'] for d in dims] + [f"SUM({d['name']}) AS {m['name']}" for d, m, _c in measures]
    sql = f"SELECT {', '.join(sel)} FROM __{root}"
    if dims:
        cols = ', '.join(d['name'] for d in dims)
        sql += f' GROUP BY {cols} ORDER BY {cols}'
    proof = (f"SELECT * FROM SEMANTIC_VIEW({sem['name']}"
             + (' DIMENSIONS ' + ', '.join(f"{root}.{d['name']}" for d in dims) if dims else '')
             + ' METRICS ' + ', '.join(f"{root}.{m['name']}" for _d, m, _c in measures) + ')')
    meas_text = ' and '.join(c for _d, _m, c in measures)
    question = f'What is the total {meas_text}' + (' by ' + ' and '.join(d['caption'] for d in dims) if dims else '') + '?'
    return {'sheet': sheet_name, 'state': 'CANDIDATE', 'reason': '', 'question': question, 'sql': sql,
            'proof_sql': proof, 'dims': [d['name'] for d in dims],
            'measures': [{'metric': m['name'], 'caption': c} for _d, m, c in measures]}


def _parse(text):
    """(value, tolerance) of a Tableau display string, or None."""
    t = str(text or '').strip()
    if not t or '%' in t:
        return None
    neg = t.startswith('(') and t.endswith(')') or t.startswith('-') or t.startswith('−')
    m = re.search(r'(\d[\d,]*)(?:\.(\d+))?\s*([KMB])?', t, re.I)
    if not m:
        return None
    dec = len(m.group(2) or '')
    scale = {'K': 1e3, 'M': 1e6, 'B': 1e9}.get((m.group(3) or '').upper(), 1.0)
    value = float(m.group(1).replace(',', '') + ('.' + m.group(2) if m.group(2) else '')) * scale
    return (-value if neg else value), 0.5 * (10 ** -dec) * scale + 1e-9


def prove(session, cand, detail):
    """VERIFIED when the SEMANTIC_VIEW() values equal Tableau's for every mark; NOT_MATCHED; NOT_COMPARABLE."""
    out = dict(cand)
    try:
        rows = [list(r) for r in session.sql(cand['proof_sql']).collect()]
    except Exception as exc:
        out.update(state='NOT_COMPARABLE', reason='SEMANTIC_VIEW_QUERY_FAILED:' + type(exc).__name__)
        return out
    nd = len(cand['dims'])
    for i, m in enumerate(cand['measures']):
        marks = [d for d in detail or [] if d.get('Measure') in (m['caption'], '—', None) or len(cand['measures']) == 1]
        tab = [_parse(d.get('Tableau')) for d in marks]
        if not tab or any(t is None for t in tab):
            out.update(state='NOT_COMPARABLE', reason=f"{m['caption']}: Tableau values are not plain numbers")
            return out
        try:
            sv = sorted(float(r[nd + i]) for r in rows if r[nd + i] is not None)
        except (TypeError, ValueError, IndexError):
            out.update(state='NOT_COMPARABLE', reason='unexpected result shape')
            return out
        tab = sorted(tab)
        if len(sv) != len(tab):
            out.update(state='NOT_MATCHED', reason=f"{m['caption']}: {len(sv)} values vs Tableau's {len(tab)}")
            return out
        bad = sum(1 for a, (b, tol) in zip(sv, tab) if abs(a - b) > tol)
        if bad:
            out.update(state='NOT_MATCHED', reason=f"{m['caption']}: {bad} of {len(tab)} values differ from Tableau")
            return out
    out.update(state='VERIFIED', reason=f'{len(rows)} mark(s) equal Tableau through the Semantic View')
    return out


def _lit(text):
    return "'" + str(text).replace('\\', '\\\\').replace("'", "''") + "'"


def entry(cand, verified_at=None):
    if cand.get('state') != 'VERIFIED':
        raise ValueError('only a verified query can be added')
    name = 'VQ_' + re.sub(r'[^A-Z0-9]+', '_', str(cand['sheet']).upper()).strip('_')[:60]
    return {'name': name or 'VQ_SHEET', 'question': cand['question'], 'sql': cand['sql'], 'sheet': cand['sheet'],
            'verified_at': int(verified_at if verified_at is not None else time.time())}


def clause(entries):
    items, used = [], set()
    for e in entries:
        name, n = e['name'], 2
        while name in used:
            name, n = f"{e['name']}_{n}", n + 1
        used.add(name)
        items.append(f"    {name} AS (\n      QUESTION {_lit(e['question'])}\n      VERIFIED_AT {int(e['verified_at'])}\n"
                     f"      ONBOARDING_QUESTION FALSE\n      SQL {_lit(e['sql'])}\n    )")
    return '  AI_VERIFIED_QUERIES (\n' + ',\n'.join(items) + '\n  )'


def apply(session, sem, entries, approved_by):
    """(status, sem). Recreates the view with the clause; restores it without when Snowflake rejects the clause."""
    import v3_semantic as SV
    if not str(approved_by or '').strip():
        raise ValueError('a named person must approve adding verified queries')
    new = dict(sem, verified_queries=list(entries))
    try:
        session.sql(SV.ddl(new)).collect()
        return 'ADDED', new
    except Exception as exc:
        old = sem
        try:
            session.sql(SV.ddl(old)).collect()
        except Exception:
            return 'VQ_REJECTED_AND_RESTORE_FAILED:' + type(exc).__name__, old
        return 'VQ_REJECTED_BY_SNOWFLAKE', old
