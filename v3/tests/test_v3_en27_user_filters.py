"""EN27: Tableau user filters become Snowflake row access policies (with MA01). Written before the code.
  * Detection: every calculation whose formula uses USERNAME(), ISMEMBEROF(), FULLNAME(), USERDOMAIN(),
    ISUSERNAME() or ISFULLNAME() is a user filter candidate (reported with the fields it reads).
  * Translation (one exact form): [Field] = USERNAME() / USERNAME() = [Field] / ISUSERNAME([Field]) -> a policy
    that keeps a row when the viewer's CURRENT_USER() is entitled to that value in WBR_DB.V3_PORTAL.ENTITLEMENTS
    (USER_NAME, ENTITLEMENT, VALUE), maintained by the owner. Tableau user names and groups do not exist in Snowflake,
    and a Streamlit-in-Snowflake app runs with the owner's role, so the policy keys on CURRENT_USER(), never on roles.
  * ISMEMBEROF / FULLNAME / other forms are reported as 'needs an entitlement design' (named, never guessed).
  * SQL is generated for the owner (CREATE ROW ACCESS POLICY IF NOT EXISTS; ALTER TABLE ... ADD ROW ACCESS POLICY);
    nothing is applied by the app. Not yet proven on a corpus workbook: none uses user functions."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import v3_user_filters as UF  # noqa: E402

IR = {'fields': {
    'f1': {'caption': 'My Region', 'is_calculation': True, 'formula': '[Region Manager] = USERNAME()',
           'datasource_id': 'ds1'},
    'f2': {'caption': 'Exec', 'is_calculation': True, 'formula': 'ISMEMBEROF("Executives") OR [Region] = "East"',
           'datasource_id': 'ds1'},
    'f3': {'caption': 'Profit Ratio', 'is_calculation': True, 'formula': 'SUM([Profit])/SUM([Sales])',
           'datasource_id': 'ds1'},
    'f4': {'caption': 'Mine', 'is_calculation': True, 'formula': 'ISUSERNAME([Owner])', 'datasource_id': 'ds2'},
    'c1': {'caption': 'Region Manager', 'remote_name': 'REGION_MANAGER', 'datasource_id': 'ds1'},
    'c2': {'caption': 'Owner', 'remote_name': 'OWNER_LOGIN', 'datasource_id': 'ds2'}},
    'datasources': {'ds1': {'caption': 'Orders'}, 'ds2': {'caption': 'Accounts'}}}
BINDINGS = {'Orders': 'DB.S.ORDERS', 'Accounts': 'DB.S.ACCOUNTS'}


class Detect(unittest.TestCase):
    def test_candidates_and_forms(self):
        c = {r['calculation']: r for r in UF.detect(IR)}
        self.assertEqual(set(c), {'My Region', 'Exec', 'Mine'})
        self.assertEqual(c['My Region']['form'], 'field_equals_user')
        self.assertEqual(c['My Region']['field'], 'Region Manager')
        self.assertEqual(c['Mine']['form'], 'field_equals_user')
        self.assertEqual(c['Exec']['form'], 'needs an entitlement design')
        self.assertIn('ISMEMBEROF', c['Exec']['functions'])


class Policies(unittest.TestCase):
    def test_policy_sql_for_the_exact_form(self):
        rows = UF.policies(IR, BINDINGS)
        ok = {r['calculation']: r for r in rows if r['state'] == 'READY'}
        self.assertEqual(set(ok), {'My Region', 'Mine'})
        sql = '\n'.join(ok['My Region']['sql'])
        self.assertIn('CREATE ROW ACCESS POLICY IF NOT EXISTS WBR_DB.V3_PORTAL.RAP_', sql)
        self.assertIn('CURRENT_USER()', sql)
        self.assertNotIn('CURRENT_ROLE', sql)
        self.assertIn("E.ENTITLEMENT = 'ORDERS.REGION_MANAGER'", sql)
        self.assertIn('ALTER TABLE DB.S.ORDERS ADD ROW ACCESS POLICY', sql)
        self.assertIn('ON (REGION_MANAGER)', sql)
        self.assertNotIn('OR REPLACE', sql)
        nd = next(r for r in rows if r['calculation'] == 'Exec')
        self.assertEqual(nd['state'], 'NEEDS_DESIGN')
        self.assertEqual(nd.get('sql'), None)

    def test_unbound_or_unknown_column_is_named(self):
        rows = UF.policies(IR, {'Orders': None})
        r = next(r for r in rows if r['calculation'] == 'My Region')
        self.assertEqual(r['state'], 'NOT_BOUND')

    def test_entitlement_table_setup(self):
        sql = '\n'.join(UF.setup_sql('WBR_OWNER'))
        self.assertIn('CREATE TABLE IF NOT EXISTS WBR_DB.V3_PORTAL.ENTITLEMENTS', sql)
        self.assertNotIn('OR REPLACE', sql)

    def test_console_shows_it(self):
        self.assertIn('v3_user_filters', (ROOT / 'v3_console.py').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
