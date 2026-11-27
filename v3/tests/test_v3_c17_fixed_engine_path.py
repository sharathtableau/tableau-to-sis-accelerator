"""C17: FIXED LODs before dimension filters on the ORIGINAL-ENGINE path too. Written BEFORE the fix (tests first).

C5 made the exact-plan path (v3_sheet_plan) compute every row-level FIXED window over the context rows only. The
original engine's renderers (r_bar, r_pie, r_table, ...) still built `SELECT AGG(.. win OVER (PARTITION BY k) ..)
FROM T WHERE <every filter>` and, on the window-in-aggregate error, q() hoisted the window into
`(SELECT *, win FROM T WHERE <every filter>) t` -- the FIXED value then saw only the rows that pass the dimension
filters (Tableau: context and data source filters only). Rule (general, same as C5): while a sheet renders, the
engine knows the sheet's context WHERE (engine.lod_scope); the hoist computes the windows under it and applies the
sheet's full WHERE outside. No sheet being rendered (lod_where None) = the old behaviour, unchanged.
Proof: Superstore ({FIXED [Order ID]: SUM([Profit])} under a Category filter; orders span categories) and
E-Commerce ({FIXED customer: SUM(revenue)} under a product filter), each against a hand-written reference in
Tableau's documented order."""
import glob
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
import engine  # noqa: E402

SQL = ("SELECT SEGMENT AS D, SUM(CASE WHEN (SUM(PROFIT) OVER (PARTITION BY ORDER_ID)) > 0 THEN SALES END) AS V "
       "FROM DB.S.T WHERE (CATEGORY IN ('Furniture')) GROUP BY 1")


class Hoist(unittest.TestCase):
    def test_no_scope_is_unchanged(self):
        out = engine._hoist_windows(SQL)
        self.assertIn("FROM DB.S.T WHERE (CATEGORY IN ('Furniture'))) t", out)

    def test_scope_computes_the_window_before_the_filters(self):
        out = engine._hoist_windows(SQL, lod_where='')
        self.assertIn('OVER (PARTITION BY ORDER_ID) AS __W0__ FROM DB.S.T ) t', ' '.join(out.split()) + ' ')
        self.assertIn(") t WHERE (CATEGORY IN ('Furniture')) GROUP BY 1", ' '.join(out.split()))

    def test_context_conditions_stay_inside(self):
        out = ' '.join(engine._hoist_windows(SQL, lod_where="WHERE (REGION IN ('East'))").split())
        self.assertIn("AS __W0__ FROM DB.S.T WHERE (REGION IN ('East'))) t WHERE (CATEGORY IN ('Furniture'))", out)

    def test_where_equal_to_the_context_is_left_inside(self):
        sql = SQL.replace("WHERE (CATEGORY IN ('Furniture'))", "WHERE (REGION IN ('East'))")
        self.assertEqual(engine._hoist_windows(sql, lod_where="WHERE (REGION IN ('East'))"),
                         engine._hoist_windows(sql))

    def test_render_sets_the_scope_for_the_handler_only(self):
        seen = {}
        s = {'name': 'S', 'kind': 'bar', 'datasource': None,
             'applied_filters': [{'caption': 'Region', 'kind': 'in', 'values': ['East'], 'context': True},
                                 {'caption': 'Category', 'kind': 'in', 'values': ['Furniture']}]}
        old = engine.r_bar
        engine.r_bar = lambda sheet, where: seen.update(scope=engine.lod_where_now(), where=where)
        try:
            engine._render_sheet_impl(s, [])
        finally:
            engine.r_bar = old
        self.assertIn("REGION", seen['scope'])
        self.assertNotIn('CATEGORY', seen['scope'])
        self.assertIn('CATEGORY', seen['where'])
        self.assertIsNone(engine.lod_where_now())


SUPERSTORE = sorted(glob.glob(str(ROOT / 'Workbooks' / 'Superstore_Tableau2024_3.twbx')))
ECOM = sorted(glob.glob(str(ROOT / 'Workbooks' / 'E-Commerce*.twbx')))


