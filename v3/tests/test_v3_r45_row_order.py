"""R45: row order of a sheet's query does not depend on the warehouse's threads.
Found by the release44 Windows gate: on a many-core machine DuckDB returned GROUP BY rows in a different order
on every run, so a table's rows (and the A4 / A10 comparisons) changed between reruns. Snowflake gives no order
for GROUP BY either. Rule: the plan's aggregate query is ordered by its dimensions (Tableau's default header
order) before its row cap, so the rows -- and which rows the cap keeps -- are the same every time.
Written BEFORE the fix: with DuckDB at 16 threads the same plan gave different row orders."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
WI = ROOT / 'Workbooks' / 'World_Indicators_Tableau2024_3.twbx'
SS = ROOT / 'Workbooks' / 'Superstore_Tableau2024_3.twbx'


def many_threads():
    import duckdb
    real = duckdb.connect

    def connect(*a, **k):
        c = real(*a, **k)
        c.execute('SET threads=16')
        return c
    duckdb.connect = connect
    return lambda: setattr(duckdb, 'connect', real)


class Corpus(unittest.TestCase):
    def same_order(self, path, sheet_name):
        if not path.exists():
            self.skipTest('corpus workbook')
        import engine
        import plan_census as PC
        import v3_sheet_plan as P
        from v3_local import preview_context
        undo = many_threads()
        try:
            r = PC.intake(path)
            sheet = next(s for d in r['ir']['dashboards'] for s in d['sheets'] if s['name'] == sheet_name)
            runs, seen = [], []
            real_q = engine.q
            engine.q = lambda sql, *a, **k: (seen.append(sql), real_q(sql, *a, **k))[1]
            for _ in range(2):
                with preview_context(r['ir'], {k: v for k, v in r['bindings'].items() if v}):
                    plan = P.from_sheet(sheet, r['ir'])
                    runs.append(P.execute(plan, sheet, engine).astype(str).reset_index(drop=True))
        finally:
            undo()
            engine.q = real_q
        import re
        self.assertTrue(any(re.search(r'GROUP BY [\d, ]+ ORDER BY [\d, ]+ LIMIT \d+', q) for q in seen))
        self.assertGreater(len(runs[0]), 20)
        self.assertTrue(runs[0].equals(runs[1]))

    def test_world_indicators_health(self):
        self.same_order(WI, 'Health Indicators')

    def test_superstore_second_workbook(self):
        self.same_order(SS, 'Product Detail Sheet')


if __name__ == '__main__':
    unittest.main()
