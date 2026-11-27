"""C7: FIXED level-of-detail bodies beyond a single aggregate. Written BEFORE the fix (tests first).

Tableau allows any aggregate expression after the colon -- {FIXED [Property], [Year]: SUM([Revenue])/SUM([Rooms])} --
and any dimension, calculated ones included, as a key. The translator accepted a single aggregate call only and
resolved keys as physical column names, so Hospitality's RevPAR / ADR / OCC and everything built on them dropped
(11 Nov corpus). Rule: every aggregate of the body is computed over the same FIXED partition (each AGG(x) OVER
(PARTITION BY keys)) -- exact, since a FIXED value is the body evaluated over the partition's rows; the body may
combine them with arithmetic, constants and parameters but never reference a row-level field outside an aggregate
(refused: Tableau rejects it too). A calculated key is its own row-level expression."""
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))


def tr(formula, calc_defs=None):
    import calc_translator as C
    return C.translate_formula(formula, calc_defs=calc_defs)


class Bodies(unittest.TestCase):
    def test_ratio_body(self):
        sql, agg = tr('{FIXED [Property]: SUM([Revenue])/SUM([Rooms])}')
        self.assertEqual(sql, 'SUM(REVENUE) OVER (PARTITION BY PROPERTY)/NULLIF(SUM(ROOMS) OVER (PARTITION BY PROPERTY), 0)')
        self.assertFalse(agg)                                     # row-level, like every FIXED value

    def test_arithmetic_with_constants(self):
        sql, _ = tr('{FIXED [Property]: (SUM([A]) + SUM([B])) * 100}')
        self.assertEqual(sql, '(SUM(A) OVER (PARTITION BY PROPERTY) + SUM(B) OVER (PARTITION BY PROPERTY)) * 100')

    def test_nested_fixed_is_row_level_not_an_aggregate(self):
        """A FIXED whose body / keys nest parentheses is still a row-level value (Online Retail 'Latest purchase':
        {FIXED [Cust Key]: MAX([Shifted Date])} was classed as an aggregate, so it was grouped as a column name)."""
        sql, agg = tr('{FIXED [K]: MAX([D])}', calc_defs={'K': '"C_" + STR([Customer])', 'D': 'DATEADD("day", 3, [Date])'})
        self.assertIn('OVER (PARTITION BY', sql)
        self.assertFalse(agg)
        self.assertTrue(tr('SUM([x]) / {FIXED [K]: MAX([D])}', calc_defs={'K': 'STR([C])', 'D': 'DATEADD("day", 3, [Date])'})[1])

    def test_single_aggregate_unchanged(self):
        self.assertEqual(tr('{FIXED [Customer]: SUM([Sales])}')[0], 'SUM(SALES) OVER (PARTITION BY CUSTOMER)')

    def test_row_field_outside_an_aggregate_refuses(self):
        self.assertEqual(tr('{FIXED [Property]: SUM([Revenue]) / [Rooms]}'), (None, False))

    def test_calculated_key(self):
        sql, _ = tr('{FIXED [Property], [Yr]: SUM([Revenue])}', calc_defs={'Yr': 'STR(YEAR([Date]))'})
        self.assertEqual(sql, 'SUM(REVENUE) OVER (PARTITION BY PROPERTY, (CAST(YEAR(DATE) AS VARCHAR)))')

    def test_body_is_a_reference_to_an_aggregate_calculation(self):
        """{FIXED k: [Users]} with Users = SUM([x]): the referenced calculation is the body (Campaign Incrementality's
        'Annualized Revenue' = [revenue]/{fixed name, group: [Users]}*365)."""
        sql, _ = tr('[revenue] / {FIXED [Name], [Group]: [Users]} * 365', calc_defs={'Users': 'SUM([users])'})
        self.assertEqual(sql, 'REVENUE / NULLIF(SUM(USERS) OVER (PARTITION BY NAME, GROUP_), 0) * 365')   # 6.3: GROUP is reserved
        sql, _ = tr('{FIXED [Name]: [Rate]}', calc_defs={'Rate': '[Num] / [Den]', 'Num': 'SUM([a])', 'Den': 'SUM([b])'})
        self.assertEqual(sql, '(SUM(A) OVER (PARTITION BY NAME)) / NULLIF((SUM(B) OVER (PARTITION BY NAME)), 0)')
        self.assertEqual(tr('{FIXED [Name]: [Row]}', calc_defs={'Row': '[a] * 2'}), (None, False))   # not aggregate

    def test_aggregate_key_refuses(self):
        self.assertEqual(tr('{FIXED [K]: SUM([Revenue])}', calc_defs={'K': 'SUM([X])'}), (None, False))


