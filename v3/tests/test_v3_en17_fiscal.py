"""EN17: fiscal-year date parts on shelves, and Tableau's week / weekday numbering. A datasource (or one date field)
with a fiscal year starting in month s groups year / quarter by the fiscal calendar; Tableau names a fiscal year by
the calendar year it ends in (start April: 1 Apr 2023 - 31 Mar 2024 is FY 2024). Week numbers follow Tableau: week 1
holds 1 January and weeks start on the datasource's first day of week. Written before the code."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import v3_sheet_plan as P  # noqa: E402


def value(expr_sql, d):
    import duckdb
    import backend
    c = duckdb.connect()
    backend.duckdb_compat(c)
    got = c.execute(backend.local_sql(f"SELECT {expr_sql.replace('__D__', f'DATE {chr(39)}{d}{chr(39)}')}")).fetchone()[0]
    return str(got)[:10] if hasattr(got, 'isoformat') else got


class Fiscal(unittest.TestCase):
    def test_fiscal_year_and_quarter(self):
        for d, fy, fq in (('2023-03-31', 2023, 4), ('2023-04-01', 2024, 1), ('2023-12-31', 2024, 3),
                          ('2024-01-01', 2024, 4)):
            self.assertEqual(value(P._date_sql('__D__', 'year', 'sunday', 4), d), fy, d)
            self.assertEqual(value(P._date_sql('__D__', 'quarter', 'sunday', 4), d), fq, d)

    def test_fiscal_truncation(self):
        self.assertEqual(value(P._date_sql('__D__', 'year_trunc', 'sunday', 4), '2024-02-10'), '2023-04-01')
        self.assertEqual(value(P._date_sql('__D__', 'quarter_trunc', 'sunday', 4), '2023-05-15'), '2023-04-01')
        self.assertEqual(value(P._date_sql('__D__', 'quarter_trunc', 'sunday', 4), '2024-03-31'), '2024-01-01')

    def test_calendar_unchanged(self):
        self.assertEqual(P._date_sql('__D__', 'year', 'sunday', 1), P._date_sql('__D__', 'year', 'sunday'))
        self.assertEqual(value(P._date_sql('__D__', 'month', 'sunday', 4), '2023-04-01'), 4)    # months never shift

    def test_fiscal_start_from_the_workbook(self):
        ds = ("<datasource name='federated.x'><date-options fiscal-year-start='april' start-of-week='monday' />"
              "<column name='[Ship Date]' datatype='date' fiscal-year-start='7' /></datasource>")
        self.assertEqual(P._fiscal_start({'datasource_xml': ds}, 'federated.x::Order Date'), 4)
        self.assertEqual(P._fiscal_start({'datasource_xml': ds}, 'federated.x::Ship Date'), 7)
        self.assertEqual(P._fiscal_start({'datasource_xml': '<datasource />'}, 'f::Order Date'), 1)

    def test_tableau_header_text(self):
        self.assertEqual(P.key_text('FY 2024', 'year'), '2024')


class Weeks(unittest.TestCase):
    def test_week_number_follows_the_first_day_of_week(self):
        # 2024-01-01 is a Monday. Sunday start: 6 Jan (Sat) week 1, 7 Jan (Sun) week 2.
        self.assertEqual(value(P._date_sql('__D__', 'week', 'sunday'), '2024-01-06'), 1)
        self.assertEqual(value(P._date_sql('__D__', 'week', 'sunday'), '2024-01-07'), 2)
        # Monday start: 7 Jan still week 1, 8 Jan week 2
        self.assertEqual(value(P._date_sql('__D__', 'week', 'monday'), '2024-01-07'), 1)
        self.assertEqual(value(P._date_sql('__D__', 'week', 'monday'), '2024-01-08'), 2)

    def test_weekday_is_sunday_zero_whatever_the_session(self):
        self.assertEqual(value(P._date_sql('__D__', 'weekday', 'sunday'), '2024-03-17'), 0)    # a Sunday
        self.assertNotIn('DAYOFWEEK', P._date_sql('__D__', 'weekday', 'sunday'))


if __name__ == '__main__':
    unittest.main()
