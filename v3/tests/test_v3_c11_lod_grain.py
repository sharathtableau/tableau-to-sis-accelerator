"""C11: INCLUDE / EXCLUDE LODs at the sheet's grain, and FIXED over FIXED (a FIXED inside a FIXED's body or its
PARTITION BY, a table calculation ordered by a FIXED). Rules on the Tableau constructs, checked on independent
arithmetic and on two corpus workbooks (Online Retail INCLUDE, Hospitality nested FIXED)."""
import glob
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools')]
import calc_translator as CT  # noqa: E402
import v3_sheet_plan as P  # noqa: E402

ROWS = [('E', 'a', 1, 1), ('E', 'a', 1, 2), ('E', 'b', 2, 10), ('E', 'b', 3, None), ('W', 'a', 1, 5),
        ('W', 'c', 4, None), ('W', 'c', 4, None)]


class Eng:
    rdim = staticmethod(lambda cap: cap)


def duck():
    import duckdb
    c = duckdb.connect()
    c.execute('CREATE TABLE T (REGION VARCHAR, CITY VARCHAR, DAY INT, SALES INT)')
    c.executemany('INSERT INTO T VALUES (?, ?, ?, ?)', ROWS)
    return c


def by_region(expr):
    return dict(duck().execute(f'SELECT REGION, {expr} FROM T GROUP BY 1').fetchall())


def tableau_include(view_agg, inner, keys):
    """Independent: the inner aggregate per (region, keys) group, then the view aggregation over those groups."""
    out = {}
    for reg in sorted({r[0] for r in ROWS}):
        groups = {}
        for r in ROWS:
            if r[0] == reg:
                groups.setdefault(tuple(r[{'CITY': 1, 'DAY': 2}[k]] for k in keys), []).append(r[3])
        vals = []
        for v in groups.values():
            nn = [x for x in v if x is not None]
            vals.append({'SUM': sum(nn) if nn else None, 'COUNT': len(nn), 'MIN': min(nn) if nn else None,
                         'MAX': max(nn) if nn else None}[inner])
        vals = [x for x in vals if x is not None]
        out[reg] = {'SUM': sum(vals), 'AVG': sum(vals) / len(vals) if vals else None, 'MIN': min(vals),
                    'MAX': max(vals)}[view_agg]
    return out


DIMS = [{'caption': 'REGION', 'date_part': 'none'}]


class Translate(unittest.TestCase):
    def test_include_and_exclude_become_plan_markers(self):
        self.assertEqual(CT.translate_formula('{INCLUDE DAY([Order Date]), [Region]: SUM([Sales])}'),
                         ('__INCLUDE__(SUM(SALES), __KEYS__(DAY(ORDER_DATE), REGION))', True))
        self.assertEqual(CT.translate_formula('{EXCLUDE [Region]: SUM([Sales])}'),
                         ('__EXCLUDE__(SUM(SALES), __KEYS__(REGION))', True))

    def test_any_aggregate_body_translates_and_bad_shapes_refuse(self):
        self.assertEqual(CT.translate_formula('{INCLUDE [Region]: AVG([Sales])}')[0],
                         '__INCLUDE__(AVG(SALES), __KEYS__(REGION))')
        self.assertTrue(CT.translate_formula('{EXCLUDE [Region]: SUM([Sales]) / SUM([Profit])}')[0])
        for f in ('{INCLUDE [Region]: SUM({FIXED [City]: SUM([Sales])})}', '{INCLUDE : SUM([Sales])}',
                  '{INCLUDE [Region]: [Sales]}'):
            self.assertEqual(CT.translate_formula(f), (None, False), f)

    def test_a_fixed_over_a_fixed_is_row_level(self):
        defs = {'R': '{FIXED [P]: SUM([Rev])/SUM([Rooms])}'}
        sql, agg = CT.translate_formula('IF {FIXED [P]: AVG([R])} >= 1 THEN TRUE ELSE FALSE END', calc_defs=defs)
        self.assertFalse(agg, sql)
        self.assertTrue(CT.translate_formula('WINDOW_SUM(SUM([Rev]))')[1])     # a table calc stays aggregate


