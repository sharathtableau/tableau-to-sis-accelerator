"""EN01 run and evidence store: a migration run survives a refresh or a redeploy.

Every run, stage event, sheet result, Cortex call, Cortex proposal (with code's ruling), cause proof,
fix, person's approval and deployment is appended to tables in SCHEMA. A saved copy of the run state
(RUN_SNAPSHOTS) lets the console reopen a run without new Cortex calls. Later phases read the same
tables: the Cortex scorecard (CF01), the proven-fix and translation libraries (EN02), Cortex controls
(EN03), accuracy per release (EN05) and the agents (AG01-AG02).

Rules
- The app only INSERTs and SELECTs. Rows are never updated; a later state is a new row. This is a
  convention, not a grant: the app's owner role also owns the tables (a separate owning role is SF05).
- Saving never blocks a migration. Every write is caught; the console says "Evidence not saved" and
  names the exception class only. Cortex service text is never stored (failed calls keep only the
  exception class, as v3_ai records them).
- Nothing read back changes a verdict. A reopened run shows the verdicts that were recorded.
- The tables are created once by sql/en01_evidence_store.sql, which is generated from TABLES by ddl().
  The app never creates or alters them; without them it works exactly as before.
"""
import base64
import datetime
import gzip
import hashlib
import json
import math
import os
import time
import uuid
from pathlib import Path

SCHEMA = os.environ.get('V3_EVIDENCE_SCHEMA', 'WBR_DB.TABLEAU_ACCELERATOR_V3_EVIDENCE')
PART_CHARS = 1_000_000          # one snapshot row (one bound value) holds at most this much base64 text
MAX_BYTES_FIELD = 2_000_000     # a bytes value above this is not kept in a snapshot (e.g. large images)
ROW_BATCH = 50                  # rows per INSERT statement
RESPONSE_CHARS = 60_000         # a stored Cortex answer is cut to this many characters
NOT_IN_SNAPSHOT = ('package', '_store', '_raw')   # _raw: the workbook bytes (release 21a loads); not kept     # the package is rebuilt from the IR and bindings on reopen

S, N, F, B, T, V = 'STRING', 'NUMBER', 'FLOAT', 'BOOLEAN', 'TIMESTAMP_TZ', 'VARIANT'

TABLES = {
    'RUNS': ('One row per migration run; written with the first saved step of the run', [
        ('RUN_ID', S), ('STARTED_AT', T), ('STARTED_BY', S), ('SNOWFLAKE_USER', S), ('APP_IDENTIFIER', S),
        ('APP_BUILD_SHA256', S), ('ENGINE_VERSION', S), ('WORKBOOK_NAME', S), ('ORIGIN', S),
        ('SOURCE_REF', S), ('WORKBOOK_SHA256', S)]),
    'RUN_EVENTS': ('Run log, one row per event; written at the end of each stage and after each action', [
        ('RUN_ID', S), ('EVENT_NO', N), ('RECORDED_AT', T), ('STAGE', S), ('TITLE', S), ('KIND', S),
        ('DETAIL', S)]),
    'RUN_SNAPSHOTS': ('Saved run state used to reopen a run; part 0 is written last, so a snapshot with '
                      'part 0 is complete', [
        ('RUN_ID', S), ('SNAPSHOT_NO', N), ('PART_NO', N), ('PARTS', N), ('SNAPSHOT_AT', T), ('REASON', S),
        ('SAVED_BY', S), ('STATES', V), ('FAILED_STAGE', S), ('BYTES', N), ('SHA256', S),
        ('CONTENT_GZ_B64', S)]),
    'SHEET_RESULTS': ("Each sheet's comparison with Tableau; a new PASS_NO each time validation results change", [
        ('RUN_ID', S), ('PASS_NO', N), ('RECORDED_AT', T), ('SHEET', S), ('SUPPORT_STATE', S), ('STATUS', S),
        ('MATCHED_VALUES', N), ('MISMATCHED_VALUES', N), ('ROWS_MISSING_IN_APP', N), ('NOTE', S),
        ('FIX_APPLIED', S), ('REFERENCE', S), ('SUMMARY', V), ('DIFFERENCES', V)]),
    'SHEET_RENDERS': ('Whether each sheet draws on Snowflake (every run); a new PASS_NO when the results change', [
        ('RUN_ID', S), ('PASS_NO', N), ('RECORDED_AT', T), ('DASHBOARD', S), ('SHEET', S), ('KIND', S),
        ('ENGINE_STATE', S), ('RENDERS', S), ('ROWS_DRAWN', N), ('OUTPUT', S), ('NOTE', S), ('MESSAGES', S)]),
    'CORTEX_CALLS': ('One row per Cortex call: model, status, tokens and cost; failures keep the exception '
                     'class only', [
        ('RUN_ID', S), ('CALL_NO', N), ('CALLED_AT', T), ('STAGE', S), ('PURPOSE', S), ('FUNCTION', S),
        ('MODEL', S), ('STATUS', S), ('ELAPSED_MS', N), ('INPUT_TOKENS', N), ('OUTPUT_TOKENS', N),
        ('TOKEN_SOURCE', S), ('AI_CREDITS', F), ('COST_USD', F), ('RATE_BASIS', S), ('NOTE', S),
        ('PROMPT_SHA256', S), ('RESPONSE', V)]),
    'CORTEX_PROPOSALS': ("What Cortex proposed and code's ruling; a new row when the ruling or its use "
                         'changes', [
        ('RUN_ID', S), ('PROPOSAL_NO', N), ('RECORDED_AT', T), ('STAGE', S), ('KIND', S), ('SUBJECT', S),
        ('PROPOSAL', V), ('CODE_CHECK', S), ('CODE_VERDICT', S), ('PASSED_CODE_CHECK', B),
        ('CHANGES_APP', B), ('EVIDENCE', V)]),
    'PROOFS': ('Cause proof: one row per hypothesis tested by code with a read-only query', [
        ('RUN_ID', S), ('PROOF_NO', N), ('PROOF_KEY', S), ('RECORDED_AT', T), ('SHEET', S),
        ('HYPOTHESIS_ID', S), ('HYPOTHESIS', S), ('CORTEX_RANK', S), ('CORTEX_REASON', S), ('TEST', S),
        ('RESULT', S), ('EVIDENCE', S), ('FIX_KEY', S), ('TESTED_SQL', S), ('ENGINE_SQL', S),
        ('CONCLUSION', S)]),
    'APPROVALS': ("A person's decision (apply or remove a fix, bind a table, switch the data model, deploy), "
                  'written when they click', [
        ('RUN_ID', S), ('APPROVAL_ID', S), ('APPROVED_AT', T), ('ACTION', S), ('SUBJECT', S),
        ('APPROVED_BY', S), ('SNOWFLAKE_USER', S), ('IDENTITY_SOURCE', S), ('BUILD_SHA256', S),
        ('DETAIL', V)]),
    'FIXES': ('A proven fix applied to or removed from a sheet, linked to its approval and proof', [
        ('RUN_ID', S), ('FIX_ID', S), ('RECORDED_AT', T), ('APPROVAL_ID', S), ('SHEET', S), ('ACTION', S),
        ('QUERY_FIX', S), ('FIX_KEY', S), ('PROOF_KEY', S), ('PROPOSED_BY', S), ('BUILD_SHA256_AFTER', S),
        ('DETAIL', V)]),
    'DEPLOYMENTS': ('One row per deploy attempt, linked to its approval', [
        ('RUN_ID', S), ('DEPLOY_ID', S), ('DEPLOYED_AT', T), ('APPROVAL_ID', S), ('APP_IDENTIFIER', S),
        ('BUILD_SHA256', S), ('FILES', N), ('LOCATION', S), ('URL', S), ('RESULT', S), ('ERROR_CLASS', S)]),
}