class DateArithmetic(unittest.TestCase):
    """Tableau: a date +/- a number is that many days later / earlier (GOLD KR 'Last Complete Week':
    {MAX([week_start])}-7=[week_start] -> 'No function matches -(TIMESTAMP, INTEGER)'; Snowflake rejects it too)."""
    def setUp(self):
        import calc_translator as C
        self._old = set(C.DATE_FIELDS)
        C.set_date_fields({'WEEK_START', 'ORDER_DATE'})

    def tearDown(self):
        import calc_translator as C
        C.set_date_fields(self._old)

    def test_days(self):
        self.assertEqual(tr('{max([week_start])}-7=[week_start]')[0],
                         "DATEADD('day', -7, (SELECT max(WEEK_START) FROM __TBL__))=WEEK_START")
        self.assertEqual(tr('[Order Date] + 30')[0], "DATEADD('day', 30, ORDER_DATE)")
        self.assertEqual(tr('MAX([Order Date]) - 1')[0], "DATEADD('day', -1, MAX(ORDER_DATE))")

    def test_numbers_unchanged(self):
        self.assertEqual(tr('[Sales] - 7')[0], 'SALES - 7')
        self.assertEqual(tr('YEAR([Order Date]) - 1')[0], 'YEAR(ORDER_DATE) - 1')     # a number, not a date
        self.assertEqual(tr("DATEDIFF('day', [Order Date], [Ship Date]) - 2")[0],
                         "DATEDIFF('day', ORDER_DATE, SHIP_DATE) - 2")


class NamesAndAttr(unittest.TestCase):
    """DATENAME returns Tableau's text (full month / weekday names) on DuckDB and Snowflake alike; ATTR(x) is the value
    when every row agrees (E-commerce Widget: 'Scalar Function datename / attr does not exist')."""
    def test_translate(self):
        sql = tr("DATENAME('month', [Order Date])")[0]
        self.assertIn("WHEN 3 THEN 'March'", sql)
        self.assertTrue(sql.startswith('CASE MONTH(ORDER_DATE)'))
        self.assertEqual(tr("DATENAME('year', [Order Date])")[0], 'CAST(YEAR(ORDER_DATE) AS VARCHAR)')
        self.assertEqual(tr('ATTR([Region])')[0], 'CASE WHEN MIN(REGION) = MAX(REGION) THEN MIN(REGION) END')
        self.assertEqual(tr("DATENAME('hour', [Order Date])"), (None, False))      # not translated: refused
        sql = tr('"In " + DATENAME(\'month\', [D]) + IF COUNTD([C]) = 1 THEN " for " + ATTR([C]) ELSE " all" END + '
                 'IF [X] > 1 THEN "!" END')[0]
        self.assertNotIn(' + ', sql)                          # every '+' joins text: '||'
        self.assertEqual(tr('IF [X] > 1 THEN 1 ELSE 0 END + 5')[0], 'CASE WHEN X > 1 THEN 1 ELSE 0 END + 5')

    def test_values_on_the_local_engine(self):
        import engine
        month, wday = tr("DATENAME('month', [D])")[0], tr("DATENAME('weekday', [D])")[0]
        got = engine.q(f"SELECT {month} AS M, {wday} AS W FROM (SELECT DATE '2024-03-05' AS D) t")
        self.assertEqual((got['M'].iloc[0], got['W'].iloc[0]), ('March', 'Tuesday'))


