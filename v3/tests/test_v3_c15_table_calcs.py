"""C14 (table functions): moving windows WINDOW_*(e, start, end), the full WINDOW_* family (STDEV / VAR / PERCENTILE /
CORR / COVAR), RANK_MODIFIED / RANK_PERCENTILE, SIZE(), RANK's 'asc' argument, RANK partitioned by the addressing,
and PREVIOUS_VALUE refused with its reason. Values computed independently in Python on the view's marks."""
from pathlib import Path
import statistics
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import calc_translator as CT  # noqa: E402
import v3_sheet_plan as P  # noqa: E402

ROWS = [('E', 1, 10), ('E', 2, 30), ('E', 3, 30), ('E', 4, 5), ('E', 5, 20),
        ('W', 1, 1), ('W', 2, 2), ('W', 3, 3), ('W', 4, 4), ('W', 5, 50)]


class Eng:
    CALCS = {}
    rdim = staticmethod(lambda cap: cap)


def duck():
    import duckdb
    c = duckdb.connect()
    c.execute('CREATE TABLE V (R VARCHAR, M INT, S DOUBLE, S2 DOUBLE)')
    c.executemany('INSERT INTO V VALUES (?, ?, ?, ?)', [(r, m, s, s * (m % 3 + 1)) for r, m, s in ROWS])
    return c


DIMS = [{'caption': 'R', 'kind': 'dim', 'channel': 'rows', 'date_part': 'none', 'discrete': True},
        {'caption': 'M', 'kind': 'dim', 'channel': 'cols', 'date_part': 'none', 'discrete': True}]
PLAN = {'fields': DIMS, 'sort': [], 'aggregate': True}


def run(formula):
    """Each R is a row of the view, M runs across (Table across: partition by R, along M)."""
    sql, _agg = CT.translate_formula(formula.replace('[S]', '[S]'))
    assert sql is not None, CT.refusal(formula)
    expr = P.window_sql(sql, PLAN, {'window': {'addressing': 'cols'}, 'caption': formula}, Eng, DIMS)
    import backend
    rows = duck().execute(backend.local_sql(f'SELECT R, M, {expr} FROM V GROUP BY R, M ORDER BY R, M')).fetchall()
    return {(r, m): v for r, m, v in rows}


def per_r(fn):
    out = {}
    for r in ('E', 'W'):
        vals = [s for rr, m, s in sorted(ROWS) if rr == r]
        for i, v in enumerate(fn(vals)):
            out[(r, i + 1)] = v
    return out


def close(a, b):
    return all((a[k] is None and b[k] is None) or abs(float(a[k]) - float(b[k])) < 1e-9 for k in b) and a.keys() == b.keys()


