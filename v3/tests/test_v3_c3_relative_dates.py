"""C3: relative-date filters (Tableau 'last 3 months', 'year to date', 'next month' ...). Written BEFORE the fix; the
engine reported them as 'not translated' and showed every date. Tableau writes first-period / last-period offsets
(in whole periods of period-type) from an anchor: anchor-date when set, else today; include-null keeps NULL dates.
No corpus workbook uses one, so the proof is synthetic: dates in DuckDB, expected windows computed in Python."""
from datetime import date
from pathlib import Path
import sys
import unittest
import xml.etree.ElementTree as ET

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import tableau_parser as TP  # noqa: E402

DAYS = [date(2023, 12, 31), date(2024, 1, 1), date(2024, 2, 10), date(2024, 3, 15), date(2024, 3, 31),
        date(2024, 4, 1), date(2024, 4, 30), date(2024, 5, 1), None]


def parse(attrs):
    xml = f"<worksheet><view><filter class='relative-date' column='[ds].[none:Order Date:qk]' {attrs} /></view></worksheet>"
    return TP.applied_filters(ET.fromstring(xml), {})[0]


def rows(f):
    import backend
    import engine
    pred = engine._relative_date_predicate(dict(f, caption='Order Date'))
    con = duckdb.connect()
    con.execute('CREATE TABLE t (ORDER_DATE DATE)')
    con.executemany('INSERT INTO t VALUES (?)', [(d,) for d in DAYS])
    sql = backend._rewrite_dateadd_for_duckdb(f'SELECT ORDER_DATE FROM t WHERE {pred} ORDER BY 1 NULLS LAST')
    return [r[0] for r in con.execute(sql).fetchall()]


class RelativeDates(unittest.TestCase):
    def test_parse(self):
        f = parse("first-period='-2' last-period='0' period-type='month' anchor-date='#2024-03-15#' include-null='true'")
        self.assertEqual((f['kind'], f['first'], f['last'], f['period'], f['anchor'], f['include_null']),
                         ('relative_date', -2, 0, 'month', '2024-03-15', True))

    def test_last_three_months_with_anchor(self):
        got = rows(parse("first-period='-2' last-period='0' period-type='month' anchor-date='#2024-03-15#'"))
        self.assertEqual(got, [date(2024, 1, 1), date(2024, 2, 10), date(2024, 3, 15), date(2024, 3, 31)])

    def test_next_month_and_nulls(self):
        got = rows(parse("first-period='1' last-period='1' period-type='month' anchor-date='#2024-03-15#' "
                         "include-null='true'"))
        self.assertEqual(got, [date(2024, 4, 1), date(2024, 4, 30), None])

    def test_year_to_date(self):
        got = rows(parse("period-type='year' range-type='todate' anchor-date='#2024-03-15#'"))
        self.assertEqual(got, [date(2024, 1, 1), date(2024, 2, 10), date(2024, 3, 15)])

    def test_default_anchor_is_today(self):
        f = parse("first-period='0' last-period='0' period-type='day'")
        import engine
        self.assertIn('CURRENT_DATE', engine._relative_date_predicate(dict(f, caption='Order Date')))


    def test_weeks_start_on_the_datasource_first_day(self):
        import engine
        import backend
        f = dict(parse("first-period='0' last-period='0' period-type='week' anchor-date='#2024-03-13#'"),
                 caption='Order Date')                                     # Wednesday 13 March 2024
        con = duckdb.connect()
        con.execute('CREATE TABLE t (ORDER_DATE DATE)')
        days = [date(2024, 3, d) for d in range(9, 18)]
        con.executemany('INSERT INTO t VALUES (?)', [(d,) for d in days])
        for start, want in (('sunday', (10, 16)), ('monday', (11, 17))):
            pred = engine._relative_date_predicate(f, start)
            got = [r[0].day for r in con.execute(backend._rewrite_dateadd_for_duckdb(
                f'SELECT ORDER_DATE FROM t WHERE {pred} ORDER BY 1')).fetchall()]
            self.assertEqual((got[0], got[-1], len(got)), want + (7,), start)


if __name__ == '__main__':
    unittest.main()
