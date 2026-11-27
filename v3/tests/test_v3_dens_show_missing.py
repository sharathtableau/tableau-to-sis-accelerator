"""DENS: Tableau's "Show Missing Values" (<show-full-range> on a bin or date field): the view gets every member of
that field's range [min, max] (bin size / date unit steps) for each combination of the other dimensions present;
padded marks have no measure value, and table calculations run over the padded marks (Sankey path frames, running
totals). Written before the code."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import v3_sheet_plan as P  # noqa: E402
import calc_translator as CT  # noqa: E402

ROWS = [('E', 1.0, '2024-01-15', 10), ('E', 2.4, '2024-03-02', 5), ('E', 5.2, '2024-03-20', 7),
        ('W', 2.0, '2024-02-10', 1), ('W', 4.9, '2024-04-01', 2)]


def duck():
    import duckdb
    import backend
    c = duckdb.connect()
    backend.duckdb_compat(c)
    c.execute('CREATE TABLE T (R VARCHAR, X DOUBLE, D DATE, S INT)')
    c.executemany('INSERT INTO T VALUES (?, ?, ?, ?)', ROWS)
    return c


BIN = 'FLOOR(((X) - 0) / 1) * 1 + 0'


def run(dims, measures):
    """dims: [(caption, sql, date_part, full_range)]; measures: [(caption, sql, window)]"""
    import engine as E
    import backend
    sqls = {c: s for c, s, *_ in dims}
    msql = {c: (s, w) for c, s, w in measures}
    plan = {'fields': [{'caption': c, 'kind': 'dim', 'channel': 'rows', 'date_part': p, 'discrete': True,
                        'full_range': fr} for c, s, p, fr in dims] +
                      [{'caption': c, 'kind': 'measure', 'agg': 'usr', 'channel': 'text', 'date_part': 'none',
                        'window': {'addressing': 'field', 'ordering_field': f'[ds].[{dims[-1][0]}]'} if w else None}
                       for c, s, w in measures], 'sort': [], 'aggregate': True}

    class Eng:
        CALCS, PARAMS, IR = {}, {}, {}
        rdim = staticmethod(lambda cap: sqls[cap])
        scan_row_windows = staticmethod(E.scan_row_windows)
        _match_paren_engine = staticmethod(E._match_paren_engine)
        tbl = staticmethod(lambda sheet: 'T')
        sub_params = staticmethod(lambda q: q)

        @staticmethod
        def resolve_measure(T, name, ref):
            s, w = msql[ref['caption']]
            return {'sql': s, 'window': w}
    sql, cols = P.compile_query(plan, {'name': 'S'}, Eng, '')
    return duck().execute(backend.local_sql(sql)).fetchall()


class Padding(unittest.TestCase):
    def test_bins_padded_per_partition(self):
        rows = run([('R', 'R', 'none', False), ('Bin', BIN, 'none', True)], [('S', 'SUM(S)', False)])
        got = {(r, float(b)): s for r, b, s in rows}
        # range over the view: 1..5 for every R present; padded marks have no value
        self.assertEqual(set(got), {(r, float(b)) for r in 'EW' for b in range(1, 6)})
        self.assertEqual(got[('E', 1.0)], 10)
        self.assertIsNone(got[('E', 3.0)])
        self.assertEqual(got[('W', 4.0)], 2)

    def test_table_calcs_run_over_padded_marks(self):
        sql, _a = CT.translate_formula('INDEX()')
        rows = run([('R', 'R', 'none', False), ('Bin', BIN, 'none', True)], [('I', sql, True)])
        got = {(r, float(b)): i for r, b, i in rows}
        self.assertEqual([got[('E', float(b))] for b in range(1, 6)], [1, 2, 3, 4, 5])

    def test_months_padded(self):
        rows = run([('Month', 'D', 'month_trunc', True)], [('S', 'SUM(S)', False)])
        got = {str(m)[:10]: s for m, s in rows}
        self.assertEqual(sorted(got), ['2024-01-01', '2024-02-01', '2024-03-01', '2024-04-01'])
        self.assertEqual(got['2024-03-01'], 12)

    def test_without_show_missing_nothing_is_added(self):
        rows = run([('R', 'R', 'none', False), ('Bin', BIN, 'none', False)], [('S', 'SUM(S)', False)])
        self.assertEqual(len(rows), 5)


class FromTheWorkbook(unittest.TestCase):
    def test_show_full_range_marks_the_field(self):
        xml = ("<worksheet name='S'><table><show-full-range><column>[federated.a].[Path Frame (bin)]</column>"
               "</show-full-range></table></worksheet>")
        self.assertEqual(P._full_range_fields({'source_xml': xml}), {'Path Frame (bin)'})


if __name__ == '__main__':
    unittest.main()
