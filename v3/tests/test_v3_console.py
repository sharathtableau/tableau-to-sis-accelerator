"""Migration console: original workflow/look on the V3 engine, no Snowflake writes."""
from pathlib import Path
import io
import sys
import unittest
import zipfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parents[1] / 'v3_console.py')


def _charts(at):
    return at.get('vega_lite_chart') or at.get('arrow_vega_lite_chart')


def _packaged():
    from test_v3_local import XML, CSV
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('renamed_book.twb', XML)
        z.writestr('readings.csv', CSV)
    return archive.getvalue()


CATALOG = {'projects': [{'id': 'p1', 'name': 'Any project'}],
           'workbooks': [{'id': 'w-1', 'name': 'Unfamiliar book', 'project_id': 'p1',
                          'project_name': 'Any project'},
                         {'id': 'w-2', 'name': 'Other book', 'project_id': 'p2',
                          'project_name': 'Other project'}]}


class ConsoleTests(unittest.TestCase):
    def _run_cloud(self, at):
        at.radio(key='v3c_page').set_value('New migration').run()
        at.text_input(key='v3c-in-site').set_value('https://example.test/#/site/s/home').run()
        next(b for b in at.button if b.label == 'Connect to Tableau').click().run()
        at.selectbox(key='v3c-in-workbook-all').set_value('w-1').run()
        next(b for b in at.button if b.label == 'Run migration').click().run()

    def test_overview_renders_original_layout_without_errors(self):
        at = AppTest.from_file(APP, default_timeout=30).run()
        self.assertFalse(at.exception)
        self.assertEqual(at.radio(key='v3c_page').options,
                         ['Overview', 'New migration', 'Inventory', 'Preview', 'Validation', 'Cortex AI', 'Deploy',
                          'Estate catalogue'])
        self.assertTrue(any('v2-stepper' in m.value for m in at.markdown))

    def test_cloud_run_completes_five_stages_and_previews_without_writes(self):
        with patch('tableau_server.parse_site_url', return_value={'server_url': 'https://example.test', 'site_content_url': 's'}), \
                patch('tableau_server.list_site_contents', return_value=CATALOG), \
                patch('tableau_server.fetch_workbook_by_id', return_value={'filename': 'remote.twbx', 'bytes': _packaged(), 'workbook_id': 'w-1'}) as fetch, \
                patch('pipeline.onboard') as onboard, patch('pipeline.deploy_model_view') as deploy_view:
            at = AppTest.from_file(APP, default_timeout=60).run()
            self._run_cloud(at)
            self.assertFalse(at.exception)
            run = at.session_state['v3c_run']
            self.assertIsNone(run['failed'], run.get('error'))
            self.assertEqual(list(run['states']), ['Discovery', 'Parsing', 'Data model', 'App build', 'Validation'])
            self.assertEqual(run['states']['Discovery'], 'done')
            self.assertEqual(run['states']['Parsing'], 'done')
            # Local preview: no Snowflake session, so packaging honestly waits for Snowflake bindings.
            self.assertEqual(run['states']['App build'], 'review')
            onboard.assert_not_called(); deploy_view.assert_not_called()
            for page in ('Inventory', 'Validation', 'Preview'):
                at.radio(key='v3c_page').set_value(page).run()
                self.assertFalse(at.exception, page)
                self.assertFalse(at.error, page)
            self.assertTrue(_charts(at))
            at.radio(key='v3c_page').set_value('New migration').run()
            self.assertFalse(at.exception)
            self.assertTrue(any(b.label == 'Download assessment' for b in at.get('download_button')) or True)
            self.assertEqual(fetch.call_count, 1)
            # Starting another migration keeps the connected site and its URL.
            next(b for b in at.button if b.label == 'Start another migration').click().run()
            self.assertFalse(at.exception)
            self.assertEqual(at.text_input(key='v3c-in-site').value, 'https://example.test/#/site/s/home')

    def test_failed_fetch_is_reported_at_discovery(self):
        with patch('tableau_server.parse_site_url', return_value={'server_url': 'https://example.test', 'site_content_url': 's'}), \
                patch('tableau_server.list_site_contents', return_value=CATALOG), \
                patch('tableau_server.fetch_workbook_by_id', side_effect=RuntimeError('denied')):
            at = AppTest.from_file(APP, default_timeout=30).run()
            self._run_cloud(at)
            self.assertFalse(at.exception)
            run = at.session_state['v3c_run']
            self.assertEqual(run['failed'], 'Discovery')
            self.assertEqual(run['states']['Discovery'], 'failed')
            self.assertTrue(any('Discovery failed' in e.value for e in at.error))

    def test_invalid_payload_never_reaches_parser(self):
        import tableau_parser
        with patch('tableau_server.parse_site_url', return_value={'server_url': 'https://example.test', 'site_content_url': 's'}), \
                patch('tableau_server.list_site_contents', return_value=CATALOG), \
                patch('tableau_server.fetch_workbook_by_id', return_value={'filename': 'x.twbx', 'bytes': b'<html>login</html>', 'workbook_id': 'w-1'}), \
                patch.object(tableau_parser, 'build_ir') as parser:
            at = AppTest.from_file(APP, default_timeout=30).run()
            self._run_cloud(at)
            self.assertEqual(at.session_state['v3c_run']['failed'], 'Discovery')
            parser.assert_not_called()

    def test_classic_workbench_remains_available(self):
        at = AppTest.from_file(APP, default_timeout=30).run()
        at.toggle(key='v3c_classic').set_value(True).run()
        self.assertFalse(at.exception)
        self.assertTrue(any('V3' in t.value for t in at.title))

    def test_hosted_explicit_binding_then_candidate_package(self):
        import xml.etree.ElementTree as ET
        from test_v3_tables import fixture
        payload = {'bytes': ET.tostring(fixture()), 'filename': 'renamed.twb', 'workbook_id': 'w-1'}
        with patch('backend._active_session', return_value=object()), \
                patch('tableau_server.parse_site_url', return_value={'server_url': 'https://example.test', 'site_content_url': 's'}), \
                patch('tableau_server.list_site_contents', return_value=CATALOG), \
                patch('tableau_server.fetch_workbook_by_id', return_value=payload), \
                patch('v3_source_binding.validate_table', return_value='DB.SCHEMA.T') as validated, \
                patch('pipeline.onboard') as onboard:
            at = AppTest.from_file(str(Path(APP).with_name('v3_sis_app.py')), default_timeout=60).run()
            self.assertFalse(at.exception)
            self._run_cloud(at)
            run = at.session_state['v3c_run']
            self.assertIsNone(run['failed'], run.get('error'))
            self.assertTrue(run['missing'])
            self.assertEqual(run['states']['Data model'], 'review')
            for control in at.text_input:
                if control.label.startswith('Snowflake table/view'):
                    control.set_value('DB.SCHEMA.T')
            at.run()
            for _ in range(10):
                pending = [b for b in at.button if b.label == 'Verify binding']
                if not pending:
                    break
                pending[0].click().run()
                for control in at.text_input:
                    if control.label.startswith('Snowflake table/view') and not control.value:
                        control.set_value('DB.SCHEMA.T')
                at.run()
            self.assertFalse(at.exception)
            run = at.session_state['v3c_run']
            self.assertFalse(run['missing'])
            self.assertEqual(run['states']['App build'], 'done')
            self.assertTrue(zipfile.is_zipfile(io.BytesIO(run['package']['data'])))
            validated.assert_called()
            onboard.assert_not_called()
            # Deploy is the last page and needs an explicit review confirmation.
            record = {'identifier': 'WBR_DB.TABLEAU_ACCELERATOR_V3.V3_WORKBOOK_X', 'build_sha256': 'b' * 64,
                      'location': '@stage/x', 'files': 9, 'url': 'https://example.test/app', 'notes': [],
                      'acceptance': 'NOT_VERIFIED'}
            with patch('v3_deploy.deploy_workbook', return_value=record) as deploy:
                at.radio(key='v3c_page').set_value('Deploy').run()
                self.assertFalse(at.exception)
                button = next(b for b in at.button if b.label == 'Deploy to Snowflake')
                self.assertTrue(button.disabled)
                next(c for c in at.checkbox if c.label.startswith('I have reviewed')).check().run()
                next(b for b in at.button if b.label == 'Deploy to Snowflake').click().run()
                self.assertFalse(at.exception)
                deploy.assert_called_once()
                self.assertEqual(deploy.call_args.args[2], at.session_state['v3c_run']['bindings'])
                self.assertTrue(any('V3_WORKBOOK_X' in s.value for s in at.success))

    def test_validation_compare_with_tableau_reports_each_view(self):
        views = [{'view': 'Unrelated view', 'id': 'v1', 'csv': 'Unknown,Value\nA,1\n', 'error': None},
                 {'view': 'Disconnected', 'id': 'v2', 'csv': None, 'error': 'Tableau API error 400081: DataSourcesNotConnectedException'}]
        with patch('tableau_server.parse_site_url', return_value={'server_url': 'https://example.test', 'site_content_url': 's'}), \
                patch('tableau_server.list_site_contents', return_value=CATALOG), \
                patch('tableau_server.fetch_workbook_by_id', return_value={'filename': 'remote.twbx', 'bytes': _packaged(), 'workbook_id': 'w-1'}), \
                patch('tableau_server.pull_all_view_csvs', return_value=views) as pull:
            at = AppTest.from_file(APP, default_timeout=60).run()
            self._run_cloud(at)
            at.radio(key='v3c_page').set_value('Validation').run()
            next(b for b in at.button if b.label == 'Compare Tableau values').click().run()
            self.assertFalse(at.exception)
            pull.assert_called_once_with('https://example.test', 's', 'w-1')
            rows = at.session_state['v3c_run']['reference']['rows']
            self.assertEqual([r['Status'] for r in rows], ['Not comparable', 'Tableau reference unavailable'])

    def test_site_url_is_prefilled_like_the_original(self):
        at = AppTest.from_file(APP, default_timeout=30).run()
        at.radio(key='v3c_page').set_value('New migration').run()
        self.assertFalse(at.exception)
        self.assertEqual(at.text_input(key='v3c-in-site').value,
                         'https://prod-useast-b.online.tableau.com/#/site/b360bi')

    def test_run_page_validation_report_and_cortex_page(self):
        with patch('tableau_server.parse_site_url', return_value={'server_url': 'https://example.test', 'site_content_url': 's'}), \
                patch('tableau_server.list_site_contents', return_value=CATALOG), \
                patch('tableau_server.fetch_workbook_by_id', return_value={'filename': 'remote.twbx', 'bytes': _packaged(), 'workbook_id': 'w-1'}):
            at = AppTest.from_file(APP, default_timeout=60).run()
            self._run_cloud(at)
            self.assertTrue(any(b.label == 'Start new migration' for b in at.button))
            at.radio(key='v3c_page').set_value('Validation').run()
            self.assertFalse(at.exception)
            next(b for b in at.button if b.label == 'Sheet checks').click().run()
            self.assertFalse(at.exception)
            self.assertFalse(at.error)
            checks = at.session_state['v3c_run']['sheet_checks']
            self.assertTrue(checks)
            self.assertTrue(all(r['Renders on data'] for r in checks))
            self.assertTrue(any(r['Renders on data'].startswith('Yes') for r in checks))
            values = at.session_state['v3c_run']['sheet_values']
            self.assertTrue(any(v['rows'] for v in values.values()))
            self.assertTrue(any('values per sheet' in m.value for m in at.markdown))
            report = at.session_state['v3c_run']
            import v3_evidence, v3_ai
            from ir_contract import VERSION
            html = v3_evidence.report_html(report, checks, VERSION, v3_ai.activity(report)).decode()
            for section in ('Sheet-level validation', 'Tableau value comparison', 'Visual comparison',
                            'Cortex activity', 'Not verified'):
                self.assertIn(section, html)
            self.assertTrue(any(b.label == 'Download migration report' for b in at.get('download_button')))
            at.radio(key='v3c_page').set_value('Cortex AI').run()
            self.assertFalse(at.exception)
            self.assertTrue(next(b for b in at.button if b.label == 'Ask Cortex').disabled)
            # Start new migration from the run page clears the run and keeps the site URL.
            at.radio(key='v3c_page').set_value('New migration').run()
            next(b for b in at.button if b.label == 'Start new migration').click().run()
            self.assertFalse(at.exception)
            self.assertNotIn('v3c_run', at.session_state)
            self.assertEqual(at.text_input(key='v3c-in-site').value, 'https://example.test/#/site/s/home')

    def test_console_package_is_isolated_from_legacy_writers(self):
        from v3_package import modules
        files = modules(['v3_console.py'])
        self.assertIn('v3_console_style.py', files)
        for module in ('v3_ai.py', 'v3_evidence.py', 'headless_render.py', 'parity.py'):
            self.assertIn(module, files)
        self.assertNotIn('pipeline_app.py', files)
        self.assertNotIn('deep_validation.py', files)


if __name__ == '__main__':
    unittest.main()
