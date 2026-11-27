"""Release 25a (G01, no blank sheet): refused sheets are drawn with the original engine and labelled approximated;
dashboard tabs and titles never show raw Tableau tokens."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import engine
import tableau_parser
import v3_fallback as F
from ir_contract import assess_ir, sheet_issues

CLOUD = ROOT / 'Workbooks' / 'cloud_20261010'
SS = CLOUD / 'Superstore_Tableau2024_3.twbx'


@unittest.skipUnless(SS.exists(), 'Cloud test set not present')
class FallbackTests(unittest.TestCase):
    def test_every_refused_sheet_gets_the_original_description_labelled_approximated(self):
        ir = tableau_parser.build_ir(str(SS), scoped=True)
        before = {(d['name'], s['name']): s for d in ir['dashboards'] for s in d['sheets']}
        refused = {k for k, s in before.items() if F.refused(s)}
        self.assertEqual(len(refused), 17)
        done = F.approximate(ir, SS)
        self.assertEqual({(d, s) for d, s, _k in done}, refused)
        states = {(r['dashboard'], r['sheet']): r for r in assess_ir(ir)['sheets']}
        for key in refused:
            sheet = next(s for d in ir['dashboards'] if d['name'] == key[0] for s in d['sheets'] if s['name'] == key[1])
            self.assertTrue(sheet['approximated'])
            self.assertEqual(states[key]['state'], 'PARTIAL')                     # never shown as verified
            self.assertTrue(sheet['support']['reason'].startswith(F.PREFIX))
            self.assertTrue(sheet['v3_refusal'])
            if before[key].get('geom') is not None:
                self.assertEqual(sheet['geom'], before[key]['geom'])                  # layout kept
            self.assertEqual(sheet_issues(sheet), [])
        for key in set(before) - refused:                                           # supported sheets untouched
            self.assertFalse(next(s for d in ir['dashboards'] if d['name'] == key[0]
                                  for s in d['sheets'] if s['name'] == key[1]).get('approximated'))
        self.assertEqual(len(ir['approximated_sheets']), 17)
        self.assertEqual(F.approximate(ir, SS), [])                                 # nothing left to approximate

    def test_no_refused_sheet_left_in_the_cloud_set(self):
        for f in sorted(CLOUD.glob('*.twb*')):
            ir = tableau_parser.build_ir(str(f), scoped=True)
            F.approximate(ir, f)
            left = [s['name'] for d in ir['dashboards'] for s in d['sheets'] if F.refused(s)]
            self.assertEqual(left, [], f.name)

    def test_a_sheet_the_original_engine_cannot_draw_stays_refused(self):
        ir = {'dashboards': [{'name': 'D', 'sheets': [{'name': 'S', 'kind': 'unsupported',
                                                        'support': {'status': 'UNSUPPORTED', 'reason': 'r'}}]}]}
        from unittest.mock import patch
        with patch('tableau_parser.build_ir', return_value={'dashboards': [{'name': 'D', 'sheets': [
                {'name': 'S', 'kind': 'unsupported'}]}]}):
            self.assertEqual(F.approximate(ir, 'x.twb'), [])
        self.assertEqual(ir['dashboards'][0]['sheets'][0]['kind'], 'unsupported')


class TitleTests(unittest.TestCase):
    def test_field_inserts_become_words(self):
        t = 'What if Forecast Based on < [federated.10nnk].[yr:Order Date:ok] > Sales'
        self.assertEqual(engine.readable_inserts(t), 'What if Forecast Based on Year of Order Date Sales')
        self.assertEqual(engine.readable_inserts('<[ds].[sum:Sales:qk]> total'), 'Sum of Sales total')
        self.assertEqual(engine.readable_inserts('<[Parameters].[Churn Rate]>'), 'Churn Rate')
        self.assertEqual(engine.readable_inserts('Report <Sheet Name>'), 'Report')

    def test_display_title_never_shows_braces_or_brackets(self):
        t = engine.display_title('A ( {param:New Business Growth} Growth, < [d].[yr:Order Date:ok] >)')
        self.assertNotIn('{param:', t)
        self.assertNotIn('[', t)

    def test_tabs_use_the_dashboard_name(self):
        src = (ROOT / 'v3_runtime.py').read_text(encoding='utf-8') + (ROOT / 'v3_console.py').read_text(encoding='utf-8')
        self.assertNotIn("d.get('title') or d.get('name', 'Dashboard')", src)
        self.assertNotIn("dashboards[i].get('title') or dashboards[i].get('name', 'Dashboard')", src)


if __name__ == '__main__':
    unittest.main()
