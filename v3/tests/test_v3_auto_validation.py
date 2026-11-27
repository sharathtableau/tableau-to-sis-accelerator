"""Lab: shipped Tableau reference matched by workbook address; validation runs as the last run stage."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import v3_console as VC
import tableau_references as TR

ECOM = 'ECommerce_Live_Relationship_Events_Customers_Products_17891384213890'


class ReferenceTests(unittest.TestCase):
    def test_slug_from_view_address(self):
        self.assertEqual(VC._capture_slug('/views/' + ECOM + '/Dashboard'), ECOM)
        self.assertEqual(VC._capture_slug('/t/b360bi/views/SalesDashboardVer01/SALES'), 'SalesDashboardVer01')
        self.assertEqual(VC._capture_slug('nonsense'), '')

    def test_loads_only_the_matching_workbook(self):
        run = {'result': {'cloud_content_url': ECOM, 'cloud_workbook_id': 'w1'}}
        text = VC._auto_reference(run)
        self.assertIn('ecommerce_tableau_sheet_capture', run['capture_name'])
        self.assertIn(ECOM, text)
        self.assertEqual(run['capture_bytes'].decode('utf-8'), TR.REFERENCES[run['capture_name']])
        for other in ({'result': {'cloud_content_url': 'SomeOtherWorkbook', 'cloud_workbook_id': 'w2'}}, {'result': {}}):
            self.assertIsNone(VC._auto_reference(other))
            self.assertNotIn('capture_bytes', other)

    def test_uploaded_reference_is_never_replaced(self):
        run = {'result': {'cloud_content_url': ECOM, 'cloud_workbook_id': 'w1'}, 'capture_bytes': b'{}', 'capture_name': 'mine.json'}
        self.assertIsNone(VC._auto_reference(run))
        self.assertEqual(run['capture_name'], 'mine.json')


    def test_fallback_needs_the_dashboard_and_every_captured_sheet(self):
        import json
        cap = json.loads(TR.REFERENCES['ecommerce_tableau_sheet_capture_20261004.json'])
        names = list(cap['sheets'])
        full = {'cloud_workbook_id': 'w1', 'cloud_content_url': 'DifferentSlug',
                'ir': {'dashboards': [{'name': cap['dashboard'], 'sheets': [{'name': n} for n in names]}]}}
        run = {'result': full}
        self.assertIn('all %d captured sheet names' % len(names), VC._auto_reference(run))
        short = dict(full, ir={'dashboards': [{'name': cap['dashboard'], 'sheets': [{'name': n} for n in names[:-1]]}]})
        miss = {'result': short}
        self.assertIsNone(VC._auto_reference(miss))
        self.assertIn('DifferentSlug', miss['capture_auto_miss'])


class AutoValidationTests(unittest.TestCase):
    def test_steps_render_compare_and_prove_with_timing(self):
        sheet = {'name': 'Avg'}
        run = {'result': {'ir': {'dashboards': [{'name': 'Dashboard', 'sheets': [sheet, {'name': 'AOV'}]}]}},
               'capture_bytes': b'{}', 'capture_name': 'c.json'}

        def checks(r, sf):
            r['sheet_checks'] = [{'Renders on data': 'Yes'}, {'Renders on data': 'Yes'}]

        def compare(r):
            r['sheet_reference'] = {'results': {
                'Avg': ({'Status': 'Difference found', 'Matched values': 14, 'Mismatched values': 7}, [{'Match': False}]),
                'AOV': ({'Status': 'Match', 'Matched values': 21}, [])}}
        proven = {'results': [{'Result': 'Proven', 'Hypothesis': 'FIXED values counted once per LOD key'}]}
        announced = []
        with patch('v3_evidence.sheet_checks', side_effect=checks), \
             patch.object(VC, '_compare_capture', side_effect=compare), \
             patch.object(VC, '_proof_entry', return_value=('Dashboard', {'rows': []})), \
             patch('v3_prove.prove', return_value=proven), \
             patch('v3_exact_loop.prove_run', return_value=[{'Result': 'Verified'}, {'Result': 'Not matched'}]), \
             patch('v3_exact_loop.applicable', return_value=[('k', {})]), \
             patch.object(VC.backend, '_active_session', return_value=object()):
            steps = list(VC._auto_validation(run, announced.append))
        self.assertEqual([t for t, _x, _o in steps][:2],
                         ['Rendering every sheet on Snowflake', "Comparing every sheet with Tableau's values"])
        self.assertTrue(all(ok for _t, _x, ok in steps))
        self.assertIn('1 of 2 sheets match Tableau, 1 with differences', steps[1][1])
        self.assertEqual(steps[2][0], 'Proving exact plans against Tableau (every sheet)')       # release 26
        self.assertIn("1 of 2 sheets: an exact plan equals Tableau's values; 1 ready for a person to apply", steps[2][1])
        self.assertIn('fix ready for reviewer approval', steps[3][1])
        self.assertEqual(run['proof_tried'], ['Avg'])
        self.assertNotIn('query_fix', sheet)                     # the fix still needs the reviewer's click
        self.assertEqual(len(run['validation_timing']), 4)
        summary = VC._auto_validation_summary(run)
        self.assertIn('1 of 2 sheets match Tableau', summary)
        self.assertIn('1 with a proven cause', summary)

    def test_a_failing_step_is_reported_not_raised(self):
        run = {'result': {'ir': {'dashboards': []}}}
        with patch('v3_evidence.sheet_checks', side_effect=RuntimeError('warehouse suspended')), \
             patch.object(VC, '_compare_capture', side_effect=RuntimeError('no values')):
            steps = list(VC._auto_validation(run, lambda t: None))
        self.assertFalse(any(ok for _t, _x, ok in steps))
        self.assertIn('warehouse suspended', steps[0][1])


if __name__ == '__main__':
    unittest.main()