class IncludeExact(unittest.TestCase):
    def check(self, view_agg, inner, keys):
        m = {'caption': 'x', 'agg': view_agg.lower()}
        expr = P.lod_view_sql(f"__INCLUDE__({inner}(SALES), __KEYS__({', '.join(keys)}))", m, DIMS, Eng)
        got, want = by_region(expr), tableau_include(view_agg, inner, keys)
        for k in want:
            self.assertAlmostEqual(float(got[k]), float(want[k]), 9, (view_agg, inner, keys, k))

    def test_decomposable_combinations_equal_the_two_step_computation(self):
        for view_agg, inner in (('SUM', 'SUM'), ('MIN', 'MIN'), ('MAX', 'MAX'), ('SUM', 'COUNT'), ('AVG', 'SUM'),
                                ('AVG', 'COUNT')):
            for keys in (['CITY'], ['DAY'], ['CITY', 'DAY']):
                self.check(view_agg, inner, keys)

    def test_a_group_with_no_value_is_skipped_by_avg(self):
        # W/c has only NULL sales: AVG over W's city groups is 5 (a), not 5 / 2
        self.assertEqual(by_region(P.lod_view_sql('__INCLUDE__(SUM(SALES), __KEYS__(CITY))',
                                                  {'agg': 'avg'}, DIMS, Eng))['W'], 5)

    def test_the_calc_may_carry_its_own_outer_aggregation(self):
        self.assertEqual(P.lod_view_sql('AVG(__INCLUDE__(SUM(SALES), __KEYS__(CITY)))', {'agg': 'usr'}, DIMS, Eng),
                         P.lod_view_sql('__INCLUDE__(SUM(SALES), __KEYS__(CITY))', {'agg': 'avg'}, DIMS, Eng))

    def test_refusals(self):
        for expr, agg in (('__INCLUDE__(SUM(SALES), __KEYS__(CITY)) * 2', 'sum'),       # no aggregation around it
                          ('__INCLUDE__(SUM(SALES), __KEYS__(CITY))', 'usr')):
            with self.assertRaises(P.PlanError, msg=(expr, agg)):
                P.lod_view_sql(expr, {'caption': 'x', 'agg': agg}, DIMS, Eng, rows=[])
        with self.assertRaises(P.PlanError):         # a general shape needs the plan's row layer
            P.lod_view_sql('__INCLUDE__(MIN(SALES), __KEYS__(CITY))', {'caption': 'x', 'agg': 'avg'}, DIMS, Eng)


class ExcludeExact(unittest.TestCase):
    def test_exclude_is_the_coarser_total_on_every_mark(self):
        dims = [{'caption': 'REGION', 'date_part': 'none'}, {'caption': 'CITY', 'date_part': 'none'}]
        expr = P.lod_view_sql('__EXCLUDE__(SUM(SALES), __KEYS__(CITY))', {'agg': 'sum'}, dims, Eng)
        got = dict(((r, c), v) for r, c, v in duck().execute(f'SELECT REGION, CITY, {expr} FROM T GROUP BY 1, 2')
                   .fetchall())
        region = {}
        for r in ROWS:
            region[r[0]] = region.get(r[0], 0) + (r[3] or 0)
        self.assertEqual(got, {k: region[k[0]] for k in got})


class NestedFixedLayers(unittest.TestCase):
    def test_a_window_in_an_over_clause_is_detected(self):
        import engine
        self.assertTrue(P._window_in_over('ROW_NUMBER() OVER (ORDER BY SUM(SUM(X) OVER (PARTITION BY A)))', engine))
        self.assertTrue(P._window_in_over('COUNT(X) OVER (PARTITION BY (AVG(Y) OVER (PARTITION BY B)))', engine))
        self.assertFalse(P._window_in_over('SUM(X) OVER (PARTITION BY A), MAX(Y) OVER ()', engine))

    def test_scan_takes_a_window_in_a_partition_first(self):
        import engine
        text, found = engine.scan_row_windows('COUNT(X) OVER (PARTITION BY B, (AVG(Y) OVER (PARTITION BY A)))', 0)
        self.assertEqual([e for e, _ in found], ['AVG(Y) OVER (PARTITION BY A)'])
        self.assertEqual(engine.scan_row_windows(text, 1)[1][0][0], 'COUNT(X) OVER (PARTITION BY B, (__W0__))')

    def test_layers_compute_inner_first_and_equal_two_steps(self):
        import engine
        sel = ['A AS D0', 'SUM(COUNT(X) OVER (PARTITION BY (MAX(Y) OVER (PARTITION BY A) > 2))) AS M0']
        s, src, where = P._fixed_before_filters(sel, 'T', '', '', engine)
        import duckdb
        c = duckdb.connect()
        c.execute("CREATE TABLE T AS SELECT * FROM (VALUES ('p', 1, 1), ('p', 2, 3), ('q', 3, 1)) v(A, X, Y)")
        got = dict(c.execute(f'SELECT {s[0]} FROM {src} {where} GROUP BY 1').fetchall())
        # p: max Y 3 > 2 -> flag True; partition True holds p's 2 rows -> COUNT 2 per row, SUM over p's rows 4
        # q: max Y 1 -> False; partition False holds 1 row -> 1
        self.assertEqual(got, {'p': 4, 'q': 1})



