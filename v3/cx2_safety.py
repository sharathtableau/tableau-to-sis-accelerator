"""CX9 safety and cost for the Cortex visual review (CX2): a token budget per run, a guard against instructions
hidden in workbook text or in the images, and a per-call cost and evaluation record.

Rules (master plan 6A): images and workbook text are DATA, never instructions; a guarded finding is dropped and
counted, never shown as a finding; nothing here changes a verdict (verdicts come only from code evidence).
"""
import re
import time

VERSION = 'cx9-safety/2'
DEFAULT_BUDGET_TOKENS = 120000          # input tokens per review run (prompt text + images), owner-adjustable
IMAGE_TOKENS_PER_PX = 1 / 750.0         # Anthropic vision estimate: tokens ~= width * height / 750
MAX_IMAGE_SIDE = 1568                   # larger images are downscaled by the model service

# phrases that only appear when text tries to steer the model (workbook captions, image text, model output)
_INJECTION = re.compile(
    r"ignore\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\s+(?:instructions?|prompts?|rules?)"
    r"|disregard\s+(?:the\s+)?(?:previous|above|system|instructions?)"
    r"|you\s+are\s+now\b|act\s+as\s+(?:an?\s+)?(?:admin|system|developer)"
    r"|system\s+prompt|developer\s+message|new\s+instructions?\s*:"
    r"|\b(?:drop|delete|truncate|grant|revoke|alter|insert|update|create)\s+(?:table|schema|database|role|user|stage|into)\b"
    r"|<\s*/?\s*(?:script|iframe|img|svg)\b|javascript:|\bexec(?:ute)?\s+immediate\b", re.I)


def is_injection(text):
    return bool(_INJECTION.search(str(text or '')))


def safe_label(label, limit=120):
    """A zone label (sheet / zone name from the workbook) is quoted into the prompt as DATA: braces, quotes,
    backticks and newlines removed, instruction-like text replaced, length capped."""
    s = re.sub(r'[\r\n\t`{}<>"\\]+', ' ', str(label or 'zone'))
    s = ' '.join(s.split())[:limit]
    return '[label withheld: instruction-like text]' if is_injection(s) else s


def guard_findings(findings):
    """Model findings -> (kept, guarded). A finding whose text carries instructions (from the images or the
    model) is dropped: it is evidence of an injection attempt, not a visual difference."""
    kept, guarded = [], []
    for f in findings or []:
        text = ' '.join(str(f.get(k, '')) for k in ('tableau', 'app', 'xml_hint'))
        (guarded if is_injection(text) else kept).append(f)
    return kept, guarded


def image_tokens(size):
    w, h = size
    scale = min(1.0, MAX_IMAGE_SIDE / float(max(w, h) or 1))
    return int(round(w * scale * h * scale * IMAGE_TOKENS_PER_PX))


