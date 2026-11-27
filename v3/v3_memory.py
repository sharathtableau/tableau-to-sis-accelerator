"""TM01 translation memory + EN02 proven-fix library.

One table keeps what was proven once:
  cf05        a CF05 translation Verified against Tableau's values and applied by a person
  proven_fix  a cause-proof fix proven by code against Tableau's values and applied by a person
Later runs give the closest proven translations to Cortex as examples and proven fixes for the same formulas to the
cause proof as a code fact. Examples never bypass code (allowlist, referee, Tableau values, a person's approval).
Every value is bound; instruction-like text is refused; without the table the app behaves as before."""
import hashlib
import json
import re

TABLE = 'WBR_DB.CX_CALC.TRANSLATION_MEMORY'
KINDS = ('cf05', 'proven_fix')
VERSION = 'tm01/1'
_ROLE = re.compile(r'^[A-Za-z_][A-Za-z0-9_$]{0,254}$')


def sig(formula):
    s = re.sub(r'\s+', '', str(formula or ''))
    return hashlib.sha256(s.upper().encode('utf-8')).hexdigest()


def setup_sql(role='WBR_OWNER'):
    """One-time SQL a person runs (shown in the app). Never OR REPLACE: proven entries are kept."""
    if not _ROLE.match(str(role or '')):
        raise ValueError('role rejected')
    return ['CREATE SCHEMA IF NOT EXISTS WBR_DB.CX_CALC;',
            f'CREATE TABLE IF NOT EXISTS {TABLE} (KIND STRING, FORMULA_SHA STRING, FORMULA STRING, ANSWER VARIANT, '
            'CAPTION STRING, WORKBOOK STRING, EVIDENCE VARIANT, RUN_ID STRING, VERSION STRING, '
            'SAVED_AT TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP());',
            f'GRANT SELECT, INSERT, UPDATE ON TABLE {TABLE} TO ROLE {role.upper()};']


def _answer_ok(kind, answer):
    if not isinstance(answer, dict):
        return False
    if kind == 'cf05':
        return bool(str(answer.get('sql_expression') or '').strip()) and answer.get('level') in ('row', 'aggregate')
    return bool(answer.get('hypothesis'))


def save(session, kind, formula, answer, caption='', workbook='', evidence=None, run_id='', proven=False):
    """MERGE one proven entry (keyed by kind + formula). Returns a status code."""
    import cx2_safety as S
    if kind not in KINDS:
        return 'KIND_REFUSED'
    if not proven:
        return 'NOT_PROVEN'
    if any(S.is_injection(t) for t in (formula, caption, workbook, json.dumps(answer, default=str))):
        return 'GUARD_REFUSED'
    if session is None:
        return 'NO_SNOWFLAKE_SESSION'
    if kind == 'cf05' and isinstance(answer, dict) and 'level' not in answer:
        answer = dict(answer, level='aggregate')
    sql = (f'MERGE INTO {TABLE} t USING (SELECT ? AS KIND, ? AS FORMULA_SHA, ? AS FORMULA, PARSE_JSON(?) AS ANSWER, '
           '? AS CAPTION, ? AS WORKBOOK, PARSE_JSON(?) AS EVIDENCE, ? AS RUN_ID, ? AS VERSION) s '
           'ON t.KIND = s.KIND AND t.FORMULA_SHA = s.FORMULA_SHA '
           'WHEN MATCHED THEN UPDATE SET ANSWER = s.ANSWER, CAPTION = s.CAPTION, WORKBOOK = s.WORKBOOK, '
           'EVIDENCE = s.EVIDENCE, RUN_ID = s.RUN_ID, VERSION = s.VERSION, SAVED_AT = CURRENT_TIMESTAMP() '
           'WHEN NOT MATCHED THEN INSERT (KIND, FORMULA_SHA, FORMULA, ANSWER, CAPTION, WORKBOOK, EVIDENCE, RUN_ID, '
           'VERSION) VALUES (s.KIND, s.FORMULA_SHA, s.FORMULA, s.ANSWER, s.CAPTION, s.WORKBOOK, s.EVIDENCE, '
           's.RUN_ID, s.VERSION)')
    params = [kind, sig(formula), str(formula)[:4000], json.dumps(answer, default=str)[:20000],
              S.safe_label(caption or '')[:200], S.safe_label(workbook or '')[:200],
              json.dumps(evidence or {}, default=str)[:60000], str(run_id)[:64], VERSION]
    try:
        session.sql(sql, params=params).collect()
        return 'OK'
    except Exception as exc:                       # codes only
        return 'MEMORY_WRITE_FAILED:' + type(exc).__name__


def _words(text):
    return set(re.findall(r'[A-Za-z_]+|[{}\[\]:<>=/*+-]', str(text or '').upper()))


class Memory:
    def __init__(self, entries=None):
        self.entries = list(entries or [])

    @classmethod
    def from_table(cls, session, limit=500):
        """(Memory, status). Rows with a bad answer shape or instruction-like text are skipped."""
        import cx2_safety as S
        if session is None:
            return cls([]), 'NO_SNOWFLAKE_SESSION'
        try:
            rows = session.sql(f'SELECT KIND, FORMULA, ANSWER, CAPTION, WORKBOOK FROM {TABLE} '
                               'ORDER BY SAVED_AT DESC LIMIT ?', params=[int(limit)]).collect()
        except Exception as exc:
            return cls([]), 'NO_MEMORY_TABLE:' + type(exc).__name__
        out = []
        for row in rows:
            kind, formula, answer, caption, workbook = (list(row) + [None] * 5)[:5]
            try:
                answer = json.loads(answer) if isinstance(answer, str) else answer
            except ValueError:
                continue
            if kind not in KINDS or not _answer_ok(kind, answer):
                continue
            if any(S.is_injection(t) for t in (formula, caption, workbook, json.dumps(answer, default=str))):
                continue
            out.append({'kind': kind, 'formula': str(formula), 'answer': answer, 'caption': caption,
                        'workbook': workbook})
        return cls(out), 'OK'

    def examples(self, formula, k=3):
        """The k closest proven CF05 translations (word overlap), as prompt examples."""
        pool = [e for e in self.entries if e['kind'] == 'cf05']
        if not pool:
            return []
        q = _words(formula)
        scored = sorted(((len(q & _words(e['formula'])) / float(len(q | _words(e['formula'])) or 1), i)
                         for i, e in enumerate(pool)), key=lambda x: (-x[0], x[1]))
        return [{'tableau_formula': pool[i]['formula'][:600],
                 'snowflake_expression': str(pool[i]['answer'].get('sql_expression'))[:600],
                 'level': pool[i]['answer'].get('level')} for s, i in scored[:k] if s > 0]

    def fixes_for(self, formulas):
        """Proven fixes saved for any of these formulas (same text, whitespace ignored)."""
        want = {sig(f) for f in formulas or []}
        out = []
        for e in self.entries:
            if e['kind'] == 'proven_fix' and sig(e['formula']) in want:
                hit = {'hypothesis': e['answer'].get('hypothesis'), 'lod_key': e['answer'].get('lod_key'),
                       'workbook': e.get('workbook')}
                if hit not in out:
                    out.append(hit)
        return out
