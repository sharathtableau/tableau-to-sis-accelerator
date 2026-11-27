"""CC03: scheduled re-check of deployed apps (source drift).

record_baseline()  a person records COUNT(*) and HASH_AGG(*) of every Snowflake table a migration's app reads
recheck()          (procedure V3_RECHECK, run by a scheduled task) recomputes each baseline table and records
                   unchanged / changed / missing in RECHECK_RESULTS; a changed source means: re-validate the app
                   against Tableau in the console (Tableau is not reachable from inside Snowflake).
Read-only on workbook data; code-built queries over validated identifiers; bound values; codes only."""
import re

SCHEMA = 'WBR_DB.V3_AGENT'
BASELINE = SCHEMA + '.RECHECK_BASELINE'
RESULTS = SCHEMA + '.RECHECK_RESULTS'
_TABLE = re.compile(r'^[A-Za-z_][A-Za-z0-9_$]*\.[A-Za-z_][A-Za-z0-9_$]*\.[A-Za-z_][A-Za-z0-9_$]*$')
_IDENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_$]{0,254}$')
_ZIP = re.compile(r"^@[A-Za-z_][A-Za-z0-9_$]*\.[A-Za-z_][A-Za-z0-9_$]*\.[A-Za-z_][A-Za-z0-9_$]*/[A-Za-z0-9_\-./]+\.zip$")


def tables(bindings):
    out = set()
    for v in (bindings or {}).values():
        if isinstance(v, dict):
            for t in (v.get('tables') or {}).values():
                if isinstance(t, dict) and _TABLE.match(str(t.get('table') or '')):
                    out.add(str(t['table']).upper())
        elif isinstance(v, str) and _TABLE.match(v):
            out.add(v.upper())
    return sorted(out)


def fingerprint(session, table):
    if not _TABLE.match(str(table)):
        raise ValueError('table rejected')
    row = session.sql(f'SELECT COUNT(*) AS N, HASH_AGG(*) AS H FROM {table}').collect()[0]
    return int(row[0]), (None if row[1] is None else int(row[1]))


def record_baseline(session, app, bindings, approved_by):
    if not str(approved_by or '').strip():
        raise ValueError('a named person must record the baseline')
    if not _IDENT.match(str(app or '')):
        return {'status': 'APP_REFUSED'}
    done, failed = 0, []
    for t in tables(bindings):
        try:
            n, h = fingerprint(session, t)
            session.sql(f'INSERT INTO {BASELINE} (APP, TABLE_NAME, ROWS_N, HASH_VALUE, RECORDED_BY) '
                        'SELECT ?, ?, ?, ?, ?', params=[str(app).upper(), t, n, h, str(approved_by)[:200]]).collect()
            done += 1
        except Exception as exc:
            failed.append(f'{t}: {type(exc).__name__}')
    return {'status': 'OK' if not failed else 'PARTIAL', 'tables': done, 'failed': failed}


def recheck(session):
    """All latest baselines -> results rows. Returns {'results', 'changed_apps'}."""
    try:
        base = session.sql(f'SELECT APP, TABLE_NAME, ROWS_N, HASH_VALUE FROM {BASELINE} '
                           'QUALIFY ROW_NUMBER() OVER (PARTITION BY APP, TABLE_NAME ORDER BY RECORDED_AT DESC) = 1'
                           ).collect()
    except Exception as exc:
        return {'status': 'NO_BASELINE_TABLE:' + type(exc).__name__, 'results': [], 'changed_apps': []}
    results = []
    for app, table, n0, h0 in [tuple(r)[:4] for r in base]:
        try:
            n1, h1 = fingerprint(session, table)
            status = 'unchanged' if (int(n0), None if h0 is None else int(h0)) == (n1, h1) else 'changed'
        except Exception as exc:
            n1, status = None, 'missing'
        results.append({'app': app, 'table': table, 'status': status, 'rows_then': n0, 'rows_now': n1})
    for r in results:
        try:
            session.sql(f'INSERT INTO {RESULTS} (APP, TABLE_NAME, STATUS, ROWS_THEN, ROWS_NOW) SELECT ?, ?, ?, ?, ?',
                        params=[r['app'], r['table'], r['status'], r['rows_then'], r['rows_now']]).collect()
        except Exception:
            pass                                   # the returned summary still reports it
    return {'status': 'OK', 'results': results,
            'changed_apps': sorted({r['app'] for r in results if r['status'] != 'unchanged'})}


def recheck_handler(session):
    return recheck(session)


def latest(session, limit=200):
    try:
        rows = session.sql(f'SELECT APP, TABLE_NAME, STATUS, ROWS_THEN, ROWS_NOW, CHECKED_AT FROM {RESULTS} '
                           'ORDER BY CHECKED_AT DESC LIMIT ?', params=[int(limit)]).collect()
    except Exception as exc:
        return [], 'NO_RESULTS_TABLE:' + type(exc).__name__
    keys = ('app', 'table', 'status', 'rows_then', 'rows_now', 'checked_at')
    return [dict(zip(keys, list(r))) for r in rows], 'OK'


def setup_sql(candidate_zip, warehouse='POWERHOUSE', role='WBR_OWNER', cron='0 6 * * * UTC'):
    """One-time SQL the owner runs. Never OR REPLACE. The task needs EXECUTE TASK for the owner role."""
    if not _ZIP.match(str(candidate_zip or '')) or not _IDENT.match(str(warehouse)) or not _IDENT.match(str(role)) \
            or not re.match(r'^[0-9*/,\- ]+ [A-Za-z_/]+$', str(cron)):
        raise ValueError('stage path, warehouse, role or schedule rejected')
    return [f'CREATE SCHEMA IF NOT EXISTS {SCHEMA};',
            f'CREATE TABLE IF NOT EXISTS {BASELINE} (APP STRING, TABLE_NAME STRING, ROWS_N NUMBER, HASH_VALUE NUMBER, '
            'RECORDED_BY STRING, RECORDED_AT TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP());',
            f'CREATE TABLE IF NOT EXISTS {RESULTS} (APP STRING, TABLE_NAME STRING, STATUS STRING, ROWS_THEN NUMBER, '
            'ROWS_NOW NUMBER, CHECKED_AT TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP());',
            f"CREATE PROCEDURE IF NOT EXISTS {SCHEMA}.V3_RECHECK()\n  RETURNS VARIANT\n  LANGUAGE PYTHON\n"
            f"  RUNTIME_VERSION = '3.11'\n  PACKAGES = ('snowflake-snowpark-python')\n  IMPORTS = ('{candidate_zip}')\n"
            "  HANDLER = 'v3_recheck.recheck_handler'\n  EXECUTE AS OWNER;",
            f"CREATE TASK IF NOT EXISTS {SCHEMA}.V3_RECHECK_DAILY\n  WAREHOUSE = {warehouse}\n"
            f"  SCHEDULE = 'USING CRON {cron}'\n  AS CALL {SCHEMA}.V3_RECHECK();",
            f'ALTER TASK {SCHEMA}.V3_RECHECK_DAILY RESUME;',
            f'GRANT SELECT, INSERT ON TABLE {BASELINE} TO ROLE {role.upper()};',
            f'GRANT SELECT ON TABLE {RESULTS} TO ROLE {role.upper()};']
