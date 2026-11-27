"""V7: text tables with dimensions on Columns are Tableau crosstabs, with their totals. Written before the code.
Tableau lays a text table out as row headers (Rows dimensions) x column headers (Columns dimensions, and Measure Names
when it sits there), one cell per measure. The plan showed the long result instead (11 corpus sheets). Totals:
<rows total='true'> / <cols total='true'> add grand totals, <subtotals><column> a subtotal per member of an outer
header. Tableau's automatic totals re-aggregate the measure over the coarser level (not the sum of the shown cells),
so they come from the plan run at that level (here: the run() callback)."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools')]
import v3_crosstab as X  # noqa: E402
import v3_sheet_plan as P  # noqa: E402

DATA = [('E', 'a', 'Q1', 10.0), ('E', 'a', 'Q2', 20.0), ('E', 'b', 'Q1', 5.0), ('W', 'a', 'Q1', 1.0),
        ('W', 'a', 'Q2', 2.0), ('W', 'b', 'Q2', 4.0)]


class Eng:
    value_labels = staticmethod(lambda cap: {})


def plan(totals=None, agg='sum'):
    p = {'schema': P.SCHEMA, 'mark': 'text', 'sort': [], 'aggregate': True, 'fields': [
        {'id': 'r', 'caption': 'Region', 'channel': 'rows', 'kind': 'dim', 'discrete': True, 'date_part': 'none'},
        {'id': 's', 'caption': 'Seg', 'channel': 'rows', 'kind': 'dim', 'discrete': True, 'date_part': 'none'},
        {'id': 'q', 'caption': 'Qtr', 'channel': 'cols', 'kind': 'dim', 'discrete': True, 'date_part': 'none'},
        {'id': 'v', 'caption': 'Sales', 'channel': 'text', 'kind': 'measure', 'agg': agg, 'discrete': False,
         'date_part': 'none'}]}
    if totals:
        p['totals'] = totals
    return p


def run(pl):
    """The plan at its own level of detail, computed from DATA (stands in for execute())."""
    import pandas as pd
    df = pd.DataFrame(DATA, columns=['Region', 'Seg', 'Qtr', 'Sales'])
    dims = [f['caption'] for f in pl['fields'] if f['kind'] == 'dim']
    agg = next(f['agg'] for f in pl['fields'] if f['kind'] == 'measure')
    fn = {'sum': 'sum', 'avg': 'mean'}[agg]
    g = df.groupby(dims, as_index=False)['Sales'].agg(fn) if dims else pd.DataFrame({'Sales': [getattr(df['Sales'], fn)()]})
    return g.rename(columns={**{d: f'D|none|{d}' for d in dims}, 'Sales': f'M|{agg}|Sales'})


class Pivot(unittest.TestCase):
    def test_rows_by_columns(self):
        out = X.crosstab(plan(), run(plan()), Eng)
        self.assertEqual(list(out.columns), ['Region', 'Seg', 'Q1', 'Q2'])
        self.assertEqual(out.values.tolist(), [['E', 'a', '10', '20'], ['E', 'b', '5', ''],
                                               ['W', 'a', '1', '2'], ['W', 'b', '', '4']])

    def test_not_a_crosstab_without_column_headers(self):
        p = plan()
        p['fields'][2]['channel'] = 'rows'
        self.assertFalse(X.applies(p))
        self.assertTrue(X.applies(plan()))


class Totals(unittest.TestCase):
    def test_subtotals_and_grand_total_column(self):
        p = plan({'grand_rows': True, 'grand_cols': True, 'subtotals': ['Region']})
        out = X.crosstab(p, run(p), Eng, run=run)
        self.assertEqual(list(out.columns), ['Region', 'Seg', 'Q1', 'Q2', 'Grand Total'])
        rows = {tuple(r[:2]): r[2:] for r in out.values.tolist()}
        self.assertEqual(rows[('E', 'Total')], ['15', '20', '35'])          # Region subtotal row
        self.assertEqual(rows[('E', 'a')][-1], '30')                        # grand total column
        self.assertEqual(rows[('Grand Total', '')], ['16', '26', '42'])
        self.assertEqual(out.values.tolist()[-1][0], 'Grand Total')          # the grand total row is last

    def test_totals_reaggregate_like_tableau(self):
        p = plan({'grand_rows': True}, agg='avg')
        out = X.crosstab(p, run(p), Eng, run=run)
        last = out.values.tolist()[-1]
        self.assertEqual(last[:2], ['Grand Total', ''])
        self.assertEqual(last[2], '5.33')         # AVG over the underlying rows for Q1 (10, 5, 1), not avg of cells

    def test_totals_from_the_workbook_xml(self):
        xml = ("<table><rows total='true'>[d].[none:Region:nk]</rows><cols>[d].[none:Qtr:nk]</cols><subtotals>"
               "<column>[d].[none:Region:nk]</column></subtotals></table>")
        # a total on a shelf totals that shelf's headers: <rows total> = a grand total ROW, <cols total> = a COLUMN
        self.assertEqual(X.parse_totals(xml), {'grand_rows': True, 'grand_cols': False, 'subtotals': ['Region']})


class Corpus(unittest.TestCase):
    """Superstore 'What If Forecast' (Region / Segment x Quarter / Month, Measure Names on Rows, subtotals and a grand
    total) and Email Campaign 'MOM By Email Domain' (a second workbook) are drawn as crosstabs."""
    def table(self, wb, sheet):
        import glob
        import engine
        import plan_census as PC
        from v3_local import preview_context
        path = glob.glob(str(ROOT / 'Workbooks' / '**' / wb), recursive=True)[0]
        r = PC.intake(path)
        ir = r['ir']
        s = next(s for d in ir['dashboards'] for s in d['sheets'] if s['name'] == sheet)
        pl = P.from_sheet(s, ir)
        with preview_context(ir, {k: v for k, v in r['bindings'].items() if v}):
            df = P.execute(pl, s, engine)
            return pl, X.crosstab(pl, df, engine, run=lambda v: P.execute(v, s, engine))

    def test_what_if_forecast(self):
        pl, out = self.table('Superstore_Tableau2024_3.twbx', 'What If Forecast')
        self.assertTrue(pl.get('totals'))
        self.assertIn('Grand Total', ' '.join(map(str, out.columns)))
        # month header labels follow the engine's month-name style (set per workbook); either form is a month header
        self.assertTrue(any(c.endswith(('January', 'Jan')) for c in map(str, out.columns)), list(out.columns))
        self.assertTrue((out.iloc[:, 0] == 'Central').any())

    def test_email_mom(self):
        pl, out = self.table('Email Campaign Analytics.twbx', 'MOM By Email Domain')
        self.assertGreater(out.shape[1], 2)


if __name__ == '__main__':
    unittest.main()
