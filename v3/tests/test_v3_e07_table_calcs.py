"""E07 ordered table calculations: no silent guesses. Written BEFORE the fix (tests first).
  * Difference / % difference: Tableau writes "relative to" as diff-options + <address><value>n</value> (-1 previous,
    +1 next, any offset) or diff-options First / Last. The plan always used "previous". Now every offset, First and
    Last are computed; an unknown option refuses.
  * Specific-dimension addressing: Tableau lists the addressed fields as <order field=...> children; the parser read
    only an ordering-field attribute and otherwise addressed every dimension. Now the listed fields, in order.
Expected values are computed independently with plain Python, not with the code under test."""
from pathlib import Path
import sys
import unittest

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import v3_sheet_plan as P  # noqa: E402

DIFF = ("<table-calc diff-options='{opt}' ordering-type='Rows' type='{kind}'>"
        "<address><value>{off}</value></address></table-calc>")


def plan_for(tc_xml, dims=('Region', 'Year')):
    fields = [{'caption': d, 'kind': 'dim', 'channel': 'rows', 'date_part': 'none', 'agg': 'none', 'discrete': True,
               'field_id': 'ds::' + d} for d in dims]
    m = {'caption': 'Sales', 'kind': 'measure', 'channel': 'text', 'date_part': 'none', 'agg': 'sum', 'discrete': False,
         'table_calc': P._table_calc([tc_xml])}
    return {'fields': fields + [m], 'measure_values': []}, m


def frame():
    return pd.DataFrame({P.col_name({'caption': 'Region', 'date_part': 'none'}): ['E', 'E', 'E', 'W', 'W', 'W'],
                         P.col_name({'caption': 'Year', 'date_part': 'none'}): [2021, 2022, 2023, 2021, 2022, 2023],
                         'M|sum|Sales': [10.0, 20.0, 40.0, 5.0, 5.0, 10.0]})


class Parse(unittest.TestCase):
    def test_relative_offsets_first_last_and_unknown(self):
        self.assertEqual(P._table_calc([DIFF.format(opt='Relative', kind='Diff', off='-1')])['relative_to'], ('offset', -1))
        self.assertEqual(P._table_calc([DIFF.format(opt='Relative', kind='PctDiff', off='1')])['relative_to'], ('offset', 1))
        self.assertEqual(P._table_calc(["<table-calc diff-options='First' ordering-type='Rows' type='Diff' />"])
                         ['relative_to'], ('first', None))
        self.assertEqual(P._table_calc(["<table-calc diff-options='Last' ordering-type='Rows' type='Diff' />"])
                         ['relative_to'], ('last', None))
        self.assertTrue(P._table_calc(["<table-calc diff-options='Weird' ordering-type='Rows' type='Diff' />"])
                        ['type'].startswith('unknown:'))

    def test_order_children_become_the_addressing(self):
        tc = P._table_calc(["<table-calc ordering-type='Field'><order field='[ds].[Year]' />"
                            "<order field='[ds].[Region]' /></table-calc>"])
        self.assertEqual(tc['ordering_fields'], ['[ds].[Year]', '[ds].[Region]'])
        plan, m = plan_for("<table-calc ordering-type='Field' type='CumTotal'><order field='[ds].[Year]' /></table-calc>")
        along, part = P._addressing(plan, m)
        self.assertEqual(([d['caption'] for d in along], [d['caption'] for d in part]), (['Year'], ['Region']))


