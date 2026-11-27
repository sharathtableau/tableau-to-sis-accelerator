"""V4: Measure Values on an axis with the same measures also on Text / Label. Written before the fix.
Customer Opt-In 'Table A (2)': Measure Values on Columns and Text, plus the individual measures (Optin Base, ...) on
Text. When the measures are unpivoted into one Measure Values column, each bar's label is that bar's own measure value
(Tableau labels a Measure Values mark with its value); the individual measure pills point at the same values and must
not break the chart (it raised KeyError: 'M_sum_Optin_Base')."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools')]
import v3_sheet_plan as P  # noqa: E402


class Eng:
    import altair as alt
    value_labels = staticmethod(lambda cap: {})
    cat_colors = staticmethod(lambda cap, values: (None, None))


def plan_and_frame():
    import pandas as pd
    plan = {'schema': P.SCHEMA, 'mark': 'bar', 'sort': [], 'aggregate': True, 'fields': [
        {'id': 'n', 'caption': 'Measure Names', 'channel': 'cols', 'kind': 'measure_names', 'discrete': True,
         'date_part': 'none', 'agg': 'none'},
        {'id': 'v', 'caption': 'Measure Values', 'channel': 'cols', 'kind': 'measure', 'measure_values': True,
         'discrete': False, 'date_part': 'none', 'agg': 'none'},
        {'id': 'c', 'caption': 'Channel', 'channel': 'rows', 'kind': 'dim', 'discrete': True, 'date_part': 'none'},
        {'id': 't', 'caption': 'Measure Values', 'channel': 'text', 'kind': 'measure', 'measure_values': True,
         'discrete': False, 'date_part': 'none', 'agg': 'none'},
        {'id': 'a', 'caption': 'A', 'channel': 'text', 'kind': 'measure', 'agg': 'sum', 'discrete': False,
         'date_part': 'none'},
        {'id': 'b', 'caption': 'B', 'channel': 'text', 'kind': 'measure', 'agg': 'sum', 'discrete': False,
         'date_part': 'none'}],
        'measure_values': [{'caption': 'A', 'agg': 'sum'}, {'caption': 'B', 'agg': 'sum'}]}
    df = pd.DataFrame({'D|none|Channel': ['x', 'y'], 'M|sum|A': [1.0, 2.0], 'M|sum|B': [10.0, 20.0]})
    return plan, df


class Labels(unittest.TestCase):
    def test_chart_draws_and_each_bar_is_labelled_with_its_own_value(self):
        plan, df = plan_and_frame()
        d = P.build_chart(plan, df, Eng).to_dict()
        rows = next(iter(d['datasets'].values()))
        got = sorted((r['D_none_Measure_Names'], r['D_none_Channel'], r['__label']) for r in rows)
        self.assertEqual([g[2] for g in got], ['1', '2', '10', '20'])

    def test_corpus_sheet(self):
        import glob
        import engine
        import plan_census as PC
        from v3_local import preview_context
        path = glob.glob(str(ROOT / 'Workbooks' / '**' / 'Customer Opt-In_Out Analysis.twbx'), recursive=True)[0]
        r = PC.intake(path)
        ir = r['ir']
        s = next(s for d in ir['dashboards'] for s in d['sheets'] if s['name'] == 'Table A (2)')
        plan = P.from_sheet(s, ir)
        with preview_context(ir, {k: v for k, v in r['bindings'].items() if v}):
            df = P.execute(plan, s, engine)
            self.assertIsNotNone(P.build_chart(plan, df, engine, {'w': 700, 'h': 400}).to_dict())


class FieldNames(unittest.TestCase):
    def test_captions_that_differ_only_in_symbols_stay_apart(self):
        self.assertEqual(list(P._safe_names(['D|none|"> PY"', 'D|none|"< PY"']).values()),
                         ['D_none____PY_', 'D_none____PY__2'])


if __name__ == '__main__':
    unittest.main()
