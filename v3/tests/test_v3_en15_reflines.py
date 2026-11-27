"""EN15: Tableau reference lines, bands and distributions on the Tableau-comparison path (v3_sheet_plan). Written
before the code. Tableau's rules (Analytics pane):
  * a line is computed from its VALUE field (which may differ from the axis measure, e.g. a target on a Sales axis --
    a bullet chart) or is a constant / parameter value;
  * formula: average, median, min, max, sum, total over the marks of its scope; 'quantiles' with tile-count n draws
    the n-1 quantile lines of the marks' values;
  * scope: per-table (all marks), per-pane (marks sharing every discrete header but the innermost on each of Rows /
    Columns), per-cell (marks sharing every discrete header);
  * a percentage distribution (<reference-line-value percentage='80'/>) draws the line at 80% of that value; fill
    below / above shades the band;
  * label: none, value, automatic (the value), custom ('<Value>' and '<FieldName>' substituted).
Values here are computed independently in Python over the view's marks."""
from pathlib import Path
import statistics
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools')]
import v3_reflines as R  # noqa: E402

XML = """<worksheet name='S'><table><view>
<reference-line axis-column='[d].[sum:Sales:qk]' formula='average' id='refline0' label='Avg &lt;Value&gt;'
  label-type='custom' scope='per-table' value-column='[d].[sum:Sales:qk]' />
<reference-line axis-column='[d].[sum:Sales:qk]' formula='median' id='refline1' label-type='none' scope='per-cell'
  value-column='[d].[sum:Target:qk]' />
<reference-line axis-column='[d].[sum:Sales:qk]' fill-above='false' fill-below='true' formula='average' id='refline2'
  label-type='none' percentage-bands='true' scope='per-cell' value-column='[d].[sum:Target:qk]'>
  <reference-line-value percentage='80' /></reference-line>
<reference-line axis-column='[d].[sum:Sales:qk]' formula='average' id='refline3' label-type='value' scope='per-pane'
  value-column='[Parameters].[Goal]' />
<reference-line axis-column='[d].[avg:Ease:qk]' fill-above='true' fill-below='true' formula='quantiles' id='refline4'
  label-type='automatic' scope='per-pane' tile-count='4' value-column='[d].[avg:Ease:qk]' />
</view></table></worksheet>"""

# marks: Region (outer, rows) > Category (inner, rows) ; Segment on colour (several marks per cell)
MARKS = [('E', 'a', 'x', 10, 20), ('E', 'a', 'y', 30, 20), ('E', 'b', 'x', 5, 8), ('E', 'b', 'y', 15, 8),
         ('W', 'a', 'x', 40, 50), ('W', 'b', 'x', 1, 2), ('W', 'b', 'y', 3, 2)]


def fields():
    return [{'id': 'r', 'caption': 'Region', 'kind': 'dim', 'channel': 'rows', 'discrete': True, 'date_part': 'none'},
            {'id': 'c', 'caption': 'Category', 'kind': 'dim', 'channel': 'rows', 'discrete': True,
             'date_part': 'none'},
            {'id': 's', 'caption': 'Sales', 'kind': 'measure', 'channel': 'cols', 'agg': 'sum', 'date_part': 'none',
             'discrete': False},
            {'id': 'g', 'caption': 'Segment', 'kind': 'dim', 'channel': 'color', 'discrete': True,
             'date_part': 'none'},
            {'id': 't', 'caption': 'Target', 'kind': 'measure', 'channel': 'refline', 'agg': 'sum',
             'date_part': 'none', 'discrete': False}]


def frame():
    import pandas as pd
    return pd.DataFrame(MARKS, columns=['D|none|Region', 'D|none|Category', 'D|none|Segment', 'M|sum|Sales',
                                        'M|sum|Target'])


class Parse(unittest.TestCase):
    def test_reads_every_kind(self):
        specs = R.parse(XML)
        self.assertEqual([s['formula'] for s in specs], ['average', 'median', 'average', 'average', 'quantiles'])
        self.assertEqual([s['scope'] for s in specs], ['per-table', 'per-cell', 'per-cell', 'per-pane', 'per-pane'])
        self.assertEqual((specs[0]['value']['agg'], specs[0]['value']['name']), ('sum', 'Sales'))
        self.assertEqual(specs[1]['value']['name'], 'Target')
        self.assertEqual(specs[2]['percentages'], [80.0])
        self.assertTrue(specs[2]['fill_below'])
        self.assertEqual(specs[3]['value'], {'param': 'Goal'})
        self.assertEqual(specs[4]['tiles'], 4)
        self.assertEqual(specs[0]['label'], 'Avg <Value>')

    def test_no_reference_lines(self):
        self.assertEqual(R.parse('<worksheet />'), [])


