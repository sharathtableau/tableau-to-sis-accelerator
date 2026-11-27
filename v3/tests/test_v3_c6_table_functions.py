"""C6: table-calculation functions inside calculated fields. Written BEFORE the fix (tests first).

The translator refused LOOKUP / FIRST / LAST / RUNNING_* / TOTAL and passed ZN through untranslated, so every sheet
using them failed (11 Nov corpus: ~50 of 86 failures -- 'calculation not translated' / 'Untranslated field' /
'Scalar Function zn does not exist'; BDO, Email Campaign, Clara, GOLD CRM, Hospitality, Online Retail ...).
Tableau's definitions (functions_functions_tablecalculation):
  LOOKUP(e, n)      e at the row n rows away along the addressing (n<0 earlier); LOOKUP(e, FIRST()) the first row,
                    LOOKUP(e, LAST()) the last row of the partition
  FIRST() / LAST()  offset to the first / last row of the partition (0 on that row; FIRST() <= 0, LAST() >= 0)
  RUNNING_SUM/AVG/MIN/MAX/COUNT(e)  the aggregate of e from the first row of the partition to the current one
  TOTAL(SUM|MIN|MAX|COUNT(x))       the aggregate over the partition (TOTAL(AVG) != AVG(AVG): refused)
  ZN(e)             e, or 0 when e is NULL
All become windows over the aggregated view; the plan fills the partition and order from the sheet's addressing
(window_sql), exactly as for INDEX / WINDOW_* / RANK."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

W = '__TC_ORDER__'


def tr(formula):
    import calc_translator as C
    return C.translate_formula(formula)


class Translate(unittest.TestCase):
    def test_lookup(self):
        self.assertEqual(tr('LOOKUP(SUM([Sales]), -1)')[0], f'LAG(SUM(SALES), 1) OVER ({W})')
        self.assertEqual(tr('LOOKUP(SUM([Sales]), 2)')[0], f'LEAD(SUM(SALES), 2) OVER ({W})')
        self.assertEqual(tr('LOOKUP(SUM([Sales]), 0)')[0], '(SUM(SALES))')
        self.assertEqual(tr('LOOKUP(SUM([Sales]), FIRST())')[0],
                         f'FIRST_VALUE(SUM(SALES)) OVER ({W} ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)')
        self.assertEqual(tr('LOOKUP(SUM([Sales]), LAST())')[0],
                         f'LAST_VALUE(SUM(SALES)) OVER ({W} ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)')
        self.assertEqual(tr('LOOKUP(SUM([Sales]), [Parameters].[Offset])'), (None, False))     # not a constant

    def test_first_last(self):
        self.assertEqual(tr('FIRST() = 0')[0], f'(1 - ROW_NUMBER() OVER ({W})) = 0')
        self.assertEqual(tr('LAST()=0')[0], '(ROW_NUMBER() OVER (__TC_ORDER_DESC__) - 1)=0')

    def test_running(self):
        frame = 'ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW'
        self.assertEqual(tr('RUNNING_SUM(SUM([Sales]))')[0], f'SUM(SUM(SALES)) OVER ({W} {frame})')
        self.assertEqual(tr('RUNNING_AVG(SUM([Sales]))')[0], f'AVG(SUM(SALES)) OVER ({W} {frame})')
        self.assertEqual(tr('RUNNING_COUNT(SUM([Sales]))')[0], f'COUNT(SUM(SALES)) OVER ({W} {frame})')

    def test_total(self):
        self.assertEqual(tr('SUM([Sales]) / TOTAL(SUM([Sales]))')[0],
                         'SUM(SALES) / NULLIF(SUM(SUM(SALES)) OVER (), 0)')
        self.assertEqual(tr('TOTAL(COUNT([Sales]))')[0], 'SUM(COUNT(SALES)) OVER ()')
        self.assertEqual(tr('TOTAL(AVG([Sales]))'), (None, False))                           # not re-aggregable

    def test_zn(self):
        self.assertEqual(tr('ZN(SUM([Sales]))')[0], 'COALESCE((SUM(SALES)), 0)')
        sql, agg = tr('(ZN(SUM([Sales])) - LOOKUP(ZN(SUM([Sales])), -1)) / ABS(LOOKUP(ZN(SUM([Sales])), -1))')
        self.assertIn(f'LAG(COALESCE((SUM(SALES)), 0), 1) OVER ({W})', sql)
        self.assertTrue(agg)

    def test_still_refused(self):
        for f in ('PREVIOUS_VALUE(0) + 1', 'RUNNING_MEDIAN(SUM([Sales]))'):       # SIZE() translates since C14
            self.assertEqual(tr(f), (None, False), f)

    def test_parser_flags_every_table_function_as_a_window(self):
        import tableau_parser as TP
        calcs, _drops = TP.translate_calcs({'Share': 'SUM([Sales]) / TOTAL(SUM([Sales]))',
                                            'Prev': 'LOOKUP(SUM([Sales]), -1)'}, {}, {}, {}, {}, {})
        self.assertTrue(calcs['Share'].get('window'))
        self.assertTrue(calcs['Prev'].get('window'))
        import calc_translator as C
        self.assertTrue(C.is_window_sql(tr('LAST()=0')[0]))                # LAST() alone (descending form)
        self.assertTrue(tr('LAST()=0')[1])


class Addressing(unittest.TestCase):
    """The plan fills the window from the sheet's addressing; LAST() orders the same addressing descending."""
    def test_window_sql_fills_order_and_descending_order(self):
        import types
        import v3_sheet_plan as P
        eng = types.SimpleNamespace(rdim=lambda c: c.upper())
        plan = {'fields': [{'caption': 'Month', 'kind': 'dim', 'date_part': 'none', 'channel': 'cols'}]}
        dims = plan['fields']
        f = {'caption': 'X', 'window': {'addressing': 'table'}}
        old = P._addressing
        P._addressing = lambda plan, f: (dims, [])
        try:
            self.assertEqual(P.window_sql(f'LAG(SUM(S), 1) OVER ({W})', plan, f, eng, dims),
                             'LAG(SUM(S), 1) OVER (ORDER BY MONTH)')
            self.assertEqual(P.window_sql('(ROW_NUMBER() OVER (__TC_ORDER_DESC__) - 1)', plan, f, eng, dims),
                             '(ROW_NUMBER() OVER (ORDER BY MONTH DESC) - 1)')
        finally:
            P._addressing = old

    def test_order_follows_the_sheets_sort(self):
        """Tableau's addressing follows each dimension's sort: a computed sort by a measure (descending here) and a
        manual member order -- never the dimension's alphabetical order (11 Nov: Online Retail 'Q.top' INDEX() in
        1..10 picked the first 10 countries alphabetically instead of the top 10 by sales)."""
        import types
        import v3_sheet_plan as P
        eng = types.SimpleNamespace(rdim=lambda c: c.upper(), CALCS={'sales': {'sql': 'SUM(S)', 'agg_ready': True}})
        dims = [{'caption': 'Country', 'kind': 'dim', 'date_part': 'none', 'channel': 'rows'}]
        old = P._addressing
        P._addressing = lambda plan, f: (dims, [])
        try:
            plan = {'fields': dims, 'sort': [{'field': 'Country', 'by': 'sales', 'by_agg': 'usr', 'direction': 'desc'}]}
            self.assertEqual(P.window_sql(f'ROW_NUMBER() OVER ({W})', plan, {'caption': 'i'}, eng, dims),
                             'ROW_NUMBER() OVER (ORDER BY SUM(S) DESC, COUNTRY)')
            self.assertEqual(P.window_sql('(ROW_NUMBER() OVER (__TC_ORDER_DESC__) - 1)', plan, {'caption': 'l'}, eng,
                                          dims), '(ROW_NUMBER() OVER (ORDER BY SUM(S), COUNTRY DESC) - 1)')
            plan = {'fields': dims, 'sort': [{'field': 'Country', 'manual': ['US', 'UK']}]}
            self.assertEqual(P.window_sql(f'ROW_NUMBER() OVER ({W})', plan, {'caption': 'i'}, eng, dims),
                             "ROW_NUMBER() OVER (ORDER BY CASE CAST(COUNTRY AS VARCHAR) WHEN 'US' THEN 0 "
                             "WHEN 'UK' THEN 1 ELSE 2 END, COUNTRY)")
            plan = {'fields': dims, 'sort': [{'field': 'Country', 'by': 'unknown', 'by_agg': 'usr', 'direction': 'desc'}]}
            with self.assertRaises(P.PlanError):
                P.window_sql(f'LAG(SUM(S), 1) OVER ({W})', plan, {'caption': 'p'}, eng, dims)
            self.assertEqual(P.window_sql('SUM(SUM(S)) OVER ()', plan, {'caption': 't'}, eng, dims),
                             'SUM(SUM(S)) OVER ()')                       # order-free: never refused
        finally:
            P._addressing = old

    def test_any_other_path_refuses_instead_of_guessing_the_order(self):
        import engine
        with self.assertRaises(ValueError) as cm:
            engine.q('SELECT LAG(1, 1) OVER (__TC_ORDER__) AS X')
        self.assertIn('addressing', str(cm.exception))


