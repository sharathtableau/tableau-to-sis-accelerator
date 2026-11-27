"""E15 safe intake + EN24 published datasources and custom SQL as views. Written BEFORE the code (tests first).

E15  check_archive(): every upload / Cloud download is checked before anything is written or parsed:
       size, member count, total and per-member expansion (zip bombs), unsafe member paths, encrypted members,
       exactly one .twb, and workbook XML without DOCTYPE / ENTITY declarations. Refusals carry a CODE only.
     The accepted archive gets a snapshot manifest (sha256 of the package and of the workbook XML, members,
     Tableau repository revision, every extract's refresh time) -- the lineage the run records.
     source_policy(): per datasource, live vs extract vs published vs file, and what the app reads.
EN24 published datasources (connection class 'sqlproxy'): the published definition (.tds / .tdsx) is fetched
     read-only through the existing Tableau client and its connection is spliced into the workbook's datasource,
     so the normal verified binding applies (no new binding path). Lineage per published source.
     Custom SQL / physical joins / relationship plans become view DDL an owner runs (CREATE VIEW IF NOT EXISTS,
     never OR REPLACE); bind_views() binds a view only after its schema is verified.
Proof workbooks: Campaign Incrementality and GOLD CRM Scorecard (live Snowflake), a custom-SQL plan (view DDL)."""
import glob
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import v3_intake as I  # noqa: E402

TWB = b"<?xml version='1.0' encoding='utf-8' ?><workbook><datasources/><worksheets/></workbook>"


def zbytes(members, compress=zipfile.ZIP_DEFLATED):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', compress) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


class Archive(unittest.TestCase):
    def code(self, raw, name='w.twbx', **limits):
        with self.assertRaises(I.IntakeRefused) as cm:
            I.check_archive(raw, name, **limits)
        return cm.exception.code

    def test_a_normal_package_gets_a_manifest(self):
        m = I.check_archive(zbytes({'w.twb': TWB, 'Data/x.csv': b'a,b\n1,2\n'}), 'w.twbx')
        self.assertEqual(m['twb'], 'w.twb')
        self.assertEqual(len(m['sha256']), 64)
        self.assertEqual(len(m['twb_sha256']), 64)
        self.assertEqual([x['name'] for x in m['members']], ['w.twb', 'Data/x.csv'])

    def test_refusals_are_codes(self):
        self.assertEqual(self.code(b'not a zip', 'w.twbx'), 'NOT_A_WORKBOOK_PACKAGE')
        self.assertEqual(self.code(zbytes({'Data/x.csv': b'1'})), 'TWB_COUNT')
        self.assertEqual(self.code(zbytes({'a.twb': TWB, 'b.twb': TWB})), 'TWB_COUNT')
        self.assertEqual(self.code(zbytes({'w.twb': TWB, '../evil.csv': b'1'})), 'UNSAFE_MEMBER_PATH')
        self.assertEqual(self.code(zbytes({'w.twb': TWB, '/abs/x.csv': b'1'})), 'UNSAFE_MEMBER_PATH')
        self.assertEqual(self.code(zbytes({'w.twb': TWB, 'C:/x.csv': b'1'})), 'UNSAFE_MEMBER_PATH')
        self.assertEqual(self.code(zbytes({'w.twb': TWB}), max_archive_bytes=10), 'ARCHIVE_TOO_LARGE')
        self.assertEqual(self.code(zbytes({'w.twb': TWB, 'a': b'1', 'b': b'2'}), max_members=2), 'TOO_MANY_MEMBERS')
        bomb = zbytes({'w.twb': TWB, 'Data/z.csv': b'0' * 3_000_000})
        self.assertEqual(self.code(bomb, max_ratio=100, ratio_floor=1_000_000), 'EXPANSION_RATIO')
        self.assertEqual(self.code(bomb, max_uncompressed=1_000_000), 'UNCOMPRESSED_TOO_LARGE')

    def test_encrypted_member_is_refused(self):
        raw = bytearray(zbytes({'w.twb': TWB}))
        # set the encryption flag (general purpose bit 0) in the local and central headers
        for sig in (b'PK\x03\x04', b'PK\x01\x02'):
            i = raw.find(sig)
            off = i + (6 if sig == b'PK\x03\x04' else 8)
            raw[off] |= 1
        self.assertEqual(self.code(bytes(raw)), 'ENCRYPTED_MEMBER')

    def test_xml_declarations_are_refused(self):
        evil = b"<?xml version='1.0'?><!DOCTYPE w [<!ENTITY a 'aaaa'>]><workbook>&a;</workbook>"
        self.assertEqual(self.code(evil, 'w.twb'), 'XML_DECLARATIONS')
        self.assertEqual(self.code(zbytes({'w.twb': evil})), 'XML_DECLARATIONS')
        self.assertEqual(self.code(b'<workbook><unclosed></workbook>', 'w.twb'), 'XML_MALFORMED')
        self.assertEqual(I.check_archive(TWB, 'w.twb')['twb'], 'w.twb')

    def test_code_never_carries_the_file_text(self):
        try:
            I.check_archive(b'<workbook secret="s3cr3t"><x></workbook>', 'w.twb')
        except I.IntakeRefused as exc:
            self.assertNotIn('s3cr3t', str(exc))


