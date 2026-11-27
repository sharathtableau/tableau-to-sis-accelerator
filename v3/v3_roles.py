"""E10: who may do what in the migration console. AUTHOR migrates, reviews and approves fixes; PUBLISHER deploys
(single apps and portals); everyone else is a VIEWER (Overview, Estate catalogue). The access table is maintained by
the owner (one-time SQL below). No table, or no rows in it, = open mode: every page, as before, with a note.
Identity = the viewer's Streamlit in Snowflake user name (an owner's-rights app cannot read the viewer's roles).
Hiding a page is navigation; data separation is EN27 (row access policies)."""
import re

TABLE = 'WBR_DB.V3_PORTAL.ROLES'
ROLES = ('AUTHOR', 'PUBLISHER')
PAGE_ROLE = {'Overview': 'VIEWER', 'Estate catalogue': 'VIEWER', 'Deploy': 'PUBLISHER'}   # every other page: AUTHOR
_IDENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_$]{0,254}$')


def roles_for(session, user):
    """{'mode': LOCAL | OPEN | OPEN_NO_ROWS | TABLE, 'roles': set}. Read-only, bound user name."""
    if session is None:
        return {'mode': 'LOCAL', 'roles': {'VIEWER', *ROLES}}
    try:
        rows = session.sql(f'SELECT ROLE FROM {TABLE} WHERE UPPER(USER_NAME) = ?',
                           params=[str(user or '').upper()]).collect()
    except Exception:
        return {'mode': 'OPEN', 'roles': {'VIEWER', *ROLES}}
    got = {str(r[0]).upper() for r in rows if r and str(r[0]).upper() in ROLES}
    if got:
        return {'mode': 'TABLE', 'roles': {'VIEWER', *got}}
    try:
        n = session.sql(f'SELECT COUNT(*) AS N FROM {TABLE}').collect()
        n = int(n[0][0]) if n else 0
    except Exception:
        n = 0
    if n == 0:
        return {'mode': 'OPEN_NO_ROWS', 'roles': {'VIEWER', *ROLES}}
    return {'mode': 'TABLE', 'roles': {'VIEWER'}}


def required(page, access):
    """The role a page needs that this person lacks, else None."""
    need = PAGE_ROLE.get(page, 'AUTHOR')
    if access.get('mode') != 'TABLE' or need in access.get('roles', set()):
        return None
    return need


def allowed(page, access):
    return required(page, access) is None


def setup_sql(owner_role='WBR_OWNER'):
    if not _IDENT.match(str(owner_role or '')):
        raise ValueError('role rejected')
    return ['CREATE SCHEMA IF NOT EXISTS WBR_DB.V3_PORTAL;',
            f'CREATE TABLE IF NOT EXISTS {TABLE} (USER_NAME STRING NOT NULL, ROLE STRING NOT NULL, '
            'ADDED_BY STRING, ADDED_AT TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP());',
            f"-- one row per person and role, for example:\n-- INSERT INTO {TABLE} (USER_NAME, ROLE, ADDED_BY) "
            "SELECT 'YOUR_USER', 'PUBLISHER', CURRENT_USER();",
            f'GRANT SELECT ON TABLE {TABLE} TO ROLE {owner_role.upper()};']