class Values(unittest.TestCase):
    """Partition by Region (addressing Rows = Region, Year -> but Region partitions when addressing Year only)."""

    def calc(self, xml):
        xml = xml.replace("ordering-type='Rows'", "ordering-type='Field' ordering-field='[ds].[Year]'")
        plan, m = plan_for(xml)
        return list(P.table_calc(frame(), plan, m))

    @staticmethod
    def expected(fn):
        out = []
        for reg in ('E', 'W'):
            vals = [v for r, v in zip(['E'] * 3 + ['W'] * 3, [10.0, 20.0, 40.0, 5.0, 5.0, 10.0]) if r == reg]
            out += [fn(vals, i) for i in range(3)]
        return out

    def same(self, got, want):
        self.assertEqual(len(got), len(want))
        for g, w in zip(got, want):
            self.assertTrue((pd.isna(g) and w is None) or (w is not None and abs(g - w) < 1e-9), (got, want))

    def test_previous_is_unchanged(self):
        self.same(self.calc(DIFF.format(opt='Relative', kind='Diff', off='-1')),
                  self.expected(lambda v, i: v[i] - v[i - 1] if i else None))

    def test_next(self):
        self.same(self.calc(DIFF.format(opt='Relative', kind='Diff', off='1')),
                  self.expected(lambda v, i: v[i] - v[i + 1] if i < 2 else None))

    def test_two_back_pct(self):
        self.same(self.calc(DIFF.format(opt='Relative', kind='PctDiff', off='-2')),
                  self.expected(lambda v, i: (v[i] - v[i - 2]) / abs(v[i - 2]) if i >= 2 else None))

    def test_first_and_last(self):
        self.same(self.calc("<table-calc diff-options='First' ordering-type='Rows' type='Diff' />"),
                  self.expected(lambda v, i: v[i] - v[0]))
        self.same(self.calc("<table-calc diff-options='Last' ordering-type='Rows' type='PctDiff' />"),
                  self.expected(lambda v, i: (v[i] - v[2]) / abs(v[2])))

    def test_unknown_option_refuses(self):
        with self.assertRaises(P.PlanError):
            self.calc("<table-calc diff-options='Weird' ordering-type='Rows' type='Diff' />")


class NullsAndZero(unittest.TestCase):
    """Tableau: division by zero is Null (never inf); window aggregates ignore Null inputs."""

    def run_calc(self, xml, values):
        plan, m = plan_for(xml, dims=('Year',))
        df = pd.DataFrame({P.col_name({'caption': 'Year', 'date_part': 'none'}): [2021, 2022, 2023, 2024],
                           'M|sum|Sales': values})
        return list(P.table_calc(df, plan, m))

    def test_pct_diff_from_zero_is_null(self):
        got = self.run_calc("<table-calc diff-options='Relative' ordering-type='Rows' type='PctDiff'>"
                            "<address><value>-1</value></address></table-calc>", [0.0, 5.0, 0.0, 2.0])
        self.assertTrue(pd.isna(got[0]) and pd.isna(got[1]))
        self.assertAlmostEqual(got[2], -1.0)
        self.assertTrue(pd.isna(got[3]))

    def test_pct_total_of_zero_total_is_null(self):
        got = self.run_calc("<table-calc ordering-type='Rows' type='PctTotal' />", [0.0, 0.0, 0.0, 0.0])
        self.assertTrue(all(pd.isna(v) for v in got))

    def test_running_average_ignores_nulls(self):
        got = self.run_calc("<table-calc aggregation='Avg' ordering-type='Rows' type='CumTotal' />",
                            [1.0, None, 3.0, 8.0])
        self.assertAlmostEqual(got[0], 1.0)
        self.assertAlmostEqual(got[2], 2.0)          # (1 + 3) / 2, the Null row is not counted
        self.assertAlmostEqual(got[3], 4.0)          # (1 + 3 + 8) / 3

    def test_rank_of_null_is_null_and_ties_follow_the_rank_option(self):
        x = "<table-calc ordering-type='Rows' rank-options='{o},Descending' type='Rank' />"
        vals = [5.0, None, 5.0, 2.0]
        self.assertTrue(pd.isna(self.run_calc(x.format(o='Competition'), vals)[1]))
        self.assertEqual([self.run_calc(x.format(o=o), vals)[i] for o in ('Competition', 'Modified', 'Dense')
                          for i in (0, 3)], [1, 3, 2, 3, 1, 2])


if __name__ == '__main__':
    unittest.main()