CAMPAIGN = sorted(glob.glob(str(ROOT / 'Workbooks' / '_powerbi_project_sweep' / 'Campaign Incrementatlity*.twbx')))
GOLD = sorted(glob.glob(str(ROOT / 'Workbooks' / '_powerbi_project_sweep' / 'GOLD CRM Scorecard*.twbx')))
CUSTOM_SQL_DS = (b"<datasource name='federated.cs' caption='Orders + Category'><connection class='federated'>"
                 b"<named-connections><named-connection name='snowflake.x'><connection class='snowflake' "
                 b"server='org-acct.snowflakecomputing.com' dbname='WBR_DB' schema='S'/></named-connection>"
                 b"</named-connections><relation connection='snowflake.x' name='Custom SQL Query' type='text'>"
                 b"SELECT O.ORDER_ID, C.CATEGORY FROM WBR_DB.S.ORDERS O JOIN WBR_DB.S.CATEGORY C ON O.CID = C.CID"
                 b"</relation><metadata-records><metadata-record class='column'><remote-name>ORDER_ID</remote-name>"
                 b"<local-name>[ORDER_ID]</local-name><parent-name>[Custom SQL Query]</parent-name>"
                 b"<local-type>string</local-type></metadata-record><metadata-record class='column'>"
                 b"<remote-name>CATEGORY</remote-name><local-name>[CATEGORY]</local-name>"
                 b"<parent-name>[Custom SQL Query]</parent-name><local-type>string</local-type></metadata-record>"
                 b"</metadata-records></connection></datasource>")


def twb_of(path):
    with zipfile.ZipFile(path) as z:
        return z.read(next(n for n in z.namelist() if n.lower().endswith('.twb')))


def publish(xml, index=0, content_url='PubSource'):
    """(workbook XML whose datasource #index is a published (sqlproxy) source, that source's .tds bytes, its name)."""
    root = ET.fromstring(xml)
    dss = [d for d in root.findall('./datasources/datasource') if d.get('name') != 'Parameters']
    ds = dss[index]
    tds = ET.tostring(ds)
    conn = ds.find('connection')
    i = list(ds).index(conn)
    ds.remove(conn)
    for ex in [e for e in ds if e.tag == 'extract' or e.tag.endswith('object-graph')]:
        ds.remove(ex)
    proxy = ET.Element('connection', {'class': 'sqlproxy', 'dbname': content_url, 'server': 'example.online.tableau.com',
                                      'channel': 'https', 'directory': '/dataserver', 'port': '443'})
    ET.SubElement(proxy, 'relation', {'name': 'sqlproxy', 'table': '[sqlproxy]', 'type': 'table'})
    ds.insert(i, proxy)
    ds.insert(0, ET.Element('repository-location', {'id': content_url, 'path': '/t/mysite/datasources',
                                                     'revision': '1.3', 'site': 'mysite'}))
    return ET.tostring(root), tds, ds.get('name')