# Release 20a (SL02): shared Semantic View registry. Kept apart from the EN01 tables so a store set up
# before release 20 keeps working; these objects are added by re-running the same set-up script.
REGISTRY_TABLES = {
    'SEMANTIC_VIEW_EVENTS': ('Shared Semantic View decisions: reused, refreshed or created, by which run, '
                             'workbook and datasource, with any view that could not be used and why', [
        ('RUN_ID', S), ('RECORDED_AT', T), ('RECORDED_BY', S), ('VIEW_NAME', S), ('BASE_NAME', S), ('KIND', S),
        ('ACTION', S), ('VERIFIED', B), ('OWNER_ROLE', S), ('DATASOURCE', S), ('WORKBOOK_NAME', S),
        ('TABLES', V), ('BLOCKED', V)]),
    # Release 21a: every load of a workbook's own data into Snowflake (hosted app or laptop loader).
    'DATA_LOADS': ('Workbook data loaded into Snowflake: datasource, source files and fingerprint, target table, '
                   'rows, who approved, and whether it came from the hosted app or the laptop loader', [
        ('RUN_ID', S), ('RECORDED_AT', T), ('RECORDED_BY', S), ('VIA', S), ('APPROVAL_ID', S), ('DATASOURCE', S),
        ('WORKBOOK_NAME', S), ('TABLE_NAME', S), ('SOURCE_FILES', V), ('SOURCE_SHA256', S), ('ROWS_LOADED', N),
        ('COLUMNS', N), ('RESULT', S)]),
    # Release 23b: runs a person removed from the Saved runs list. The run's evidence is kept (audit trail).
    'HIDDEN_RUNS': ('Runs removed from the Saved runs list by a person; the run and its evidence are kept', [
        ('RUN_ID', S), ('HIDDEN_AT', T), ('HIDDEN_BY', S), ('REASON', S)]),
}

REGISTRY_VIEWS = {
    'V_SEMANTIC_VIEW_REGISTRY': ('One row per shared Semantic View: tables, last decision, owner and how many '
                                 'workbooks and datasources use it', """
SELECT VIEW_NAME, BASE_NAME, KIND, ANY_VALUE(TABLES) AS TABLES,
       MAX_BY(ACTION, RECORDED_AT) AS LAST_ACTION, MAX_BY(OWNER_ROLE, RECORDED_AT) AS OWNER_ROLE,
       MIN(RECORDED_AT) AS FIRST_SEEN, MAX(RECORDED_AT) AS LAST_USED,
       COUNT(DISTINCT WORKBOOK_NAME) AS WORKBOOKS, COUNT(DISTINCT DATASOURCE) AS DATASOURCES,
       COUNT_IF(ACTION = 'reused') AS REUSES, ARRAY_AGG(DISTINCT WORKBOOK_NAME) AS WORKBOOK_NAMES
FROM {S}.SEMANTIC_VIEW_EVENTS WHERE VERIFIED
GROUP BY VIEW_NAME, BASE_NAME, KIND"""),
}

VIEWS = {
    'V_RUN_LATEST': ('Each run with its latest saved state', """
SELECT r.*, s.SNAPSHOT_NO, s.SNAPSHOT_AT AS LAST_SAVED_AT, s.REASON AS LAST_SAVED_FOR, s.STATES, s.FAILED_STAGE
FROM {S}.RUNS r
LEFT JOIN (SELECT RUN_ID, SNAPSHOT_NO, SNAPSHOT_AT, REASON, STATES, FAILED_STAGE FROM {S}.RUN_SNAPSHOTS
           WHERE PART_NO = 0
           QUALIFY ROW_NUMBER() OVER (PARTITION BY RUN_ID ORDER BY SNAPSHOT_NO DESC) = 1) s
  ON s.RUN_ID = r.RUN_ID"""),
    'V_CORTEX_COST_BY_RUN': ('Cortex calls, tokens and cost by run, stage and model', """
SELECT RUN_ID, STAGE, MODEL, COUNT(*) AS CALLS,
       COUNT_IF(STATUS ILIKE 'Completed%') AS COMPLETED, COUNT_IF(STATUS ILIKE 'Failed%') AS FAILED,
       SUM(INPUT_TOKENS) AS INPUT_TOKENS, SUM(OUTPUT_TOKENS) AS OUTPUT_TOKENS,
       SUM(AI_CREDITS) AS AI_CREDITS, SUM(COST_USD) AS COST_USD,
       COUNT_IF(TOKEN_SOURCE ILIKE 'Estimated%') AS ESTIMATED_CALLS
FROM {S}.CORTEX_CALLS GROUP BY RUN_ID, STAGE, MODEL"""),
    'V_CORTEX_SCORECARD': ('Per run and stage: Cortex proposals, how many passed code\'s check, how many changed '
                           'the app (latest ruling per proposal)', """
WITH latest AS (SELECT * FROM {S}.CORTEX_PROPOSALS
                QUALIFY ROW_NUMBER() OVER (PARTITION BY RUN_ID, KIND, SUBJECT ORDER BY PROPOSAL_NO DESC) = 1)
SELECT RUN_ID, STAGE, KIND, COUNT(*) AS PROPOSED,
       COUNT_IF(PASSED_CODE_CHECK) AS PASSED_CODE_CHECK,
       COUNT_IF(PASSED_CODE_CHECK = FALSE) AS FAILED_CODE_CHECK,
       COUNT_IF(PASSED_CODE_CHECK IS NULL) AS ADVISORY_OR_NOT_CHECKED,
       COUNT_IF(CHANGES_APP) AS CHANGED_APP
FROM latest GROUP BY RUN_ID, STAGE, KIND"""),
    'V_SHEET_LATEST': ('Latest draw result and latest Tableau comparison for every sheet of every run', """
WITH d AS (SELECT * FROM {S}.SHEET_RENDERS
           QUALIFY ROW_NUMBER() OVER (PARTITION BY RUN_ID, DASHBOARD, SHEET ORDER BY PASS_NO DESC) = 1),
     c AS (SELECT * FROM {S}.SHEET_RESULTS
           QUALIFY ROW_NUMBER() OVER (PARTITION BY RUN_ID, SHEET ORDER BY PASS_NO DESC) = 1)
SELECT r.RUN_ID, r.WORKBOOK_NAME, r.STARTED_AT, d.DASHBOARD, d.SHEET, d.KIND, d.ENGINE_STATE, d.RENDERS,
       d.ROWS_DRAWN, d.NOTE, c.STATUS AS TABLEAU_COMPARISON, c.MATCHED_VALUES, c.MISMATCHED_VALUES
FROM {S}.RUNS r
JOIN d ON d.RUN_ID = r.RUN_ID
LEFT JOIN c ON c.RUN_ID = d.RUN_ID AND c.SHEET = d.SHEET"""),
    'V_FAILURES': ('Every failed stage, every sheet that does not draw, and every sheet that differs from Tableau, '
                   'across all runs (latest result per sheet)', """
SELECT r.RUN_ID, r.WORKBOOK_NAME, r.STARTED_AT, 'Stage failed' AS FAILURE, e.STAGE AS WHERE_FOUND,
       e.TITLE AS ITEM, e.DETAIL
FROM {S}.RUN_EVENTS e JOIN {S}.RUNS r ON r.RUN_ID = e.RUN_ID
WHERE e.KIND = 'failed'
UNION ALL
SELECT RUN_ID, WORKBOOK_NAME, STARTED_AT, 'Sheet does not draw', DASHBOARD, SHEET, NOTE
FROM {S}.V_SHEET_LATEST WHERE RENDERS = 'Error'
UNION ALL
SELECT RUN_ID, WORKBOOK_NAME, STARTED_AT, 'Differs from Tableau', DASHBOARD, SHEET,
       MISMATCHED_VALUES || ' value(s) differ'
FROM {S}.V_SHEET_LATEST WHERE TABLEAU_COMPARISON = 'Difference found'"""),
    'V_APPROVAL_TRAIL': ('Who approved what and when, with the fix or deployment it led to', """
SELECT a.RUN_ID, r.WORKBOOK_NAME, a.APPROVED_AT, a.APPROVED_BY, a.SNOWFLAKE_USER, a.IDENTITY_SOURCE,
       a.ACTION, a.SUBJECT, a.BUILD_SHA256, f.FIX_ID, f.QUERY_FIX, f.FIX_KEY, f.PROPOSED_BY, f.PROOF_KEY,
       d.DEPLOY_ID, d.APP_IDENTIFIER, d.RESULT AS DEPLOY_RESULT
FROM {S}.APPROVALS a
JOIN {S}.RUNS r ON r.RUN_ID = a.RUN_ID
LEFT JOIN {S}.FIXES f ON f.APPROVAL_ID = a.APPROVAL_ID
LEFT JOIN {S}.DEPLOYMENTS d ON d.APPROVAL_ID = a.APPROVAL_ID"""),
}