class DateLiterals(unittest.TestCase):
    """Tableau writes date filter members as #yyyy-mm-dd hh:mm:ss#. A date-part member ('#2022-03-01#' for a MONTH
    filter) was unreadable -> the filter silently dropped (Online Retail 'Customers.' showed every month); an exact
    date member was compared as text (Customer Opt-In 'Daily Opt-out': conversion error)."""
    def test_members(self):
        import engine
        self.assertEqual(engine._value_predicate({'caption': 'D', 'kind': 'in', 'datepart': 'mn',
                                                  'values': ['#2022-03-01 00:00:00#']}, expr='D'),
                         'EXTRACT(MONTH FROM D) IN (3)')
        self.assertEqual(engine._value_predicate({'caption': 'D', 'kind': 'not_in',
                                                  'values': ['#2023-07-28 00:00:00#']}, expr='D'),
                         "D NOT IN (CAST('2023-07-28 00:00:00' AS TIMESTAMP))")
        self.assertEqual(engine._value_predicate({'caption': 'R', 'kind': 'in', 'values': ['East']}, expr='R'),
                         "(R IN ('East'))")


class Bins(unittest.TestCase):
    """A Tableau bin (<calculation class='bin' formula='[x]' size='n' peg='p'/>) is its lower bound,
    FLOOR((x - peg) / size) * size + peg. Bins were never translated (Clara 'Histogram', GOLD CRM 'Sankey')."""
    def test_clara_histogram_bins(self):
        import glob
        import engine
        import plan_census as PC
        import v3_sheet_plan as P
        from v3_local import preview_context
        paths = sorted(glob.glob(str(ROOT / 'Workbooks' / '**' / 'Clara*.twbx'), recursive=True))
        if not paths:
            self.skipTest('corpus workbook')
        r = PC.intake(Path(paths[0]))
        ir = r['ir']
        e = next(v for v in ir['fields'].values() if v.get('caption') == 'Spnd Amt (bin)')
        self.assertIn('FLOOR', e['calculation']['sql'])
        s = next(s for d in ir['dashboards'] for s in d['sheets'] if s['name'] == 'Histogram')
        with preview_context(ir, {k: v for k, v in r['bindings'].items() if v}):
            df = P.execute(P.from_sheet(s, ir), s, engine)
            with engine.field_scope(s):
                n = engine.q(f"SELECT COUNT(DISTINCT {e['calculation']['sql']}) AS N FROM {engine.tbl(s)}")
        self.assertGreater(len(df), 1)
        self.assertLessEqual(len(df), int(n['N'].iloc[0]) + 1)
        size = float(re.search(r"/ ([\d.]+)\)", e['calculation']['sql']).group(1))
        bins = sorted(float(v) for v in df.iloc[:, 0] if v == v)
        self.assertTrue(all(abs(b / size - round(b / size)) < 1e-9 for b in bins))   # every value is a bin edge


class TablePlaceholder(unittest.TestCase):
    """A no-key FIXED's table is the query's FROM target -- never the column of EXTRACT(MONTH FROM DATE) that may
    come first (Hospitality: 'Table with name DATE does not exist')."""
    def test_extract_from_is_not_the_table(self):
        import engine
        seen = []
        old = engine._run_query
        engine._run_query = lambda sql: seen.append(sql)
        try:
            engine.q("SELECT EXTRACT(MONTH FROM DATE) AS M, (SELECT MAX(X) FROM __TBL__) AS T FROM SALES_T GROUP BY 1")
        finally:
            engine._run_query = old
        self.assertIn('(SELECT MAX(X) FROM SALES_T)', seen[0])