def candidates(xml):
    import tableau_parser
    import v3_source_binding as SB
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, 'w.twb')
        Path(p).write_bytes(xml)
        ir = tableau_parser.build_ir(p, scoped=True)
        root = tableau_parser.load_twb_xml(p)
    return {k: (v.get('candidate'), bool(v.get('model_candidate'))) for k, v in SB.source_candidates(root, ir).items()}


class Published(unittest.TestCase):
    def check_workbook(self, path):
        xml = twb_of(path)
        pub, tds, name = publish(xml)
        self.assertEqual([p['datasource_id'] for p in I.published_sources(ET.fromstring(pub))], [name])
        self.assertIsNone(candidates(pub)[name][0])                          # unbindable as published
        seen = []

        def fetch(server, site, content_url):
            seen.append((server, site, content_url))
            return tds
        out, report = I.expand_published(pub, fetch)
        self.assertEqual(seen, [('example.online.tableau.com', 'mysite', 'PubSource')])
        self.assertEqual(report[0]['state'], 'EXPANDED')
        self.assertEqual(report[0]['revision'], '1.3')
        self.assertEqual(len(report[0]['definition_sha256']), 64)
        self.assertEqual(candidates(out)[name], candidates(xml)[name])      # the same verified binding as authored

    @unittest.skipUnless(CAMPAIGN, 'corpus workbook')
    def test_campaign_incrementality(self):
        self.check_workbook(CAMPAIGN[0])

    @unittest.skipUnless(GOLD, 'corpus workbook')
    def test_gold_crm(self):
        self.check_workbook(GOLD[0])

    @unittest.skipUnless(CAMPAIGN, 'corpus workbook')
    def test_fetch_failure_and_tdsx(self):
        pub, tds, name = publish(twb_of(CAMPAIGN[0]))
        out, report = I.expand_published(pub, lambda *a: (_ for _ in ()).throw(RuntimeError('token xyz')))
        self.assertEqual(out, pub)
        self.assertEqual(report[0]['state'], 'NOT_FETCHED:RuntimeError')
        self.assertNotIn('xyz', str(report))
        out, report = I.expand_published(pub, None)
        self.assertEqual(report[0]['state'], 'NO_TABLEAU_CLIENT')
        tdsx = zbytes({'Pub.tds': tds})
        out, report = I.expand_published(pub, lambda *a: tdsx)
        self.assertEqual(report[0]['state'], 'EXPANDED')
        out, report = I.expand_published(pub, lambda *a: b'<datasource><connection class="snowflake"/></datasource>')
        self.assertEqual(report[0]['state'], 'DEFINITION_REFUSED')

    @unittest.skipUnless(CAMPAIGN, 'corpus workbook')
    def test_package_is_rewritten_with_the_expanded_workbook(self):
        raw = Path(CAMPAIGN[0]).read_bytes()
        pub, tds, name = publish(twb_of(CAMPAIGN[0]))
        new = I.rewrite_package(raw, 'x.twbx', pub)
        I.check_archive(new, 'x.twbx')
        self.assertEqual(twb_of(io.BytesIO(new)), pub)
        with zipfile.ZipFile(io.BytesIO(raw)) as a, zipfile.ZipFile(io.BytesIO(new)) as b:
            self.assertEqual(sorted(a.namelist()), sorted(b.namelist()))
        self.assertEqual(I.rewrite_package(TWB, 'x.twb', pub), pub)


