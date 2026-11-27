"""V11: the worksheet's line formatting on the plan's charts. Written before the code.
  * reference lines: <style-rule element='refline'> line-visibility / stroke-size / stroke-color, for all lines of the
    sheet (no id) or one line (id='refline0', which wins). 'off' hides the line (its band still shows);
  * pane dividers: <style-rule element='table-div'> line-visibility='off' on Rows and Columns -> panes drawn without
    their border.
Most corpus sheets carry these as Tableau 'clean' templates (refline off: 178 sheets, table-div off: 225 / 207)."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools')]
import v3_reflines as R  # noqa: E402
import v3_sheet_plan as P  # noqa: E402

LINE = ("<reference-line axis-column='[d].[sum:S:qk]' formula='average' id='refline{i}' label-type='none' "
        "scope='per-table' value-column='[d].[sum:S:qk]' />")


def xml(rules, n=2):
    return ("<worksheet><table><style><style-rule element='refline'>" + rules + "</style-rule></style><view>"
            + ''.join(LINE.format(i=i) for i in range(n)) + "</view></table></worksheet>")


class RefLineStyle(unittest.TestCase):
    def test_sheet_wide_off(self):
        specs = R.parse(xml("<format attr='line-visibility' value='off' /><format attr='stroke-size' value='0' />"))
        self.assertEqual([s['line']['visible'] for s in specs], [False, False])

    def test_a_line_of_its_own_wins(self):
        specs = R.parse(xml("<format attr='line-visibility' value='off' />"
                            "<format attr='line-visibility' id='refline1' value='on' />"
                            "<format attr='stroke-size' id='refline1' value='3' />"
                            "<format attr='stroke-color' id='refline1' value='#ff0000' />"))
        self.assertEqual(specs[0]['line']['visible'], False)
        self.assertEqual(specs[1]['line'], {'visible': True, 'width': 3.0, 'color': '#ff0000'})

    def test_default_is_visible(self):
        self.assertTrue(R.parse(xml(''))[0]['line']['visible'])

    def test_hidden_line_is_not_drawn(self):
        import altair
        import pandas as pd
        plan = {'schema': P.SCHEMA, 'mark': 'bar', 'sort': [], 'aggregate': True, 'fields': [
            {'id': 'k', 'caption': 'K', 'channel': 'rows', 'kind': 'dim', 'discrete': True, 'date_part': 'none'},
            {'id': 's', 'caption': 'S', 'channel': 'cols', 'kind': 'measure', 'agg': 'sum', 'discrete': False,
             'date_part': 'none'}], 'reflines': R.parse(xml("<format attr='line-visibility' value='off' />", 1))}
        R.bind(plan)

        class Eng:
            alt = altair
            value_labels = staticmethod(lambda cap: {})
            cat_colors = staticmethod(lambda cap, values: (None, None))
        d = str(P.build_chart(plan, pd.DataFrame({'D|none|K': ['a'], 'M|sum|S': [1.0]}), Eng).to_dict())
        self.assertNotIn("'rule'", d)


class PaneDividers(unittest.TestCase):
    def test_dividers_off_removes_pane_borders(self):
        import altair as alt
        import pandas as pd
        chart = alt.Chart(pd.DataFrame({'a': [1]})).mark_bar().encode(x='a:Q')
        style = {'table-div|rows': {'line-visibility': 'off'}, 'table-div|cols': {'line-visibility': 'off'}}
        d = P.apply_ws_style(chart, style, alt).to_dict()
        self.assertEqual(d['config']['view']['strokeWidth'], 0)

    def test_default_keeps_them(self):
        import altair as alt
        import pandas as pd
        chart = alt.Chart(pd.DataFrame({'a': [1]})).mark_bar().encode(x='a:Q')
        d = P.apply_ws_style(chart, {}, alt).to_dict()
        self.assertNotIn('strokeWidth', d.get('config', {}).get('view', {}))


if __name__ == '__main__':
    unittest.main()
