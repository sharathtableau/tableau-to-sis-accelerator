"""Release 26: exact plans from the workbook shelves, proven against Tableau's own values."""
import json
import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

import v3_exact_loop as X
import v3_sheet_plan as P
from ir_contract import assess_ir, sheet_issues

CLOUD = ROOT / 'Workbooks' / 'cloud_20261010'
SS = CLOUD / 'Superstore_Tableau2024_3.twbx'


def plan(fields, **kw):
    out = {'schema': P.SCHEMA, 'mark': 'bar', 'fields': fields, 'measure_values': [], 'sort': []}
    out.update(kw)
    return out


def dim(i, caption, channel='rows', part='none', discrete=True):
    return {'id': i, 'caption': caption, 'channel': channel, 'kind': 'dim', 'discrete': discrete,
            'date_part': part, 'agg': 'none'}


def meas(i, caption, agg='sum', channel='cols', **kw):
    return dict({'id': i, 'caption': caption, 'channel': channel, 'kind': 'measure', 'discrete': False,
                 'date_part': 'none', 'agg': agg}, **kw)


class Engine:                       # display labels only (no aliases)
    @staticmethod
    def value_labels(_c):
        return {}


class ClosedListTests(unittest.TestCase):
    def test_validate_accepts_only_the_closed_lists(self):
        ok = plan([dim('a', 'Region'), meas('b', 'Sales')])
        self.assertEqual(P.validate(ok, {'Region', 'Sales'}), [])
        bad = plan([dim('a', 'Region', part='fortnight'), meas('b', 'Sales', agg='sumx')], mark='sparkle')
        problems = P.validate(bad)
        self.assertTrue(any('mark' in x for x in problems))
        self.assertTrue(any('date_part' in x for x in problems))
        self.assertTrue(any('agg' in x for x in problems))
        self.assertTrue(P.validate(plan([meas('b', 'Unknown')]), {'Sales'}))
        tc = plan([meas('b', 'Sales', table_calc={'type': 'DROP TABLE', 'addressing': 'rows'})])
        self.assertTrue(any('table_calc' in x for x in P.validate(tc)))

    def test_table_calc_xml_is_typed(self):
        tc = P._table_calc(['<table-calc ordering-type="Columns" rank-options="Competition,Descending" type="Rank" />'])
        self.assertEqual((tc['type'], tc['addressing'], tc['rank'], tc['descending']), ('Rank', 'cols', 'competition', True))
        self.assertEqual(P._table_calc(['<table-calc ordering-type="Rows" />'])['type'], 'Window')
        self.assertTrue(P._table_calc(['<table-calc type="Moving" />'])['type'].startswith('unknown:'))


class TableCalcTests(unittest.TestCase):
    def test_rank_percent_of_total_and_running_total(self):
        p = plan([dim('r', 'Region', 'rows'), meas('s', 'Sales', table_calc={'type': 'Rank', 'addressing': 'rows',
                                                                          'rank': 'competition', 'descending': True})])
        df = pd.DataFrame({'D|none|Region': ['E', 'W', 'S'], 'M|sum|Sales': [5, 9, 5]})
        self.assertEqual(list(P.table_calc(df, p, p['fields'][1])), [2, 1, 2])
        p['fields'][1]['table_calc'] = {'type': 'PctTotal', 'addressing': 'rows'}
        self.assertAlmostEqual(sum(P.table_calc(df, p, p['fields'][1])), 1.0)
        p['fields'][1]['table_calc'] = {'type': 'CumTotal', 'addressing': 'rows', 'aggregation': 'sum'}
        self.assertEqual(list(P.table_calc(df, p, p['fields'][1])), [5, 19, 10])     # E, S, W order


