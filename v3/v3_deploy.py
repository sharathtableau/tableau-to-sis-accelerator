"""Approved deployment of one migrated workbook as its own Streamlit in Snowflake app.

Uses the original accelerator's working deploy path unchanged:
pipeline.deploy_streamlit_app (copy to a space-free temp dir, PUT through Snowpark,
CREATE OR REPLACE STREAMLIT ... ROOT_LOCATION, no USE statements) - the same call the
original console's Deploy & Ask page makes. V3 decides only WHAT is deployed:
- exactly v3_package.workbook_bundle(ir, bindings): validated IR, verified Snowflake
  bindings and the hash-inventoried runtime; nothing is regenerated;
- one app per workbook, named from the package payload hash (V3_WORKBOOK_<16 hex>), in
  the V3 schema, so it never replaces the console or another workbook's app;
- no tables, views or data are created or changed; bindings travel inside the package,
  so the per-deploy datasources.json is written empty.
Only called from the explicit, reviewed Deploy button in the console.
"""
import io
import json
from pathlib import Path
import re
import tempfile
import zipfile

DB = 'WBR_DB'
SCHEMA = 'TABLEAU_ACCELERATOR_V3'
STAGE = 'V3_WORKBOOK_STAGE'
WAREHOUSE = 'POWERHOUSE'
_IDENT = re.compile(r'V3_WORKBOOK_[A-Z0-9_]{1,200}')
_NAME = re.compile(r'[A-Z][A-Z0-9_]{2,120}')
RESERVED_PREFIXES = ('TABLEAU_TO_SIS', 'V3_WORKBOOK_STAGE')   # the consoles and the package stage


def app_identifier(name):
    """A user-chosen app name as a plain Snowflake identifier (letters, digits, underscore)."""
    ident = re.sub(r'[^A-Z0-9]+', '_', re.sub(r"[-'’]", '', str(name or '').strip().upper())).strip('_')
    if ident[:1].isdigit():
        ident = 'APP_' + ident
    if not _NAME.fullmatch(ident or ''):
        raise ValueError('Use 3-120 letters, digits or underscores, starting with a letter')
    if ident.startswith(RESERVED_PREFIXES):
        raise ValueError('That name is reserved for the accelerator itself')
    return ident


def app_exists(session, ident):
    rows = session.sql(f"SHOW STREAMLITS LIKE '{ident}' IN SCHEMA {DB}.{SCHEMA}").collect()
    return bool(rows)


def package_files(data):
    """(identifier, build, {name: bytes}) from a workbook_bundle archive."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        files = {n: archive.read(n) for n in archive.namelist() if not n.endswith('/')}
    build = json.loads(files['candidate_build.json'])
    name = json.loads(files['snowflake.yml'])['entities']['v3_candidate']['identifier']['name']
    if not _IDENT.fullmatch(name):
        raise ValueError('Unexpected deployment identifier')
    files.pop('snowflake.yml')  # CLI manifest; not used by the running app
    return name, build['build_sha256'], files


def deploy_workbook(session, ir, bindings, title, app_name=None, replace=False, ask=None):
    """Write the verified package to a temp folder and hand it to the original deploy.

    app_name: optional readable name chosen at deploy time; the build-specific stage folder
    still uses the package hash. An existing app with that name is replaced only when asked."""
    if session is None:
        raise RuntimeError('Deployment needs the hosted Snowflake session.')
    from v3_package import workbook_bundle
    import pipeline
    data, _ = workbook_bundle(ir, bindings, ask=ask)
    name, build, files = package_files(data)
    ident = app_identifier(app_name) if app_name else name
    if app_name and not replace and app_exists(session, ident):
        raise ValueError(f'An app named {ident} already exists; choose another name or confirm replacing it')
    folder = Path(tempfile.mkdtemp(prefix='v3deploy_'))
    for filename, payload in files.items():
        (folder / filename).write_bytes(payload)
    runtime = sorted(n for n in files if n not in ('app.py', 'datasources.json'))
    record = pipeline.deploy_streamlit_app(
        session, name, 'app.py', db=DB, schema=SCHEMA, stage=STAGE, identifier=ident,
        warehouse=WAREHOUSE, datasources={}, title=title[:200], runtime_modules=runtime,
        root_dir=str(folder))
    return {'identifier': record['identifier'], 'build_sha256': build,
            'location': record['root_location'], 'files': len(record['files']),
            'url': record.get('url'), 'notes': [], 'acceptance': 'NOT_VERIFIED'}


GENERATED_ON_DEPLOY = {'datasources.json'}        # written by the deploy itself, not part of the package


def edited_files(session, ident):
    """X15: before an existing app is replaced, the files of its deployed build that differ from that build's own
    manifest (candidate_build.json) -- hand edits a re-run would overwrite. Returns (changes, reason):
    changes = [{'File', 'Change'}] (Change: 'edited' / 'removed' / 'added'); changes is None when the check cannot
    run (reason says why) -- the caller then asks for explicit confirmation. Read-only."""
    import hashlib
    if not _NAME.fullmatch(str(ident or '')):
        return None, 'invalid app name'
    try:
        rows = session.sql(f'DESCRIBE STREAMLIT {DB}.{SCHEMA}.{ident}').collect()
        info = {k.lower(): v for k, v in rows[0].as_dict().items()} if rows else {}
        root = str(info.get('root_location') or '').rstrip('/')
        if not root.startswith(f'@{DB}.{SCHEMA}.'):
            return None, 'the app was not deployed by this accelerator (no package folder)'

        def read(name):
            with session.file.get_stream(f'{root}/{name}') as f:
                return f.read()
        built = json.loads(read('candidate_build.json'))
        hashes = built.get('files') or {}
        listed = {str(r.as_dict().get('name', '')).rsplit('/', 1)[-1] for r in session.sql(f'LIST {root}/').collect()}
    except Exception as exc:                       # no manifest, no access, not a V3 app
        return None, f'the deployed files could not be read ({type(exc).__name__})'
    changes = []
    for name, digest in sorted(hashes.items()):
        if name not in listed:
            changes.append({'File': name, 'Change': 'removed'})
            continue
        try:
            same = hashlib.sha256(read(name)).hexdigest() == digest
        except Exception as exc:
            return None, f'{name} could not be read ({type(exc).__name__})'
        if not same:
            changes.append({'File': name, 'Change': 'edited'})
    for name in sorted(listed - set(hashes) - GENERATED_ON_DEPLOY - {'candidate_build.json', ''}):
        changes.append({'File': name, 'Change': 'added'})
    return changes, None