class Windows(unittest.TestCase):
    def check(self, formula, fn):
        got, want = run(formula), per_r(fn)
        self.assertTrue(close(got, want), f'{formula}\n got  {got}\n want {want}')

    def test_moving_and_partial_windows(self):
        self.check('WINDOW_AVG(SUM([S]), -2, 0)', lambda v: [statistics.mean(v[max(0, i - 2):i + 1]) for i in range(len(v))])
        self.check('WINDOW_SUM(SUM([S]), FIRST(), 0)', lambda v: [sum(v[:i + 1]) for i in range(len(v))])
        self.check('WINDOW_MAX(SUM([S]), 0, 1)', lambda v: [max(v[i:i + 2]) for i in range(len(v))])
        self.check('WINDOW_SUM(SUM([S]), FIRST(), LAST())', lambda v: [sum(v)] * len(v))

    def test_window_statistics(self):
        self.check('WINDOW_STDEV(SUM([S]))', lambda v: [statistics.stdev(v)] * len(v))
        self.check('WINDOW_VARP(SUM([S]))', lambda v: [statistics.pvariance(v)] * len(v))
        self.check('WINDOW_PERCENTILE(SUM([S]), 0.5)', lambda v: [statistics.median(v)] * len(v))
        self.check('SIZE()', lambda v: [len(v)] * len(v))

    def test_ranks(self):
        def modified(desc):
            return lambda v: [sum(1 for w in v if (w >= x if desc else w <= x)) for x in v]
        self.check('RANK_MODIFIED(SUM([S]))', modified(True))
        self.check('RANK_PERCENTILE(SUM([S]))', lambda v: [(modified(False)(v)[i] - 1) / (len(v) - 1)
                                                           for i in range(len(v))])
        self.check('RANK(SUM([S]))', lambda v: [1 + sum(1 for w in v if w > x) for x in v])        # per R
        self.check('RANK_UNIQUE(SUM([S]), "asc")', lambda v: [sorted(range(len(v)), key=lambda i: (v[i], i)).index(i) + 1
                                                             for i in range(len(v))])

    def test_correlation(self):
        got = run('WINDOW_CORR(SUM([S]), SUM([S2]))')
        for r in ('E', 'W'):
            x = [s for rr, m, s in sorted(ROWS) if rr == r]
            y = [s * (m % 3 + 1) for rr, m, s in sorted(ROWS) if rr == r]
            self.assertAlmostEqual(float(got[(r, 1)]), statistics.correlation(x, y), 9)

    def test_refusals(self):
        self.assertEqual(CT.translate_formula('PREVIOUS_VALUE(0) + SUM([S])'), (None, False))
        self.assertIn('recursive', CT.refusal('PREVIOUS_VALUE(0) + SUM([S])'))
        self.assertEqual(CT.translate_formula('WINDOW_MEDIAN(SUM([S]), -1, 0)'), (None, False))
        self.assertIn('moving window', CT.refusal('WINDOW_MEDIAN(SUM([S]), -1, 0)'))


class NestedTableCalcs(unittest.TestCase):
    """A table calculation over another table calculation (RUNNING_SUM of a percent of total, WINDOW_MAX of a
    LOOKUP): evaluated in layers over the view's marks -- the aggregates first, then each window level."""
    def compile(self, formula):
        import engine as E
        plan = {'fields': DIMS + [{'caption': 'X', 'kind': 'measure', 'agg': 'usr', 'channel': 'text',
                                   'date_part': 'none', 'window': {'addressing': 'cols'}}], 'sort': [],
                'aggregate': True}
        sql, _a = CT.translate_formula(formula)
        assert sql, CT.refusal(formula)

        class Eng2(Eng):
            CALCS = {'X': {'sql': sql, 'agg_ready': True, 'window': True}}
            PARAMS = {}
            IR = {}
            scan_row_windows = staticmethod(E.scan_row_windows)
            _match_paren_engine = staticmethod(E._match_paren_engine)
            tbl = staticmethod(lambda sheet: 'V')
            sub_params = staticmethod(lambda q: q)

            @staticmethod
            def resolve_measure(T, name, ref):
                return {'sql': sql, 'window': True}
        q, cols = P.compile_query(plan, {'name': 'S'}, Eng2, '')
        import backend
        rows = duck().execute(backend.local_sql(q)).fetchall()
        return {(r[0], r[1]): r[2] for r in rows}

    def test_running_sum_of_percent_of_total(self):
        got = self.compile('RUNNING_SUM(SUM([S]) / TOTAL(SUM([S])))')
        want = per_r(lambda v: [sum(v[:i + 1]) / sum(v) for i in range(len(v))])
        self.assertTrue(close(got, want), (got, want))

    def test_window_max_of_a_lookup(self):
        got = self.compile('WINDOW_MAX(LOOKUP(SUM([S]), -1))')
        want = per_r(lambda v: [max(v[:-1])] * len(v))
        self.assertTrue(close(got, want), (got, want))

    def test_single_level_sql_is_unchanged(self):
        self.assertFalse(P._nested_window('SUM(SUM(S)) OVER (PARTITION BY R)'))


if __name__ == '__main__':
    unittest.main()
