"""A6: reference lines / bands / distributions on every axis Tableau draws them on. Written with the code; the
binder found only measure pills, so lines on a Measure Values axis and on a continuous date axis were dropped
('axis field not on the view') -- corpus: 5 sheets in 4 workbooks.
Rule (generic): axis 'Multiple Values' = the unpivoted Measure Values axis (computed over the long form; a value
field that is itself one of the Measure Values stays in the query); a continuous date on Rows / Columns gets a
date line (min / max / average / median of the dates); Measure Values only on Text = a text table, no axis, no
line (said so). A Measure Values member carries its field id, so two calculations sharing a caption resolve.
Proof: Superstore 2024.3 'CommissionProjection' (max OTE over Commission + Base), Online Retail 'Q. top quantity'
(130% of max quantity, members '0' / 'quantity'), Campaign Incrementality 'Daily' (date line, hidden by the
author), Customer Opt-In 'Table A (2)' (Measure Values on
Columns); a synthetic text table."""
import glob
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
import v3_reflines as R  # noqa: E402
import v3_sheet_plan as P  # noqa: E402


def wb(name):
    hits = glob.glob(str(ROOT / 'Workbooks' / '**' / name), recursive=True)
    return Path(hits[0]) if hits else None


def render(path, sheet_name):
    import engine
    import plan_census as PC
    from v3_local import preview_context
    r = PC.intake(path)
    sheet = next(s for d in r['ir']['dashboards'] for s in d['sheets'] if s['name'] == sheet_name)
    with preview_context(r['ir'], {k: v for k, v in r['bindings'].items() if v}):
        plan = P.from_sheet(sheet, r['ir'])
        df = P.execute(plan, sheet, engine)
        chart = P.build_chart(plan, df, engine)
        T = engine.tbl(sheet)
        return plan, df, chart, T, r, sheet


def ref_values(chart, name='__ref0_0'):
    d = chart.to_dict()
    for rows in (d.get('datasets') or {}).values():
        if rows and name in rows[0]:
            return {r[name] for r in rows}
    return set()


SS, OR = wb('Superstore_Tableau2024_3.twbx'), wb('Online Retail Dashboard.twbx')
CI, CO = wb('Campaign Incrementatlity vs Holdout Group.twbx'), wb('Customer Opt-In_Out Analysis.twbx')


@unittest.skipUnless(SS, 'corpus workbook')
class MeasureValuesAxis(unittest.TestCase):
    def test_commission_projection_max_ote(self):
        import engine
        from v3_local import preview_context
        plan, df, chart, T, r, sheet = render(SS, 'CommissionProjection')
        spec = plan['reflines'][0]
        self.assertEqual((spec['axis_col'], spec.get('unbound')), ('M|mv|Measure Values', None))
        self.assertIn('"rule"', str(chart.to_dict()).replace("'", '"'))
        got = ref_values(chart)
        self.assertEqual(len(got), 1)
        self.assertAlmostEqual(next(iter(got)), float(df[spec['value_col']].max()), places=2)


@unittest.skipUnless(OR, 'corpus workbook')
class SecondWorkbook(unittest.TestCase):
    def test_shared_caption_members_and_130_percent_line(self):
        plan, df, chart, *_ = render(OR, 'Q. top quantity')
        self.assertEqual([m['caption'] for m in plan['measure_values']], ['0', 'quantity'])
        self.assertTrue(all(m.get('member_id') for m in plan['measure_values']))
        self.assertFalse(any('field_id' in m for m in plan['measure_values']))     # not an IR contract field id
        spec = plan['reflines'][0]
        got = next(iter(ref_values(chart)))
        self.assertAlmostEqual(got, 1.3 * float(df[spec['value_col']].max()), places=2)


@unittest.skipUnless(CI and CO, 'corpus workbooks')
class DateAndText(unittest.TestCase):
    def test_date_axis_binds_and_respects_hidden_line(self):
        import tableau_parser as TP
        ir = TP.build_ir(str(CI), scoped=True)
        sheet = next(s for d in ir['dashboards'] for s in d['sheets'] if s['name'] == 'Daily')
        spec = P.from_sheet(sheet, ir)['reflines'][0]
        self.assertTrue(spec.get('temporal'))
        self.assertIsNone(spec.get('unbound'))
        self.assertFalse(spec['line']['visible'])                       # the author hid it: nothing drawn

    def test_measure_values_on_columns_and_text(self):
        import tableau_parser as TP
        ir = TP.build_ir(str(CO), scoped=True)
        sheet = next(s for d in ir['dashboards'] for s in d['sheets'] if s['name'] == 'Table A (2)')
        spec = P.from_sheet(sheet, ir)['reflines'][0]
        self.assertEqual((spec.get('unbound'), spec['axis_col'], spec.get('value')), (None, 'M|mv|Measure Values', None))
        holder = {'fields': [{'channel': 'text', 'measure_values': True, 'kind': 'measure', 'caption': 'Measure Values'},
                             {'channel': 'rows', 'kind': 'dim', 'discrete': True, 'caption': 'C'}],
                  'reflines': [dict(spec, axis={'name': 'Multiple Values', 'agg': 'none'}, unbound=None)]}
        holder['reflines'][0].pop('unbound')
        R.bind(holder)
        self.assertIn('text table', holder['reflines'][0]['unbound'])   # Measure Values only on Text: no axis


class TemporalValues(unittest.TestCase):
    def test_max_and_average_dates(self):
        import pandas as pd
        df = pd.DataFrame({'D|none|Day': pd.to_datetime(['2024-01-01', '2024-01-03', '2024-01-05'])})
        plan = {'fields': [], 'reflines': []}
        for formula, want in (('max', '2024-01-05'), ('min', '2024-01-01'), ('average', '2024-01-03')):
            plan['reflines'] = [{'formula': formula, 'scope': 'per-table', 'axis_col': 'D|none|Day',
                                 'temporal': True, 'percentages': [], 'fill_below': False, 'fill_above': False}]
            out = R.values(plan, df)
            self.assertEqual(out[0]['lines'][0][0][:10], want)
        self.assertEqual(R.label_text({'label_type': 'automatic'}, '2024-01-05T00:00:00', None), '2024-01-05')


if __name__ == '__main__':
    unittest.main()
