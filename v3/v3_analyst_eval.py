"""CF03: Ask the data evaluated against the verified queries (CF02) as ground truth.

Each VERIFIED verified query is asked as its question. Cortex Analyst's SQL runs only as one read-only query with a
row limit (v3_semantic.run_read_only; owner decision 21 Nov). Its numbers are compared with the code-built
SEMANTIC_VIEW() proof query, which already equals Tableau's values. Correct = same row count and every proof metric
matched by one numeric column of the answer (sorted values within 1e-6 relative). Codes only, never error text."""
REL_TOL = 1e-6


def _col(rows, name):
    keys = {k.upper(): k for k in (rows[0] if rows else {})}
    k = keys.get(str(name).upper())
    return None if k is None else [r.get(k) for r in rows]


def _num(vals):
    try:
        return sorted(float(v) for v in vals if v is not None)
    except (TypeError, ValueError):
        return None


def _same(a, b):
    return len(a) == len(b) and all(abs(x - y) <= REL_TOL * max(1.0, abs(x)) for x, y in zip(a, b))


def compare(proof_rows, answer_rows, metrics):
    if not answer_rows or len(answer_rows) != len(proof_rows):
        return False
    columns = [_num([r.get(k) for r in answer_rows]) for k in answer_rows[0]]
    for m in metrics:
        want = _num(_col(proof_rows, m) or [])
        if not want or not any(c is not None and _same(c, want) for c in columns):
            return False
    return True


def evaluate(session, view, verified, ask=None):
    """-> {'rows': [{'Sheet', 'Question', 'Result', 'SQL'}], 'sql_correctness'}."""
    import v3_semantic as SV
    ask = ask or SV.ask_analyst
    out = []
    for vq in verified or []:
        if vq.get('state') != 'VERIFIED':
            continue
        row = {'Sheet': vq.get('sheet'), 'Question': vq.get('question'), 'SQL': ''}
        try:
            proof = [r.as_dict() if hasattr(r, 'as_dict') else dict(r) for r in session.sql(vq['proof_sql']).collect()]
        except Exception as exc:
            out.append(dict(row, Result='PROOF_FAILED:' + type(exc).__name__))
            continue
        try:
            sql, _text = ask(view, vq['question'])
        except Exception as exc:
            out.append(dict(row, Result='ANALYST_FAILED:' + type(exc).__name__))
            continue
        row['SQL'] = (sql or '')[:2000]
        if not sql:
            out.append(dict(row, Result='NO_SQL'))
            continue
        try:
            answer = SV.run_read_only(session, sql)
        except ValueError:
            out.append(dict(row, Result='SQL refused'))
            continue
        except Exception as exc:
            out.append(dict(row, Result='QUERY_FAILED:' + type(exc).__name__))
            continue
        ok = compare(proof, answer, [m['metric'] for m in vq.get('measures') or []])
        out.append(dict(row, Result='Correct' if ok else 'Incorrect'))
    decided = [r for r in out if r['Result'] in ('Correct', 'Incorrect', 'SQL refused')]
    return {'rows': out, 'sql_correctness': (sum(r['Result'] == 'Correct' for r in decided) / len(decided))
            if decided else None}