class ModelExtracts(unittest.TestCase):
    """A datasource whose model spans several data files binds to its decoded extract (every table of the model), not
    the first raw file. Clara Brazil: both datasources relate a CSV to a Calendar CSV; 7 sheets used the Calendar's
    Date1 ('column DATE1 not found'). Only Clara has this shape in the corpus: both of its datasources are checked."""
    def test_rule(self):
        import types
        import init_workbook as IW
        import v3_local as L
        old = IW.datasource_files
        IW.datasource_files = lambda root: {'M': ['a.csv', 'cal.csv', 'x.hyper'], 'S': ['b.csv', 'y.hyper'],
                                            'N': ['c.csv', 'd.csv', 'z.hyper']}
        try:
            got = L.prefer_model_extracts(None, {'M': '/a.csv', 'S': '/b.csv', 'N': '/c.csv'},
                                          {'x.csv': '/x.csv', 'y.csv': '/y.csv', 'z.csv': '/z.csv'},
                                          [{'extract': 'x.hyper', 'state': 'DECODED'},
                                           {'extract': 'y.hyper', 'state': 'DECODED'},
                                           {'extract': 'z.hyper', 'state': 'BLOCKED'}])
        finally:
            IW.datasource_files = old
        self.assertEqual(got, {'M': '/x.csv', 'S': '/b.csv', 'N': '/c.csv'})   # multi-file + decoded only

    def test_clara_calendar_fields(self):
        import glob
        import engine
        import plan_census as PC
        import v3_sheet_plan as P
        from v3_local import preview_context
        paths = sorted(glob.glob(str(ROOT / 'Workbooks' / '**' / 'Clara*.twbx'), recursive=True))
        if not paths:
            self.skipTest('corpus workbook')
        r = PC.intake(Path(paths[0]))
        ir = r['ir']
        for cap in ('clara_media', 'clara_response'):
            self.assertIn('federated_', Path(r['bindings'][cap]).name, cap)
        sh = {s['name']: s for d in ir['dashboards'] for s in d['sheets']}
        with preview_context(ir, {k: v for k, v in r['bindings'].items() if v}):
            for name in ('Trend', 'Trend M'):
                df = P.execute(P.from_sheet(sh[name], ir), sh[name], engine)
                self.assertGreater(len(df), 0, name)
            s = sh['Trend']
            with engine.field_scope(s):
                n = engine.q(f'SELECT COUNT(*) AS N, COUNT(DATE1) AS D FROM {engine.tbl(s)}')
        self.assertEqual(int(n['N'].iloc[0]), 239)                 # the extract's rows (fact grain kept)
        self.assertEqual(int(n['D'].iloc[0]), 239)                 # every row has its Calendar date


class Hospitality(unittest.TestCase):
    """Second-workbook proof on real data: RevPAR per property-year equals the ratio of the two sums computed by
    hand over the same property-year rows."""
    def test_revpar_equals_hand_computed_ratio(self):
        import glob
        import engine
        import plan_census as PC
        from v3_local import preview_context
        paths = sorted(glob.glob(str(ROOT / 'Workbooks' / '**' / 'Hospitality*.twbx'), recursive=True))
        if not paths:
            self.skipTest('corpus workbook')
        r = PC.intake(Path(paths[0]))
        ir = r['ir']
        self.assertIn('=RevPAR', ir['calcs'])
        sql = ir['calcs']['=RevPAR']['sql']
        s = next(s for d in ir['dashboards'] for s in d['sheets'] if s.get('datasource_id'))
        with preview_context(ir, {k: v for k, v in r['bindings'].items() if v}):
            with engine.field_scope(s):
                T = engine.tbl(s)
                year = ir['calcs']['=Year of Date']['sql']
                got = engine.q(f"SELECT PROPERTY AS P, {year} AS Y, MIN({sql}) AS V, MAX({sql}) AS W FROM {T} GROUP BY 1, 2")
                want = engine.q(f"SELECT PROPERTY AS P, {year} AS Y, SUM(ROOMS_REVENUE) / NULLIF(SUM(ROOMS_AVAILABLE), 0) AS V "
                                f"FROM {T} GROUP BY 1, 2")
        g = {(a, b): (c, d) for a, b, c, d in got.values}
        w = {(a, b): c for a, b, c in want.values}
        self.assertEqual(set(g), set(w))
        for k, v in w.items():
            self.assertAlmostEqual(g[k][0], g[k][1], places=6)       # one value per partition
            self.assertAlmostEqual(float(g[k][0]), float(v), places=6)


if __name__ == '__main__':
    unittest.main()
