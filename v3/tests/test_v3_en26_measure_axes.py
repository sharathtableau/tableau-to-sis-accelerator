"""EN26 / E16: more than one measure on Rows or Columns. Written before the code.
Tableau draws each measure on a shelf in its own pane, side by side (separate axes) -- unless the workbook folds a
measure onto the previous one's axis (dual axis: <encoding attr='space' field=M fold='true'>), optionally with
synchronised scales (synchronized='true'). Each measure keeps its own pane's mark (bar + line combo). The plan
previously drew only the LAST measure on the shelf and dropped the others."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools')]
import v3_sheet_plan as P  # noqa: E402

A, B = '[d].[sum:A:qk]', '[d].[sum:B:qk]'


def sheet(fold=False, sync=False, marks=('Bar', 'Line')):
    enc = (f"<encoding attr='space' class='0' field='{B}' field-type='quantitative' fold='true' scope='rows'"
           f"{' synchronized=' + chr(39) + 'true' + chr(39) if sync else ''} type='space' />") if fold else ''
    xml = (f"<worksheet name='S'><table><style><style-rule element='axis'>{enc}</style-rule></style><rows>({A} + {B})"
           f"</rows><cols>[d].[none:K:nk]</cols></table></worksheet>")
    occ = [{'id': 'k', 'channel': 'cols', 'caption': 'K', 'role': 'dimension', 'discrete': True, 'token': '[d].[none:K:nk]',
            'datepart': None},
           {'id': 'a', 'channel': 'rows', 'caption': 'A', 'role': 'measure', 'aggregation': 'sum', 'discrete': False,
            'token': A, 'datepart': None},
           {'id': 'b', 'channel': 'rows', 'caption': 'B', 'role': 'measure', 'aggregation': 'sum', 'discrete': False,
            'token': B, 'datepart': None}]
    panes = [{'id': '1', 'marks': ['Automatic'], 'axes': {}},
             {'id': '2', 'marks': [marks[0]], 'axes': {'y-axis-name': A}},
             {'id': '3', 'marks': [marks[1]], 'axes': {'y-axis-name': B}}]
    return {'name': 'S', 'worksheet_semantics': {'source_xml': xml, 'occurrences': occ, 'panes': panes}}


def frame():
    import pandas as pd
    return pd.DataFrame({'D|none|K': ['x', 'y', 'z'], 'M|sum|A': [1.0, 2.0, 3.0], 'M|sum|B': [100.0, 50.0, 10.0]})


class Eng:
    import altair as alt
    value_labels = staticmethod(lambda cap: {})
    cat_colors = staticmethod(lambda cap, values: (None, None))


class Plan(unittest.TestCase):
    def test_side_by_side_by_default(self):
        plan = P.from_sheet(sheet())
        self.assertEqual(plan['measure_axes']['rows'], [{'measures': ['a'], 'synchronized': False},
                                                         {'measures': ['b'], 'synchronized': False}])
        self.assertEqual(plan['measure_marks'], {'a': 'bar', 'b': 'line'})
        self.assertEqual(P.validate(plan), [])

    def test_fold_is_a_dual_axis(self):
        plan = P.from_sheet(sheet(fold=True, sync=True))
        self.assertEqual(plan['measure_axes']['rows'], [{'measures': ['a', 'b'], 'synchronized': True}])

    def test_one_measure_has_no_axes_entry(self):
        s = sheet()
        s['worksheet_semantics']['occurrences'] = s['worksheet_semantics']['occurrences'][:2]
        self.assertNotIn('measure_axes', P.from_sheet(s))


class Chart(unittest.TestCase):
    def spec(self, **kw):
        plan = P.from_sheet(sheet(**kw))
        return P.build_chart(plan, frame(), Eng).to_dict()

    def test_side_by_side_draws_every_measure_in_its_own_pane(self):
        d = self.spec()
        self.assertIn('vconcat', d)                      # measures on Rows stack vertically
        fields = [c['encoding']['y']['field'] for c in d['vconcat']]
        self.assertEqual(fields, ['M_sum_A', 'M_sum_B'])
        self.assertEqual([c['mark']['type'] if isinstance(c['mark'], dict) else c['mark'] for c in d['vconcat']],
                         ['bar', 'line'])

    def test_dual_axis_overlays_with_independent_scales(self):
        d = self.spec(fold=True)
        self.assertIn('layer', d)
        self.assertEqual(d['resolve']['scale']['y'], 'independent')
        self.assertEqual(len(d['layer']), 2)

    def test_synchronised_dual_axis_shares_the_scale(self):
        d = self.spec(fold=True, sync=True)
        self.assertIn('layer', d)
        self.assertNotEqual((d.get('resolve') or {}).get('scale', {}).get('y'), 'independent')


class Corpus(unittest.TestCase):
    """World Indicators: 'Technology' (two measures folded, synchronised, line + bar) and 'Health' (two measures side
    by side on Columns); a second workbook: Clara 'Dual Trend' (folded, not synchronised)."""
    def plan_of(self, wb, name):
        import glob
        import plan_census as PC
        path = glob.glob(str(ROOT / 'Workbooks' / '**' / wb), recursive=True)[0]
        ir = PC.intake(path)['ir']
        s = next(s for d in ir['dashboards'] for s in d['sheets'] if s['name'] == name)
        return P.from_sheet(s, ir)

    def test_world_indicators(self):
        t = self.plan_of('World Indicators.twbx', 'Technology')
        self.assertEqual([(len(g['measures']), g['synchronized']) for g in t['measure_axes']['rows']], [(2, True)])
        h = self.plan_of('World Indicators.twbx', 'Health')
        self.assertEqual([len(g['measures']) for g in h['measure_axes']['cols']], [1, 1])

    def test_clara_dual_trend(self):
        c = self.plan_of('Clara Brazil EDA Dashboard.twbx', 'Dual Trend')
        self.assertEqual([(len(g['measures']), g['synchronized']) for g in c['measure_axes']['rows']], [(2, False)])


if __name__ == '__main__':
    unittest.main()
