"""C12: (1) a sheet with nothing on rows / columns (a pie, a text card) uses the view's FIRST datasource as its
primary (Tableau's rule), not the first datasource-dependency; (2) a no-key FIXED inside a CONTEXT filter reads the
whole table (context filters run before FIXED), also when the context condition is repeated in the sheet's WHERE
next to its dimension filters."""
import glob
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools')]
import tableau_parser as TP  # noqa: E402
import v3_sheet_plan as P  # noqa: E402

WS = """<worksheet name='Pie'><table><view>
  <datasources><datasource caption='Main' name='federated.main' /><datasource name='Parameters' />
    <datasource caption='Other' name='federated.other' /></datasources>
  <datasource-dependencies datasource='Parameters' /><datasource-dependencies datasource='federated.other' />
  <datasource-dependencies datasource='federated.main' /></view>
  <rows /><cols /></table></worksheet>"""


class Primary(unittest.TestCase):
    def test_no_shelf_field_uses_the_first_view_datasource(self):
        ws = ET.fromstring(WS)
        self.assertEqual(TP.sheet_datasource_id(ws, {'federated.main': 'Main', 'federated.other': 'Other'}),
                         'federated.main')

    def test_a_shelf_field_still_decides(self):
        ws = ET.fromstring(WS.replace('<rows />', '<rows>[federated.other].[none:Type:nk]</rows>'))
        self.assertEqual(TP.sheet_datasource_id(ws, {'federated.main': 'Main', 'federated.other': 'Other'}),
                         'federated.other')


class ContextConditionReadsTheWholeTable(unittest.TestCase):
    def test_masking(self):
        import engine
        ctx = "((M)<(SELECT MAX(M) FROM __TBL__))"
        where = f"WHERE (M = 2) AND {ctx}"
        sel, src, out = P._fixed_before_filters(['M AS D0'], 'T', where, f'WHERE {ctx}', engine)
        self.assertIn('(SELECT MAX(M) FROM T)', out)          # the context condition: the whole table
        self.assertNotIn('__ctx', out)

    def test_a_dimension_filter_fixed_still_reads_the_context_rows(self):
        import engine
        ctx = "(M > 0)"
        where = f"WHERE {ctx} AND (M = (SELECT MAX(M) FROM __TBL__))"
        sel, src, out = P._fixed_before_filters(['M AS D0'], 'T', where, f'WHERE {ctx}', engine)
        self.assertIn('(SELECT MAX(M) FROM (SELECT * FROM T WHERE (M > 0)) __ctx)', out)

    def test_split_and_respects_parentheses_and_quotes(self):
        self.assertEqual([x.strip() for x in P._split_and("(a AND b) AND c = 'x AND y' AND (d)")],
                         ['(a AND b)', "c = 'x AND y'", '(d)'])


def corpus(name):
    return glob.glob(str(ROOT / 'Workbooks' / '**' / name), recursive=True)


@unittest.skipUnless(corpus('Customer Opt-In_Out Analysis.twbx'), 'corpus')
class Corpus(unittest.TestCase):
    def test_opt_in_pie_is_the_selected_month(self):
        import engine
        import plan_census
        from v3_local import preview_context
        r = plan_census.intake(Path(corpus('Customer Opt-In_Out Analysis.twbx')[0]))
        ir = r['ir']
        sheets = {s['name']: s for d in ir['dashboards'] for s in d['sheets']}
        with preview_context(ir, {k: v for k, v in r['bindings'].items() if v}):
            pie = P.execute(P.from_sheet(sheets['Pie'], ir), sheets['Pie'], engine)
            T = engine.tbl(sheets['Pie'])
            want = engine.q(f"SELECT SUM(NEW_OPTIN_CNT) AS S FROM {T} WHERE REF_MONTH = DATE '2024-10-01'")
            table = P.execute(P.from_sheet(sheets['Table A (2)'], ir), sheets['Table A (2)'], engine)
        self.assertAlmostEqual(float(pie.iloc[:, 1].sum()), float(want.iloc[0, 0]), 2)
        self.assertGreater(len(table), 0)          # the last complete month (Oct 2024) is no longer dropped



# ---------------------------------------------------------------- C12 relationships: related-table calculations and
# filters. Retails (Customer + Orders on RowID; 54 keys repeat in both tables); the related-table filter on R1
# Superstore (Orders + Returns) is tests/test_v3_dm4_relationship_query.test_filter_on_a_related_field_applies.
RETAILS = 'Retails Dashboard.twbx'


def intake(name):
    import plan_census
    return plan_census.intake(Path(corpus(name)[0]))


