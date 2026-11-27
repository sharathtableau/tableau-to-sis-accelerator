"""DM1: bundled data files that share a basename in different folders of the .twbx. Written BEFORE the fix.

Superstore.twbx carries Data/en_US-US/Sample - Superstore.xls (the 'Orders' datasource) AND
Data/Superstore/Sample - Superstore.xls (the 'Sample - Superstore' datasource). Every connection names its exact
archive path, but the extraction flattened files to basenames, so the whole workbook was refused. Now each
datasource binds to the file at its own archive path; only a reference that is still ambiguous (a bare basename
that occurs twice) refuses."""
import io
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))


def twbx(files, connections):
    ds = ''.join(f"<datasource caption='{cap}' name='ds{i}'><connection class='textscan' {attrs} /></datasource>"
                 for i, (cap, attrs) in enumerate(connections))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('w.twb', f"<workbook><datasources>{ds}</datasources></workbook>")
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


class Unit(unittest.TestCase):
    def test_each_datasource_gets_its_own_archive_path(self):
        import pipeline
        raw = twbx({'Data/A/x.csv': 'v\n1\n', 'Data/B/x.csv': 'v\n2\n', 'Data/B/y.csv': 'v\n3\n'},
                   [('First', "directory='Data/A' filename='x.csv'"), ('Second', "filename='Data/B/x.csv'"),
                    ('Third', "filename='y.csv'"), ('Bare', "filename='x.csv'")])
        out = Path(tempfile.mkdtemp())
        files, hypers = pipeline.bundled_data_files(raw, str(out))
        root = ET.fromstring(zipfile.ZipFile(io.BytesIO(raw)).read('w.twb'))
        got = pipeline.match_files_to_datasources(root, files)
        self.assertEqual(Path(got['First']).read_text().split()[1], '1')
        self.assertEqual(Path(got['Second']).read_text().split()[1], '2')
        self.assertEqual(Path(got['Third']).read_text().split()[1], '3')      # unique basename still matches
        self.assertIsNone(got['Bare'])                                         # ambiguous: never guessed


class Corpus(unittest.TestCase):
    def check(self, name):
        import engine
        import plan_census as PC
        import v3_sheet_plan as P
        from v3_local import preview_context
        r = PC.intake(ROOT / 'Workbooks' / name)
        self.assertNotIn('Bundled data contains duplicate filenames; explicit binding is required.', r['intake_errors'])
        b = r['bindings']
        self.assertTrue(str(b['Orders (Sample - Superstore)']).replace('\\', '/').count('en_US-US'))
        self.assertTrue(b['Sales Commission'] and b['Sales Target'])
        ir = r['ir']
        ran = total = 0
        with preview_context(ir, {k: v for k, v in b.items() if v}):
            for s in {s['name']: s for d in ir['dashboards'] for s in d['sheets']
                      if b.get(ir['field_scopes'][s['datasource_id']]['caption'])}.values():
                total += 1
                ran += 1 if len(P.execute(P.from_sheet(s, ir), s, engine)) else 0
        self.assertGreater(total, 0)
        return ran, total

    def test_superstore_and_r1(self):
        for name in ('Superstore.twbx', 'R1_Test_Upload_-_Superstore.twbx'):
            ran, total = self.check(name)
            self.assertEqual(ran, total, name)


if __name__ == '__main__':
    unittest.main()