class Policy(unittest.TestCase):
    @unittest.skipUnless(CAMPAIGN, 'corpus workbook')
    def test_policy_and_snapshot_lineage(self):
        raw = Path(CAMPAIGN[0]).read_bytes()
        m = I.check_archive(raw, 'c.twbx')
        self.assertTrue(m['repository'].get('revision'))
        self.assertTrue(any(e.get('refreshed') for e in m['extracts']))
        rows = I.source_policy(ET.fromstring(twb_of(CAMPAIGN[0])))
        modes = {r['Mode'] for r in rows}
        self.assertIn('Extract over live Snowflake', modes)
        self.assertTrue(all(r['App reads'] for r in rows))

    def test_modes(self):
        x = (b"<workbook><datasources>"
             b"<datasource name='a' caption='A'><connection class='sqlproxy' dbname='P' server='s'/></datasource>"
             b"<datasource name='b' caption='B'><connection class='federated'><named-connections><named-connection name='n'>"
             b"<connection class='sqlserver' server='db'/></named-connection></named-connections></connection></datasource>"
             b"<datasource name='c' caption='C'><connection class='federated'><named-connections><named-connection name='n'>"
             b"<connection class='textscan' filename='x.csv'/></named-connection></named-connections></connection></datasource>"
             b"</datasources></workbook>")
        rows = {r['Datasource']: r for r in I.source_policy(ET.fromstring(x))}
        self.assertEqual(rows['A']['Mode'], 'Published datasource')
        self.assertEqual(rows['B']['Mode'], 'Live, not Snowflake')
        self.assertIn('replicate', rows['B']['App reads'].lower())
        self.assertEqual(rows['C']['Mode'], 'File')


class Views(unittest.TestCase):
    def test_view_ddl_from_a_verified_plan(self):
        import v3_model_binding as MB
        ds = ET.fromstring(CUSTOM_SQL_DS)
        plan = MB.build_live(ds)
        sql = I.view_sql(plan, 'Orders + Category', 'WBR_DB.V3_SOURCES')
        self.assertTrue(sql.startswith('CREATE VIEW IF NOT EXISTS WBR_DB.V3_SOURCES.V3_SRC_ORDERS_CATEGORY_'))
        self.assertIn(' AS ' + MB.compile_query(plan), sql)
        self.assertNotIn('OR REPLACE', sql.upper())
        with self.assertRaises(ValueError):
            I.view_sql(plan, 'x', "WBR_DB.S; DROP")
        with self.assertRaises(ValueError):
            I.view_sql({'version': 'nope'}, 'x', 'WBR_DB.S')
        self.assertEqual(I.view_name(plan, 'Orders + Category', 'WBR_DB.V3_SOURCES'), sql.split()[5])

    def test_bind_views_needs_a_verified_view(self):
        calls = []

        class S:
            def sql(self, text, params=None):
                calls.append(text)

                class R:
                    def collect(_):
                        if text.startswith('DESCRIBE'):
                            return [{'NAME': 'A', 'TYPE': 'VARCHAR(10)'}]
                        return []
                return R()
        plan = {'version': 'custom-sql/1', 'datasource_id': 'd'}
        fields = [{'physical': 'A', 'datatype': 'string', 'is_calculation': False, 'internal': 'A'}]
        out = I.bind_view(S(), 'DB.S.V3_SRC_X_1', fields)
        self.assertEqual(out, {'state': 'BOUND', 'table': 'DB.S.V3_SRC_X_1'})
        out = I.bind_view(S(), 'DB.S.V3_SRC_X_1', [dict(fields[0], physical='B')])
        self.assertTrue(out['state'].startswith('NOT_BOUND'))
        self.assertTrue(I.bind_view(None, 'DB.S.V', fields)['state'] == 'NO_SNOWFLAKE_SESSION')


class Wiring(unittest.TestCase):
    def test_intake_and_console_use_it(self):
        self.assertIn('v3_intake', (ROOT / 'v3_local.py').read_text(encoding='utf-8'))
        self.assertIn('v3_intake', (ROOT / 'v3_console.py').read_text(encoding='utf-8'))
        self.assertIn('def download_datasource_bytes', (ROOT / 'tableau_server.py').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
