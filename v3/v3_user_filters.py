"""EN27: Tableau user filters -> Snowflake row access policies (SQL for the owner; nothing applied by the app).

Tableau user names and groups do not exist in Snowflake, and a Streamlit-in-Snowflake app runs with the owner's role,
so a policy keys on the viewer's CURRENT_USER() through an owner-maintained entitlement table
WBR_DB.V3_PORTAL.ENTITLEMENTS (USER_NAME, ENTITLEMENT, VALUE). One exact form is translated:
[Field] = USERNAME(), USERNAME() = [Field], ISUSERNAME([Field]). Every other use of a user function is reported as
needing an entitlement design (named, never guessed)."""
import hashlib
import re

SCHEMA = 'WBR_DB.V3_PORTAL'
ENTITLEMENTS = SCHEMA + '.ENTITLEMENTS'
USER_FUNCS = ('USERNAME', 'ISMEMBEROF', 'FULLNAME', 'USERDOMAIN', 'ISUSERNAME', 'ISFULLNAME')
_FUNC = re.compile(r'\b(' + '|'.join(USER_FUNCS) + r')\s*\(', re.I)
_EXACT = [re.compile(r'^\s*\[([^\[\]]+)\]\s*=\s*USERNAME\s*\(\s*\)\s*$', re.I),
          re.compile(r'^\s*USERNAME\s*\(\s*\)\s*=\s*\[([^\[\]]+)\]\s*$', re.I),
          re.compile(r'^\s*ISUSERNAME\s*\(\s*\[([^\[\]]+)\]\s*\)\s*$', re.I)]
_TABLE = re.compile(r'^[A-Za-z_][A-Za-z0-9_$]*\.[A-Za-z_][A-Za-z0-9_$]*\.[A-Za-z_][A-Za-z0-9_$]*$')
_COL = re.compile(r'^[A-Za-z_][A-Za-z0-9_$]*$')


def detect(ir):
    out = []
    for fid, f in (ir.get('fields') or {}).items():
        formula = str(f.get('formula') or '')
        if not f.get('is_calculation') or not _FUNC.search(formula):
            continue
        field = next((m.group(1) for rx in _EXACT for m in [rx.match(formula)] if m), None)
        out.append({'field_id': fid, 'calculation': f.get('caption') or fid, 'datasource_id': f.get('datasource_id'),
                    'formula': formula[:400], 'functions': sorted({m.upper() for m in _FUNC.findall(formula)}),
                    'form': 'field_equals_user' if field else 'needs an entitlement design', 'field': field})
    return out


def _column(ir, ds, caption):
    """The bound column: the field's remote name when known, else the engine's column name for the caption."""
    from calc_translator import to_phys
    for f in (ir.get('fields') or {}).values():
        if f.get('datasource_id') == ds and f.get('caption') == caption and not f.get('is_calculation'):
            return str(f.get('remote_name') or to_phys(caption)).upper()
    return None


def _ds_caption(ir, ds):
    scope = (ir.get('field_scopes') or {}).get(ds)
    if isinstance(scope, dict) and scope.get('caption'):
        return scope['caption']
    dss = ir.get('datasources')
    if isinstance(dss, dict):
        return (dss.get(ds) or {}).get('caption')
    return None


def policies(ir, bindings):
    rows = []
    for c in detect(ir):
        row = dict(c, state='NEEDS_DESIGN', sql=None)
        if c['form'] == 'field_equals_user':
            ds_caption = _ds_caption(ir, c['datasource_id'])
            table = (bindings or {}).get(ds_caption)
            col = _column(ir, c['datasource_id'], c['field'])
            if not isinstance(table, str) or not _TABLE.match(table):
                row['state'] = 'NOT_BOUND'
            elif not col or not _COL.match(col):
                row['state'] = 'COLUMN_UNKNOWN'
            else:
                t = table.upper()
                ent = f"{t.split('.')[-1]}.{col}"
                name = f"{SCHEMA}.RAP_{hashlib.sha256((t + '.' + col).encode()).hexdigest()[:10].upper()}"
                row.update(state='READY', table=t, column=col, entitlement=ent, sql=[
                    f'CREATE ROW ACCESS POLICY IF NOT EXISTS {name} AS (V VARCHAR) RETURNS BOOLEAN ->\n'
                    f'  EXISTS (SELECT 1 FROM {ENTITLEMENTS} E WHERE E.USER_NAME = CURRENT_USER()\n'
                    f"          AND E.ENTITLEMENT = '{ent}' AND E.VALUE = V);",
                    f'ALTER TABLE {t} ADD ROW ACCESS POLICY {name} ON ({col});'])
        rows.append(row)
    return rows


def setup_sql(role='WBR_OWNER'):
    if not _COL.match(str(role)):
        raise ValueError('role rejected')
    return [f'CREATE SCHEMA IF NOT EXISTS {SCHEMA};',
            f'CREATE TABLE IF NOT EXISTS {ENTITLEMENTS} (USER_NAME STRING, ENTITLEMENT STRING, VALUE STRING, '
            'NOTE STRING, UPDATED_AT TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP());',
            f'GRANT SELECT ON TABLE {ENTITLEMENTS} TO ROLE {role.upper()};']