def ddl(schema=SCHEMA):
    """The one-time setup script (run by a person in Snowsight). Safe to re-run."""
    out = ['-- EN01 run and evidence store (generated by v3_store.ddl(); do not edit by hand).\n'
           '-- Run once in Snowsight as the role that owns the Cortex lab app (WBR_OWNER).\n'
           '-- The app only INSERTs and SELECTs; it never creates or alters these objects.\n'
           '-- In Snowsight use Run All (Ctrl+Shift+Enter); every statement is safe to re-run.',
           'USE ROLE WBR_OWNER;',
           f"CREATE SCHEMA IF NOT EXISTS {schema} COMMENT = 'Tableau to SiS accelerator: run and evidence store (EN01)';"]
    for name, (comment, cols) in TABLES.items():
        body = ',\n'.join(f'  {c} {t}' for c, t in cols)
        out.append(f"CREATE TABLE IF NOT EXISTS {schema}.{name} (\n{body}\n) COMMENT = '{comment.replace(chr(39), chr(39) * 2)}';")
    out.append('-- Release 20a/21a: shared Semantic View registry (SL02) and workbook data loads.')
    for name, (comment, cols) in REGISTRY_TABLES.items():
        body = ',\n'.join(f'  {c} {t}' for c, t in cols)
        out.append(f"CREATE TABLE IF NOT EXISTS {schema}.{name} (\n{body}\n) COMMENT = '{comment.replace(chr(39), chr(39) * 2)}';")
    for name, (comment, sql) in list(VIEWS.items()) + list(REGISTRY_VIEWS.items()):
        out.append(f"CREATE OR REPLACE VIEW {schema}.{name} COMMENT = '{comment.replace(chr(39), chr(39) * 2)}' AS"
                   + sql.format(S=schema) + ';')
    import v3_catalog                       # release 20b: estate catalogue tables and views
    out.extend(v3_catalog.ddl(schema))
    import v3_rationalise                   # release 22a: rationalisation report
    out.extend(v3_rationalise.ddl(schema))
    out.append(f'SHOW TABLES IN SCHEMA {schema};')
    return '\n\n'.join(out) + '\n'


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _sha(obj):
    return hashlib.sha256(json.dumps(jsonable(obj), sort_keys=True, default=str).encode()).hexdigest()


