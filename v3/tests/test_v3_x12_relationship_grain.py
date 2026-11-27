"""X12: relationship-grain aggregation. In a RELATIONSHIP datasource (Tableau object model: several logical tables
with relationships), an aggregate whose inputs are only FIXED LOD values with one key K is aggregated at K's grain --
one value per (view mark, K) -- not once per underlying row (ECommerce 'Avg. Days to 2nd Purchase': per customer,
not per event; confirmed against Tableau's capture, master plan X12). Single-table and physical-join datasources keep
row-level replication (Tableau's documented FIXED behaviour there), and an expression that also reads a row-level
field (ECommerce CLV, the Current Period KPI) is unchanged. Written before the code."""
import glob
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools')]
import v3_sheet_plan as P  # noqa: E402

FIRST = "MIN(CASE WHEN T = 'order' THEN D END) OVER (PARTITION BY C)"
SECOND = "MAX(CASE WHEN T = 'order' THEN D END) OVER (PARTITION BY C)"
# customer a: 3 events (gap 10 days), b: 1 event... (gap 0), c: 2 events (gap 4); region R1 = a, b; R2 = c
ROWS = [('a', 'R1', 'order', 1), ('a', 'R1', 'order', 11), ('a', 'R1', 'view', 5),
        ('b', 'R1', 'order', 3),
        ('c', 'R2', 'order', 2), ('c', 'R2', 'order', 6)]


class Detect(unittest.TestCase):
    def test_only_fixed_inputs_with_one_key(self):
        import engine
        g = P._key_grain(f'AVG(({SECOND}) - ({FIRST}))', engine)
        self.assertEqual((g[0], g[2]), ('AVG', 'C'))

    def test_a_fixed_inside_a_fixed_on_the_same_key(self):
        import engine
        second = f"MIN(CASE WHEN T = 'order' AND D > {FIRST} THEN D END) OVER (PARTITION BY C)"
        g = P._key_grain(f"AVG(DATEDIFF('day', {FIRST}, {second}))", engine)
        self.assertEqual((g[0], g[2]), ('AVG', 'C'))

    def test_a_row_level_field_or_a_second_key_or_min_max_is_not(self):
        import engine
        self.assertIsNone(P._key_grain(f"AVG(CASE WHEN T = 'order' THEN {FIRST} END)", engine))     # reads T
        self.assertIsNone(P._key_grain(f'AVG({FIRST} - MIN(D) OVER (PARTITION BY R))', engine))      # two keys
        self.assertIsNone(P._key_grain(f'MIN({FIRST})', engine))                                     # no change
        self.assertIsNone(P._key_grain('AVG(D)', engine))


class Apply(unittest.TestCase):
    def test_every_qualifying_aggregate_in_a_measure_and_nothing_else(self):
        import engine
        kg = []
        avg = f'AVG(({SECOND}) - ({FIRST}))'
        out = P._apply_key_grain(f"CASE WHEN {avg} = MAX({avg}) OVER () THEN 'Max' END", engine, kg)
        self.assertEqual(kg, ['C', 'C'])
        self.assertEqual(out.count('__KG'), 2)
        self.assertEqual(P._apply_key_grain('SUM(X) / COUNT(Y)', engine, []), 'SUM(X) / COUNT(Y)')


class Exact(unittest.TestCase):
    def run_query(self, relationship, dims=True):
        import duckdb
        import engine
        c = duckdb.connect()
        c.execute('CREATE TABLE EV (C VARCHAR, R VARCHAR, T VARCHAR, D INT)')
        c.executemany('INSERT INTO EV VALUES (?, ?, ?, ?)', ROWS)
        plan = {'fields': ([{'caption': 'R', 'kind': 'dim', 'date_part': 'none', 'agg': 'none'}] if dims else []) +
                [{'caption': 'Gap', 'kind': 'measure', 'agg': 'avg'}], 'aggregate': True}

        class Eng:
            IR = {'field_scopes': {'d': {'relationship_model': relationship}}}
            CALCS = {}
            scan_row_windows = staticmethod(engine.scan_row_windows)
            _match_paren_engine = staticmethod(engine._match_paren_engine)
            rdim = staticmethod(lambda cap: cap)
            tbl = staticmethod(lambda s: 'EV')
            sub_params = staticmethod(lambda s: s)
            PARAMS = {}

            @staticmethod
            def resolve_measure(T, name, ref):
                return {'sql': f'AVG(({SECOND}) - ({FIRST}))'}
        sql, _ = P.compile_query(plan, {'datasource_id': 'd', 'name': 'S'}, Eng, "WHERE T <> 'x'", lod_where='')
        return dict(c.execute(sql).fetchall()) if dims else c.execute(sql).fetchall()[0][0]

    def test_relationship_model_averages_per_customer(self):
        self.assertEqual(self.run_query(True), {'R1': 5.0, 'R2': 4.0})        # (10 + 0) / 2 customers; c: 4
        self.assertAlmostEqual(self.run_query(True, dims=False), 14 / 3)      # (10 + 0 + 4) / 3 customers

    def test_single_table_keeps_row_replication(self):
        self.assertEqual(self.run_query(False), {'R1': 7.5, 'R2': 4.0})       # (10*3 + 0) / 4 events


def corpus(name):
    return glob.glob(str(ROOT / 'Workbooks' / '**' / name), recursive=True)


REL = 'ECommerce_Live_Relationship_Events_Customers_Products.twbx'
JOIN = 'Globalsalesdashboard.twbx'                # a physical join inside one logical table


@unittest.skipUnless(corpus(REL) and corpus(JOIN), 'corpus')
class Scope(unittest.TestCase):
    def test_a_relationship_model_is_flagged_and_a_physical_join_is_not(self):
        import plan_census
        flags = {}
        for name in (REL, JOIN):
            ir = plan_census.intake(Path(corpus(name)[0]))['ir']
            flags[name] = {bool(s.get('relationship_model')) for s in ir['field_scopes'].values()}
        self.assertEqual(flags[REL], {True})
        self.assertNotIn(True, flags[JOIN])

if __name__ == '__main__':
    unittest.main()
