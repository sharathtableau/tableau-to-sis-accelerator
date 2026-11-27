"""V9: Gantt bars on a continuous (numeric) axis. Tableau draws a Gantt bar from the axis value for the length of the
Size measure (on a date axis: Size days). Written before the code; previously only date axes used Size as length,
a numeric axis drew Size as bar thickness."""
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


class Gantt(unittest.TestCase):
    def test_numeric_axis_bar_runs_for_size(self):
        import pandas as pd
        plan = {'schema': P.SCHEMA, 'mark': 'gantt', 'sort': [], 'aggregate': True, 'fields': [
            {'id': 'x', 'caption': 'Start', 'channel': 'cols', 'kind': 'measure', 'agg': 'min', 'discrete': False,
             'date_part': 'none'},
            {'id': 'r', 'caption': 'Task', 'channel': 'rows', 'kind': 'dim', 'discrete': True, 'date_part': 'none'},
            {'id': 's', 'caption': 'Length', 'channel': 'size', 'kind': 'measure', 'agg': 'sum', 'discrete': False,
             'date_part': 'none'}]}
        df = pd.DataFrame({'D|none|Task': ['a', 'b'], 'M|min|Start': [1.0, 4.0], 'M|sum|Length': [2.0, 3.0]})
        d = P.build_chart(plan, df, Eng).to_dict()
        rows = next(iter(d['datasets'].values()))
        self.assertEqual([r['__end'] for r in rows], [3.0, 7.0])
        self.assertEqual(d['encoding']['x2']['field'], '__end')
        self.assertNotIn('size', d['encoding'])


if __name__ == '__main__':
    unittest.main()
