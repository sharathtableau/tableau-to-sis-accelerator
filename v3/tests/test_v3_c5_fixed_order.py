"""C5: FIXED LODs are computed BEFORE dimension filters. Written BEFORE the fix (tests first).

Tableau's order of operations: extract / data source filters -> context filters -> FIXED LODs -> dimension
filters (sheet filters, dashboard filter controls, filter actions) -> ... So a FIXED value sees only the rows that
pass the context (and data source) filters. The plan path computed {FIXED k: AGG(x)} as a window over the rows of
the sheet's WHERE (every filter), and a no-key FIXED ({MAX(x)}) over the whole table (no context either).
Rule (general): when a sheet has filters beyond its context filters, every row-level FIXED window is computed in a
base query restricted by the context filters only, and the sheet's other filters apply outside it; a no-key FIXED
reads the context-filtered table. A sheet without such filters is unchanged.
Reference: a hand-written query in Tableau's documented order (window over the context rows, then the filters).
Workbooks: E-Commerce (Software) 'CLV' / 'CLV%' ({FIXED customer: SUM(revenue)} under a product filter);
Superstore 2024.3 stays Verified against Tableau's own capture (test_v3_sheet_census / test_v3_exact)."""
import glob
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

ECOM = sorted(glob.glob(str(ROOT / 'Workbooks' / 'E-Commerce*.twbx')))
CLV = "SUM(CASE WHEN NOT IS_REFUNDED THEN NET_REVENUE_USD END) OVER (PARTITION BY CUSTOMER_ID)"


