"""tools/plan_census.py: the corpus census of the Tableau-comparison path (v3_sheet_plan), offline or on live
Snowflake (--sf, read-only, the owner's named connection). Per sheet one closed outcome; failures grouped by a
normalised reason, largest first. Written BEFORE the tool (tests first)."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
import plan_census as PC  # noqa: E402


class Pieces(unittest.TestCase):
    def test_reason_is_normalised_without_data_values(self):
        a = PC.reason(RuntimeError("Binder Error: Referenced column \"CLUSTER_X\" not found in FROM clause!\nLINE 1: SELECT 'Jane Doe'"))
        b = PC.reason(RuntimeError("Binder Error: Referenced column \"OTHER\" not found in FROM clause!"))
        self.assertEqual(a, b)
        self.assertNotIn('Jane', a)
        self.assertTrue(a.startswith('RuntimeError: '))

    def test_snowflake_bindings_only(self):
        b = {'A': 'DB.SCH.T', 'B': '/tmp/x.csv', 'C': None, 'D': {'model': 1}, 'E': 'bad name; drop'}
        self.assertEqual(PC.snowflake_bindings(b), {'A': 'DB.SCH.T', 'D': {'model': 1}})

    def test_report_ranks_failures(self):
        rows = [{'workbook': 'w1', 'sheet': 's1', 'outcome': 'FAILED', 'reason': 'X'},
                {'workbook': 'w2', 'sheet': 's2', 'outcome': 'FAILED', 'reason': 'X'},
                {'workbook': 'w1', 'sheet': 's3', 'outcome': 'FAILED', 'reason': 'Y'},
                {'workbook': 'w1', 'sheet': 's4', 'outcome': 'RAN', 'reason': ''},
                {'workbook': 'w3', 'sheet': 's5', 'outcome': 'NO_DATA', 'reason': ''}]
        md = PC.report(rows, 'offline')
        self.assertIn('| 2 | 2 | X |', md)
        self.assertLess(md.index('| X |'), md.index('| Y |'))
        self.assertIn('RAN 1', md)
        self.assertIn('NO_DATA 1', md)


class NoCortex(unittest.TestCase):
    def test_intake_skips_the_cortex_review(self):
        src = (ROOT / 'tools' / 'plan_census.py').read_text(encoding='utf-8')
        self.assertNotIn('prepare_payload(', src)
        self.assertNotIn('assist(', src)


class Offline(unittest.TestCase):
    def test_one_workbook_offline(self):
        rows = PC.census([ROOT / 'Workbooks' / 'Superstore_Tableau2024_3.twbx'], sf=False)
        self.assertTrue(rows)
        self.assertTrue({r['outcome'] for r in rows} <= set(PC.OUTCOMES))
        self.assertTrue(any(r['outcome'] == 'RAN' for r in rows))


if __name__ == '__main__':
    unittest.main()