@unittest.skipUnless(corpus(RETAILS), 'corpus')
class Relationships(unittest.TestCase):
    def test_a_calculation_mixing_both_tables_is_computed_on_their_join(self):
        import engine
        import pandas as pd
        from v3_local import preview_context
        r = intake(RETAILS)
        ir = r['ir']
        sheets = {s['name']: s for d in ir['dashboards'] for s in d['sheets']}
        with preview_context(ir, {k: v for k, v in r['bindings'].items() if v}):
            df = P.execute(P.from_sheet(sheets['Total Customer'], ir), sheets['Total Customer'], engine)
        cu, od = [pd.read_csv(v) for v in r['bindings'].values() if v]
        j = od.merge(cu, left_on='RowID', right_on='RowID (Customer)')
        d, mx = pd.to_datetime(j['Order_Date']), pd.to_datetime(od['Order_Date']).max()
        cy = j[(mx.year - d.dt.year == 0) & (d <= mx)]['Customer Name'].count()
        py = j[(mx.year - d.dt.year == 1) & (d <= mx - pd.DateOffset(years=1))]['Customer Name'].count()
        self.assertAlmostEqual(float(df['M|usr|YOY Growth Customer'].iloc[0]), (cy - py) / py, 9)
        # the parent's own measure stays on the parent's rows (the join would add the 54 repeated keys' rows)
        self.assertEqual(int(df['M|cnt|Customer Name'].iloc[0]), int(cu['Customer Name'].count()))

    def test_a_filter_mixing_both_tables_keeps_the_keys_of_the_joined_rows_that_pass(self):
        """Retails 'Tabular data': Customer 1 = {FIXED Order_ID (Orders): COUNT(Customer Name (Customer))} > 1,
        kept False -> the relationship keys whose joined rows pass; each table's own rows for those keys."""
        import engine
        import pandas as pd
        from v3_local import preview_context
        r = intake(RETAILS)
        ir = r['ir']
        sheets = {s['name']: s for d in ir['dashboards'] for s in d['sheets']}
        sheet = sheets['Tabular data']
        with preview_context(ir, {k: v for k, v in r['bindings'].items() if v}):
            df = P.execute(P.from_sheet(sheet, ir), sheet, engine)
        states = next(f for f in sheet['applied_filters'] if f['caption'] == 'State')['values']
        cu, od = [pd.read_csv(v) for v in r['bindings'].values() if v]
        j = od.merge(cu, left_on='RowID', right_on='RowID (Customer)')
        keep = set(j.loc[j.groupby('Order_ID')['Customer Name'].transform('count') <= 1, 'RowID'])
        o = od[od.RowID.isin(keep) & od.State.isin(states)]
        self.assertEqual(df['D|none|Order ID1'].nunique(), o['Order_ID'].nunique())
        self.assertAlmostEqual(float(df['M|sum|Sales'].sum()), float(o['Sales'].sum()), 2)


class NestedTableCalcs(unittest.TestCase):
    def test_a_table_calc_over_a_table_calc_is_detected(self):
        self.assertTrue(P._nested_window('SUM(SUM(X) / NULLIF(SUM(SUM(X)) OVER (), 0)) OVER (ORDER BY 1)'))
        self.assertFalse(P._nested_window('SUM(X) / NULLIF(SUM(SUM(X)) OVER (), 0)'))
        self.assertFalse(P._nested_window('RANK() OVER (ORDER BY SUM(X) DESC), MAX(Y) OVER (PARTITION BY A)'))

class MonthYear(unittest.TestCase):
    """Discrete Month / Year ('my:' on a shelf) groups like the truncated month; Tableau's header text keys alike."""
    def test_part(self):
        self.assertEqual(P.DATE_PARTS['my'], 'month_trunc')
        self.assertEqual(P.key_text('January 2025', 'month_trunc'), P.key_text('2025-01-01', 'month_trunc'))

    @unittest.skipUnless(corpus('Clara Brazil EDA Dashboard.twbx'), 'corpus')
    def test_clara_monthly_sheets_plan(self):
        import plan_census
        r = plan_census.intake(Path(corpus('Clara Brazil EDA Dashboard.twbx')[0]))
        ir = r['ir']
        sheets = {s['name']: s for d in ir['dashboards'] for s in d['sheets']}
        for name in ('KPI Monthly Details', 'Monthly Distribution'):
            plan = P.from_sheet(sheets[name], ir)
            self.assertIn('month_trunc', {f['date_part'] for f in plan['fields']}, name)

if __name__ == '__main__':
    unittest.main()