class FormatTests(unittest.TestCase):
    def test_tableau_formats(self):
        self.assertEqual(P.d3_format('c"$"#,##0;("$"#,##0)'), '$,.0f')
        self.assertEqual(P.d3_format('p0.0%'), '.1%')
        self.assertEqual(P.d3_format('C1033%'), '$,.2f')
        self.assertEqual(P.format_value(-1234.4, 'c"$"#,##0;("$"#,##0)'), '($1,234)')
        self.assertEqual(P.format_value(0.1234, 'p0.0%'), '12.3%')

    def test_date_keys_match_both_sides(self):
        self.assertEqual(P.key_text('January 2021', 'month_trunc'), P.key_text('2021-01-01', 'month_trunc'))
        self.assertEqual(P.key_text('September 29, 2024', 'week_trunc'), P.key_text('2024-10-02', 'week_trunc'))
        self.assertEqual(P.key_text('March', 'month'), P.key_text(3, 'month'))
        self.assertEqual(P.key_text('10/12/2024', 'none'), P.key_text('2024-10-12 00:00:00', 'none'))


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.p = plan([dim('r', 'Region'), meas('s', 'Sales')])
        self.df = pd.DataFrame({'D|none|Region': ['East', 'West'], 'M|sum|Sales': [1234.4, 99.6]})

    def test_verified_needs_every_row_and_value(self):
        entry = {'columns': ['Region', 'SUM(Sales)'], 'rows': [['East', '$1,234'], ['West', '$100']], 'total_rows': 2}
        self.assertEqual(X.compare(self.p, self.df, entry, Engine)['status'], 'Verified')
        entry['rows'][1][1] = '$101'
        r = X.compare(self.p, self.df, entry, Engine)
        self.assertEqual(r['status'], 'Not matched')
        self.assertEqual(r['different'], 1)

    def test_a_tableau_column_the_plan_lacks_is_never_ignored(self):
        entry = {'columns': ['Region', 'SUM(Sales)', 'SUM(Profit)'], 'rows': [['East', '$1,234', '$1'], ['West', '$100', '$2']],
                 'total_rows': 2}
        r = X.compare(self.p, self.df, entry, Engine)
        self.assertEqual(r['status'], 'Not matched')
        self.assertIn('SUM(Profit)', ' '.join(r['reasons']))

    def test_extra_app_rows_fail(self):
        entry = {'columns': ['Region', 'SUM(Sales)'], 'rows': [['East', '$1,234']], 'total_rows': 1}
        self.assertEqual(X.compare(self.p, self.df, entry, Engine)['status'], 'Not matched')

    def test_measure_names_values_are_pivoted(self):
        p = plan([meas('mv', 'Measure Values', agg='none', channel='text', measure_values=True)],
                 measure_values=[{'caption': 'Sales', 'agg': 'sum'}, {'caption': 'Discount', 'agg': 'avg'}], mark='text')
        df = pd.DataFrame({'M|sum|Sales': [10.0], 'M|avg|Discount': [0.155]})
        entry = {'columns': ['Measure Names', 'Measure Values'], 'rows': [['Sales', '10.00'], ['Avg. Discount', '0.16']],
                 'total_rows': 2}
        self.assertEqual(X.compare(p, df, entry, Engine)['status'], 'Verified')

    def test_duplicate_headers_pair_the_table_calc_by_evidence(self):
        p = plan([dim('c', 'Customer'), meas('s', 'Sales', table_calc={'type': 'Rank', 'addressing': 'table',
                                                                      'rank': 'competition', 'descending': True})])
        df = pd.DataFrame({'D|none|Customer': ['A', 'B'], 'M|sum|Sales': [5.0, 9.0]})
        df['M|sum|Sales|tc'] = P.table_calc(df, p, p['fields'][1])
        entry = {'columns': ['Customer', 'SUM(Sales)', 'SUM(Sales)'], 'rows': [['A', '2', '$5'], ['B', '1', '$9']],
                 'total_rows': 2}
        self.assertEqual(X.compare(p, df, entry, Engine)['status'], 'Verified')


