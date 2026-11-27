"""CC03: scheduled re-check of deployed apps (drift). Written before the code.
  * A person records a baseline for a migration: every Snowflake table the app reads (from its bindings, including
    each table of a relationship model) gets COUNT(*) and HASH_AGG(*) -- one read-only, code-built query per table.
  * A scheduled task (owner SQL, never OR REPLACE) calls V3_RECHECK(), which recomputes each baseline table and
    records unchanged / changed / missing in RECHECK_RESULTS. A changed source is the signal to re-validate the app
    against Tableau in the console (Tableau is not reachable from inside Snowflake).
  * Identifiers are validated; values are bound; no workbook data leaves Snowflake; failures are codes."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import v3_recheck as RC  # noqa: E402

BINDINGS = {'Orders': 'DB.S.ORDERS', 'File': '/tmp/tableau-v3-x/a.csv', 'None': None,
            'Model': {'tables': {'t1': {'table': 'DB.S.CUSTOMERS'}, 't2': {'table': 'DB.S.ORDERS'}}},
            'Bad': 'DB.S.X; DROP TABLE Y'}


class Sess:
    def __init__(self, now=None, baselines=None, fail_tables=()):
        self.sqls, self.params = [], []
        self.now, self.baselines, self.fail = now or {}, baselines or [], set(fail_tables)

    def sql(self, q, params=None):
        self.sqls.append(q)
        self.params.append(params)
        outer = self

        class R:
            def collect(self):
                if q.startswith('SELECT COUNT(*)'):
                    t = q.split(' FROM ')[1].strip()
                    if t in outer.fail:
                        raise RuntimeError('does not exist')
                    return [outer.now.get(t, (0, 0))]
                if 'FROM WBR_DB.V3_AGENT.RECHECK_BASELINE' in q:
                    return outer.baselines
                return []
        return R()


class Baseline(unittest.TestCase):
    def test_tables_from_bindings(self):
        self.assertEqual(RC.tables(BINDINGS), ['DB.S.CUSTOMERS', 'DB.S.ORDERS'])

    def test_record_baseline_one_query_per_table_and_bound_insert(self):
        s = Sess(now={'DB.S.ORDERS': (100, 123), 'DB.S.CUSTOMERS': (10, 9)})
        out = RC.record_baseline(s, 'TABLEAU_TO_SIS_SALES', BINDINGS, approved_by='Owner')
        self.assertEqual(out['status'], 'OK')
        self.assertEqual(out['tables'], 2)
        counts = [q for q in s.sqls if q.startswith('SELECT COUNT(*)')]
        self.assertEqual(counts, ['SELECT COUNT(*) AS N, HASH_AGG(*) AS H FROM DB.S.CUSTOMERS',
                                  'SELECT COUNT(*) AS N, HASH_AGG(*) AS H FROM DB.S.ORDERS'])
        ins = [q for q in s.sqls if q.startswith('INSERT')]
        self.assertTrue(ins and all('TABLEAU_TO_SIS_SALES' not in q for q in ins))

    def test_needs_a_person_and_a_valid_app_name(self):
        with self.assertRaises(ValueError):
            RC.record_baseline(Sess(), 'APP', BINDINGS, approved_by='')
        self.assertEqual(RC.record_baseline(Sess(), "APP'; DROP", BINDINGS, approved_by='x')['status'], 'APP_REFUSED')


class Recheck(unittest.TestCase):
    def test_unchanged_changed_missing(self):
        base = [('APP', 'DB.S.ORDERS', 100, 123), ('APP', 'DB.S.CUSTOMERS', 10, 9), ('APP', 'DB.S.GONE', 5, 5)]
        s = Sess(now={'DB.S.ORDERS': (100, 123), 'DB.S.CUSTOMERS': (11, 7)}, baselines=base,
                 fail_tables={'DB.S.GONE'})
        out = RC.recheck(s)
        by = {r['table']: r['status'] for r in out['results']}
        self.assertEqual(by, {'DB.S.ORDERS': 'unchanged', 'DB.S.CUSTOMERS': 'changed', 'DB.S.GONE': 'missing'})
        self.assertEqual(out['changed_apps'], ['APP'])
        self.assertTrue(any(q.startswith('INSERT INTO WBR_DB.V3_AGENT.RECHECK_RESULTS') for q in s.sqls))
        self.assertNotIn('does not exist', str(out))

    def test_setup_sql_task_and_procedure(self):
        sql = '\n'.join(RC.setup_sql('@WBR_DB.V3_AGENT.RELEASES/candidate.zip', 'POWERHOUSE', 'WBR_OWNER'))
        self.assertNotIn('OR REPLACE', sql)
        self.assertIn('CREATE PROCEDURE IF NOT EXISTS WBR_DB.V3_AGENT.V3_RECHECK()', sql)
        self.assertIn('CREATE TASK IF NOT EXISTS WBR_DB.V3_AGENT.V3_RECHECK_DAILY', sql)
        self.assertIn('CALL WBR_DB.V3_AGENT.V3_RECHECK()', sql)
        self.assertIn('ALTER TASK WBR_DB.V3_AGENT.V3_RECHECK_DAILY RESUME', sql)

    def test_console_offers_it(self):
        self.assertIn('v3_recheck', (ROOT / 'v3_console.py').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