def _run(wb, sql, scope, sheet):
    """(engine result under the scope, the same query with no scope)."""
    import plan_census as PC
    from v3_local import preview_context
    r = PC.intake(Path(wb))
    with preview_context(r['ir'], {k: v for k, v in r['bindings'].items() if v}):
        T = engine.tbl(next(x for d in r['ir']['dashboards'] for x in d['sheets'] if x['name'] == sheet))
        with engine.lod_scope(scope):
            got = engine.q(sql.replace('{T}', T))
        ref = engine.q(REF[wb].replace('{T}', T).replace('{CTX}', scope))
        old = engine.q(sql.replace('{T}', T))
    return got, ref, old


REF = {}


@unittest.skipUnless(SUPERSTORE, 'corpus workbook')
class Superstore(unittest.TestCase):
    SQL = ("SELECT SEGMENT AS D, SUM(CASE WHEN (SUM(PROFIT) OVER (PARTITION BY ORDER_ID)) > 0 THEN SALES END) AS V "
           "FROM {T} WHERE (CATEGORY IN ('Furniture')) GROUP BY 1 ORDER BY 1")

    def setUp(self):
        REF[SUPERSTORE[0]] = ("WITH b AS (SELECT *, SUM(PROFIT) OVER (PARTITION BY ORDER_ID) AS F FROM {T} {CTX}) "
                              "SELECT SEGMENT AS D, SUM(CASE WHEN F > 0 THEN SALES END) AS V FROM b "
                              "WHERE (CATEGORY IN ('Furniture')) GROUP BY 1 ORDER BY 1")

    def check(self, scope):
        got, ref, old = _run(SUPERSTORE[0], self.SQL, scope, 'Sales by Segment')
        self.assertEqual(list(got['D']), list(ref['D']))
        for a, b in zip(got['V'], ref['V']):
            self.assertAlmostEqual(float(a), float(b), places=4)
        return got, old

    def test_order_fixed_sees_every_category_of_the_order(self):
        got, old = self.check('')
        self.assertNotEqual([round(float(v), 2) for v in got['V']], [round(float(v), 2) for v in old['V']])

    def test_a_context_filter_restricts_the_fixed_value(self):
        self.check("WHERE (REGION IN ('East'))")


@unittest.skipUnless(ECOM, 'corpus workbook')
class Ecommerce(unittest.TestCase):
    SQL = ("SELECT AVG(CASE WHEN EVENT_TYPE = 'order' THEN (SUM(CASE WHEN NOT IS_REFUNDED THEN NET_REVENUE_USD END) "
           "OVER (PARTITION BY CUSTOMER_ID)) END) AS V FROM {T} WHERE (PRODUCT_NAME = 'Microsoft Copilot for Office "
           "Annual') GROUP BY CHANNEL ORDER BY 1")

    def test_customer_fixed_before_the_product_filter(self):
        REF[ECOM[0]] = ("WITH b AS (SELECT *, SUM(CASE WHEN NOT IS_REFUNDED THEN NET_REVENUE_USD END) OVER "
                        "(PARTITION BY CUSTOMER_ID) AS F FROM {T} {CTX}) SELECT AVG(CASE WHEN EVENT_TYPE = 'order' "
                        "THEN F END) AS V FROM b WHERE (PRODUCT_NAME = 'Microsoft Copilot for Office Annual') "
                        "GROUP BY CHANNEL ORDER BY 1")
        got, ref, old = _run(ECOM[0], self.SQL, '', 'CLV')
        self.assertEqual(len(got), len(ref))
        for a, b in zip(got['V'], ref['V']):
            self.assertAlmostEqual(float(a), float(b), places=4)
        self.assertNotAlmostEqual(float(got['V'].sum()), float(old['V'].sum()), places=1)


if __name__ == '__main__':
    unittest.main()
