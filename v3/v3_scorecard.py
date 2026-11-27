"""CF01: Cortex contribution scorecard.

For each place Cortex proposes something, count what it proposed, what passed code's check, what code rejected and
what still waits for evidence. Read from the run's stored records by a closed table of rules (RULES), never from model
text, so the numbers match the results the console shows. Advisory only: it never changes a verdict."""

PASS, REJECT, WAIT = 'pass', 'reject', 'wait'


def _rows(rec):
    if isinstance(rec, dict):
        return rec.get('rows') or []
    return rec or []


def _calc_xl(rec):
    m = {'Agree': PASS, 'Disagree': REJECT, 'Rejected by allowlist': REJECT}
    return [m[r.get('Result')] for r in _rows(rec) if r.get('Result') in m]


def _states(passed, rejected, waiting):
    def rule(rec):
        out = []
        for r in _rows(rec):
            s = r.get('state')
            out += [PASS] if s in passed else [REJECT] if s in rejected else [WAIT] if s in waiting else []
        return out
    return rule


def _top_rank(rec):
    out = []
    for proof in (rec or {}).values():
        if not isinstance(proof, dict) or proof.get('error'):
            continue
        top = next((r for r in proof.get('results') or [] if str(r.get('Cortex rank')) == '1'), None)
        if top is not None:
            out.append(PASS if top.get('Result') == 'Proven' else REJECT)
    return out


def _models(rec):
    return [PASS if r.get('verified') else REJECT for r in (rec or {}).values() if isinstance(r, dict)]


def _mappings(rec):
    return [PASS if r.get('ok') else REJECT for r in (rec or {}).values()
            if isinstance(r, dict) and not r.get('error') and 'ok' in r]


def _briefs(rec):
    m = {'OK': PASS, 'REJECTED_BY_CODE': REJECT}
    return [m[r.get('brief_status')] for r in _rows(rec) if r.get('brief_status') in m]


def _documents(rec):
    out = []
    for doc in rec or []:
        if not isinstance(doc, dict):
            continue
        out += [PASS] * len(doc.get('rows') or [])
        out += [REJECT] * sum(int(v or 0) for v in (doc.get('dropped') or {}).values())
    return out


def _ask_eval(rec):
    m = {'Correct': PASS, 'Incorrect': REJECT, 'SQL refused': REJECT}
    return [m[r.get('Result')] for r in _rows(rec) if r.get('Result') in m]


# (run key, stage, decision, rule) -- closed list; add a row when Cortex gains a new proposal point
RULES = [
    ('calc_xl', 'Parsing', 'Calculation cross-check', _calc_xl),
    ('cf05', 'Parsing', 'First calculation translation (CF05)',
     _states({'AGREES_WITH_ENGINE', 'VERIFIED'}, {'REJECTED_BY_CODE', 'DISAGREES_WITH_ENGINE', 'NOT_MATCHED'},
             {'PROPOSED', 'NO_TABLEAU_REFERENCE'})),
    ('cx3', 'Parsing', 'Hard calculation plans (CX3)',
     _states({'VERIFIED'}, {'REJECTED_BY_CODE', 'NOT_MATCHED'}, {'PROPOSED', 'NO_TABLEAU_REFERENCE'})),
    ('cortex_models', 'Data model', 'Semantic model design', _models),
    ('cortex_mappings', 'Data model', 'Table mapping', _mappings),
    ('proofs', 'Validation', 'Cause proof (top-ranked cause)', _top_rank),
    ('cx4', 'Validation', 'Diagnosis agent (CX4)', _top_rank),
    ('cx5', 'Validation', 'Repair briefs (CX5)', _briefs),
    ('cx8', 'Inventory', 'Document requirements (CX8)', _documents),
    ('cf03', 'Cortex AI', 'Ask the data vs verified queries (CF03)', _ask_eval),
]


def _rate(p, r):
    return f'{round(100 * p / (p + r))}%' if p + r else '—'


def scorecard(run):
    out = []
    for key, stage, decision, rule in RULES:
        if not (run or {}).get(key):
            continue
        try:
            marks = rule(run[key])
        except Exception:
            continue                              # an unexpected record shape is left out, never guessed
        if not marks:
            continue
        p, r, w = marks.count(PASS), marks.count(REJECT), marks.count(WAIT)
        out.append({'Stage': stage, 'Decision': decision, 'Cortex proposed': len(marks), 'Passed code check': p,
                    'Rejected by code': r, 'Awaiting evidence': w, 'Pass rate': _rate(p, r)})
    return out


def totals(rows):
    keys = ('Cortex proposed', 'Passed code check', 'Rejected by code', 'Awaiting evidence')
    t = {k: sum(int(r.get(k) or 0) for r in rows or []) for k in keys}
    t['Pass rate'] = _rate(t['Passed code check'], t['Rejected by code'])
    return t


def calls_by_stage(run):
    out = {}
    for e in (run or {}).get('ai_log') or []:
        if e.get('Status') in ('Not run', 'Not attempted'):
            continue
        out[e.get('Step')] = out.get(e.get('Step'), 0) + 1
    return out
