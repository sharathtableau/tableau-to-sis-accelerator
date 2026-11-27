"""EN33: warehouse and Cortex spend (read-only view) + resource monitor SQL for an account admin.

The owner has no ACCOUNTADMIN, so the app only READS spend (ACCOUNT_USAGE views, where the role may read them) and
GENERATES the monitor SQL for an admin; nothing here changes the account. Codes only, never error text.
Per-run Cortex ceilings are EN03 (v3_ai.limits)."""
import re

_IDENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_$]{0,254}$')


def spend(session, days=30):
    d = max(1, min(int(days or 30), 90))
    try:
        daily = session.sql(
            'SELECT TO_VARCHAR(USAGE_DATE) AS DAY, SERVICE_TYPE, SUM(CREDITS_USED) AS CREDITS '
            'FROM SNOWFLAKE.ACCOUNT_USAGE.METERING_DAILY_HISTORY '
            f'WHERE USAGE_DATE >= DATEADD(day, -{d}, CURRENT_DATE()) GROUP BY 1, 2 ORDER BY 1 DESC, 2').collect()
    except Exception as exc:
        return {'status': 'NO_ACCOUNT_USAGE_ACCESS:' + type(exc).__name__, 'by_day': [], 'cortex': []}
    out = {'status': 'OK', 'days': d,
           'by_day': [{'day': str(r[0]), 'service': r[1], 'credits': float(r[2] or 0)} for r in daily], 'cortex': []}
    try:
        rows = session.sql(
            'SELECT FUNCTION_NAME, MODEL_NAME, SUM(TOKEN_CREDITS) AS CREDITS '
            'FROM SNOWFLAKE.ACCOUNT_USAGE.CORTEX_FUNCTIONS_USAGE_HISTORY '
            f'WHERE START_TIME >= DATEADD(day, -{d}, CURRENT_TIMESTAMP()) GROUP BY 1, 2 ORDER BY 3 DESC').collect()
        out['cortex'] = [{'function': r[0], 'model': r[1], 'credits': float(r[2] or 0)} for r in rows]
    except Exception as exc:
        out['cortex_status'] = 'NO_CORTEX_USAGE_ACCESS:' + type(exc).__name__
    return out


def admin_sql(warehouse='POWERHOUSE', credit_quota=100):
    if not _IDENT.match(str(warehouse)):
        raise ValueError('warehouse rejected')
    q = int(credit_quota)
    if q <= 0:
        raise ValueError('credit quota must be positive')
    mon = f'V3_{str(warehouse).upper()}_MONITOR'
    return ['-- run as an account admin (resource monitors need ACCOUNTADMIN)',
            f'CREATE RESOURCE MONITOR IF NOT EXISTS {mon} WITH CREDIT_QUOTA = {q} FREQUENCY = MONTHLY '
            'START_TIMESTAMP = IMMEDIATELY\n  TRIGGERS ON 80 PERCENT DO NOTIFY ON 100 PERCENT DO SUSPEND;',
            f'ALTER WAREHOUSE {str(warehouse).upper()} SET RESOURCE_MONITOR = {mon};',
            '-- Cortex AI functions are billed as AI services, not warehouse credits: cap them per run with '
            'V3_CORTEX_MAX_CREDITS_PER_RUN (EN03) and watch them in the spend view.']
