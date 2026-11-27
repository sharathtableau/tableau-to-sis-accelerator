"""Live Tableau reference comparison: identity by captions, display-precision tolerance, no guessing."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
import v3_reference as R

CAPTURED = ROOT / 'reports/ai00_1c_20261001/workbooks/7bf7d935-ee7c-470e-9684-50ad72fc2e0c'


class ParseTests(unittest.TestCase):
    def test_display_precision_sets_tolerance(self):
        self.assertEqual(R.parse_number('4,430,337.4'), (4430337.4, 0.05))
        self.assertEqual(R.parse_number('12'), (12.0, 0.5))
        value, tol = R.parse_number('-0.9%')
        self.assertAlmostEqual(value, -0.009); self.assertAlmostEqual(tol, 0.0005)
        self.assertEqual(R.parse_number('(1,200)')[0], -1200.0)
        self.assertEqual(R.parse_number('$680'), (680.0, 0.5))
        self.assertEqual(R.parse_number('North'), (None, None))

    def test_rows_compare_on_dimension_keys_including_numeric_looking_keys(self):
        header = ['Year', 'Amount']
        tableau = [{'Year': '2024', 'Amount': '10.5'}, {'Year': '2025', 'Amount': '7'}]
        app = [{'Year': 2024, 'Amount': 10.52}, {'Year': 2025, 'Amount': 6.4}]
        result = R.compare_rows(header, tableau, app, ['Year'])
        self.assertEqual((result['matched'], result['mismatched']), (1, 1))
        missing = R.compare_rows(header, tableau, app[:1], ['Year'])
        self.assertEqual(missing['missing_in_app'], 1)

    def test_unavailable_reference_is_reported_not_compared(self):
        row, detail = R.compare_view({'dashboards': []}, {'view': 'Any', 'csv': None,
                                     'error': 'Tableau API error 400081: DataSourcesNotConnectedException'}, None)
        self.assertEqual(row['Status'], 'Tableau reference unavailable')
        self.assertIn('not connected', row['Note']); self.assertEqual(detail, [])

    def test_unmatched_columns_are_not_guessed(self):
        ir = {'dashboards': [{'name': 'Board', 'sheets': [{'name': 'S1'}]}]}
        row, _ = R.compare_view(ir, {'view': 'Board', 'csv': 'Unknown,Value\nA,1\n'}, lambda s, d: None)
        self.assertEqual(row['Status'], 'Not comparable')


@unittest.skipUnless(CAPTURED.is_dir(), 'captured Tableau reference not present')
class CapturedReferenceTests(unittest.TestCase):
    def test_published_view_matches_app_and_planted_difference_is_caught(self):
        from v3_local import prepare_payload, preview_context
        twbx = next(CAPTURED.glob('*.twbx'))
        result = prepare_payload(twbx.read_bytes(), 'renamed_upload.twbx')
        csv_text = next((CAPTURED / 'references').glob('*.csv')).read_text(encoding='utf-8-sig')
        view_name = result['ir']['dashboards'][0]['name']
        first_value = csv_text.splitlines()[1].split(',', 1)[1].strip('"')
        with preview_context(result['ir'], result['bindings'], snowflake=False):
            good, detail = R.compare_view(result['ir'], {'view': view_name, 'csv': csv_text}, R.headless_capture)
            bad, _ = R.compare_view(result['ir'], {'view': view_name, 'csv': csv_text.replace(first_value, '1.0', 1)},
                                    R.headless_capture)
        self.assertEqual(good['Status'], 'Match', good)
        self.assertEqual(good['Matched values'], len(csv_text.strip().splitlines()) - 1)
        self.assertEqual(bad['Status'], 'Difference found')
        self.assertEqual(bad['Mismatched values'], 1)


if __name__ == '__main__':
    unittest.main()