class BundleTests(unittest.TestCase):
    def test_bundle_merges_dashboards(self):
        import v3_sheet_capture as SC
        one = {'schema': SC.SCHEMA, 'dashboard': 'A', 'sheets': {'s1': {'columns': [], 'rows': []}}}
        two = {'schema': SC.SCHEMA, 'dashboard': 'B', 'sheets': {'s2': {'columns': [], 'rows': []}}}
        cap = SC.load(json.dumps({'schema': SC.BUNDLE, 'captures': {'w/A': one, 'w/B': two}}))
        self.assertEqual(set(cap['sheets']), {'s1', 's2'})
        self.assertEqual(len(SC.dashboard_captures(cap)), 2)
        with self.assertRaises(ValueError):
            SC.load(json.dumps({'schema': SC.BUNDLE, 'captures': {'x': {'schema': 'other'}}}))


@unittest.skipUnless(SS.exists(), 'Cloud test set not present')
class WorkbookTests(unittest.TestCase):
    def test_every_refused_sheet_gets_a_valid_plan_and_passes_the_contract(self):
        import tableau_parser
        ir = tableau_parser.build_ir(str(SS), scoped=True)
        done = X.attach_plans(ir, SS)
        self.assertEqual(len(done), 17)
        self.assertEqual(assess_ir(ir)['issues'], [])
        for d in ir['dashboards']:
            for s in d['sheets']:
                if s.get('sheet_plan'):
                    self.assertEqual(s['kind'], 'planned')
                    self.assertEqual(s['support']['status'], 'PARTIAL')          # never verified at intake
                    self.assertEqual(sheet_issues(s), [])
                    self.assertEqual(P.validate(s['sheet_plan']), [])

    def test_contract_rejects_a_tampered_plan(self):
        import tableau_parser
        ir = tableau_parser.build_ir(str(SS), scoped=True)
        X.attach_plans(ir, SS)
        s = next(s for d in ir['dashboards'] for s in d['sheets'] if s.get('sheet_plan'))
        s['sheet_plan']['mark'] = 'DROP TABLE'
        self.assertTrue(any(i.code == 'sheet-plan' for i in sheet_issues(s)))

    def test_superstore_sheets_equal_tableau(self):
        """End to end offline: the console's flow (bundle reference, prove, apply) on the bundled data."""
        import tableau_reference_bundles as B
        import exact_scoreboard as S
        run, entry = S.pipeline_run(SS)
        run.setdefault('bindings', {})
        run['capture_bytes'] = B.BUNDLES['Superstore_Tableau2024_3'].encode()
        rows = X.prove_run(run, False)
        status = {r['Sheet']: r['Result'] for r in rows}
        self.assertEqual(len(rows), 20)
        self.assertGreaterEqual(sum(v == 'Verified' for v in status.values()), 19)
        self.assertEqual(status['Forecast'], 'Partly verified')       # FC: an approximated forecast, never Verified
        fc = next(r for r in rows if r['Sheet'] == 'Forecast')
        self.assertIn('approximated forecast', ' '.join(str(v) for v in fc.values()))
        done = X.apply_verified(run, 'test', 'bundle', 'now')
        self.assertEqual(len(done), 20)
        self.assertEqual(X.applicable(run), [])
        self.assertEqual(assess_ir(run['result']['ir'])['issues'], [])


