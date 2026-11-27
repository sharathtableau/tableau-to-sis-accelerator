"""MA01: a portal app composing approved workbook packages (Analytics Home + one page per dashboard).

Owner defaults (21 Nov, changeable): 1:1 migration first -- a portal only composes packages that were validated and
approved as single apps; access by viewer user name (st.user; an owner's-rights app cannot read the viewer's roles)
from WBR_DB.V3_PORTAL.ACCESS ('*' = everyone); hiding a page is navigation, NOT security -- data separation is EN27's
row access policies; only the owner deploys a portal. Each page renders with the dashboard's own IR and bindings
through the same runtime as the single app; nothing is re-migrated or re-validated here."""
import ast
import copy
import hashlib
import json
import re

SCHEMA = 'WBR_DB.V3_PORTAL'
ACCESS = SCHEMA + '.ACCESS'
_NAME = re.compile(r'^[A-Za-z][A-Za-z0-9_ ]{1,60}$')
HOME = 'Analytics Home'


def portal_identifier(name):
    if not _NAME.match(str(name or '')):
        raise ValueError('portal name: letters, digits, spaces or underscores')
    return 'V3_PORTAL_' + re.sub(r'[^A-Z0-9]+', '_', str(name).upper()).strip('_')


def setup_sql(role='WBR_OWNER'):
    if not re.match(r'^[A-Za-z_][A-Za-z0-9_$]*$', str(role)):
        raise ValueError('role rejected')
    return [f'CREATE SCHEMA IF NOT EXISTS {SCHEMA};',
            f'CREATE TABLE IF NOT EXISTS {ACCESS} (PORTAL STRING, DASHBOARD_ID STRING, USER_NAME STRING, '
            'NOTE STRING, UPDATED_AT TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP());',
            f'GRANT SELECT ON TABLE {ACCESS} TO ROLE {role.upper()};',
            "-- one row per dashboard and viewer; USER_NAME = '*' opens a dashboard to everyone, e.g.",
            f"-- INSERT INTO {ACCESS} (PORTAL, DASHBOARD_ID, USER_NAME) VALUES ('V3_PORTAL_SALES', '<id>', '*');"]


def pages_for(user, access, ids):
    u = str(user or '').upper()
    allowed = {d for d, who in access or [] if str(who) == '*' or (u and str(who).upper() == u)}
    return [i for i in ids if i in allowed]


def _validate(ir, bindings):
    from ir_contract import require_valid_ir
    from v3_package import validate_bindings
    require_valid_ir(ir)
    return validate_bindings(ir, bindings)


def portal_bundle(name, packages, validate=None):
    """(zip bytes, build sha) -- every package validated exactly as a single app would be."""
    import v3_package as VP
    ident = portal_identifier(name)
    if not packages:
        raise ValueError('a portal needs at least one approved package')
    ids = [p['id'] for p in packages]
    if len(set(ids)) != len(ids):
        raise ValueError('each dashboard package is included once')
    dashboards = []
    for p in packages:
        b = (validate or _validate)(p['ir'], p['bindings'])
        ir = copy.deepcopy(p['ir'])
        dashboards.append({'id': p['id'], 'title': str(p.get('title') or p['id'])[:120], 'ir': ir, 'bindings': b,
                           'ask': p.get('ask'), 'build': p.get('build'), 'approval': p.get('approval'),
                           'validated_at': p.get('validated_at')})
    payload = json.dumps({'portal': ident, 'dashboards': dashboards}, sort_keys=True, default=str)
    source = ('import json\nfrom v3_portal import run_portal\nPAYLOAD = json.loads(' + repr(payload) + ')\n'
              'run_portal(PAYLOAD)\n')
    ast.parse(source)
    files = VP.modules(['v3_runtime.py', 'v3_portal.py'])
    files['app.py'] = source.encode()
    target = dict(VP.TARGET, name=ident)
    provenance = {'kind': 'portal', 'portal': ident, 'packages': [{'id': d['id'], 'build': d['build'],
                                                                   'approval': d['approval']} for d in dashboards]}
    return VP.archive(files, 'app.py', target, provenance)