class Values(unittest.TestCase):
    def values(self, i, params=None):
        plan = {'fields': fields(), 'reflines': R.parse(XML)}
        R.bind(plan)
        return R.values(plan, frame(), params or {})[i]

    def test_per_table_average_of_the_marks(self):
        v = self.values(0)
        self.assertEqual(set(v['lines'][0]), {statistics.mean(m[3] for m in MARKS)})
        self.assertEqual(v['axis'], 'M|sum|Sales')

    def test_per_cell_median_of_another_field(self):
        v = self.values(1)
        want = {}
        for r, c, _g, _s, t in MARKS:
            want.setdefault((r, c), []).append(t)
        got = [(r, c, x) for (r, c, *_m), x in zip(MARKS, v['lines'][0])]
        self.assertTrue(all(x == statistics.median(want[(r, c)]) for r, c, x in got))

    def test_percentage_distribution_is_a_band(self):
        v = self.values(2)
        cell = {}
        for r, c, _g, _s, t in MARKS:
            cell.setdefault((r, c), []).append(t)
        self.assertEqual([round(x, 9) for x in v['lines'][0]],
                         [round(0.8 * statistics.mean(cell[(r, c)]), 9) for r, c, *_m in MARKS])
        self.assertEqual(v['fill'], 'below')

    def test_parameter_value(self):
        self.assertEqual(set(self.values(3, {'Goal': '25'})['lines'][0]), {25.0})

    def test_per_pane_is_every_header_but_the_innermost(self):
        plan = {'fields': fields(), 'reflines': [dict(R.parse(XML)[0], scope='per-pane')]}
        R.bind(plan)
        v = R.values(plan, frame(), {})[0]
        by_region = {}
        for r, _c, _g, s, _t in MARKS:
            by_region.setdefault(r, []).append(s)
        self.assertEqual(v['lines'][0], [statistics.mean(by_region[m[0]]) for m in MARKS])

    def test_quantiles(self):
        import pandas as pd
        f = [{'id': 'r', 'caption': 'Region', 'kind': 'dim', 'channel': 'rows', 'discrete': True, 'date_part': 'none'},
             {'id': 'e', 'caption': 'Ease', 'kind': 'measure', 'channel': 'cols', 'agg': 'avg', 'date_part': 'none',
              'discrete': False}]
        df = pd.DataFrame({'D|none|Region': list('abcdefgh'), 'M|avg|Ease': [1, 2, 3, 4, 5, 6, 7, 8]})
        plan = {'fields': f, 'reflines': [R.parse(XML)[4]]}
        R.bind(plan)
        v = R.values(plan, df, {})[0]
        self.assertEqual(len(v['lines']), 3)
        self.assertEqual([set(l) for l in v['lines']], [{2.75}, {4.5}, {6.25}])     # Tableau: linear quantiles

    def test_labels(self):
        self.assertEqual(R.label_text({'label_type': 'custom', 'label': 'Avg <Value>'}, 1234.5, 'n#,##0'),
                         'Avg 1,235')
        self.assertEqual(R.label_text({'label_type': 'none'}, 5, None), '')
        self.assertEqual(R.label_text({'label_type': 'value'}, 5, None), '5')


class InThePlan(unittest.TestCase):
    def test_value_field_off_the_view_joins_the_query(self):
        plan = {'fields': [f for f in fields() if f['id'] != 't'], 'reflines': R.parse(XML)}
        R.bind(plan)
        extra = [f for f in plan['fields'] if f['channel'] == 'refline']
        self.assertEqual([(f['caption'], f['agg']) for f in extra], [('Target', 'sum')])

    def test_drawn_on_the_chart(self):
        import altair
        import v3_sheet_plan as P
        plan = {'schema': P.SCHEMA, 'mark': 'bar', 'fields': fields(), 'sort': [], 'aggregate': True,
                'reflines': R.parse(XML)[:3]}
        R.bind(plan)

        class Eng:
            alt = altair
            value_labels = staticmethod(lambda cap: {})
            cat_colors = staticmethod(lambda cap, values: (None, None))
        spec = str(P.build_chart(plan, frame(), Eng).to_dict())
        self.assertIn("'rule'", spec)          # the per-table line
        self.assertIn("'tick'", spec)          # the per-cell lines on the Category band
        self.assertIn('__ref0_0', spec)
        self.assertEqual(P.validate(plan), [])


class Corpus(unittest.TestCase):
    """Two corpus workbooks: R1 'Performance' (bullet: per-cell target line and 80% band) and ECommerce
    'Revenue vs. Customers' (per-table averages on two axes)."""
    def plan_of(self, wb, sheet):
        import glob
        import plan_census as PC
        import v3_sheet_plan as P
        path = next(p for p in glob.glob(str(ROOT / 'Workbooks' / '**' / wb), recursive=True))
        ir = PC.intake(path)['ir']
        s = next(s for d in ir['dashboards'] for s in d['sheets'] if s['name'] == sheet)
        return P.from_sheet(s, ir)

    def test_r1_performance(self):
        plan = self.plan_of('R1_Test_Upload_-_Superstore.twbx', 'Performance')
        self.assertEqual([(r['formula'], r['scope']) for r in plan['reflines']],
                         [('average', 'per-cell'), ('average', 'per-cell')])
        self.assertTrue(all(r.get('value_col') for r in plan['reflines']))

    def test_ecommerce_revenue_vs_customers(self):
        plan = self.plan_of('E-Commerce (Software) Sales Dashboard VOTD.twbx', 'Revenue vs. Customers')
        self.assertEqual([(r['formula'], r['scope']) for r in plan['reflines']][:2],
                         [('average', 'per-table'), ('average', 'per-table')])



class Parameters(unittest.TestCase):
    def test_a_parameter_line_finds_the_parameter_by_its_caption(self):
        plan = {'fields': fields(), 'reflines': [R.parse(XML)[3]]}
        R.bind(plan, captions={'Goal': 'Sales Goal'})
        self.assertEqual(plan['reflines'][0]['value'], {'param': 'Sales Goal'})
        self.assertEqual(set(R.values(plan, frame(), {'Sales Goal': '7'})[0]['lines'][0]), {7.0})


if __name__ == '__main__':
    unittest.main()
