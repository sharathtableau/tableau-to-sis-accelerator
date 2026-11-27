"""E15 safe intake + EN24 published datasources and custom SQL as views.

check_archive()     every upload / Tableau download is checked before it is written or parsed (size, members,
                    expansion, member paths, encryption, one .twb, no XML DOCTYPE / ENTITY); refusals are codes.
                    Returns the snapshot manifest (lineage) the run records.
source_policy()     per datasource: live / extract / published / file, and what the migrated app reads.
expand_published()  a published datasource (sqlproxy) gets its published definition (fetched read-only through
                    the existing Tableau client) spliced in, so the normal verified binding applies.
view_sql()          a verified source plan (custom SQL, physical join, relationship model) as view DDL an owner
                    runs (CREATE VIEW IF NOT EXISTS, never OR REPLACE); bind_view() binds it once verified.
Nothing here writes to Snowflake or Tableau."""
import copy
import hashlib
import io
import os
import re
import xml.etree.ElementTree as ET
import zipfile

MB = 1024 * 1024
LIMITS = {
    'max_archive_bytes': int(os.environ.get('V3_MAX_ARCHIVE_MB', '512')) * MB,
    'max_members': 5000,
    'max_uncompressed': int(os.environ.get('V3_MAX_UNCOMPRESSED_MB', '4096')) * MB,
    'max_ratio': 200,               # per member: uncompressed / compressed, checked above ratio_floor
    'ratio_floor': 50 * MB,
    'max_twb_bytes': 256 * MB,
}
_DECL = re.compile(rb'<!\s*(DOCTYPE|ENTITY)', re.I)
_SCHEMA = re.compile(r'^[A-Za-z_][A-Za-z0-9_$]*\.[A-Za-z_][A-Za-z0-9_$]*$')
_DATA = ('.hyper', '.tde', '.csv', '.xls', '.xlsx', '.txt', '.json')


