"""Local app integration without Snowflake or Tableau network calls."""
from copy import deepcopy
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
import engine
import v3_local
from streamlit.testing.v1 import AppTest

XML = b'''<workbook><datasources><datasource name="federated.fresh" caption="Unseen readings">
<connection class="textscan" filename="readings.csv"/>
<column name="[Location]" role="dimension" datatype="string"/>
<column name="[Reading]" role="measure" datatype="real"/>
</datasource></datasources><worksheets><worksheet name="Measurements"><table>
<datasource-dependencies datasource="federated.fresh">
<column name="[Location]" role="dimension" datatype="string"/>
<column name="[Reading]" role="measure" datatype="real"/>
</datasource-dependencies><cols>[federated.fresh].[sum:Reading:qk]</cols>
<rows>[federated.fresh].[none:Location:nk]</rows><panes><pane><mark class="Bar"/></pane></panes>
</table></worksheet></worksheets><dashboards><dashboard name="New dashboard">
<zones><zone name="Measurements" x="0" y="0" w="100000" h="100000"/></zones>
</dashboard></dashboards></workbook>'''
CSV = b'Location,Reading\nNorth,3\nSouth,7\nNorth,5\n'


class Upload(io.BytesIO):
    def __init__(self, raw, name):
        super().__init__(raw)
        self.name = name


class LocalTests(unittest.TestCase):
    def test_no_corpus_datasource_reuse_and_state_restored(self):
        before = deepcopy(config.DATASOURCES)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'unseen.twb'
            path.write_bytes(XML)
            result = v3_local.prepare(path, XML)
            self.assertEqual(result['bindings'], {'Unseen readings': None})
            data = Path(folder) / 'readings.csv'
            data.write_bytes(CSV)
            with v3_local.preview_context(result['ir'], {'Unseen readings': str(data)}):
                self.assertEqual(set(config.DATASOURCES), {'Unseen readings'})
                values = engine.q('SELECT SUM(READING) AS V FROM V3_LOCAL.PREVIEW.UNSEEN_READINGS')
                self.assertEqual(values.iloc[0, 0], 15)
            self.assertEqual(config.DATASOURCES, before)

    def test_bundled_duplicate_filenames_are_refused(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('book.twb', XML)
            archive.writestr('one/readings.csv', CSV)
            archive.writestr('two/readings.csv', b'Location,Reading\nOther,999\n')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'duplicate.twbx'
            path.write_bytes(buffer.getvalue())
            result = v3_local.prepare(path, buffer.getvalue())
            self.assertTrue(result['intake_errors'])
            self.assertFalse(result['bindings']['Unseen readings'])

    def test_real_app_upload_assessment_and_preview(self):
        def upload(label, **kwargs):
            return Upload(XML, 'never-seen.twb') if label == 'Tableau workbook' else Upload(CSV, 'readings.csv')
        app = Path(__file__).resolve().parents[1] / 'v3_app.py'
        with patch('streamlit.file_uploader', side_effect=upload):
            at = AppTest.from_file(str(app), default_timeout=30).run()
            self.assertFalse(at.exception)
            at.button[0].click().run()
            self.assertFalse(at.exception)
            self.assertTrue(any('Assessment: NEEDS REVIEW' in h.value for h in at.subheader))
            at.checkbox[0].check().run()
            self.assertFalse(at.exception)
            self.assertFalse(at.error)
            self.assertFalse(any('could not render' in w.value for w in at.warning))


if __name__ == '__main__':
    unittest.main()