@unittest.skipUnless(ECOM, 'corpus workbook')
class Ecommerce(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import plan_census as PC
        cls.r = PC.intake(Path(ECOM[0]))
        cls.ir = cls.r['ir']
        cls.sheets = {s['name']: s for d in cls.ir['dashboards'] for s in d['sheets']}

    def run_sheet(self, name, extra_filters=(), select='', group='', ctx=''):
        """(app result, reference result, the sheet's WHERE) -- the reference is computed in the same data context:
        the FIXED window over the context rows (ctx), then the sheet's own WHERE."""
        import engine
        import v3_sheet_plan as P
        from v3_local import preview_context
        s = dict(self.sheets[name])
        s['applied_filters'] = list(s.get('applied_filters') or []) + list(extra_filters)
        with preview_context(self.ir, {k: v for k, v in self.r['bindings'].items() if v}):
            pl = P.from_sheet(s, self.ir)
            df = P.execute(pl, s, engine)
            sb = P.bind_fields(pl, s, engine)
            with engine.field_scope(sb):
                T = engine.tbl(sb)
                where = P.sheet_where(sb, engine)
                want = engine.q(f"WITH b AS (SELECT *, {CLV} AS __CLV FROM {T} {ctx}) "
                                f"SELECT {select} FROM b {where} {group}")
        return df, want, where

    def test_clv_kpi_is_fixed_before_the_product_filter(self):
        df, want, where = self.run_sheet('CLV%', select="AVG(CASE WHEN EVENT_TYPE = 'order' THEN __CLV END) AS V")
        self.assertIn('PRODUCT_NAME', where)
        self.assertAlmostEqual(float(df.iloc[0, 0]), float(want['V'].iloc[0]), places=4)
        self.assertNotAlmostEqual(float(df.iloc[0, 0]), 1453.6855, places=1)     # the old after-filter value

    def test_clv_by_month_is_fixed_before_the_product_filter(self):
        df, want, _w = self.run_sheet('CLV', select="EXTRACT(MONTH FROM EVENT_DATE) AS D0, EVENT_DATE AS D1, "
                                      "AVG(CASE WHEN EVENT_TYPE = 'order' THEN __CLV END) AS V", group='GROUP BY 1, 2')
        got = {(int(a), str(b)): c for a, b, c in df.values}
        exp = {(int(a), str(b)): c for a, b, c in want.values}
        self.assertEqual(set(got), set(exp))
        for k, v in exp.items():
            if v == v:                                                     # NaN marks stay NaN
                self.assertAlmostEqual(float(got[k]), float(v), places=4, msg=str(k))

    def test_a_context_filter_restricts_the_fixed_value(self):
        country = {'caption': 'Country', 'kind': 'in', 'values': ['United States'], 'context': True}
        df, want, _w = self.run_sheet('CLV%', [country], select="AVG(CASE WHEN EVENT_TYPE = 'order' THEN __CLV END) AS V",
                                      ctx="WHERE (COUNTRY IN ('United States'))")
        self.assertAlmostEqual(float(df.iloc[0, 0]), float(want['V'].iloc[0]), places=4)

    def test_no_key_fixed_reads_the_context_rows(self):
        """{MAX([Event Date])} (Current Period) is computed over the context rows only, never the sheet filters."""
        import engine
        import v3_sheet_plan as P
        from v3_local import preview_context
        country = {'caption': 'Country', 'kind': 'in', 'values': ['United States'], 'context': True}
        s = dict(self.sheets['Revenue'])
        s['applied_filters'] = list(s.get('applied_filters') or []) + [country]
        with preview_context(self.ir, {k: v for k, v in self.r['bindings'].items() if v}):
            pl = P.from_sheet(s, self.ir)
            sb = P.bind_fields(pl, s, engine)
            with engine.field_scope(sb):
                where = P.sheet_where(sb, engine)
                lod = P.lod_where(sb, engine)
                sql, _ = P.compile_query(pl, sb, engine, where, lod_where=lod)
            P.execute(pl, s, engine)                                       # runs
        self.assertNotIn('__TBL__', sql)
        self.assertIn("(SELECT MAX(EVENT_DATE) FROM (SELECT * FROM ", sql)
        self.assertIn("WHERE (COUNTRY IN ('United States'))", lod)

    def test_nested_fixed_is_computed_layer_by_layer_before_the_filters(self):
        """'Avg. Days to 2nd Purchase': {FIXED customer: MIN(order date > {FIXED customer: MIN(order date)})} under the
        Current Period filter -- the inner FIXED in the first base query, the outer one over it, both before the
        sheet's filter (the outer one must not stay inline after the WHERE)."""
        import engine
        import v3_sheet_plan as P
        from v3_local import preview_context
        s = self.sheets['Avg. Days to 2nd Purchase']
        with preview_context(self.ir, {k: v for k, v in self.r['bindings'].items() if v}):
            pl = P.from_sheet(s, self.ir)
            sb = P.bind_fields(pl, s, engine)
            with engine.field_scope(sb):
                where = P.sheet_where(sb, engine)
                sql, _ = P.compile_query(pl, sb, engine, where, lod_where=P.lod_where(sb, engine))
            df = P.execute(pl, s, engine)
        outer = sql[:sql.index(' FROM (SELECT *')]
        self.assertNotIn('OVER (PARTITION BY CUSTOMER_ID)', outer)          # every FIXED moved before the WHERE
        self.assertIn(') __lod1', sql)
        self.assertEqual(len(df), 21)

    def test_a_sheet_without_other_filters_is_unchanged(self):
        import engine
        import v3_sheet_plan as P
        from v3_local import preview_context
        s = dict(self.sheets['CLV%'], applied_filters=[])
        with preview_context(self.ir, {k: v for k, v in self.r['bindings'].items() if v}):
            pl = P.from_sheet(s, self.ir)
            sb = P.bind_fields(pl, s, engine)
            with engine.field_scope(sb):
                where = P.sheet_where(sb, engine)
                a, _ = P.compile_query(pl, sb, engine, where)
                b, _ = P.compile_query(pl, sb, engine, where, lod_where=P.lod_where(sb, engine))
        self.assertEqual(a, b)


class ContextOnCalculations(unittest.TestCase):
    """Found by the C5 corpus run (Campaign Incrementality 'KPI C'): a context filter on a calculated field was
    written as the caption-as-column ('PRODUCT_IS_SELECTED IN (...)'). The same rule as the sheet's own filter loop."""
    def test_boolean_and_member_context_filters_on_calculations(self):
        import engine
        old = dict(engine.CALCS)
        engine.CALCS.update({'Is Big': {'sql': 'SALES > 100', 'agg_ready': False},
                             'Bucket': {'sql': "CASE WHEN SALES > 100 THEN 'big' END", 'agg_ready': False},
                             'Ratio': {'sql': 'SUM(P)/SUM(S)', 'agg_ready': True}})
        try:
            c = engine._context_cond
            self.assertEqual(c({'caption': 'Is Big', 'kind': 'in', 'values': ['true']}), '(SALES > 100)')
            self.assertEqual(c({'caption': 'Is Big', 'kind': 'in', 'values': ['false']}), 'NOT (SALES > 100)')
            self.assertIsNone(c({'caption': 'Is Big', 'kind': 'in', 'values': ['true', 'false']}))
            self.assertEqual(c({'caption': 'Bucket', 'kind': 'in', 'values': ['big']}),
                             "((CASE WHEN SALES > 100 THEN 'big' END) IN ('big'))")
            self.assertIsNone(c({'caption': 'Ratio', 'kind': 'in', 'values': ['1']}))
        finally:
            engine.CALCS.clear()
            engine.CALCS.update(old)

if __name__ == '__main__':
    unittest.main()
