"""Corpus census (6 Nov): Tableau clusters (World Indicators, 4 sheets). Tableau saves a cluster result as a
categorical-bin group with a `default` label ("Not Clustered") for members in no bin. Two general defects:
  * extract_groups ignored `default`: ungrouped members kept their own value instead of the default label;
  * the comparison path resolved a field the renderer does not consume (e.g. a group on Color) by caption only; the
    caption equalled a hidden column's physical name, so the query read a column that does not exist.
The plan path now binds every plan field by its occurrence identity (field_id). Written BEFORE the fix."""
from pathlib import Path
import sys
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import tableau_parser as TP  # noqa: E402

XML = """<workbook><datasources><datasource><column caption='{cap}' name='[G]'>
<calculation class='categorical-bin' column='[Country]' {attr} new-bin='true'>
<bin value='&quot;Low&quot;'><value>&quot;A&quot;</value></bin></calculation></column></datasource></datasources></workbook>"""


class GroupDefault(unittest.TestCase):
    def sql(self, attr):
        return next(iter(TP.extract_groups(ET.fromstring(XML.format(cap='G1', attr=attr))).values()))

    def test_default_label_for_ungrouped_members(self):
        self.assertTrue(self.sql("default='&quot;Not Clustered&quot;'").endswith("ELSE 'Not Clustered' END"))

    def test_other_and_plain_groups_unchanged(self):
        self.assertTrue(self.sql("use-other='true'").endswith("ELSE 'Other' END"))
        self.assertTrue(self.sql('').endswith('ELSE COUNTRY END'))

    def test_quote_in_default_is_escaped(self):
        self.assertTrue(self.sql("default='&quot;O&apos;Neil&quot;'").endswith("ELSE 'O''Neil' END"))


class ClusterSheets(unittest.TestCase):
    def test_world_indicators_cluster_sheets_run_and_show_cluster_labels(self):
        import engine
        import v3_sheet_plan as P
        from v3_local import prepare_payload, preview_context
        p = ROOT / 'Workbooks' / 'World_Indicators_Tableau2024_3.twbx'
        r = prepare_payload(p.read_bytes(), p.name)
        ir = r['ir']
        sheets = [s for d in ir['dashboards'] for s in d['sheets'] if s['name'] in
                  ('Biz Tax rate', 'Days to Start Biz', 'Hours to do Taxes', 'Ease of Biz Correlation Sheet')]
        self.assertEqual(len(sheets), 4)
        labels = {'Friendly', 'Low', 'Moderate', 'Very Friendly', 'Not Clustered'}
        with preview_context(ir, dict(r['bindings'])):
            for s in sheets:
                df = P.execute(P.from_sheet(s, ir), s, engine)
                col = next(c for c in df.columns if 'clusters' in c)
                self.assertTrue(len(df) and set(df[col].dropna()) <= labels, (s['name'], set(df[col].dropna())))


if __name__ == '__main__':
    unittest.main()