def text_tokens(session, model, text):
    """AI_COUNT_TOKENS for the prompt text when the account has it; a 4-chars-per-token estimate otherwise.
    Returns (tokens, source)."""
    if session is not None:
        try:
            row = session.sql("SELECT AI_COUNT_TOKENS('ai_complete', ?, ?)", params=[model, text]).collect()
            if row and row[0][0] is not None:
                return int(row[0][0]), 'AI_COUNT_TOKENS'
        except Exception:
            pass                                  # codes only; the estimate below is used
    return max(1, len(str(text)) // 4), 'estimate'


class Budget:
    """Per-run input-token cap. `allow(n)` is asked BEFORE each call; a refused call is recorded, not made."""

    def __init__(self, limit=DEFAULT_BUDGET_TOKENS):
        self.limit = int(limit)
        self.used = 0
        self.refused = 0

    def allow(self, tokens):
        if self.used + tokens > self.limit:
            self.refused += 1
            return False
        self.used += tokens
        return True

    def summary(self):
        return {'limit_tokens': self.limit, 'used_tokens_est': self.used, 'calls_refused': self.refused}


def call_record(zone, model, status, prompt_tokens, token_source, image_tok, seconds, n_findings, n_guarded):
    """One row of the per-call cost log (also the evaluation set's call side)."""
    return {'zone': zone, 'model': model, 'status': status, 'prompt_tokens': prompt_tokens,
            'prompt_token_source': token_source, 'image_tokens_est': image_tok,
            'input_tokens_est': prompt_tokens + image_tok, 'seconds': seconds,
            'findings': n_findings, 'guarded': n_guarded, 'at': time.strftime('%Y-%m-%dT%H:%M:%S')}


def _eval_id(*parts):
    import hashlib
    return hashlib.sha256('\u241f'.join(str(p) for p in parts).encode('utf-8')).hexdigest()[:32]


def evaluation_rows(run_id, workbook, dashboard, findings):
    """The evaluation set: one row per finding with the code verdict and an empty owner label
    ('confirmed' / 'rejected' is set later by a person). Measures whether the AI step helps. Ids are stable."""
    return [{'id': _eval_id(run_id, dashboard, i, f.get('zone'), f.get('category')),
             'run': run_id, 'workbook': workbook, 'dashboard': dashboard, 'zone': f.get('zone'),
             'category': f.get('category'), 'severity': f.get('severity'), 'verdict': f.get('verdict'),
             'construct': f.get('construct'), 'owner_label': None} for i, f in enumerate(findings)]


def cx3_evaluation_rows(run_id, workbook, rows):
    """CX3 rows in the same evaluation set: item = calculation, category = lod / table_calc, verdict = the code
    state (VERIFIED, NOT_MATCHED, ...), construct = the plan kind."""
    return [{'id': _eval_id(run_id, 'CX3', i, r.get('caption')), 'run': run_id, 'workbook': workbook,
             'dashboard': None, 'zone': r.get('caption'), 'category': r.get('kind'), 'severity': None,
             'verdict': r.get('state'), 'construct': (r.get('plan') or {}).get('kind'), 'owner_label': None}
            for i, r in enumerate(rows)]


# ---------------------------------------------------------------- PII redaction (CX9-03, AI_REDACT)
# Identity categories only: DATE_OF_BIRTH / AGE / GENDER would also strip ordinary analytic fields (order dates, age
# bands, gender splits) that a migration review needs. AI_REDACT limit: input + output 4,096 tokens (short rows only).
REDACT_CATEGORIES = ('NAME', 'EMAIL', 'PHONE_NUMBER', 'ADDRESS', 'NATIONAL_ID', 'PASSPORT', 'TAX_IDENTIFIER',
                     'PAYMENT_CARD_DATA', 'DRIVERS_LICENSE', 'IP_ADDRESS')


def redact_sql(column):
    """AI_REDACT around a column of a code-built query (the category list is a code constant)."""
    return f"AI_REDACT({column}, [" + ', '.join("'" + c + "'" for c in REDACT_CATEGORIES) + '])'


# ---------------------------------------------------------------- evaluation store in Snowflake (CX9-02)
EVAL_SCHEMA = 'WBR_DB.CX_EVAL'
EVAL_STEPS = ('CX2', 'CX3', 'CX4', 'CX5', 'CX6', 'CX7', 'CX8')
LABELS = ('confirmed', 'rejected')
_ID = re.compile(r'[0-9a-f]{32}')
_CHUNK = 50


def eval_setup_sql(role='WBR_OWNER'):
    """One-time SQL a person runs (shown in the app). Never OR REPLACE."""
    t, c = f'{EVAL_SCHEMA}.EVAL_ROWS', f'{EVAL_SCHEMA}.CORTEX_CALLS'
    return [f'CREATE SCHEMA IF NOT EXISTS {EVAL_SCHEMA};',
            f'CREATE TABLE IF NOT EXISTS {t} (EVAL_ID STRING, STEP STRING, RUN_ID STRING, WORKBOOK STRING, '
            'DASHBOARD STRING, ITEM STRING, CATEGORY STRING, SEVERITY STRING, VERDICT STRING, CONSTRUCT STRING, '
            'OWNER_LABEL STRING, LABELLED_BY STRING, LABELLED_AT TIMESTAMP_LTZ, '
            'RECORDED_AT TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP());',
            f'CREATE TABLE IF NOT EXISTS {c} (STEP STRING, RUN_ID STRING, ITEM STRING, MODEL STRING, STATUS STRING, '
            'INPUT_TOKENS_EST NUMBER, SECONDS FLOAT, RECORDED_AT TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP());',
            f'GRANT SELECT, INSERT, UPDATE ON TABLE {t} TO ROLE {role};',
            f'GRANT SELECT, INSERT ON TABLE {c} TO ROLE {role};']


def _clean(v, limit=200):
    if v is None:
        return None
    return safe_label(v, limit)


def _insert(session, table, cols, rows):
    for i in range(0, len(rows), _CHUNK):
        part = rows[i:i + _CHUNK]
        marks = ', '.join('(' + ', '.join('?' * len(cols)) + ')' for _ in part)
        session.sql(f'INSERT INTO {table} ({", ".join(cols)}) VALUES {marks}',
                    params=[v for r in part for v in r]).collect()


def record_evaluation(session, step, rows, calls, run_id=None):
    """Store one step's evaluation rows and call costs. Every value is bound and passed through safe_label.
    Returns a status code."""
    if step not in EVAL_STEPS:
        return 'UNKNOWN_STEP'
    if session is None:
        return 'NO_SNOWFLAKE_SESSION'
    ev = [[r['id'], step, _clean(r.get('run'), 64), _clean(r.get('workbook')), _clean(r.get('dashboard')),
           _clean(r.get('zone')), _clean(r.get('category'), 60), _clean(r.get('severity'), 20),
           _clean(r.get('verdict'), 40), _clean(r.get('construct'), 120)] for r in rows if _ID.fullmatch(str(r.get('id')))]
    rid = _clean(run_id or (rows[0].get('run') if rows else ''), 64)
    cl = [[step, rid, _clean(c.get('zone')), _clean(c.get('model'), 80), _clean(c.get('status'), 60),
           int(c.get('input_tokens_est') or 0), float(c.get('seconds') or 0)] for c in calls or []]
    try:
        if ev:
            _insert(session, f'{EVAL_SCHEMA}.EVAL_ROWS', ['EVAL_ID', 'STEP', 'RUN_ID', 'WORKBOOK', 'DASHBOARD', 'ITEM',
                                                         'CATEGORY', 'SEVERITY', 'VERDICT', 'CONSTRUCT'], ev)
        if cl:
            _insert(session, f'{EVAL_SCHEMA}.CORTEX_CALLS', ['STEP', 'RUN_ID', 'ITEM', 'MODEL', 'STATUS',
                                                            'INPUT_TOKENS_EST', 'SECONDS'], cl)
        return 'SAVED'
    except Exception as exc:                      # codes only
        return 'EVAL_WRITE_FAILED:' + type(exc).__name__


def label_evaluation(session, eval_id, label):
    """A person's label on one row; the labeller is Snowflake's CURRENT_USER(), never a client value."""
    if label not in LABELS:
        return 'UNKNOWN_LABEL'
    if not _ID.fullmatch(str(eval_id or '')):
        return 'UNKNOWN_ID'
    if session is None:
        return 'NO_SNOWFLAKE_SESSION'
    try:
        session.sql(f'UPDATE {EVAL_SCHEMA}.EVAL_ROWS SET OWNER_LABEL = ?, LABELLED_BY = CURRENT_USER(), '
                    'LABELLED_AT = CURRENT_TIMESTAMP() WHERE EVAL_ID = ?', params=[label, eval_id]).collect()
        return 'LABELLED'
    except Exception as exc:
        return 'EVAL_WRITE_FAILED:' + type(exc).__name__


def evaluation_history(session, step=None):
    """({(step, verdict): {rows, labelled, confirmed, precision}}, status) across every stored run."""
    if session is None:
        return {}, 'NO_SNOWFLAKE_SESSION'
    where, params = ('WHERE STEP = ? ', [step]) if step in EVAL_STEPS else ('', [])
    try:
        got = session.sql(f'SELECT STEP, VERDICT, OWNER_LABEL, COUNT(*) FROM {EVAL_SCHEMA}.EVAL_ROWS {where}'
                          'GROUP BY 1, 2, 3', params=params).collect()
    except Exception as exc:
        return {}, 'NO_EVAL_TABLE:' + type(exc).__name__
    out = {}
    for st_, verdict, lab, n in got:
        m = out.setdefault((st_, verdict), {'rows': 0, 'labelled': 0, 'confirmed': 0})
        m['rows'] += int(n)
        if lab in LABELS:
            m['labelled'] += int(n)
            m['confirmed'] += int(n) if lab == 'confirmed' else 0
    for m in out.values():
        m['precision'] = round(m['confirmed'] / m['labelled'], 3) if m['labelled'] else None
    return out, 'OK'


def cost_history(session):
    """({step: {calls, input_tokens_est, seconds}}, status) across every stored run."""
    if session is None:
        return {}, 'NO_SNOWFLAKE_SESSION'
    try:
        got = session.sql(f'SELECT STEP, COUNT(*), SUM(INPUT_TOKENS_EST), SUM(SECONDS) FROM {EVAL_SCHEMA}.CORTEX_CALLS '
                          'GROUP BY 1').collect()
    except Exception as exc:
        return {}, 'NO_EVAL_TABLE:' + type(exc).__name__
    return {st_: {'calls': int(n), 'input_tokens_est': int(t or 0), 'seconds': float(sec or 0)}
            for st_, n, t, sec in got}, 'OK'


def evaluation_metrics(rows):
    """Precision of each verdict class against the owner's labels (labelled rows only)."""
    out = {}
    for r in rows:
        if r.get('owner_label') not in ('confirmed', 'rejected'):
            continue
        m = out.setdefault(r['verdict'], {'labelled': 0, 'confirmed': 0})
        m['labelled'] += 1
        m['confirmed'] += r['owner_label'] == 'confirmed'
    for m in out.values():
        m['precision'] = round(m['confirmed'] / m['labelled'], 3) if m['labelled'] else None
    return out
