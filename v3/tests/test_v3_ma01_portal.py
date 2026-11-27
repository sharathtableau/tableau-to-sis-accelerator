"""MA01: multi-dashboard portal apps. Written before the code. Owner defaults (21 Nov, "don't stop"; changeable):
1:1 first (a portal composes approved packages only), access by viewer USER NAME (st.user; owner's-rights apps cannot
read the viewer's roles), '*' = everyone, data separation by EN27 policies, only the owner deploys a portal.
  * portal_bundle(): one package with every dashboard's own validated IR + bindings (validate_bindings, require_valid_ir
    -- the same checks as a single app), an Analytics Home and one page per dashboard; app.py is code, never typed.
  * pages_for(user, access): the dashboards a viewer may open ('*' rows for everyone); hiding is navigation, NOT
    security (policies are).
  * deploy_portal(): needs a named approver, a valid portal name (V3_PORTAL_<NAME>), at least one package; uses the
    same deploy path as a single app.
  * Access table SQL for the owner (never OR REPLACE)."""
from pathlib import Path
import ast
import io
import json
import sys
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import v3_portal as PO  # noqa: E402


def package(i):
    return {'id': f'wb{i}', 'title': f'Workbook {i}', 'ir': {'dashboards': [{'name': f'D{i}'}]},
            'bindings': {'Orders': 'DB.S.ORDERS'}, 'build': f'{i}' * 64, 'approval': f'appr-{i}'}


class Access(unittest.TestCase):
    def test_pages_for_a_viewer(self):
        access = [('wb1', 'ALICE'), ('wb2', '*'), ('wb3', 'BOB')]
        self.assertEqual(PO.pages_for('alice', access, ['wb1', 'wb2', 'wb3']), ['wb1', 'wb2'])
        self.assertEqual(PO.pages_for('carol', access, ['wb1', 'wb2', 'wb3']), ['wb2'])
        self.assertEqual(PO.pages_for(None, access, ['wb1', 'wb2', 'wb3']), ['wb2'])

    def test_access_sql(self):
        sql = '\n'.join(PO.setup_sql('WBR_OWNER'))
        self.assertIn('CREATE TABLE IF NOT EXISTS WBR_DB.V3_PORTAL.ACCESS', sql)
        self.assertNotIn('OR REPLACE', sql)


class Bundle(unittest.TestCase):
    def test_every_package_is_validated_and_app_is_code(self):
        seen = []
        data, build = PO.portal_bundle('SALES', [package(1), package(2)],
                                       validate=lambda ir, b: seen.append(b) or b)
        self.assertEqual(len(seen), 2)
        files = zipfile.ZipFile(io.BytesIO(data)).namelist()
        self.assertIn('app.py', files)
        self.assertIn('v3_portal.py', files)
        self.assertIn('v3_runtime.py', files)
        src = zipfile.ZipFile(io.BytesIO(data)).read('app.py').decode()
        ast.parse(src)
        self.assertIn('run_portal(', src)
        call = next(n.value for n in ast.parse(src).body if isinstance(n, ast.Assign) and n.targets[0].id == 'PAYLOAD')
        payload = json.loads(call.args[0].value)
        self.assertEqual([d['id'] for d in payload['dashboards']], ['wb1', 'wb2'])
        self.assertEqual(payload['dashboards'][0]['build'], '1' * 64)

    def test_refusals(self):
        with self.assertRaises(ValueError):
            PO.portal_bundle('SALES', [], validate=lambda ir, b: b)
        with self.assertRaises(ValueError):
            PO.portal_bundle("SALES'; DROP", [package(1)], validate=lambda ir, b: b)
        with self.assertRaises(ValueError):
            PO.portal_bundle('SALES', [package(1), package(1)], validate=lambda ir, b: b)      # duplicate id

    def test_deploy_needs_a_person(self):
        with self.assertRaises(ValueError):
            PO.deploy_portal(object(), 'SALES', [package(1)], approved_by='')
        self.assertEqual(PO.portal_identifier('Sales Portal'), 'V3_PORTAL_SALES_PORTAL')

    def test_console_offers_it(self):
        self.assertIn('v3_portal', (ROOT / 'v3_console.py').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
