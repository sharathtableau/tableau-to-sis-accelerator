"""C1: nested FIXED LODs, {FIXED a: AGG({FIXED a,b: AGG2(x)})}. Written BEFORE the fix (was 'not translated').
Tableau evaluates the inner LOD at ITS level (one value per (a, b)) and the outer aggregate over those values -- not
row-weighted. Proof: Globalsales AVG_SALES_REGION = per REGION, the average over its SUB_CATEGORY of SUM(SALES),
computed with pandas from the bound rows; the SQL runs row-level (one value per row) like every FIXED LOD."""
from pathlib import Path
import sys
import unittest

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
import calc_translator as C  # noqa: E402

FORMULA = '{ FIXED [REGION]:AVG(({ FIXED [REGION],[SUB_CATEGORY]: SUM([SALES])}))}'


def frame():
    import plan_census as PC
    r = PC.intake(ROOT / 'Workbooks' / 'Globalsalesdashboard.twbx')
    ir = r['ir']
    s = next(s for d in ir['dashboards'] for s in d['sheets'] if s['name'] == 'Details Standard Class')
    return pd.read_csv(r['bindings'][ir['field_scopes'][s['datasource_id']]['caption']])


class NestedLod(unittest.TestCase):
    def run_sql(self, df, formula):
        sql, is_agg = C.translate_formula(formula)
        self.assertIsNotNone(sql, formula)
        self.assertFalse(is_agg)                                         # row-level, like every FIXED LOD
        con = duckdb.connect()
        con.register('src_rows', df)
        con.execute('CREATE TABLE T AS SELECT * FROM src_rows')
        return con.execute(f"SELECT REGION, {sql.replace('__TBL__', 'T')} AS V FROM T").fetchdf()

    def test_avg_of_inner_groups_not_rows(self):
        df = frame()
        got = self.run_sql(df, FORMULA)
        inner = df.groupby(['REGION', 'SUB_CATEGORY'], dropna=False)['SALES'].sum().reset_index()
        want = inner.groupby('REGION', dropna=False)['SALES'].mean()
        rowweighted = df.assign(I=df.groupby(['REGION', 'SUB_CATEGORY'])['SALES'].transform('sum')) \
            .groupby('REGION')['I'].mean()
        self.assertTrue(any(abs(want[k] - rowweighted[k]) > 1 for k in want.index))   # the two really differ
        for reg, grp in got.groupby('REGION', dropna=False):
            self.assertEqual(grp['V'].nunique(), 1)                     # one value per region, on every row
            self.assertAlmostEqual(grp['V'].iloc[0], want[reg], places=6)

    def test_other_outer_aggregates_and_nulls(self):
        df = pd.DataFrame({'REGION': ['E', 'E', 'E', None, None], 'SUB_CATEGORY': ['a', 'a', 'b', 'a', 'b'],
                           'SALES': [1.0, 2.0, 10.0, 5.0, 7.0]})
        for agg, want in (('SUM', {'E': 13.0, None: 12.0}), ('MAX', {'E': 10.0, None: 7.0}),
                          ('MIN', {'E': 3.0, None: 5.0}), ('COUNT', {'E': 2, None: 2})):
            got = self.run_sql(df, '{FIXED [REGION]: ' + agg + '({FIXED [REGION],[SUB_CATEGORY]: SUM([SALES])})}')
            for reg, v in want.items():
                vals = got[got['REGION'].isna()]['V'] if reg is None else got[got['REGION'] == reg]['V']
                self.assertTrue((vals == v).all(), (agg, reg, vals.tolist()))

    def test_unsupported_nesting_still_refuses(self):
        for f in ('{FIXED [SUB_CATEGORY]: AVG({FIXED [REGION]: SUM([SALES])})}',          # outer keys not a subset
                  '{FIXED [REGION]: AVG({FIXED [REGION],[SUB_CATEGORY]: SUM([SALES])}) + 1}',  # not one aggregate
                  '{FIXED [REGION]: AVG({INCLUDE [SUB_CATEGORY]: SUM([SALES])})}'):
            self.assertEqual(C.translate_formula(f), (None, False), f)


    def test_correlation_survives_an_aliased_outer_table(self):
        df = pd.DataFrame({'REGION': ['E', 'E', 'W'], 'SUB_CATEGORY': ['a', 'b', 'a'], 'SALES': [1.0, 3.0, 5.0]})
        sql, _ = C.translate_formula(FORMULA)
        con = duckdb.connect()
        con.register('src_rows', df)
        con.execute('CREATE TABLE T AS SELECT * FROM src_rows')
        got = con.execute(f"SELECT t.REGION, {sql.replace('__TBL__', 'T')} AS V FROM T t ORDER BY 1").fetchall()
        self.assertEqual(got, [('E', 2.0), ('E', 2.0), ('W', 5.0)])


class EndToEnd(unittest.TestCase):
    def test_globalsales_sheet_field_through_the_plan(self):
        """The workbook's own AVG_SALES_REGION (discrete SUM pill on Rows): Tableau sums the row-level FIXED value
        over each mark's rows -> per REGION: (average over sub-categories) x (rows in the region)."""
        import engine
        import plan_census as PC
        import v3_sheet_plan as P
        from v3_local import preview_context
        r = PC.intake(ROOT / 'Workbooks' / 'Globalsalesdashboard.twbx')
        ir = r['ir']
        s = next(s for d in ir['dashboards'] for s in d['sheets'] if s['name'] == 'Details Standard Class')
        fid = next(k for k, e in ir['fields'].items() if e.get('caption') == 'AVG_SALES_REGION')
        self.assertTrue((ir['fields'][fid].get('calculation') or {}).get('sql'))
        occ = s['worksheet_semantics']['occurrences']
        keep = [o for o in occ if o['caption'] in ('REGION', 'AVG_SALES_REGION')]
        s2 = dict(s, applied_filters=[], worksheet_semantics=dict(s['worksheet_semantics'], occurrences=keep))
        b = {k: v for k, v in r['bindings'].items() if v}
        with preview_context(ir, b):
            df = P.execute(P.from_sheet(s2, ir), s2, engine)
        raw = frame()
        inner = raw.groupby(['REGION', 'SUB_CATEGORY'])['SALES'].sum().reset_index()
        avg = inner.groupby('REGION')['SALES'].mean()
        rows = raw.groupby('REGION').size()
        col = next(c for c in df.columns if c.endswith('|AVG_SALES_REGION'))
        got = df.set_index('D|none|REGION')[col]
        self.assertEqual(set(got.index), set(avg.index))
        for reg in avg.index:
            self.assertAlmostEqual(got[reg], avg[reg] * rows[reg], places=3)


if __name__ == '__main__':
    unittest.main()