class GeneralShapes(unittest.TestCase):
    """Every INCLUDE / EXCLUDE shape through the plan's row layer (windows over the filtered rows): the inner
    aggregate per (marks + keys) or (marks - keys), the view aggregation over those values."""
    def query(self, marker_sql, agg, dims=('REGION',)):
        import engine as E
        ds = [{'caption': d, 'kind': 'dim', 'channel': 'rows', 'date_part': 'none', 'discrete': True} for d in dims]
        plan = {'fields': ds + [{'caption': 'Z', 'kind': 'measure', 'agg': agg, 'channel': 'text',
                                 'date_part': 'none'}], 'sort': [], 'aggregate': True}

        class Eng2(Eng):
            CALCS, PARAMS, IR = {}, {}, {}
            scan_row_windows = staticmethod(E.scan_row_windows)
            _match_paren_engine = staticmethod(E._match_paren_engine)
            tbl = staticmethod(lambda sheet: 'T')
            sub_params = staticmethod(lambda q: q)

            @staticmethod
            def resolve_measure(T, name, ref):
                return {'sql': marker_sql}
        sql, _c = P.compile_query(plan, {'name': 'S'}, Eng2, "WHERE CITY <> 'zz'")
        return duck().execute(sql).fetchall()

    def test_include_any_inner_and_view_aggregation(self):
        for view_agg, inner in (('MAX', 'SUM'), ('AVG', 'MIN'), ('MIN', 'SUM'), ('AVG', 'AVG'), ('SUM', 'MAX')):
            got = dict(self.query(f'__INCLUDE__({inner}(SALES), __KEYS__(CITY))', view_agg.lower()))
            want = tableau_include(view_agg, inner, ['CITY']) if inner != 'AVG' else None
            if want is None:                             # AVG inner: average per city, averaged per region
                want = {}
                for reg in ('E', 'W'):
                    per = {}
                    for r in ROWS:
                        if r[0] == reg and r[3] is not None:
                            per.setdefault(r[1], []).append(r[3])
                    vals = [sum(v) / len(v) for v in per.values()]
                    want[reg] = sum(vals) / len(vals)
            for k in want:
                self.assertAlmostEqual(float(got[k]), float(want[k]), 9, (view_agg, inner, k))

    def test_exclude_average(self):
        got = dict(((r, c), v) for r, c, v in self.query('__EXCLUDE__(AVG(SALES), __KEYS__(CITY))', 'sum',
                                                         dims=('REGION', 'CITY')))
        for (reg, city), v in got.items():
            vals = [r[3] for r in ROWS if r[0] == reg and r[3] is not None]
            self.assertAlmostEqual(float(v), sum(vals) / len(vals), 9)

    def test_include_inside_a_larger_expression(self):
        got = dict(self.query('MAX(__INCLUDE__(SUM(SALES), __KEYS__(CITY))) / NULLIF(SUM(SALES), 0)', 'usr'))
        mx = tableau_include('MAX', 'SUM', ['CITY'])
        for reg in mx:
            tot = sum(r[3] or 0 for r in ROWS if r[0] == reg)
            self.assertAlmostEqual(float(got[reg]), mx[reg] / tot, 9)

def corpus(name):
    return glob.glob(str(ROOT / 'Workbooks' / '**' / name), recursive=True)


@unittest.skipUnless(corpus('Online Retail Dashboard.twbx') and corpus('Hospitality_-_Rooms.twbx'), 'corpus')
class Corpus(unittest.TestCase):
    @staticmethod
    def run_sheets(wb, names):
        import engine
        import plan_census
        from v3_local import preview_context
        r = plan_census.intake(Path(corpus(wb)[0]))
        ir = r['ir']
        sheets = {s['name']: s for d in ir['dashboards'] for s in d['sheets']}
        with preview_context(ir, {k: v for k, v in r['bindings'].items() if v}):
            return {n: P.execute(P.from_sheet(sheets[n], ir), sheets[n], engine) for n in names}

    def test_online_retail_include_day_is_the_daily_average(self):
        out = self.run_sheets('Online Retail Dashboard.twbx', ['Q.avg', 'Q.qunatity', 'q. weekly trend'])
        days = len(out['q. weekly trend'])                  # the sheet's own days (its March context)
        total = float(out['Q.qunatity'].select_dtypes('number').iloc[0, 0])
        self.assertAlmostEqual(float(out['Q.avg'].iloc[0, 0]), total / days, 6)

    def test_hospitality_success_flag_sheets_run(self):
        out = self.run_sheets('Hospitality_-_Rooms.twbx', ['Unsucc.by Brand', 'Brand Success', 'Top&Bottom N'])
        flag = [c for c in out['Brand Success'].columns if 'SuccessFlag' in c][0]
        self.assertTrue(set(out['Brand Success'][flag]) <= {True, False})
        self.assertGreater(len(out['Top&Bottom N']), 0)      # its rank group follows the pill's Compute Using (C14)


if __name__ == '__main__':
    unittest.main()
