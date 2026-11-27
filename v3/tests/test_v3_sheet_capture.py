"""Per-sheet Tableau capture comparison: matches on captions/keys, catches differences, never guesses."""
from pathlib import Path
import json
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import v3_sheet_capture as SC


def entry(columns, rows, **kw):
    return dict({'columns': columns, 'rows': rows, 'total_rows': len(rows), 'note': None}, **kw)


class SheetCaptureTests(unittest.TestCase):
    def test_match_within_displayed_precision(self):
        summary, detail = SC.compare_sheet(entry(['Region', 'AGG(Sales)'], [['West', '100.0'], ['East', '200.4']]),
                                           [{'Region': 'West', 'Sales': '100.04'}, {'Region': 'East', 'Sales': '200.36'}])
        self.assertEqual(summary['Status'], 'Match')
        self.assertEqual(summary['Matched values'], 2)
        self.assertEqual({d['Key'] for d in detail}, {'West', 'East'})

    def test_planted_difference_and_missing_row_are_caught(self):
        summary, detail = SC.compare_sheet(entry(['Region', 'SUM(Sales)'], [['West', '100.0'], ['East', '200.0'], ['North', '5.0']]),
                                           [{'Region': 'West', 'Sales (SUM)': '100.0'}, {'Region': 'East', 'Sales (SUM)': '201.0'}])
        self.assertEqual(summary['Status'], 'Difference found')
        self.assertEqual((summary['Mismatched values'], summary['Rows missing in app']), (1, 1))

    def test_measure_names_pivot_aliases_and_numeric_keys(self):
        cap = entry(['Measure Names', 'Product ID', 'Measure Values'],
                    [['Avg. Discount', '7', '0.25'], ['Total Sales', '7', '1,234.50']])
        summary, _ = SC.compare_sheet(cap, [{'Product ID': '7', 'Discount (AVG)': '0.2501', 'Total Sales (SUM)': '1,234.5'}])
        self.assertEqual(summary['Status'], 'Match')
        self.assertEqual(summary['Matched values'], 2)

    def test_month_names_duplicate_panes_and_attr_columns(self):
        cap = entry(['MONTH(Order Date)', 'AGG(Sales)', 'ATTR(Order Date)'],
                    [['January', '10.0', 'x'], ['January', '10.0', 'x'], ['February', '20.0', 'y']])
        summary, _ = SC.compare_sheet(cap, [{'Order Date': '1', 'Sales': '10'}, {'Order Date': '2', 'Sales': '20'}])
        self.assertEqual((summary['Status'], summary['Matched values']), ('Match', 2))

    def test_label_alias_comes_from_the_workbook_not_from_values(self):
        sheet = {'y': {'caption': 'Metric Selection', 'label': '$ Sales'}}
        cap = entry(['Month', 'AGG(Metric Selection)'], [['1', '5.0']])
        self.assertEqual(SC.compare_sheet(cap, [{'Month': '1', '$ Sales': '5'}], sheet)[0]['Status'], 'Match')
        self.assertEqual(SC.compare_sheet(cap, [{'Month': '1', '$ Sales': '5'}])[0]['Status'], 'Not comparable')

    def test_errors_and_schema(self):
        self.assertEqual(SC.compare_sheet({'error': 'boom'}, [])[0]['Status'], 'Tableau returned no data')
        with self.assertRaises(ValueError):
            SC.load(json.dumps({'schema': 'other', 'sheets': {}}))

    def test_real_sales_capture_is_well_formed(self):
        path = Path(__file__).resolve().parents[1] / 'reports/ui01_console_20261003/round6/sales_tableau_sheet_capture_20261003.json'
        if not path.exists():
            self.skipTest('capture evidence not present')
        cap = SC.load(path.read_bytes())
        self.assertEqual(len(cap['sheets']), 9)
        self.assertEqual(cap['sheets']['Categ_KPI']['rows'][-1], ['Books', '4,430,337.4'])


if __name__ == '__main__':
    unittest.main()


class EcommerceShapesTests(unittest.TestCase):
    """Shapes seen on the ECommerce dashboard: day dates, KPI tiles, rank tables with arrow-formatted
    deltas and Tableau SIGN columns, tooltip-named scatter rows, and Tableau total rows."""
    def setUp(self):
        import v3_sheet_capture as C
        self.C = C

    def test_day_dates_match_app_timestamps(self):
        entry = {'columns': ['DAY(Event Date)', 'AGG(C.Total Revenue)'],
                 'rows': [['October 21, 2025', '$68,678'], ['October 20, 2025', '$55,278']]}
        app = [{'Event Date': '2025-10-21 00:00:00', 'C.Total Revenue': '68,678.21'},
               {'Event Date': '2025-10-20 00:00:00', 'C.Total Revenue': '55,277.9'}]
        s, _ = self.C.compare_sheet(entry, app)
        self.assertEqual((s['Status'], s['Matched values']), ('Match', 2))

    def test_kpi_tile_and_engine_label_rule(self):
        entry = {'columns': ['AGG(C.Total Revenue)', 'AGG(CP. +%Total Revenue)', 'Select Date'],
                 'rows': [['$1,201,846', '▲ 3.5%', 'Last 21 Days']]}
        s, _ = self.C.compare_sheet(entry, [{'KPI': 'C.Total Revenue', 'Value': '$1,201,846'}])
        self.assertEqual((s['Status'], s['Matched values']), ('Match', 1))
        s, _ = self.C.compare_sheet(entry, [{'KPI': 'C.Total Revenue', 'Value': '$1,301,846'}])
        self.assertEqual(s['Status'], 'Difference found')

    def test_rank_table_with_hidden_sign_and_delta_column(self):
        entry = {'columns': ['Payment Method', 'AGG(C.Total Revenue)', 'AGG(CP. %Total Revenue)',
                             'AGG(SIGN([CP. %Total Revenue]))'],
                 'rows': [['Credit Card', '$697,682', '13.8%', '1'], ['Invoice', '$292,461', ' 14.3%', '-1']]}
        sheet = {'text_fields': ['C.Total Revenue', 'CP. %Total Revenue']}
        app = [{'Payment Method': 'Credit Card', 'Total Revenue': '$697,682', 'Δ%': '13.8% ▲'},
               {'Payment Method': 'Invoice', 'Total Revenue': '$292,461', 'Δ%': '-14.3% ▼'}]
        s, d = self.C.compare_sheet(entry, app, sheet)
        self.assertEqual((s['Status'], s['Matched values']), ('Match', 4), d)
        app[1]['Δ%'] = '14.3% ▲'                    # wrong sign must be caught
        s, _ = self.C.compare_sheet(entry, app, sheet)
        self.assertEqual(s['Mismatched values'], 1)

    def test_total_rows_are_not_counted_as_missing(self):
        entry = {'columns': ['Event Type', 'SUM(Revenue)'],
                 'rows': [['Null', '$1,201,846'], ['order', '$827,875'], ['invoice', '$373,971']]}
        app = [{'Event Type': 'order', 'Revenue': '827,875.1'}, {'Event Type': 'invoice', 'Revenue': '373,970.9'}]
        s, _ = self.C.compare_sheet(entry, app)
        self.assertEqual((s['Status'], s['Matched values'], s['Rows missing in app']), ('Match', 2, 0))
        self.assertIn('total row', s['Note'])

    def test_text_only_app_sheet_is_reported_as_such(self):
        s, _ = self.C.compare_sheet({'columns': ["'grace'"], 'rows': [['grace']]}, [])
        self.assertIn('text/HTML only', s['Note'])