class IntakeRefused(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _unsafe(name):
    n = name.replace('\\', '/')
    return (n.startswith('/') or re.match(r'^[A-Za-z]:', n) is not None or '\x00' in n
            or any(part == '..' for part in n.split('/')))


def _check_xml(xml, limit):
    if len(xml) > limit:
        raise IntakeRefused('TWB_TOO_LARGE')
    if _DECL.search(xml[:1 << 20]) or _DECL.search(xml):
        raise IntakeRefused('XML_DECLARATIONS')
    try:
        return ET.fromstring(xml)
    except ET.ParseError:
        raise IntakeRefused('XML_MALFORMED') from None


def _lineage(root):
    rep = root.find('./repository-location')
    repository = {k: rep.get(k) for k in ('id', 'site', 'revision', 'derived-from')} if rep is not None else {}
    extracts = []
    for ds in root.findall('./datasources/datasource'):
        for c in ds.iter('connection'):
            if c.get('class') in ('hyper', 'dataengine') and c.get('dbname'):
                extracts.append({'datasource': ds.get('caption') or ds.get('name'), 'file': c.get('dbname'),
                                 'refreshed': c.get('update-time')})
    return repository, extracts


def check_archive(raw, filename, **limits):
    """The snapshot manifest of an accepted workbook (.twb or .twbx); IntakeRefused(code) otherwise."""
    lim = dict(LIMITS, **limits)
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        raise IntakeRefused('EMPTY')
    if len(raw) > lim['max_archive_bytes']:
        raise IntakeRefused('ARCHIVE_TOO_LARGE')
    members, twb_name = [], None
    if zipfile.is_zipfile(io.BytesIO(raw)):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                infos = z.infolist()
                if len(infos) > lim['max_members']:
                    raise IntakeRefused('TOO_MANY_MEMBERS')
                total = 0
                for i in infos:
                    if _unsafe(i.filename):
                        raise IntakeRefused('UNSAFE_MEMBER_PATH')
                    if i.flag_bits & 0x1:
                        raise IntakeRefused('ENCRYPTED_MEMBER')
                    total += i.file_size
                    if total > lim['max_uncompressed']:
                        raise IntakeRefused('UNCOMPRESSED_TOO_LARGE')
                    if i.file_size > lim['ratio_floor'] and i.file_size > lim['max_ratio'] * max(1, i.compress_size):
                        raise IntakeRefused('EXPANSION_RATIO')
                    members.append({'name': i.filename, 'bytes': i.file_size, 'crc': '%08x' % i.CRC})
                twbs = [i for i in infos if i.filename.lower().endswith('.twb')]
                if not twbs:                                  # a packaged published datasource (.tdsx)
                    twbs = [i for i in infos if i.filename.lower().endswith('.tds')]
                if len(twbs) != 1:
                    raise IntakeRefused('TWB_COUNT')
                if twbs[0].file_size > lim['max_twb_bytes']:
                    raise IntakeRefused('TWB_TOO_LARGE')
                twb_name = twbs[0].filename
                try:
                    xml = z.read(twb_name)
                except (zipfile.BadZipFile, RuntimeError, NotImplementedError):
                    raise IntakeRefused('ARCHIVE_CORRUPT') from None
        except zipfile.BadZipFile:
            raise IntakeRefused('ARCHIVE_CORRUPT') from None
    else:
        if str(filename or '').lower().endswith(('.twbx', '.tdsx')):
            raise IntakeRefused('NOT_A_WORKBOOK_PACKAGE')
        xml, twb_name = bytes(raw), os.path.basename(str(filename or 'workbook.twb'))
    root = _check_xml(xml, lim['max_twb_bytes'])
    repository, extracts = _lineage(root)
    return {'filename': os.path.basename(str(filename or '')), 'sha256': hashlib.sha256(raw).hexdigest(),
            'bytes': len(raw), 'twb': twb_name, 'twb_sha256': hashlib.sha256(xml).hexdigest(),
            'members': members, 'repository': repository, 'extracts': extracts}


def workbook_xml(raw):
    """The single workbook XML of an accepted package (call check_archive first)."""
    if zipfile.is_zipfile(io.BytesIO(raw)):
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            names = z.namelist()
            name = next((n for n in names if n.lower().endswith('.twb')), None) or \
                next(n for n in names if n.lower().endswith('.tds'))
            return z.read(name)
    return bytes(raw)


def rewrite_package(raw, filename, new_xml):
    """The package with its workbook XML replaced (other members byte-identical); a .twb is the XML itself."""
    if not zipfile.is_zipfile(io.BytesIO(raw)):
        return bytes(new_xml)
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as src, zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            data = new_xml if info.filename.lower().endswith('.twb') else src.read(info.filename)
            dst.writestr(info, data, compress_type=info.compress_type)
    return out.getvalue()


# --------------------------------------------------------------------------- policy
def _classes(ds):
    return [c.get('class') for c in ds.iter('connection') if c.get('class') and c.get('class') != 'federated']


def source_policy(root):
    """[{Datasource, Mode, Extract refreshed, App reads}] -- one rule for every workbook."""
    rows = []
    for ds in root.findall('./datasources/datasource'):
        if ds.get('name') == 'Parameters':
            continue
        cls = _classes(ds)
        ext = next((c.get('update-time') for c in ds.iter('connection') if c.get('class') in ('hyper', 'dataengine')),
                   None)
        has_extract = ds.find('.//extract') is not None or any(c in ('hyper', 'dataengine') for c in cls)
        upstream = [c for c in cls if c not in ('hyper', 'dataengine')]
        files = [c for c in ds.iter('connection') if c.get('filename')]
        if 'sqlproxy' in cls:
            mode, reads = 'Published datasource', ('The published definition\'s own source, fetched read-only from '
                                                   'Tableau and verified like an authored one')
        elif 'snowflake' in upstream:
            mode = 'Extract over live Snowflake' if has_extract else 'Live Snowflake'
            reads = ('The live Snowflake source (verified read-only); values can differ from the extract '
                     'snapshot until validated against Tableau' if has_extract else 'The live Snowflake source '
                     '(verified read-only)')
        elif files or any(c in ('textscan', 'excel-direct', 'excel') for c in upstream):
            mode, reads = 'File', 'The bundled data, loaded into Snowflake tables this run creates'
        elif upstream:
            mode, reads = 'Live, not Snowflake', ('Refused: replicate the source into Snowflake first (EN19; the '
                                                  'replication kit below gives the landing table and steps), then '
                                                  'bind it read-only')
        elif has_extract:
            mode, reads = 'Extract only', 'The extract rows, decoded and loaded into Snowflake'
        else:
            mode, reads = 'No connection', 'Nothing (no data source)'
        rows.append({'Datasource': ds.get('caption') or ds.get('name'), 'Mode': mode,
                     'Extract refreshed': ext or '—', 'App reads': reads})
    return rows


# --------------------------------------------------------------------------- replication kit (EN19 / 6.4)
_SF_TYPES = {'string': 'VARCHAR', 'integer': 'NUMBER(38,0)', 'real': 'FLOAT', 'date': 'DATE',
             'datetime': 'TIMESTAMP_NTZ', 'boolean': 'BOOLEAN'}


def replication_kit(root, schema='WBR_DB.V3_LANDING'):
    """For every live source that is not Snowflake: the landing table DDL (columns and types from the workbook's
    own metadata), and the steps to replicate it and bind it. Code-built; an owner runs it."""
    if not _SCHEMA.match(str(schema or '')):
        raise ValueError('schema must be DATABASE.SCHEMA')
    from calc_translator import to_phys
    kits = []
    for row, ds in zip(source_policy(root), [d for d in root.findall('./datasources/datasource')
                                             if d.get('name') != 'Parameters']):
        if row['Mode'] != 'Live, not Snowflake':
            continue
        cols, seen = [], set()
        for mr in ds.findall('.//metadata-record'):
            if mr.get('class') != 'column':
                continue
            name = to_phys(mr.findtext('remote-name') or mr.findtext('local-name') or '')
            if name in seen:
                continue
            seen.add(name)
            cols.append((name, _SF_TYPES.get(mr.findtext('local-type') or '', 'VARCHAR'),
                         mr.findtext('remote-name') or ''))
        if not cols:
            continue
        table = f"{schema.upper()}.{to_phys(row['Datasource'])[:120]}"
        source = sorted({c for c in _classes(ds) if c not in ('hyper', 'dataengine')})
        ddl = (f"CREATE TABLE IF NOT EXISTS {table} (\n  " +
               ',\n  '.join(f"{n} {t} COMMENT '{_safe(r)}'" for n, t, r in cols) + '\n)')
        kits.append({'Datasource': row['Datasource'], 'Source': ', '.join(source), 'Landing table': table,
                     'ddl': ddl, 'steps': [
                         f'Create the landing table (SQL below) in {schema.upper()}.',
                         f"Replicate the {', '.join(source)} table into it (a Snowflake connector, an ELT job or a "
                         'one-time export and COPY INTO), keeping the column names above.',
                         'Run the migration again: bind the datasource to the landing table on the Data model step '
                         '(columns, types and access are checked before it is used).',
                         'Validate the sheets against Tableau (the source changed, so values are re-proven).']})
    return kits


# --------------------------------------------------------------------------- published datasources
def published_sources(root):
    out = []
    for ds in root.findall('./datasources/datasource'):
        conn = ds.find('connection')
        if conn is None or conn.get('class') != 'sqlproxy':
            continue
        rep = ds.find('./repository-location')
        site = ''
        if rep is not None:
            site = rep.get('site') or (re.match(r'^/t/([^/]+)/', rep.get('path') or '') or [None, ''])[1]
        out.append({'datasource_id': ds.get('name'), 'caption': ds.get('caption') or ds.get('name'),
                    'server': conn.get('server', ''), 'site': site or '',
                    'content_url': conn.get('dbname') or (rep.get('id') if rep is not None else ''),
                    'revision': rep.get('revision') if rep is not None else None})
    return out


def _definition(data):
    """The <datasource> of a fetched .tds / .tdsx, checked like a workbook."""
    check_archive(data, 'definition.tdsx' if zipfile.is_zipfile(io.BytesIO(data)) else 'definition.tds')
    import tableau_parser
    root = tableau_parser.parse_xml(workbook_xml(data))
    ds = root if root.tag == 'datasource' else root.find('.//datasource')
    if ds is None or ds.find('connection') is None:
        raise IntakeRefused('DEFINITION_REFUSED')
    if 'sqlproxy' in _classes(ds) or not [c for c in _classes(ds) if c not in ('hyper', 'dataengine')]:
        raise IntakeRefused('DEFINITION_REFUSED')            # a proxy of a proxy, or no source behind it
    if ds.find('connection').get('class') != 'federated' or ds.find('.//named-connection') is None:
        raise IntakeRefused('DEFINITION_REFUSED')            # the shape every published definition has
    return ds


def _splice(target, pub):
    """target keeps its name, caption, columns and calcs; its connection (and extract / object graph) become the
    published definition's; published columns the workbook does not declare are added."""
    def is_graph(e):
        return isinstance(e.tag, str) and e.tag.endswith('object-graph')
    old = target.find('connection')
    pos = list(target).index(old)
    target.remove(old)
    for e in [e for e in list(target) if e.tag == 'extract' or is_graph(e)]:
        target.remove(e)
    target.insert(pos, copy.deepcopy(pub.find('connection')))
    k = pos + 1
    for e in pub:
        if e.tag == 'extract' or is_graph(e):
            target.insert(k, copy.deepcopy(e))
            k += 1
    have = {c.get('name') for c in target.findall('./column')}
    for c in pub.findall('./column'):
        if c.get('name') not in have:
            target.append(copy.deepcopy(c))


def expand_published(xml, fetch):
    """(workbook XML, [report rows]). fetch(server, site, content_url) -> .tds / .tdsx bytes, or None."""
    root = ET.fromstring(xml)
    pubs = published_sources(root)
    if not pubs:
        return xml, []
    nodes = {d.get('name'): d for d in root.findall('./datasources/datasource')}
    report, changed = [], False
    for p in pubs:
        row = dict(p, state='', definition_sha256=None)
        if fetch is None:
            row['state'] = 'NO_TABLEAU_CLIENT'
        else:
            try:
                data = fetch(p['server'], p['site'], p['content_url'])
                definition = _definition(data)
                _splice(nodes[p['datasource_id']], definition)
                row.update(state='EXPANDED', definition_sha256=hashlib.sha256(data).hexdigest())
                changed = True
            except IntakeRefused as exc:
                row['state'] = 'DEFINITION_REFUSED' + ('' if exc.code == 'DEFINITION_REFUSED' else ':' + exc.code)
            except Exception as exc:
                row['state'] = 'NOT_FETCHED:' + type(exc).__name__
        report.append(row)
    return (ET.tostring(root, encoding='utf-8', xml_declaration=True) if changed else xml), report


def tableau_fetcher(allowed_server=None):
    """fetch() through the existing Tableau client (credentials resolved there, never here). Only the configured
    Tableau host is contacted: the published source's server must equal allowed_server when one is given, and the
    client itself refuses hosts outside the E11 allowlist."""
    def fetch(server, site, content_url):
        import tableau_server as TS
        host = re.sub(r'^https?://', '', str(server or '')).rstrip('/').lower()
        if allowed_server:
            want = re.sub(r'^https?://', '', allowed_server).rstrip('/').lower()
            if host != want:
                raise PermissionError('published source is on another Tableau host')
        return TS.fetch_published_datasource('https://' + host, site, content_url)
    return fetch


# --------------------------------------------------------------------------- views (EN24)
def _plan_sql(plan):
    import v3_model_binding as MBD
    try:
        return MBD.compile_query(plan)
    except (KeyError, TypeError) as exc:
        raise ValueError('not a verified source plan') from exc


def view_name(plan, caption, schema):
    if not _SCHEMA.match(str(schema or '')):
        raise ValueError('schema must be DATABASE.SCHEMA')
    sql = _plan_sql(plan)
    stem = re.sub(r'[^A-Z0-9]+', '_', str(caption or 'SOURCE').upper()).strip('_')[:60] or 'SOURCE'
    return f"{schema.upper()}.V3_SRC_{stem}_{hashlib.sha256(sql.encode()).hexdigest()[:8].upper()}"


def view_sql(plan, caption, schema):
    """CREATE VIEW IF NOT EXISTS ... AS <the verified plan's compiled SELECT>; an owner runs it."""
    name = view_name(plan, caption, schema)
    return f"CREATE VIEW IF NOT EXISTS {name} COMMENT = 'Tableau to SiS source: {_safe(caption)}' AS {_plan_sql(plan)}"


def _safe(text):
    return re.sub(r"[^\w .+&()-]", '', str(text or ''))[:120]


def bind_view(session, view, fields):
    """Bind a datasource to an owner-created view once its columns and types are verified (read-only)."""
    if session is None:
        return {'state': 'NO_SNOWFLAKE_SESSION'}
    from calc_translator import to_phys
    from v3_source_binding import validate_table
    try:     # the view exposes the plan's own output columns (to_phys of each field), as the plan binding does
        return {'state': 'BOUND', 'table': validate_table(session, view, [dict(f, physical=to_phys(f['physical']))
                                                                         for f in fields if f.get('physical')])}
    except Exception as exc:
        return {'state': 'NOT_BOUND:' + type(exc).__name__}
