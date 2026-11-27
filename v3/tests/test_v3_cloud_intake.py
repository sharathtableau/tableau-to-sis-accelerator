"""Real V3 parsing and UI with mocked Cloud transport, not live Cloud evidence."""
from pathlib import Path
import io
import sys
import unittest
from unittest.mock import patch
import zipfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import v3_local
import tableau_parser
from test_v3_local import XML, CSV
from streamlit.testing.v1 import AppTest


class CloudIntakeTests(unittest.TestCase):
    def test_cloud_and_upload_share_scoped_hash_named_intake_for_xml_and_zip(self):
        zipped=io.BytesIO()
        with zipfile.ZipFile(zipped,'w') as archive:
            archive.writestr('book.twb',XML)
            archive.writestr('readings.csv',CSV)
        for payload in (XML,zipped.getvalue()):
            with self.subTest(packaged=payload!=XML), patch('tableau_server.fetch_workbook',return_value={
                    'filename':'misleading.twbx','bytes':payload,'workbook_id':'id-1'}),\
                    patch('pipeline.onboard') as onboard,\
                    patch.object(tableau_parser,'build_ir',wraps=tableau_parser.build_ir) as parser:
                cloud=v3_local.prepare_cloud('https://example.test/workbook')
                uploaded=v3_local.prepare_payload(payload,'different-name.twb')
                self.assertEqual(cloud['assessment'],uploaded['assessment'])
                self.assertEqual(cloud['ir']['fields'],uploaded['ir']['fields'])
                self.assertEqual(cloud['digest'],uploaded['digest'])
                self.assertEqual(cloud['origin'],'tableau-cloud')
                for call in parser.call_args_list:
                    self.assertTrue(call.kwargs['scoped'])
                    self.assertEqual(Path(call.args[0]).stem,cloud['digest'])
                onboard.assert_not_called()

    def test_invalid_cloud_payload_never_reaches_parser(self):
        with patch('tableau_server.fetch_workbook',return_value={
                'filename':'book.twbx','bytes':b'<html>error</html>','workbook_id':'id'}),\
                patch.object(tableau_parser,'build_ir') as parser:
            with self.assertRaises(Exception): v3_local.prepare_cloud('https://example.test/workbook')
            parser.assert_not_called()

    def test_cloud_app_assessment_and_failed_retry_clears_previous_result(self):
        app=Path(__file__).resolve().parents[1]/'v3_app.py'
        archive_bytes=io.BytesIO()
        with zipfile.ZipFile(archive_bytes,'w') as archive:
            archive.writestr('book.twb',XML)
            archive.writestr('readings.csv',CSV)
        with patch('tableau_server.fetch_workbook',return_value={
                'filename':'remote.twbx','bytes':archive_bytes.getvalue(),'workbook_id':'id'}) as fetch:
            at=AppTest.from_file(str(app),default_timeout=30).run()
            at.radio[0].set_value('Tableau Cloud').run()
            at.radio[1].set_value('Direct workbook or view link').run()
            at.text_input[0].set_value('https://example.test/workbook').run()
            at.button[0].click().run()
            self.assertFalse(at.exception)
            self.assertTrue(any('Assessment:' in item.value for item in at.subheader))
            at.checkbox[0].check().run()
            self.assertFalse(at.exception)
            self.assertFalse(at.error)
            self.assertTrue((at.get('vega_lite_chart') or at.get('arrow_vega_lite_chart')))
            self.assertFalse(any('could not render' in w.value for w in at.warning))
            fetch.side_effect=RuntimeError('Transport failed')
            at.button[0].click().run()
            self.assertFalse(at.exception)
            self.assertTrue(at.error)
            self.assertFalse(any('Assessment:' in item.value for item in at.subheader))


    def test_selected_id_uses_shared_scoped_payload_boundary(self):
        with patch('tableau_server.fetch_workbook_by_id', return_value={
                'filename':'remote.twb','bytes':XML,'workbook_id':'id-2'}) as fetch, patch('pipeline.onboard') as onboard:
            result=v3_local.prepare_cloud_workbook('https://example.test','site','id-2','Same name')
            fetch.assert_called_once_with('https://example.test','site','id-2',name_hint='Same name')
            self.assertEqual(result['cloud_workbook_id'],'id-2')
            self.assertEqual(result['cloud_site_content_url'],'site')
            self.assertEqual(result['parser_mode'],'scoped')
            onboard.assert_not_called()

    def test_site_browse_duplicate_names_selection_retry_and_scope_reset(self):
        catalog={'projects':[], 'workbooks':[
            {'id':'id-1','name':'Same','project_id':'p1','project_name':'Same project'},
            {'id':'id-2','name':'Same','project_id':'p2','project_name':'Same project'}]}
        with patch('tableau_server.list_site_contents',return_value=catalog) as listing, patch(
                'tableau_server.fetch_workbook_by_id',return_value={
                    'filename':'remote.twb','bytes':XML,'workbook_id':'id-2'}) as fetch:
            at=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'v3_app.py'),default_timeout=30).run()
            at.radio[0].set_value('Tableau Cloud').run()
            at.text_input[0].set_value('https://prod-useast-b.online.tableau.com/#/site/b360bi').run()
            at.button[0].click().run()
            listing.assert_called_once_with(server_url='https://prod-useast-b.online.tableau.com',site_content_url='b360bi')
            self.assertEqual(len(at.selectbox[0].options),3)
            at.selectbox[0].set_value('p2').run()
            self.assertEqual(len(at.selectbox[1].options),1)
            at.selectbox[1].set_value('id-2').run()
            at.button[1].click().run()
            self.assertFalse(at.exception)
            fetch.assert_called_once_with('https://prod-useast-b.online.tableau.com','b360bi','id-2',name_hint='Same')
            self.assertTrue(any('Assessment:' in s.value for s in at.subheader))
            fetch.side_effect=RuntimeError('download failed')
            at.button[1].click().run()
            self.assertTrue(at.error)
            self.assertFalse(at.subheader)
            fetch.side_effect=None
            at.button[1].click().run()
            at.selectbox[0].set_value(None).run()
            self.assertFalse(at.subheader)
            listing.side_effect=RuntimeError('sign in failed')
            at.button[0].click().run()
            self.assertTrue(at.error)
            self.assertFalse(at.selectbox)
            listing.side_effect=None
            at.button[0].click().run()
            at.text_input[0].set_value('https://example.test/#/site/other').run()
            self.assertFalse(at.selectbox)
            self.assertFalse(at.subheader)
            self.assertFalse(at.exception)

    def test_empty_site_and_mode_switch_clear_catalog(self):
        with patch('tableau_server.list_site_contents',return_value={'projects':[],'workbooks':[]}):
            at=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'v3_app.py')).run()
            at.radio[0].set_value('Tableau Cloud').run()
            at.text_input[0].set_value('https://example.test/#/site/empty').run()
            at.button[0].click().run()
            self.assertTrue(any('No workbooks' in i.value for i in at.info))
            self.assertFalse(at.selectbox)
            at.radio[1].set_value('Direct workbook or view link').run()
            self.assertFalse(at.success)
            self.assertFalse(at.exception)


if __name__=='__main__': unittest.main()
