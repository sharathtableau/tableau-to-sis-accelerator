"""V10: Tableau custom tooltips. Written before the code. 497 of 663 corpus worksheets have a <customized-tooltip>;
the plan's charts showed a generic field list instead. Rule: one tooltip row per tooltip line; a line starting with
'Label:' shows that label and the rest; field references show the mark's value in the field's format; viz-in-tooltip
references and fields not on the view are left out."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools')]
import v3_sheet_plan as P  # noqa: E402
import v3_tooltips as T  # noqa: E402

XML = """<customized-tooltip><formatted-text>
<run bold='true'><![CDATA[<[d].[none:Segment:nk]>]]></run><run>Æ&#10;</run>
<run fontcolor='#787878'>Sales:&#9;</run><run><![CDATA[<[d].[sum:Sales:qk]>]]></run><run>Æ&#10;</run>
<run><![CDATA[<[d].[none:Orders:nk]> Orders]]></run><run>Æ&#10;</run>
<run><![CDATA[<Sheet name="Detail" maxwidth="300" maxheight="300" filter="<All Fields>">]]></run><run>Æ&#10;</run>
<run>Gone:&#9;</run><run><![CDATA[<[d].[sum:Other:qk]>]]></run>
</formatted-text></customized-tooltip>"""


class Eng:
    import altair as alt
    value_labels = staticmethod(lambda cap: {})
    cat_colors = staticmethod(lambda cap, values: (None, None))


def plan_frame():
    import pandas as pd
    fields = [{'id': 'g', 'caption': 'Segment', 'channel': 'rows', 'kind': 'dim', 'discrete': True, 'date_part': 'none'},
              {'id': 's', 'caption': 'Sales', 'channel': 'cols', 'kind': 'measure', 'agg': 'sum', 'discrete': False,
               'date_part': 'none', 'format': 'c"$"#,##0;-"$"#,##0'},
              {'id': 'o', 'caption': 'Orders', 'channel': 'tooltip', 'kind': 'dim', 'discrete': True,
               'date_part': 'none'}]
    by_token = {'[d].[none:Segment:nk]': fields[0], '[d].[sum:Sales:qk]': fields[1], '[d].[none:Orders:nk]': fields[2]}
    plan = {'schema': P.SCHEMA, 'mark': 'bar', 'sort': [], 'aggregate': True, 'fields': fields,
            'tooltip_lines': T.bind(T.parse(XML), by_token)}
    df = pd.DataFrame({'D|none|Segment': ['A', 'B'], 'M|sum|Sales': [1234.0, 56.0], 'D|none|Orders': [3, 4]})
    return plan, df


class Tooltips(unittest.TestCase):
    def test_parse_skips_viz_in_tooltip(self):
        lines = T.parse(XML)
        self.assertEqual(len(lines), 4)
        self.assertFalse(any('Sheet' in str(l) for l in lines))

    def test_rows(self):
        plan, df = plan_frame()
        cols = T.columns(plan, df, Eng)
        self.assertEqual([c[0] for c in cols[1:]], ['Sales', '​​​'])
        self.assertEqual(cols[0][1], ['A', 'B'])
        self.assertEqual(cols[1][1], ['$1,234', '$56'])
        self.assertEqual(cols[2][1], ['3 Orders', '4 Orders'])
        self.assertEqual(len(cols), 3)                         # 'Gone:' (field not on the view) left out

    def test_on_the_chart(self):
        plan, df = plan_frame()
        d = P.build_chart(plan, df, Eng).to_dict()
        tips = d['encoding']['tooltip']
        self.assertEqual([t['field'] for t in tips], ['__tip0', '__tip1', '__tip2'])
        self.assertEqual(tips[1]['title'], 'Sales')

    def test_superstore_sales_by_segment(self):
        import glob
        import engine
        import plan_census as PC
        from v3_local import preview_context
        path = glob.glob(str(ROOT / 'Workbooks' / '**' / 'Superstore_Tableau2024_3.twbx'), recursive=True)[0]
        r = PC.intake(path)
        ir = r['ir']
        s = next(s for d in ir['dashboards'] for s in d['sheets'] if s['name'] == 'Sales by Segment')
        pl = P.from_sheet(s, ir)
        self.assertTrue(pl.get('tooltip_lines'))
        with preview_context(ir, {k: v for k, v in r['bindings'].items() if v}):
            df = P.execute(pl, s, engine)
            cols = T.columns(pl, df, engine)
        titles = [c[0] for c in cols]
        self.assertIn('Sales', titles)
        self.assertIn('Profit', titles)


if __name__ == '__main__':
    unittest.main()
