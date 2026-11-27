"""V6: pie / donut marks on the plan path. Written before the code. Tableau's Pie mark: one wedge per member of the
colour field, angle = the Angle (wedge-size) measure, labels from Text / Label. A donut is Tableau's two-pie dual-axis
pattern (a dummy measure such as MIN(0) on both axes of Rows): drawn as a ring. The plan returned no chart for a pie
(the app showed a table)."""
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


def plan(donut=False):
    f = [{'id': 'c', 'caption': 'Segment', 'channel': 'color', 'kind': 'dim', 'discrete': True, 'date_part': 'none'},
         {'id': 'w', 'caption': 'Profit', 'channel': 'wedge-size', 'kind': 'measure', 'agg': 'sum', 'discrete': False,
          'date_part': 'none'},
         {'id': 't', 'caption': 'Profit', 'channel': 'text', 'kind': 'measure', 'agg': 'sum', 'discrete': False,
          'date_part': 'none'}]
    if donut:
        f += [{'id': 'z1', 'caption': 'MIN(0)', 'channel': 'rows', 'kind': 'measure', 'agg': 'usr', 'discrete': False,
               'date_part': 'none'},
              {'id': 'z2', 'caption': 'MIN(0)', 'channel': 'rows', 'kind': 'measure', 'agg': 'usr', 'discrete': False,
               'date_part': 'none'}]
    return {'schema': P.SCHEMA, 'mark': 'pie', 'sort': [], 'aggregate': True, 'fields': f}


def frame():
    import pandas as pd
    return pd.DataFrame({'D|none|Segment': ['A', 'B', 'C'], 'M|sum|Profit': [10.0, 30.0, 60.0],
                         'M|usr|MIN(0)': [0, 0, 0]})


class Pie(unittest.TestCase):
    def test_wedges_by_colour_with_angle_and_labels(self):
        d = P.build_chart(plan(), frame(), Eng).to_dict()
        text = str(d)
        self.assertIn("'arc'", text)
        self.assertIn("'theta'", text)
        self.assertIn('D_none_Segment', text)
        self.assertIn("'text'", text)                 # the Text field labels the wedges

    def test_donut(self):
        self.assertIn("'innerRadius'", str(P.build_chart(plan(donut=True), frame(), Eng).to_dict()))

    def test_corpus(self):
        import glob
        import engine
        import plan_census as PC
        from v3_local import preview_context
        for wb, sheet in (('Globalsalesdashboard.twbx', 'Profit by Segment'),
                          ('Customer Opt-In_Out Analysis.twbx', 'Pie')):
            path = glob.glob(str(ROOT / 'Workbooks' / '**' / wb), recursive=True)[0]
            r = PC.intake(path)
            ir = r['ir']
            s = next(s for d in ir['dashboards'] for s in d['sheets'] if s['name'] == sheet)
            pl = P.from_sheet(s, ir)
            with preview_context(ir, {k: v for k, v in r['bindings'].items() if v}):
                df = P.execute(pl, s, engine)
                self.assertIn("'arc'", str(P.build_chart(pl, df, engine, {'w': 400, 'h': 300}).to_dict()), sheet)


if __name__ == '__main__':
    unittest.main()
