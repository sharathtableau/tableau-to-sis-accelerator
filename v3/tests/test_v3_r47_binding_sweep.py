"""R47c: binding across the corpus, not one report at a time. tools/binding_sweep.py runs every datasource through
the hosted path the app runs -- the Load button (v3_data_load.load) and the next run's automatic find -- over a
Snowflake stand-in on DuckDB. Before R47 it found, besides Superstore 2024.3 (prefixed object-id):
  * Tableau 2024+ feature-flag tags (<_.fcp.X.true...relation>, ...object-graph, ...column): read as plain tags,
    the legacy '.false...' twin dropped -- as a current Tableau reads the file (tableau_parser.normalize_feature_flags)
  * a relationship between two files (clara_media.csv + Calender Brazil.csv) read every table from the first file
  * two package folders holding a file of the same name (R1 Superstore: Data/en_US-US/ and Data/Superstore/) refused
    as 'not inside the workbook': matched by the authored path now
  * a live connection to a packaged .hyper (World Indicators) was 'no data in the workbook': it is an extract
  * a column with no value at all (BDO) got no type in the new table and its field could not bind
  * metadata naming its table only by <parent-name> had no owner in the loaded-tables model (as DM3 already did)
Each case below is a different workbook; every one loads and is found again on the next run."""
from pathlib import Path
import sys
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools')]
W = ROOT / 'Workbooks'
P = W / '_powerbi_project_sweep'


def hyper_api():
    try:
        import tableauhyperapi  # noqa: F401
        return True
    except ImportError:
        return False


class Flags(unittest.TestCase):
    def test_true_takes_the_plain_tag_and_the_false_twin_is_dropped(self):
        import tableau_parser as TP
        root = TP.parse_xml('<ds><connection>'
                            '<_.fcp.M.false...relation name="legacy" type="table"/>'
                            '<_.fcp.M.true...relation type="collection"><relation name="a" type="table"/>'
                            '</_.fcp.M.true...relation>'
                            '<_.fcp.Only.false...folder name="kept"/></connection>'
                            '<_.fcp.M.true...object-graph/></ds>')
        conn = root.find('connection')
        self.assertEqual([c.tag for c in conn], ['relation', 'folder'])
        self.assertEqual(conn.find('relation').get('type'), 'collection')
        self.assertIsNotNone(root.find('object-graph'))


class Sweep(unittest.TestCase):
    def outcome(self, path, cap, needs_hyper=False):
        if not path.exists():
            self.skipTest('corpus workbook')
        if needs_hyper and not hyper_api():
            self.skipTest('tableauhyperapi')
        import binding_sweep as B
        rows = {r['datasource']: r for r in B.sweep(path)}
        self.assertIn(cap, rows)
        self.assertEqual(rows[cap]['outcome'], 'LOADED_AND_FOUND', rows[cap])
        return rows[cap]

    def test_fcp_relationship_model(self):
        r = self.outcome(W / 'Superstore_Tableau2024_3.twbx', 'Sample - Superstore')
        self.assertEqual(r['load'], 'model 3 tables')

    def test_relationship_across_two_files(self):
        r = self.outcome(P / 'Clara Brazil EDA Dashboard.twbx', 'clara_media')
        self.assertEqual(r['load'], 'model 2 tables')

    def test_same_file_name_in_two_folders(self):
        self.outcome(W / 'R1_Test_Upload_-_Superstore.twbx', 'Sample - Superstore')

    def test_live_hyper_connection_is_an_extract(self):
        self.outcome(W / 'World_Indicators_Tableau2024_3.twbx', 'World Indicators', needs_hyper=True)

    def test_column_without_values(self):
        self.outcome(P / 'BDO Pre-Optimization Dashboard.twbx', 'BDO_Media_DS')

    def test_parent_name_only_metadata(self):
        self.outcome(W / 'R7_Chain_Orders_Product_Category.twbx', 'Sales Chain Model', needs_hyper=True)


if __name__ == '__main__':
    unittest.main()
