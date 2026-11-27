"""A4: Tableau's native measure selector -- a Measure Names filter 'Measure Names = [Parameter]' -- on the exact
plan path. Written BEFORE the fix: the plan read every measure (the filter's column carries the datasource prefix
and its condition is a formula, not members) and drew all three whatever the parameter said.
Rule: the plan records the parameter; at run time only the measure whose caption is the parameter's value is drawn
(quotes / case ignored); a value naming no measure keeps them all (the engine's measure-swap rule).
Workbook: Superstore_MeasureSelector_Native.twbx ('Native Measure Selector', Sales / Profit / Quantity by Category)."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
WB = ROOT / 'Workbooks' / 'Superstore_MeasureSelector_Native.twbx'


@unittest.skipUnless(WB.exists(), 'corpus workbook')
class NativeSelector(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import plan_census as PC
        cls.r = PC.intake(WB)
        cls.sheet = next(s for d in cls.r['ir']['dashboards'] for s in d['sheets'] if s['name'] == 'Native Measure Selector')

    def run_with(self, value):
        import engine
        import v3_sheet_plan as P
        from v3_local import preview_context
        with preview_context(self.r['ir'], {k: v for k, v in self.r['bindings'].items() if v}):
            if value is not None:
                engine.PARAMS['Select Measure Native'] = value
            plan = P.from_sheet(self.sheet, self.r['ir'])
            self.assertEqual(plan.get('measure_param'), 'Select Measure Native')
            df = P.execute(plan, self.sheet, engine)
            return {c for c in df.columns if c.startswith('M|')}, df

    def test_each_value_draws_its_measure(self):
        for value, cap in (('"Sales"', 'Sales'), ('"Profit"', 'Profit'), ('"Quantity"', 'Quantity')):
            cols, df = self.run_with(value)
            self.assertEqual(len(cols), 1, (value, cols))
            self.assertIn(cap, next(iter(cols)))
            self.assertEqual(len(df), 3)                         # three categories

    def test_unknown_value_keeps_every_measure(self):
        cols, _ = self.run_with('"Discount"')
        self.assertEqual(len(cols), 3)

    def test_sales_by_category_values(self):
        import engine
        cols, df = self.run_with('"Sales"')
        from v3_local import preview_context
        with preview_context(self.r['ir'], {k: v for k, v in self.r['bindings'].items() if v}):
            T = engine.tbl(self.sheet)
            want = engine.q(f'SELECT CATEGORY, SUM(SALES) AS S FROM {T} GROUP BY 1')
        got = dict(zip(df.iloc[:, 0], df[next(iter(cols))]))
        for cat, s in want.values:
            self.assertAlmostEqual(float(got[cat]), float(s), places=2)


WI = ROOT / 'Workbooks' / 'World_Indicators_Tableau2024_3.twbx'


@unittest.skipUnless(WI.exists(), 'corpus workbook')
class SecondWorkbook(unittest.TestCase):
    """The same rule on another workbook's Measure Values sheet (World Indicators 'Health Indicators', four
    measures): with a selector parameter, the one picked is drawn with exactly the values the full plan has."""
    def test_selector_on_health_indicators(self):
        import engine
        import plan_census as PC
        import v3_sheet_plan as P
        from v3_local import preview_context
        r = PC.intake(WI)
        sheet = next(s for d in r['ir']['dashboards'] for s in d['sheets'] if s['name'] == 'Health Indicators')
        with preview_context(r['ir'], {k: v for k, v in r['bindings'].items() if v}):
            plan = P.from_sheet(sheet, r['ir'])
            full = P.execute(plan, sheet, engine)
            pick = plan['measure_values'][2]['caption']
            engine.PARAMS['__a4_pick'] = '"' + pick + '"'
            one = P.execute(dict(plan, measure_param='__a4_pick'), sheet, engine)
            picked = P.select_measures(dict(plan, measure_param='__a4_pick'), engine)['measure_values']
        self.assertEqual([m['caption'] for m in picked], [pick])       # the Measure Values the sheet draws
        col = P.col_name(picked[0], True)
        self.assertEqual(one[col].astype(float).fillna(-1).round(6).tolist(),
                         full[col].astype(float).fillna(-1).round(6).tolist())


class Parse(unittest.TestCase):
    def test_selector_formula_either_way_round(self):
        import v3_sheet_plan as P
        for f in ('[Measure Names] = [Parameters].[Pick]', '[Parameters].[Pick] = [Measure Names]'):
            xml = (f'<filter class="categorical" column="[ds].[:Measure Names]"><groupfilter function="formula" '
                   f'formula="{f}"/></filter>')
            self.assertEqual(P.measure_selector({'worksheet_semantics': {'source_xml': xml}}), 'Pick')
        xml = '<filter class="categorical" column="[ds].[:Measure Names]"><groupfilter function="formula" formula="[X] = 1"/></filter>'
        self.assertIsNone(P.measure_selector({'worksheet_semantics': {'source_xml': xml}}))


if __name__ == '__main__':
    unittest.main()
