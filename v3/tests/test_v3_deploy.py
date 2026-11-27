"""Approved per-workbook deployment through the original deploy path: validated package only,
hash-named app in the V3 schema, no data statements."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import v3_deploy as D
import v3_package as P
from test_v3_tables import fixture, parse


class _Result:
    def collect(self):
        return [{'O': 'ORG1', 'A': 'ACCT1', 'name': 'TABLEAU_ACCELERATOR_V3'}]


class _File:
    def __init__(self):
        self.puts = []

    def put(self, path, location, auto_compress=True, overwrite=False):
        self.puts.append((Path(path).name, location, auto_compress, overwrite))


class _Session:
    def __init__(self):
        self.sql_text, self.file = [], _File()

    def sql(self, text):
        self.sql_text.append(text)
        return _Result()

    def get_current_warehouse(self):
        return '"POWERHOUSE"'


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.ir = parse(fixture(), self.folder.name)
        self.bindings = {cap: 'DB.SCHEMA.T' for cap in self.ir['datasources']}

    def tearDown(self):
        self.folder.cleanup()

    def test_package_identity_is_content_hash_and_excludes_cli_manifest(self):
        data, build = P.workbook_bundle(self.ir, self.bindings)
        name, manifest_build, files = D.package_files(data)
        self.assertTrue(name.startswith('V3_WORKBOOK_'))
        self.assertEqual(manifest_build, build)
        self.assertIn('app.py', files)
        self.assertNotIn('snowflake.yml', files)

    def test_uses_original_deploy_function_with_the_verified_package(self):
        captured = {}

        def fake(session, stem, app_file, **kw):
            root = Path(kw['root_dir'])
            captured.update(stem=stem, app=app_file, kw=kw,
                            files={p.name: p.read_bytes() for p in root.iterdir()})
            return {'identifier': f"{kw['db']}.{kw['schema']}.{kw['identifier']}", 'root_location': '@x',
                    'files': ['app.py'] + kw['runtime_modules'] + ['datasources.json'], 'url': 'https://u'}
        with patch('pipeline.deploy_streamlit_app', side_effect=fake):
            record = D.deploy_workbook(object(), self.ir, self.bindings, 'Renamed title')
        data, _ = P.workbook_bundle(self.ir, self.bindings)
        name, build, files = D.package_files(data)
        self.assertEqual(captured['files'], files)
        self.assertEqual((captured['stem'], captured['app']), (name, 'app.py'))
        self.assertEqual((captured['kw']['db'], captured['kw']['schema']), ('WBR_DB', 'TABLEAU_ACCELERATOR_V3'))
        self.assertEqual(captured['kw']['datasources'], {})
        self.assertEqual(sorted(captured['kw']['runtime_modules'] + ['app.py']), sorted(files))
        self.assertEqual(record['build_sha256'], build)
        self.assertEqual(record['identifier'], 'WBR_DB.TABLEAU_ACCELERATOR_V3.' + name)

    def test_end_to_end_through_original_function_stages_files_and_creates_app_only(self):
        session = _Session()
        record = D.deploy_workbook(session, self.ir, self.bindings, "Owner's dashboard")
        data, _ = P.workbook_bundle(self.ir, self.bindings)
        _, _, files = D.package_files(data)
        self.assertEqual(sorted(p[0] for p in session.file.puts), sorted(list(files) + ['datasources.json']))
        statements = ' '.join(session.sql_text).upper()
        self.assertIn('CREATE OR REPLACE STREAMLIT "WBR_DB"."TABLEAU_ACCELERATOR_V3"."V3_WORKBOOK_', statements)
        for forbidden in ('CREATE TABLE', 'CREATE OR REPLACE TABLE', 'CREATE VIEW', 'INSERT ', 'DELETE ',
                          'DROP ', 'TABLEAU_TO_SIS_V3_CANDIDATE', 'CREATE DATABASE'):
            self.assertNotIn(forbidden, statements)
        self.assertFalse(any(t.strip().upper().startswith('USE ') for t in session.sql_text))
        self.assertTrue(record['identifier'].startswith('WBR_DB.TABLEAU_ACCELERATOR_V3.V3_WORKBOOK_'))
        self.assertEqual(record['acceptance'], 'NOT_VERIFIED')

    def test_invalid_bindings_never_reach_deploy(self):
        with patch('pipeline.deploy_streamlit_app') as deploy:
            with self.assertRaises(ValueError):
                D.deploy_workbook(object(), self.ir, {cap: 'not a table' for cap in self.ir['datasources']}, 'Any')
            deploy.assert_not_called()

    def test_no_session_refuses(self):
        with self.assertRaises(RuntimeError):
            D.deploy_workbook(None, self.ir, self.bindings, 'Any')


class StandaloneViewTests(unittest.TestCase):
    def test_standalone_view_renders_selected_dashboard_with_reset(self):
        from streamlit.testing.v1 import AppTest
        root = str(Path(__file__).resolve().parents[1])
        script = f"""
import sys, io, zipfile
sys.path.insert(0, {root!r}); sys.path.insert(0, {root + '/tests'!r})
from test_v3_local import XML, CSV
from v3_local import prepare_payload, preview_context
import v3_runtime
buf = io.BytesIO()
with zipfile.ZipFile(buf, 'w') as z:
    z.writestr('renamed.twb', XML); z.writestr('readings.csv', CSV)
result = prepare_payload(buf.getvalue(), 'renamed.twbx')
with preview_context(result['ir'], result['bindings'], snowflake=False):
    v3_runtime.render_workbook(result['ir'])
"""
        at = AppTest.from_string(script, default_timeout=60).run()
        self.assertFalse(at.exception)
        self.assertTrue(any(b.label == 'Reset workbook controls' for b in at.button))
        self.assertTrue(at.get('vega_lite_chart') or at.get('arrow_vega_lite_chart'))


if __name__ == "__main__":
    unittest.main()


class AppNameTests(unittest.TestCase):
    def test_readable_names_are_plain_identifiers_and_reserved_names_refused(self):
        self.assertEqual(D.app_identifier('ECommerce Dashboard'), 'ECOMMERCE_DASHBOARD')
        self.assertEqual(D.app_identifier("Owner's sales-board"), 'OWNERS_SALESBOARD')
        for bad in ('', 'x', 'TABLEAU_TO_SIS_V3_CANDIDATE', 'a; DROP TABLE t'):
            if bad == 'a; DROP TABLE t':
                self.assertEqual(D.app_identifier(bad), 'A_DROP_TABLE_T')
                continue
            with self.assertRaises(ValueError):
                D.app_identifier(bad)