class TableCalcFilters(unittest.TestCase):
    """A filter on a table calculation hides marks after aggregation and every table calculation (Tableau's order
    of operations). Online Retail 'Daily trend.' filters LAST()=0: Tableau shows the last day only; the app showed
    all 305 days (the filter was dropped)."""
    def test_last_filter_keeps_only_the_last_mark(self):
        import glob
        import engine
        import plan_census as PC
        import v3_sheet_plan as P
        from v3_local import preview_context
        paths = sorted(glob.glob(str(ROOT / 'Workbooks' / '**' / 'Online Retail*.twbx'), recursive=True))
        if not paths:
            self.skipTest('corpus workbook')
        r = PC.intake(Path(paths[0]))
        ir = r['ir']
        sh = {s['name']: s for d in ir['dashboards'] for s in d['sheets']}
        with preview_context(ir, {k: v for k, v in r['bindings'].items() if v}):
            s = sh['Daily trend.']
            df = P.execute(P.from_sheet(s, ir), s, engine)
            unfiltered = dict(s, applied_filters=[f for f in s['applied_filters'] if f['caption'] != 'Last'])
            full = P.execute(P.from_sheet(unfiltered, ir), unfiltered, engine)
        self.assertEqual(len(df), 1)
        self.assertGreater(len(full), 20)           # every day of the sheet's March context
        last = full.sort_values(full.columns[0]).iloc[-1]
        self.assertEqual(str(df.iloc[0, 0]), str(last.iloc[0]))
        self.assertAlmostEqual(float(df.iloc[0, 2]), float(last.iloc[2]), places=2)

    def test_index_filter_keeps_the_top_n_in_the_sheets_sort(self):
        """Online Retail 'Product. (2)': INDEX() in 1..10 on Country sorted by sales descending = the 10 highest-sales
        countries (was: all 37 -- the filter was dropped; then the first 10 alphabetically)."""
        import glob
        import engine
        import plan_census as PC
        import v3_sheet_plan as P
        from v3_local import preview_context
        paths = sorted(glob.glob(str(ROOT / 'Workbooks' / '**' / 'Online Retail*.twbx'), recursive=True))
        if not paths:
            self.skipTest('corpus workbook')
        r = PC.intake(Path(paths[0]))
        ir = r['ir']
        sh = {s['name']: s for d in ir['dashboards'] for s in d['sheets']}
        with preview_context(ir, {k: v for k, v in r['bindings'].items() if v}):
            s = sh['Product. (2)']
            top = P.execute(P.from_sheet(s, ir), s, engine)
            full = dict(s, applied_filters=[f for f in s['applied_filters'] if f['caption'] != 'index'])
            every = P.execute(P.from_sheet(full, ir), full, engine)
        best = sorted(every.values.tolist(), key=lambda x: -x[-1])[:10]
        self.assertGreater(len(every), 10)          # the sheet's own countries (its March context filter applies)
        self.assertEqual(sorted(x[0] for x in top.values.tolist()), sorted(x[0] for x in best))

if __name__ == '__main__':
    unittest.main()