def _viewer():
    import streamlit as st
    try:
        return getattr(st.user, 'user_name', None) or (st.user.get('user_name') if hasattr(st.user, 'get') else None)
    except Exception:
        return None


def _access(session, portal):
    try:
        rows = session.sql(f'SELECT DASHBOARD_ID, USER_NAME FROM {ACCESS} WHERE PORTAL = ?', params=[portal]).collect()
        return [(r[0], r[1]) for r in rows], 'OK'
    except Exception as exc:
        return [], 'NO_ACCESS_TABLE:' + type(exc).__name__


def run_portal(payload):
    import streamlit as st
    import backend
    from v3_local import preview_context
    import v3_runtime as R
    st.set_page_config(page_title=payload['portal'].replace('V3_PORTAL_', '').replace('_', ' ').title(), layout='wide')
    session = backend._active_session()
    if session is None:
        st.error('This portal needs a Snowflake session.')
        st.stop()
    access, status = _access(session, payload['portal'])
    by_id = {d['id']: d for d in payload['dashboards']}
    visible = pages_for(_viewer(), access, list(by_id))
    labels = [HOME] + [by_id[i]['title'] for i in visible]
    choice = st.radio('Page', list(range(len(labels))), format_func=lambda i: labels[i], horizontal=True,
                      key='v3-portal-page', label_visibility='collapsed')
    if choice == 0:
        st.title(HOME)
        if status != 'OK':
            st.caption('Access list not available (' + status + '); no dashboard is shown.')
        if not visible:
            st.info('No dashboard is shared with you in this portal yet.')
        for i in visible:
            d = by_id[i]
            with st.container(border=True):
                st.markdown(f"**{d['title']}**")
                st.caption(f"Build {str(d.get('build') or '')[:12]} · approval {d.get('approval') or '—'}"
                           + (f" · validated {d['validated_at']}" if d.get('validated_at') else ''))
        return
    d = by_id[visible[choice - 1]]
    with preview_context(d['ir'], d['bindings'], snowflake=True):
        st.title(d['title'])
        R.render_workbook(d['ir'], ask_bindings=d['bindings'], **({'ask': d['ask']} if d.get('ask') else {}))


def unpack(data):
    import io
    import zipfile
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        files = {n: z.read(n) for n in z.namelist() if not n.endswith('/')}
    files.pop('snowflake.yml', None)                  # CLI manifest; not used by the running app
    return files


def deploy_portal(session, name, packages, approved_by, replace=False):
    """Deploy (owner only, after approval) through the same path as a single app."""
    if not str(approved_by or '').strip():
        raise ValueError('a named person must approve deploying a portal')
    if session is None:
        raise RuntimeError('Deployment needs the hosted Snowflake session.')
    import tempfile
    from pathlib import Path
    import pipeline
    import v3_deploy as D
    ident = portal_identifier(name)
    if not replace and D.app_exists(session, ident):
        raise ValueError(f'A portal named {ident} already exists; confirm replacing it')
    data, build = portal_bundle(name, packages)
    files = unpack(data)
    folder = Path(tempfile.mkdtemp(prefix='v3portal_'))
    for filename, blob in files.items():
        (folder / filename).write_bytes(blob)
    runtime = sorted(n for n in files if n not in ('app.py', 'datasources.json'))
    record = pipeline.deploy_streamlit_app(session, ident, 'app.py', db=D.DB, schema=D.SCHEMA, stage=D.STAGE,
                                           identifier=ident, warehouse=D.WAREHOUSE, datasources={},
                                           title=ident[:200], runtime_modules=runtime, root_dir=str(folder))
    return {'identifier': record['identifier'], 'build_sha256': build, 'location': record['root_location'],
            'files': len(record['files']), 'url': record.get('url'), 'approved_by': approved_by,
            'packages': [p['id'] for p in packages]}