class LayoutTests(unittest.TestCase):
    """Release 26b: layout follows the workbook (zone size, headers, legends, titles, control defaults)."""
    def test_title_inserts_show_the_selected_value_or_all(self):
        s = {'applied_filters': [{'caption': 'Order Date', 'kind': 'in', 'values': ['4'], 'datepart': 'qr'},
                                 {'caption': 'Order Date', 'kind': 'in', 'values': ['2024'], 'datepart': 'yr'}]}
        self.assertEqual(P.resolve_title('For < [d].[qr:Order Date:ok] > of < [d].[yr:Order Date:ok] >', s, []),
                         'For Q4 of 2024')
        self.assertEqual(P.resolve_title('Category: none:Category:nk', {}, []), 'Category: All')
        parts = [{'caption': 'Order Date', 'runtime_filter': {'kind': 'date_part', 'part': 'yr', 'value': 2023}}]
        self.assertEqual(P.resolve_title('< [d].[yr:Order Date:ok] >', s, parts), '2023')    # the control wins

    def test_controls_open_at_the_saved_selection(self):
        import engine
        dash = {'sheets': [{'name': 'S', 'applied_filters': [{'caption': 'Order Date', 'kind': 'in',
                                                              'values': ['2024'], 'datepart': 'yr'}]}]}
        self.assertEqual(engine._saved_selection(dash, {'caption': 'Order Date', 'scope_sheet': 'S'}, 'yr'), ['2024'])
        self.assertEqual(engine._saved_selection(dash, {'caption': 'Region', 'scope_sheet': 'S'}), [])

    def test_shelves_drive_headers_axes_and_legends(self):
        import altair as alt
        import engine
        p = plan([dim('y', 'Year', 'rows'), dim('m', 'Month', 'rows'), dim('g', 'Segment', 'rows'),
                  dim('c', 'Category', 'cols'), meas('s', 'Sales'), dict(dim('k', 'Status', 'color'))],
                 mark='bar', hide_axis=[], hide_field_labels=[])
        df = pd.DataFrame({'D|none|Year': ['2021', '2021', '2022'], 'D|none|Month': ['Jan', 'Feb', 'Jan'],
                           'D|none|Segment': ['A', 'B', 'A'], 'D|none|Category': ['X', 'X', 'Y'],
                           'D|none|Status': ['Up', 'Down', 'Up'], 'M|sum|Sales': [1.0, 2.0, 3.0]})

        class E:
            pass
        E.alt = alt

        class _E:
            value_labels = staticmethod(lambda c: {})
            cat_colors = staticmethod(lambda c, present: (None, None))
        E.value_labels, E.cat_colors = _E.value_labels, _E.cat_colors
        spec = P.build_chart(p, df, E, {'h': 400, 'w': 1000, 'legends': []}).to_dict()
        text = json.dumps(spec)
        self.assertIn('2021 \\u00b7 Jan', text)                    # Year > Month nested headers kept, in order
        self.assertIn('"legend": null', text)                        # no legend zone on the dashboard
        self.assertIn(P.TABLEAU_DEFAULT_MARK, text)

    @unittest.skipUnless(SS.exists(), 'Cloud test set not present')
    def test_nested_members_and_measure_axes_render(self):
        import vl_convert as vlc
        import tableau_parser
        import exact_scoreboard as S
        import engine
        import v3_evidence as EV
        from v3_local import preview_context
        run, _entry = S.pipeline_run(SS)
        ir = run['result']['ir']
        sheets = S.scoped_sheets(SS, ir)
        with preview_context(ir, run.get('bindings') or {}, snowflake=False), EV.offscreen():
            for name in ('ProductDetails', 'CustomerOverview', 'Performance', 'ProductView'):
                s = next(v for (d, n), v in sheets.items() if n == name)
                pl = P.from_sheet(s, ir)
                df = P.execute(pl, s, engine)
                with engine.field_scope(P._bindable(s, engine)):
                    chart = P.build_chart(pl, df, engine, {'h': 300, 'w': 1100, 'legends': ['color']})
                spec = chart.to_dict()
                self.assertTrue(vlc.vegalite_to_svg(spec).startswith('<svg'), name)
                if name == 'ProductDetails':
                    self.assertIn('vconcat', spec)                  # each Category lists only its Sub-Categories
                if name == 'CustomerOverview':
                    self.assertEqual(spec['resolve']['scale']['x'], 'independent')   # one axis per measure


if __name__ == '__main__':
    unittest.main()
