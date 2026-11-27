"""Per-sheet Tableau references for a whole project. Written BEFORE the tools (tests first).
  * tools/capture_project.py: REST lists the project's workbooks and views; a browser the owner signs into runs
    tools/tableau_sheet_capture.js (Embedding API, every worksheet of every dashboard); one capture bundle per
    workbook (schema tableau-capture-bundle/1, what v3_exact_loop.prove_run reads). Only the pure parts are tested.
  * tools/sheet_census.py: every captured sheet proven against Tableau (v3_exact_loop.prove_run) on the workbook's
    Snowflake tables or bundled data. Proof: the repo's Superstore 2024.3 capture -> >= 19 sheets Verified, as the
    release gate's test_v3_exact."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))


class Capture(unittest.TestCase):
    def test_view_path(self):
        import capture_project as CP
        self.assertEqual(CP.view_path('b360bi', 'Superstore_Tableau2024_3/sheets/Overview'),
                         '/t/b360bi/views/Superstore_Tableau2024_3/Overview')
        self.assertEqual(CP.view_path('', 'Book/sheets/Sheet 1'), '/views/Book/Sheet%201')

    def test_bundle_shape_is_what_the_exact_loop_reads(self):
        import capture_project as CP
        import v3_sheet_capture as SC
        caps = [{'schema': 'tableau-sheet-capture/1', 'method': 'm', 'view': '/t/s/views/W/D', 'dashboard': 'D',
                 'captured_at': 't', 'sheets': {'S': {'columns': ['A'], 'rows': [['1']], 'total_rows': 1}}}]
        bundle = CP.bundle('s', 'W', caps)
        loaded = SC.load(json.dumps(bundle).encode())
        self.assertEqual([c['dashboard'] for c in SC.dashboard_captures(loaded)], ['D'])

    def test_failed_view_is_kept_as_evidence_not_dropped(self):
        import capture_project as CP
        rec = CP.failed('/t/s/views/W/D', 'D', RuntimeError('Tableau view did not become interactive'))
        self.assertEqual((rec['dashboard'], rec['sheets']), ('D', {}))
        self.assertIn('RuntimeError', rec['error'])
        self.assertIn('did not become interactive', rec['error'])

    def test_reason_is_one_short_line_without_urls(self):
        """11 Nov: every view printed only 'FAILED Error' -- the cause must be visible (one line, no URL/query)."""
        import capture_project as CP
        exc = Exception('page.evaluate: TypeError: Failed to fetch https://x.online.tableau.com/a?token=abc\n'
                        '    at stack line')
        why = CP.reason(exc)
        self.assertEqual(why, 'TypeError: Failed to fetch <url>')
        self.assertLessEqual(len(CP.reason(Exception('x' * 500))), 200)


class Census(unittest.TestCase):
    def test_superstore_capture_proves_every_sheet(self):
        import sheet_census as SCN
        import tableau_reference_bundles as B
        folder = Path(tempfile.mkdtemp())
        wb = ROOT / 'Workbooks' / 'Superstore_Tableau2024_3.twbx'
        (folder / wb.name).write_bytes(wb.read_bytes())
        (folder / (wb.stem + '.capture.json')).write_text(B.BUNDLES['Superstore_Tableau2024_3'], encoding='utf-8')
        rows = SCN.census(folder, log=lambda m: None)
        mine = [r for r in rows if r['workbook'] == wb.name]
        self.assertEqual(len(mine), 20)
        self.assertGreaterEqual(sum(r['Result'] == 'Verified' for r in mine), 19)
        md = SCN.report(rows)
        self.assertIn('Sheets: 20', md)
        self.assertIn('Verified', md)

    def test_every_sheet_is_accounted_for(self):
        """A failed view capture, a sheet the capture lacks and a sheet with no plan each get their own row."""
        import sheet_census as SCN
        ir = {'dashboards': [
            {'name': 'D1', 'sheets': [{'name': 'A', 'worksheet_semantics': {'rows': ['x']}}, {'name': 'B', 'worksheet_semantics': {'rows': ['x']}},
                                      {'name': 'C'}]},
            {'name': 'D2', 'sheets': [{'name': 'E', 'worksheet_semantics': {'rows': ['x']}}]}]}
        cap = {'schema': 'tableau-capture-bundle/1', 'captures': {
            'W/D1': {'schema': 'tableau-sheet-capture/1', 'dashboard': 'D1', 'view': 'v', 'method': 'm',
                     'captured_at': 't', 'sheets': {'A': {'columns': ['x'], 'rows': [['1']], 'total_rows': 1}}},
            'W/D2': {'schema': 'tableau-sheet-capture/1', 'dashboard': 'D2', 'view': 'v', 'method': 'm',
                     'captured_at': 't', 'sheets': {}, 'error': 'TimeoutError'}}}
        got = {(r['Dashboard'], r['Sheet']): r['Result']
               for r in SCN.gaps(ir, json.dumps(cap).encode(), {('D1', 'A')})}
        self.assertEqual(got, {('D1', 'B'): 'No Tableau reference', ('D1', 'C'): 'Not planned',
                               ('D2', 'E'): 'Capture failed'})

    def test_no_data_is_not_called_missing_reference(self):
        """11 Nov full run: live workbooks without data showed 27 'No Tableau reference' though Tableau captured them."""
        import sheet_census as SCN
        ir = {'dashboards': [{'name': 'D1', 'sheets': [{'name': 'A', 'worksheet_semantics': {'rows': ['x']}}]}]}
        cap = {'schema': 'tableau-capture-bundle/1', 'captures': {
            'W/D1': {'schema': 'tableau-sheet-capture/1', 'dashboard': 'D1', 'view': 'v', 'method': 'm',
                     'captured_at': 't', 'sheets': {'A': {'columns': ['x'], 'rows': [['1']], 'total_rows': 1}}}}}
        got = SCN.gaps(ir, json.dumps(cap).encode(), set(), no_data='bound to Snowflake: run with --sf')
        self.assertEqual([(r['Result'], r['Note']) for r in got], [('No data', 'bound to Snowflake: run with --sf')])


class ConnectedApp(unittest.TestCase):
    """Unattended capture: a Tableau Connected App (direct trust) JWT instead of a person signing in. Values come
    only from the environment; nothing is printed. Signed HS256 with the secret value, kid = secret id."""
    ENV = {'TABLEAU_CA_CLIENT_ID': 'client-1', 'TABLEAU_CA_SECRET_ID': 'kid-1',
           'TABLEAU_CA_SECRET_VALUE': 'not-a-real-secret', 'TABLEAU_CA_USERNAME': 'owner@example.com'}

    def test_token_claims_and_signature(self):
        import base64, hashlib, hmac
        import capture_project as CP
        tok = CP.connected_app_token(self.ENV, now=1_000_000, jti='j1')
        head, body, sig = tok.split('.')
        dec = lambda t: json.loads(base64.urlsafe_b64decode(t + '=' * (-len(t) % 4)))
        self.assertEqual(dec(head), {'alg': 'HS256', 'typ': 'JWT', 'kid': 'kid-1', 'iss': 'client-1'})
        self.assertEqual(dec(body), {'iss': 'client-1', 'aud': 'tableau', 'sub': 'owner@example.com', 'jti': 'j1',
                                     'exp': 1_000_000 + 300, 'scp': ['tableau:views:embed']})
        want = hmac.new(b'not-a-real-secret', f'{head}.{body}'.encode(), hashlib.sha256).digest()
        self.assertEqual(sig, base64.urlsafe_b64encode(want).rstrip(b'=').decode())

    def test_missing_settings_name_the_variable_never_a_value(self):
        import capture_project as CP
        env = dict(self.ENV)
        del env['TABLEAU_CA_USERNAME']
        with self.assertRaises(ValueError) as cm:
            CP.connected_app_token(env)
        self.assertIn('TABLEAU_CA_USERNAME', str(cm.exception))
        self.assertNotIn('not-a-real-secret', str(cm.exception))


if __name__ == '__main__':
    unittest.main()
