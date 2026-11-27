"""EN33: warehouse and Cortex spend monitoring (with EN03). Written before the code.
  * Read-only spend view for the console: daily credits by service type for the last 30 days from
    SNOWFLAKE.ACCOUNT_USAGE.METERING_DAILY_HISTORY, and Cortex AI function credits by function and model from
    SNOWFLAKE.ACCOUNT_USAGE.CORTEX_FUNCTIONS_USAGE_HISTORY. Code-built SELECTs only; a role without ACCOUNT_USAGE
    access gets a status code, never error text.
  * Admin SQL (the owner has no ACCOUNTADMIN): a resource monitor on the app warehouse (notify 80%, suspend 100%)
    with a credit quota the owner chooses; generated, validated, never run by the app."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import v3_spend as SP  # noqa: E402


class Sess:
    def __init__(self, fail=False):
        self.sqls, self.fail = [], fail

    def sql(self, q, params=None):
        self.sqls.append(q)
        outer = self

        class Row(tuple):
            pass

        class R:
            def collect(self):
                if outer.fail:
                    raise RuntimeError('Object does not exist or not authorized: internal detail')
                if 'CORTEX_FUNCTIONS_USAGE_HISTORY' in q:
                    return [('AI_COMPLETE', 'claude-sonnet-4-5', 1.25)]
                return [('2026-11-20', 'AI_SERVICES', 2.5), ('2026-11-20', 'WAREHOUSE_METERING', 4.0)]
        return R()


class Spend(unittest.TestCase):
    def test_read_only_and_shaped(self):
        s = Sess()
        out = SP.spend(s, days=30)
        self.assertEqual(out['status'], 'OK')
        self.assertEqual(out['by_day'][0], {'day': '2026-11-20', 'service': 'AI_SERVICES', 'credits': 2.5})
        self.assertEqual(out['cortex'][0]['function'], 'AI_COMPLETE')
        self.assertTrue(all(q.lstrip().upper().startswith('SELECT') for q in s.sqls))

    def test_no_access_is_a_code(self):
        out = SP.spend(Sess(fail=True))
        self.assertEqual(out['status'], 'NO_ACCOUNT_USAGE_ACCESS:RuntimeError')
        self.assertNotIn('internal detail', str(out))

    def test_days_are_bounded(self):
        s = Sess()
        SP.spend(s, days=99999)
        self.assertIn('-90', s.sqls[0])

    def test_admin_sql(self):
        sql = '\n'.join(SP.admin_sql('POWERHOUSE', 100))
        self.assertIn('CREATE RESOURCE MONITOR IF NOT EXISTS V3_POWERHOUSE_MONITOR WITH CREDIT_QUOTA = 100', sql)
        self.assertIn('ON 80 PERCENT DO NOTIFY', sql)
        self.assertIn('ON 100 PERCENT DO SUSPEND', sql)
        self.assertIn('ALTER WAREHOUSE POWERHOUSE SET RESOURCE_MONITOR = V3_POWERHOUSE_MONITOR', sql)
        self.assertNotIn('OR REPLACE', sql)
        with self.assertRaises(ValueError):
            SP.admin_sql('X; DROP', 100)
        with self.assertRaises(ValueError):
            SP.admin_sql('POWERHOUSE', -5)

    def test_console_shows_it(self):
        self.assertIn('v3_spend', (ROOT / 'v3_console.py').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