def jsonable(o, depth=0):
    """A value safe for a VARIANT column: NaN/inf -> null, bytes -> size note, unknown -> text."""
    if depth > 40:
        return None
    if o is None or isinstance(o, (bool, str)):
        return o
    if isinstance(o, int):
        return o
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {str(k): jsonable(v, depth + 1) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [jsonable(v, depth + 1) for v in o]
    if isinstance(o, (bytes, bytearray)):
        return {'bytes': len(o)}
    if isinstance(o, (datetime.datetime, datetime.date)):
        return o.isoformat()
    if hasattr(o, 'item') and callable(o.item):          # numpy scalar
        try:
            return jsonable(o.item(), depth + 1)
        except Exception:
            pass
    return str(o)[:2000]


def _num(v):
    try:
        return None if v is None or v == '' else float(v)
    except (TypeError, ValueError):
        return None


def _int(v):
    f = _num(v)
    return None if f is None or not math.isfinite(f) else int(f)


def _txt(v, n=4000):
    return None if v is None else str(v)[:n]


def build_info():
    """(app identifier, build sha256) of the running package, from its own candidate_build.json."""
    try:
        info = json.loads(Path(__file__).with_name('candidate_build.json').read_text())
        t = info.get('target') or {}
        return '.'.join(str(t.get(k)) for k in ('database', 'schema', 'name')), info.get('build_sha256')
    except Exception:
        return None, None


def viewer():
    """(name, source) of the person using the app, as Streamlit in Snowflake reports it."""
    try:
        import streamlit as st
        user = st.user
        for key in ('email', 'user_name', 'login_name', 'name'):
            try:
                value = user.get(key) if hasattr(user, 'get') else getattr(user, key, None)
            except Exception:
                value = None
            if value:
                return str(value), 'st.user.' + key
    except Exception:
        pass
    return None, None


# --------------------------------------------------------------------------- #
# Snapshot encoding (tuples, tuple keys, bytes and dates survive the round trip)
# --------------------------------------------------------------------------- #
def _enc(o, depth=0):
    if depth > 60:
        return {'__dropped__': 'depth'}
    if o is None or isinstance(o, (bool, str, int)):
        return o
    if isinstance(o, float):
        return o if math.isfinite(o) else {'__f__': repr(o)}
    if isinstance(o, dict):
        if all(isinstance(k, str) and not k.startswith('__') for k in o):
            return {k: _enc(v, depth + 1) for k, v in o.items()}
        return {'__map__': [[_enc(k, depth + 1), _enc(v, depth + 1)] for k, v in o.items()]}
    if isinstance(o, tuple):
        return {'__t__': [_enc(v, depth + 1) for v in o]}
    if isinstance(o, list):
        return [_enc(v, depth + 1) for v in o]
    if isinstance(o, (set, frozenset)):
        return {'__set__': [_enc(v, depth + 1) for v in o]}
    if isinstance(o, (bytes, bytearray)):
        if len(o) > MAX_BYTES_FIELD:
            return {'__dropped__': 'bytes', 'size': len(o)}
        return {'__b64__': base64.b64encode(bytes(o)).decode()}
    if isinstance(o, datetime.datetime):
        return {'__dt__': o.isoformat()}
    if isinstance(o, datetime.date):
        return {'__d__': o.isoformat()}
    if hasattr(o, 'item') and callable(o.item):
        try:
            return _enc(o.item(), depth + 1)
        except Exception:
            pass
    return {'__dropped__': type(o).__name__}


def _dec(o):
    if isinstance(o, list):
        return [_dec(v) for v in o]
    if not isinstance(o, dict):
        return o
    if len(o) == 1 or (len(o) == 2 and '__dropped__' in o):
        (k, v), = list(o.items())[:1]
        if k == '__map__':
            return {_hashable(_dec(a)): _dec(b) for a, b in v}
        if k == '__t__':
            return tuple(_dec(x) for x in v)
        if k == '__set__':
            return set(_hashable(_dec(x)) for x in v)
        if k == '__b64__':
            return base64.b64decode(v)
        if k == '__dt__':
            return datetime.datetime.fromisoformat(v)
        if k == '__d__':
            return datetime.date.fromisoformat(v)
        if k == '__f__':
            return float(v)
        if k == '__dropped__':
            return None
    return {k: _dec(v) for k, v in o.items()}


def _hashable(v):
    return tuple(_hashable(x) for x in v) if isinstance(v, list) else v


def dumps(run):
    """gzip+base64 text of the run state (without the package, which is rebuilt on reopen)."""
    state = {k: v for k, v in run.items() if k not in NOT_IN_SNAPSHOT}
    raw = json.dumps(_enc(state), separators=(',', ':')).encode()
    return base64.b64encode(gzip.compress(raw, 6)).decode(), len(raw)


def loads(text):
    return _dec(json.loads(gzip.decompress(base64.b64decode(text))))


# --------------------------------------------------------------------------- #
# Session, readiness and writes
# --------------------------------------------------------------------------- #
_READY = {}          # id(session) -> (ready, checked_at)
_SF_USER = {}        # id(session) -> CURRENT_USER()


def session():
    try:
        import backend
        return backend._active_session()
    except Exception:
        return None


def ready(sess):
    """True when every EN01 table exists. A missing store is re-checked at most once a minute."""
    if sess is None:
        return False
    hit = _READY.get(id(sess))
    if hit and (hit[0] or time.time() - hit[1] < 60):
        return hit[0]
    try:
        db, sch = SCHEMA.split('.')
        rows = sess.sql(f'SELECT COUNT(*) FROM {db}.INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = ? '
                        'AND TABLE_NAME IN (' + ','.join('?' * len(TABLES)) + ')',
                        params=[sch] + list(TABLES)).collect()
        ok = int(rows[0][0]) == len(TABLES)
    except Exception:
        ok = False
    _READY[id(sess)] = (ok, time.time())
    return ok


_REG_READY = {}      # id(session) -> (ready, checked_at)


def registry_ready(sess, table='SEMANTIC_VIEW_EVENTS'):
    """True when one registry table exists (re-checked at most once a minute). Each table is checked on its
    own, so a store set up for release 20a keeps logging Semantic View decisions before DATA_LOADS exists."""
    if sess is None:
        return False
    hit = _REG_READY.get((id(sess), table))
    if hit and (hit[0] or time.time() - hit[1] < 60):
        return hit[0]
    try:
        db, sch = SCHEMA.split('.')
        rows = sess.sql(f'SELECT COUNT(*) FROM {db}.INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = ? '
                        'AND TABLE_NAME = ?', params=[sch, table]).collect()
        ok = int(rows[0][0]) == 1
    except Exception:
        ok = False
    _REG_READY[(id(sess), table)] = (ok, time.time())
    return ok


def log_load(run, row, sess=None):
    """Record one data load (DATA_LOADS); kept on the run, written when the table exists."""
    st = _state(run) if run is not None else None
    who, _src = viewer()
    row = dict(row, RUN_ID=(st or {}).get('run_id'), RECORDED_AT=now(), RECORDED_BY=row.get('RECORDED_BY') or who)
    if run is not None:
        run.setdefault('data_loads', []).append(jsonable(row))
    sess = sess or session()
    if not registry_ready(sess, 'DATA_LOADS'):
        return False
    try:
        _write(sess, 'DATA_LOADS', [row])
        return True
    except Exception as exc:
        if st:
            _failed(st, 'DATA_LOADS', exc)
        return False


def log_semantic(run, rows):
    """Record shared Semantic View decisions; always kept on the run, written to Snowflake when the table exists."""
    if not rows:
        return False
    st = _state(run)
    who, _src = viewer()
    at = now()
    rows = [dict(r, RUN_ID=(st or {}).get('run_id'), RECORDED_AT=at, RECORDED_BY=who) for r in rows]
    run.setdefault('semantic_registry', []).extend(jsonable(rows))
    del run['semantic_registry'][:-50]
    sess = session()
    if not registry_ready(sess):
        return False
    try:
        _write(sess, 'SEMANTIC_VIEW_EVENTS', rows)
        return True
    except Exception as exc:
        if st:
            _failed(st, 'SEMANTIC_VIEW_EVENTS', exc)
        return False


def snowflake_user(sess):
    if sess is None:
        return None
    if id(sess) not in _SF_USER:
        try:
            _SF_USER[id(sess)] = str(sess.sql('SELECT CURRENT_USER()').collect()[0][0])
        except Exception:
            _SF_USER[id(sess)] = None
    return _SF_USER[id(sess)]


def _cast(col, typ):
    ref = f'v:"{col}"'
    if typ == V:
        return f'{ref} AS {col}'
    if typ == T:
        return f'TRY_TO_TIMESTAMP_TZ({ref}::STRING) AS {col}'
    return f'{ref}::{typ} AS {col}'


def _cols(table):
    return (TABLES.get(table) or REGISTRY_TABLES[table])[1]


def insert_sql(table, count):
    cols = _cols(table)
    return (f'INSERT INTO {SCHEMA}.{table} ({", ".join(c for c, _t in cols)}) SELECT '
            + ', '.join(_cast(c, t) for c, t in cols)
            + ' FROM (SELECT PARSE_JSON(column1) AS v FROM VALUES ' + ', '.join(['(?)'] * count) + ')')


def _write(sess, table, rows):
    cols = [c for c, _t in _cols(table)]
    for i in range(0, len(rows), ROW_BATCH):
        chunk = rows[i:i + ROW_BATCH]
        params = [json.dumps({c: jsonable(r.get(c)) for c in cols}, allow_nan=False) for r in chunk]
        sess.sql(insert_sql(table, len(chunk)), params=params).collect()


def _state(run):
    return run.get('_store') if isinstance(run, dict) else None


def start(run, source):
    """Give a new run an id and the facts for its RUNS row. Nothing is written yet."""
    sess = session()
    if not ready(sess):
        return None
    app, build = build_info()
    who, _src = viewer()
    origin = source.get('origin')
    ref = ('/'.join(str(source.get(k) or '') for k in ('server_url', 'site_content_url', 'workbook_id'))
           if origin == 'tableau-cloud' else source.get('name'))
    try:
        from ir_contract import VERSION
    except Exception:
        VERSION = None
    run['_store'] = {
        'run_id': str(uuid.uuid4()), 'started': False, 'errors': [],
        'meta': {'STARTED_AT': now(), 'STARTED_BY': who, 'SNOWFLAKE_USER': snowflake_user(sess),
                 'APP_IDENTIFIER': app, 'APP_BUILD_SHA256': build, 'ENGINE_VERSION': VERSION,
                 'WORKBOOK_NAME': source.get('name'), 'ORIGIN': origin, 'SOURCE_REF': ref,
                 'WORKBOOK_SHA256': None},
        'events': 0, 'event_no': 0, 'calls': 0, 'call_no': 0, 'proposals': [], 'proofs': [],
        'sheets_fp': None, 'pass_no': 0,
        'proposal_no': 0, 'proof_no': 0, 'snapshot_no': 0, 'snapshot_fp': None}
    return run['_store']['run_id']


def note_workbook(run, raw):
    st = _state(run)
    if st and raw is not None:
        st['meta']['WORKBOOK_SHA256'] = hashlib.sha256(raw).hexdigest()


def _failed(st, what, exc):
    st['errors'].append({'At': now(), 'Step': what, 'Error': type(exc).__name__})
    del st['errors'][:-20]


# --------------------------------------------------------------------------- #
# Rows derived from the run
# --------------------------------------------------------------------------- #
def call_rows(run):
    import v3_ai
    import v3_usage
    out = []
    ir = (run.get('result') or {}).get('ir') or {}
    rates, credit = v3_usage.rates(), v3_usage.credit_usd()
    entries = [None] * len(ir.get('ai_request_trace') or []) + list(v3_ai.log(run))
    for row, entry in zip(v3_ai.activity(run), entries):
        model = row.get('Model')
        fn = {'AI_CLASSIFY': 'AI_CLASSIFY', 'AI_AGG': 'AI_AGG'}.get(str(model), 'AI_COMPLETE')
        if str(row.get('Purpose', '')).startswith('Cortex Analyst') or row.get('Messages'):
            fn = 'CORTEX_ANALYST'
        rate = rates.get(str(model or '').lower())
        entry = entry or {}
        out.append({'CALLED_AT': entry.get('_at'), 'STAGE': row.get('Step'), 'PURPOSE': _txt(row.get('Purpose'), 500),
                    'FUNCTION': fn, 'MODEL': _txt(model, 200), 'STATUS': _txt(row.get('Status'), 200),
                    'ELAPSED_MS': _int(row.get('Elapsed ms')), 'INPUT_TOKENS': _int(row.get('Input tokens')),
                    'OUTPUT_TOKENS': _int(row.get('Output tokens')), 'TOKEN_SOURCE': row.get('Token source'),
                    'AI_CREDITS': _num(row.get('AI credits')), 'COST_USD': _num(row.get('Cost USD')),
                    'RATE_BASIS': (f'{rate[0]}/{rate[1]} AI credits per million tokens in/out; '
                                   f'{credit} USD per AI credit' if rate else None),
                    'NOTE': _txt(row.get('Note'), 300), 'PROMPT_SHA256': entry.get('_prompt_sha256'),
                    'RESPONSE': entry.get('_response')})
    return out


def _sheet_fix(run, name):
    for d in ((run.get('result') or {}).get('ir') or {}).get('dashboards', []):
        for s in d.get('sheets', []):
            if s.get('name') == name and s.get('query_fix'):
                return s.get('query_fix'), s.get('query_fix_key')
    return None, None


def sheet_rows(run):
    sref = run.get('sheet_reference') or {}
    results = sref.get('results') or {}
    states = {}
    for r in ((run.get('result') or {}).get('assessment') or {}).get('sheets', []):
        states.setdefault(r.get('sheet'), r.get('state'))
    out = []
    for name, pair in results.items():
        summary, detail = (list(pair) + [None, None])[:2]
        summary = summary or {}
        fix, _key = _sheet_fix(run, name)
        out.append({'SHEET': name, 'SUPPORT_STATE': states.get(name), 'STATUS': summary.get('Status'),
                    'MATCHED_VALUES': _int(summary.get('Matched values')),
                    'MISMATCHED_VALUES': _int(summary.get('Mismatched values')),
                    'ROWS_MISSING_IN_APP': _int(summary.get('Rows missing in app')),
                    'NOTE': _txt(summary.get('Note'), 1000), 'FIX_APPLIED': fix, 'REFERENCE': _txt(sref.get('meta'), 500),
                    'SUMMARY': summary,
                    'DIFFERENCES': [d for d in (detail or []) if isinstance(d, dict) and not d.get('Match')][:50]})
    return out


def _proven(proof):
    return next((r for r in proof.get('results') or [] if r.get('Result') == 'Proven'), None)


def proposal_rows(run):
    """Every Cortex proposal in the run, with what code checked and its ruling."""
    out = []
    ir = (run.get('result') or {}).get('ir') or {}
    review = ir.get('ai_migration_review')
    if review:
        out.append({'STAGE': 'Parsing', 'KIND': 'workbook_review', 'SUBJECT': 'workbook', 'PROPOSAL': review,
                    'CODE_CHECK': 'None (advisory; shown to the reviewer)', 'CODE_VERDICT': 'Advisory',
                    'PASSED_CODE_CHECK': None, 'CHANGES_APP': False, 'EVIDENCE': None})
    for cap, rec in (run.get('cortex_models') or {}).items():
        bound = ((run.get('bindings') or {}).get(cap) or {})
        in_use = isinstance(bound, dict) and bound.get('semantic_view') == rec.get('name')
        model = rec.get('model') or {}
        out.append({'STAGE': 'Data model', 'KIND': 'semantic_model', 'SUBJECT': cap,
                    'PROPOSAL': {'name': rec.get('name'), 'relationships': model.get('relationships'),
                                 'metrics': len(model.get('metrics') or []), 'notes': rec.get('proposal_notes')},
                    'CODE_CHECK': 'Validated against the tables, created as a separate view, rows and totals compared '
                                  'with the Tableau relationship model',
                    'CODE_VERDICT': 'Verified equal' if rec.get('verified') else 'Not verified',
                    'PASSED_CODE_CHECK': bool(rec.get('verified')), 'CHANGES_APP': bool(in_use),
                    'EVIDENCE': {'relationships_vs_tableau': rec.get('relationships_vs_tableau'),
                                 'checks': rec.get('checks'), 'rejected': rec.get('rejected')}})
    for r in ((run.get('calc_xl') or {}).get('rows') or []):
        res = r.get('Result')
        out.append({'STAGE': 'Parsing', 'KIND': 'calc_translation', 'SUBJECT': r.get('Calculation'),
                    'PROPOSAL': {'sql_expression': r.get('Cortex translation')},
                    'CODE_CHECK': 'Allowlist, then values compared with the engine (read-only query)',
                    'CODE_VERDICT': res,
                    'PASSED_CODE_CHECK': True if res == 'Agree' else False if res in (
                        'Disagree', 'Rejected by allowlist') else None,
                    'CHANGES_APP': False,
                    'EVIDENCE': {'evidence': r.get('Evidence'), 'engine_translation': r.get('Engine translation'),
                                 'tableau_formula': r.get('Tableau formula')}})
    for sheet, proof in (run.get('proofs') or {}).items():
        chosen = proof.get('chosen') or {}
        good = _proven(proof)
        rank = good.get('Cortex rank') if good else None
        out.append({'STAGE': 'Validation', 'KIND': 'cause_ranking', 'SUBJECT': sheet,
                    'PROPOSAL': {'ranked': chosen.get('ranked'), 'summary': chosen.get('summary')},
                    'CODE_CHECK': "Every testable cause run as a read-only query and compared with Tableau's values",
                    'CODE_VERDICT': (f'Ranked cause proven (#{rank})' if good and rank != 'Not ranked'
                                     else 'Proven cause not ranked by Cortex' if good else 'No cause proven'),
                    'PASSED_CODE_CHECK': bool(good and rank != 'Not ranked') if good else None,
                    'CHANGES_APP': False, 'EVIDENCE': {'conclusion': proof.get('conclusion')}})
        fix = chosen.get('proposed_fix')
        if fix:
            tested = next((x for x in proof.get('results') or []
                           if x.get('id') == fix.get('hypothesis') and x.get('fix_key')), None)
            refused = any('refused' in str(x.get('Cortex reason') or '') for x in proof.get('results') or []
                          if x.get('id') == fix.get('hypothesis'))
            q_fix, q_key = _sheet_fix(run, sheet)
            verdict = ('Refused: key is not a FIXED key of this sheet' if refused
                       else tested.get('Result') if tested else 'Not tested')
            out.append({'STAGE': 'Validation', 'KIND': 'proposed_fix', 'SUBJECT': sheet, 'PROPOSAL': fix,
                        'CODE_CHECK': "Exactly this fix run as a read-only rewrite and compared with Tableau's values",
                        'CODE_VERDICT': verdict,
                        'PASSED_CODE_CHECK': True if verdict == 'Proven' else False if (
                            refused or verdict in ('Not reproduced', 'Partly')) else None,
                        'CHANGES_APP': bool(q_fix and q_key and str(q_key) == str(fix.get('lod_key'))),
                        'EVIDENCE': {'evidence': tested.get('Evidence') if tested else None}})
    for cap, rec in (run.get('cortex_mappings') or {}).items():      # release 21b-B
        if rec.get('proposal') is not None:
            import v3_cortex_mapping
            out.append(v3_cortex_mapping.proposal_row(cap, rec, (run.get('bindings') or {}).get(cap)))
    for dash, v in (run.get('visual') or {}).items():
        if isinstance(v, dict) and v.get('verdict'):
            out.append({'STAGE': 'Validation', 'KIND': 'vision', 'SUBJECT': dash,
                        'PROPOSAL': {'verdict': v.get('verdict'), 'explanation': v.get('explanation')},
                        'CODE_CHECK': 'None (advisory; never changes a value verdict)', 'CODE_VERDICT': 'Advisory',
                        'PASSED_CODE_CHECK': None, 'CHANGES_APP': False, 'EVIDENCE': None})
    return out


def proof_rows(sheet, proof):
    rows = []
    for r in proof.get('results') or []:
        rows.append({'SHEET': sheet, 'HYPOTHESIS_ID': r.get('id'), 'HYPOTHESIS': _txt(r.get('Hypothesis'), 500),
                     'CORTEX_RANK': _txt(r.get('Cortex rank'), 40), 'CORTEX_REASON': _txt(r.get('Cortex reason'), 1000),
                     'TEST': _txt(r.get('Test'), 500), 'RESULT': r.get('Result'), 'EVIDENCE': _txt(r.get('Evidence'), 2000),
                     'FIX_KEY': r.get('fix_key'), 'TESTED_SQL': _txt(r.get('sql'), 20000),
                     'ENGINE_SQL': _txt(r.get('engine_sql'), 20000), 'CONCLUSION': _txt(proof.get('conclusion'), 2000)})
    return rows


def proof_key(proof):
    return _sha({'results': (proof or {}).get('results'), 'chosen': (proof or {}).get('chosen')})


def _fingerprint(run):
    sref = (run.get('sheet_reference') or {}).get('results') or {}
    return _sha({'states': run.get('states'), 'failed': run.get('failed'), 'bindings': run.get('bindings'),
                 'missing': run.get('missing'), 'events': len(run.get('events') or []),
                 'sheets': {k: (v[0] if v else None) for k, v in sref.items()},
                 'proofs': sorted((run.get('proofs') or {})), 'fixes': run.get('applied_fixes'),
                 'calc': (run.get('calc_xl') or {}).get('score'), 'deployment': run.get('deployment'),
                 'drawn': [(r.get('Sheet'), r.get('Renders on data')) for r in run.get('sheet_checks') or []],
                 'models': {c: r.get('verified') for c, r in (run.get('cortex_models') or {}).items()},
                 'capture': run.get('capture_digest'), 'visual': sorted(run.get('visual') or {})})


# --------------------------------------------------------------------------- #
# Checkpoint: append what is new since the last save
# --------------------------------------------------------------------------- #
def checkpoint(run, stage=None, snapshot=True, reason=None):
    """Save what is new in this run. Never raises; a failure is listed in run['_store']['errors']."""
    st = _state(run)
    if not st:
        return
    sess = session()
    if sess is None:
        return
    rid, at = st['run_id'], now()
    steps = [('RUNS', _runs_row), ('RUN_EVENTS', _event_rows), ('CORTEX_CALLS', _call_rows),
             ('CORTEX_PROPOSALS', _proposal_rows), ('PROOFS', _proof_rows), ('SHEET_RESULTS', _sheet_rows),
             ('SHEET_RENDERS', _render_rows)]
    for table, fn in steps:
        try:
            rows, commit = fn(run, st, stage, at)
            if rows:
                _write(sess, table, [dict(r, RUN_ID=rid) for r in rows])
            commit()
        except Exception as exc:
            _failed(st, table, exc)
            if table == 'RUNS':
                return
    if snapshot:
        fp = _fingerprint(run)
        if fp != st.get('snapshot_fp'):
            try:
                save_snapshot(sess, run, reason or (f'after {stage}' if stage else 'state changed'))
                st['snapshot_fp'] = fp
            except Exception as exc:
                _failed(st, 'RUN_SNAPSHOTS', exc)


def _runs_row(run, st, stage, at):
    if st['started']:
        return [], lambda: None
    return [st['meta']], lambda: st.update(started=True)


def _event_rows(run, st, stage, at):
    chronological = list(reversed(run.get('events') or []))
    new = chronological[st['events']:]
    rows = [{'EVENT_NO': st['event_no'] + i + 1, 'RECORDED_AT': at, 'STAGE': stage or 'Console',
             'TITLE': _txt(e[0], 500), 'DETAIL': _txt(e[1], 4000), 'KIND': _txt(e[2], 40) if len(e) > 2 else None}
            for i, e in enumerate(new)]
    return rows, lambda: st.update(events=len(chronological), event_no=st['event_no'] + len(rows))


def _call_rows(run, st, stage, at):
    rows = call_rows(run)
    new = rows[st['calls']:]
    for i, r in enumerate(new):
        r.update(CALL_NO=st['call_no'] + i + 1, CALLED_AT=r.get('CALLED_AT') or at)
        if r.get('RESPONSE') is not None:
            text = json.dumps(jsonable(r['RESPONSE']))
            if len(text) > RESPONSE_CHARS:
                r['RESPONSE'] = {'truncated': True, 'text': text[:RESPONSE_CHARS]}
    return new, lambda: st.update(calls=len(rows), call_no=st['call_no'] + len(new))


def _proposal_rows(run, st, stage, at):
    saved, rows, keys = set(st['proposals']), [], []
    for r in proposal_rows(run):
        k = _sha(r)
        if k in saved or k in keys:
            continue
        keys.append(k)
        rows.append(dict(r, RECORDED_AT=at, PROPOSAL_NO=st['proposal_no'] + len(rows) + 1))
    return rows, lambda: st.update(proposals=st['proposals'] + keys, proposal_no=st['proposal_no'] + len(rows))


def _proof_rows(run, st, stage, at):
    saved, rows, keys = set(st['proofs']), [], []
    for sheet, proof in (run.get('proofs') or {}).items():
        k = proof_key(proof)
        if k in saved or k in keys:
            continue
        keys.append(k)
        no = st['proof_no'] + len(keys)
        rows += [dict(r, PROOF_NO=no, PROOF_KEY=k, RECORDED_AT=at) for r in proof_rows(sheet, proof)]
    return rows, lambda: st.update(proofs=st['proofs'] + keys, proof_no=st['proof_no'] + len(keys))


def _sheet_rows(run, st, stage, at):
    rows = sheet_rows(run)
    fp = _sha([(r['SHEET'], r['SUMMARY'], r['FIX_APPLIED']) for r in rows]) if rows else None
    if not rows or fp == st['sheets_fp']:
        return [], lambda: None
    no = st['pass_no'] + 1
    return [dict(r, PASS_NO=no, RECORDED_AT=at) for r in rows], lambda: st.update(sheets_fp=fp, pass_no=no)


def render_rows(run):
    return [{'DASHBOARD': r.get('Dashboard'), 'SHEET': r.get('Sheet'), 'KIND': _txt(r.get('Kind'), 60),
             'ENGINE_STATE': r.get('Engine status'), 'RENDERS': r.get('Renders on data'),
             'ROWS_DRAWN': _int(r.get('Rows')), 'OUTPUT': _txt(r.get('Output'), 200), 'NOTE': _txt(r.get('Note'), 1000),
             'MESSAGES': _txt(r.get('Messages'), 1000)} for r in run.get('sheet_checks') or []]


def _render_rows(run, st, stage, at):
    rows = render_rows(run)
    fp = _sha([(r['DASHBOARD'], r['SHEET'], r['RENDERS'], r['ROWS_DRAWN'], r['NOTE']) for r in rows]) if rows else None
    if not rows or fp == st.get('renders_fp'):
        return [], lambda: None
    no = st.get('render_pass', 0) + 1
    return ([dict(r, PASS_NO=no, RECORDED_AT=at) for r in rows],
            lambda: st.update(renders_fp=fp, render_pass=no))


def save_snapshot(sess, run, reason):
    st = _state(run)
    text, size = dumps(run)
    digest = hashlib.sha256(text.encode()).hexdigest()
    parts = [text[i:i + PART_CHARS] for i in range(0, len(text), PART_CHARS)] or ['']
    no = st['snapshot_no'] + 1
    who, _src = viewer()
    base = {'RUN_ID': st['run_id'], 'SNAPSHOT_NO': no, 'PARTS': len(parts), 'SNAPSHOT_AT': now(),
            'REASON': _txt(reason, 200), 'SAVED_BY': who or snowflake_user(sess), 'STATES': run.get('states'),
            'FAILED_STAGE': run.get('failed'), 'BYTES': size, 'SHA256': digest}
    for i in list(range(1, len(parts))) + [0]:       # part 0 last: its presence means the snapshot is complete
        _write(sess, 'RUN_SNAPSHOTS', [dict(base, PART_NO=i, CONTENT_GZ_B64=parts[i])])
    st['snapshot_no'] = no


# --------------------------------------------------------------------------- #
# Approvals, fixes and deployments (written when the person acts)
# --------------------------------------------------------------------------- #
def approve(run, action, subject, detail=None):
    """Record a person's decision now. Returns the approval id (None when the store is off or the write failed)."""
    st = _state(run)
    sess = session()
    if not st or sess is None:
        return None
    checkpoint(run, snapshot=False)                 # the RUNS row and the evidence the person saw come first
    who, src = viewer()
    sf = snowflake_user(sess)
    aid = str(uuid.uuid4())
    row = {'RUN_ID': st['run_id'], 'APPROVAL_ID': aid, 'APPROVED_AT': now(), 'ACTION': action,
           'SUBJECT': _txt(subject, 500), 'APPROVED_BY': who or sf,
           'SNOWFLAKE_USER': sf, 'IDENTITY_SOURCE': src or ('CURRENT_USER()' if sf else None),
           'BUILD_SHA256': (run.get('package') or {}).get('build'), 'DETAIL': detail}
    try:
        _write(sess, 'APPROVALS', [row])
        return aid
    except Exception as exc:
        _failed(st, 'APPROVALS', exc)
        return None


def fix(run, approval_id, sheet, action, query_fix=None, fix_key=None, proof_ref=None, record=None):
    st = _state(run)
    sess = session()
    if not st or sess is None:
        return
    row = {'RUN_ID': st['run_id'], 'FIX_ID': str(uuid.uuid4()), 'RECORDED_AT': now(), 'APPROVAL_ID': approval_id,
           'SHEET': sheet, 'ACTION': action, 'QUERY_FIX': query_fix, 'FIX_KEY': fix_key, 'PROOF_KEY': proof_ref,
           'PROPOSED_BY': _txt((record or {}).get('Proposed by'), 500),
           'BUILD_SHA256_AFTER': (run.get('package') or {}).get('build'), 'DETAIL': record}
    try:
        _write(sess, 'FIXES', [row])
    except Exception as exc:
        _failed(st, 'FIXES', exc)
    checkpoint(run, reason=f'fix {action}: {sheet}')


def deployed(run, approval_id, record=None, error=None):
    st = _state(run)
    sess = session()
    if not st or sess is None:
        return
    record = record or {}
    row = {'RUN_ID': st['run_id'], 'DEPLOY_ID': str(uuid.uuid4()), 'DEPLOYED_AT': now(), 'APPROVAL_ID': approval_id,
           'APP_IDENTIFIER': record.get('identifier'), 'BUILD_SHA256': record.get('build_sha256'),
           'FILES': _int(record.get('files')), 'LOCATION': _txt(record.get('location'), 1000),
           'URL': _txt(record.get('url'), 1000), 'RESULT': 'Failed' if error else 'Deployed',
           'ERROR_CLASS': type(error).__name__ if error else None}
    try:
        _write(sess, 'DEPLOYMENTS', [row])
    except Exception as exc:
        _failed(st, 'DEPLOYMENTS', exc)
    checkpoint(run, reason='deploy')


# --------------------------------------------------------------------------- #
# Reading back: list runs and reopen one
# --------------------------------------------------------------------------- #
def recent_runs(sess, limit=10):
    hidden = (f' AND RUN_ID NOT IN (SELECT RUN_ID FROM {SCHEMA}.HIDDEN_RUNS)'
              if registry_ready(sess, 'HIDDEN_RUNS') else '')
    rows = sess.sql(
        f'SELECT RUN_ID, WORKBOOK_NAME, ORIGIN, STARTED_AT, STARTED_BY, SNAPSHOT_NO, LAST_SAVED_AT, '
        f'FAILED_STAGE FROM {SCHEMA}.V_RUN_LATEST WHERE SNAPSHOT_NO IS NOT NULL{hidden} '
        f'ORDER BY STARTED_AT DESC LIMIT {int(limit)}').collect()
    keys = ['RUN_ID', 'WORKBOOK_NAME', 'ORIGIN', 'STARTED_AT', 'STARTED_BY', 'SNAPSHOT_NO', 'LAST_SAVED_AT', 'FAILED_STAGE']
    return [dict(zip(keys, list(r))) for r in rows]


def can_hide(sess):
    return registry_ready(sess, 'HIDDEN_RUNS')


def hide_runs(sess, run_ids, reason='removed from the Saved runs list'):
    """Remove runs from the Saved runs list (insert-only; the runs and their evidence stay). Returns the count."""
    ids = [str(r) for r in run_ids if r]
    if not ids or not can_hide(sess):
        return 0
    who, _src = viewer()
    at = now()
    _write(sess, 'HIDDEN_RUNS', [{'RUN_ID': r, 'HIDDEN_AT': at, 'HIDDEN_BY': who, 'REASON': reason} for r in ids])
    return len(ids)


def reopen(sess, run_id):
    """The run as last saved, ready for the console. Raises ValueError when the snapshot is incomplete."""
    rows = sess.sql(
        f'SELECT SNAPSHOT_NO, PART_NO, PARTS, SHA256, CONTENT_GZ_B64 FROM {SCHEMA}.RUN_SNAPSHOTS '
        f'WHERE RUN_ID = ? AND SNAPSHOT_NO = (SELECT MAX(SNAPSHOT_NO) FROM {SCHEMA}.RUN_SNAPSHOTS '
        f'WHERE RUN_ID = ? AND PART_NO = 0) ORDER BY PART_NO', params=[run_id, run_id]).collect()
    if not rows:
        raise ValueError('no complete saved state for this run')
    no, parts, digest = rows[0][0], int(rows[0][2]), rows[0][3]
    if len(rows) != parts:
        raise ValueError('saved state is incomplete')
    text =''.join(r[4] or '' for r in sorted(rows, key=lambda r: int(r[1])))
    if hashlib.sha256(text.encode()).hexdigest() != digest:
        raise ValueError('saved state failed its hash check')
    run = loads(text)
    counts = sess.sql(
        f'SELECT (SELECT COALESCE(MAX(EVENT_NO), 0) FROM {SCHEMA}.RUN_EVENTS WHERE RUN_ID = ?), '
        f'(SELECT COALESCE(MAX(CALL_NO), 0) FROM {SCHEMA}.CORTEX_CALLS WHERE RUN_ID = ?), '
        f'(SELECT COALESCE(MAX(PASS_NO), 0) FROM {SCHEMA}.SHEET_RESULTS WHERE RUN_ID = ?), '
        f'(SELECT COALESCE(MAX(PROPOSAL_NO), 0) FROM {SCHEMA}.CORTEX_PROPOSALS WHERE RUN_ID = ?), '
        f'(SELECT COALESCE(MAX(PROOF_NO), 0) FROM {SCHEMA}.PROOFS WHERE RUN_ID = ?), '
        f'(SELECT COALESCE(MAX(PASS_NO), 0) FROM {SCHEMA}.SHEET_RENDERS WHERE RUN_ID = ?)',
        params=[run_id] * 6).collect()[0]
    attach(run, run_id, int(no), [int(c or 0) for c in counts])
    return run


def attach(run, run_id, snapshot_no, counts):
    """Continue saving a reopened run under its own id; what it already shows counts as saved.
    Numbering continues after the highest number already stored for the run."""
    event_no, call_no, pass_no, proposal_no, proof_no = counts[:5]
    render_pass = counts[5] if len(counts) > 5 else 0
    st = {'run_id': run_id, 'started': True, 'errors': [], 'meta': {},
          'events': len(run.get('events') or []), 'event_no': event_no,
          'calls': len(call_rows(run)), 'call_no': call_no,
          'proposals': [_sha(r) for r in proposal_rows(run)],
          'proofs': [proof_key(p) for p in (run.get('proofs') or {}).values()],
          'sheets_fp': None, 'pass_no': pass_no, 'proposal_no': proposal_no, 'proof_no': proof_no,
          'snapshot_no': snapshot_no, 'snapshot_fp': None, 'reopened': True}
    rows = sheet_rows(run)
    st['sheets_fp'] = _sha([(r['SHEET'], r['SUMMARY'], r['FIX_APPLIED']) for r in rows]) if rows else None
    drawn = render_rows(run)
    st['renders_fp'] = _sha([(r['DASHBOARD'], r['SHEET'], r['RENDERS'], r['ROWS_DRAWN'], r['NOTE'])
                             for r in drawn]) if drawn else None
    st['render_pass'] = render_pass
    st['snapshot_fp'] = _fingerprint(run)
    run['_store'] = st
    return run


def status(run):
    """One line for the console: saved, not saved, or store not set up."""
    st = _state(run)
    if not st:
        return None
    if st['errors']:
        last = st['errors'][-1]
        return ('warn', f"Evidence not saved for one step ({last['Step']}: {last['Error']}). The migration is "
                        'unaffected; the next step tries again.')
    return ('ok', f"Saved in Snowflake (run {st['run_id'][:8]}): {st['event_no']} events, {st['call_no']} Cortex calls, "
                  f"{st['snapshot_no']} saved states. Reopen it from Overview after a refresh.")
