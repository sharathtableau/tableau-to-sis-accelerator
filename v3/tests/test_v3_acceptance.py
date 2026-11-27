"""AC01 (E08, milestone M7): one acceptance verdict per dashboard. Code combines the evidence -- numbers in the default
state, numbers across filter states (SL06), rendered geometry (rendered_check), the CX2 visual review (code verdicts),
interactions (AC02) -- and the owner signs off last. ACCEPTED only when every check passes or does not apply; a missing
check blocks; a sign-off goes stale when the evidence changes. Written BEFORE the code (tests first)."""
import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import v3_acceptance as A  # noqa: E402

DASH = {'name': 'SALES', 'filters': [{'caption': 'Country', 'kind': 'categorical'}],
        'actions': [{'id': 'a1', 'status': 'READY'}],
        'sheets': [{'name': 'KPI', 'worksheet_semantics': {'occurrences': [{}]}},
                   {'name': 'Map', 'worksheet_semantics': {'occurrences': [{}]}}]}


def run_all_green():
    return {'result': {'ir': {'dashboards': [copy.deepcopy(DASH)]}},
            'exact': {('SALES', 'KPI'): {'status': 'Verified'}, ('SALES', 'Map'): {'status': 'Verified'}},
            'sl06': {'result': {'status': 'OK', 'dashboard': 'SALES', 'rows': [
                {'sheet': 'KPI', 'verdict': 'VERIFIED_ALL_STATES'}, {'sheet': 'Map', 'verdict': 'VERIFIED_ALL_STATES'}]}},
            'rendered_checks': {'SALES': {'zones': 56, 'problems': [], 'errors': []}},
            'cx2': {'SALES': {'status': 'OK', 'findings': [{'verdict': 'XML_BACKED'}, {'verdict': 'ADVISORY'}],
                              'evaluation': [{'owner_label': 'rejected'}, {'owner_label': None}]}},
            'ac02': {'SALES': {'status': 'OK', 'actions': [{'id': 'a1', 'verdict': 'VERIFIED'},
                                                           {'id': 'reset', 'verdict': 'VERIFIED'}]}}}


def by(res):
    return {c['check']: c for c in res['checks']}


class Verdict(unittest.TestCase):
    def test_everything_green_is_ready_to_sign_but_not_accepted_until_signed(self):
        res = A.evaluate(run_all_green(), 'SALES')
        self.assertEqual([c['check'] for c in res['checks']], list(A.CHECKS))
        self.assertTrue(res['ready_to_sign'])
        self.assertEqual(res['verdict'], 'NOT_ACCEPTED')
        self.assertEqual(by(res)['sign_off']['status'], 'PENDING')

    def test_sign_then_accepted_and_stale_when_evidence_changes(self):
        run = run_all_green()
        seen = []
        self.assertTrue(A.sign(run, 'SALES', lambda action, subject, detail: seen.append((action, subject, detail)) or 'ap1'))
        self.assertEqual(seen[0][0], 'ACCEPT_DASHBOARD')
        self.assertEqual(seen[0][2]['verdict_checks']['numbers_default'], 'PASS')
        self.assertEqual(A.evaluate(run, 'SALES')['verdict'], 'ACCEPTED')
        run['exact'][('SALES', 'Map')] = {'status': 'Not matched'}
        res = A.evaluate(run, 'SALES')
        self.assertEqual(res['verdict'], 'NOT_ACCEPTED')
        self.assertEqual(by(res)['sign_off']['status'], 'STALE')

    def test_sign_is_refused_unless_ready_or_store_fails(self):
        run = run_all_green()
        del run['ac02']
        self.assertFalse(A.sign(run, 'SALES', lambda *a: 'ap1'))
        self.assertFalse(A.sign(run_all_green(), 'SALES', lambda *a: None))            # approval not stored

    def test_missing_evidence_blocks_and_is_named(self):
        run = run_all_green()
        for key in ('exact', 'sl06', 'rendered_checks', 'cx2', 'ac02'):
            run.pop(key)
        res = A.evaluate(run, 'SALES')
        for check in ('numbers_default', 'numbers_filter_states', 'visual_geometry', 'visual_review', 'interactions'):
            self.assertEqual(by(res)[check]['status'], 'MISSING', check)
            self.assertTrue(by(res)[check]['detail'])
        self.assertFalse(res['ready_to_sign'])

    def test_each_check_fails_on_its_own_evidence(self):
        cases = [
            (lambda r: r['exact'].__setitem__(('SALES', 'Map'), {'status': 'Partly verified'}), 'numbers_default', 'FAIL'),
            (lambda r: r['exact'].pop(('SALES', 'Map')), 'numbers_default', 'MISSING'),
            (lambda r: r['sl06']['result']['rows'][1].__setitem__('verdict', 'DIFFERS_IN_A_STATE'), 'numbers_filter_states', 'FAIL'),
            (lambda r: r['sl06']['result']['rows'].pop(), 'numbers_filter_states', 'MISSING'),
            (lambda r: r['sl06']['result'].__setitem__('dashboard', 'Other'), 'numbers_filter_states', 'MISSING'),
            (lambda r: r['rendered_checks']['SALES']['problems'].append({'zone': 'v3cz56'}), 'visual_geometry', 'FAIL'),
            (lambda r: r['rendered_checks']['SALES'].__setitem__('zones', 0), 'visual_geometry', 'FAIL'),
            (lambda r: r['cx2']['SALES']['evaluation'][0].__setitem__('owner_label', 'confirmed'), 'visual_review', 'FAIL'),
            (lambda r: r['cx2']['SALES']['evaluation'][0].__setitem__('owner_label', None), 'visual_review', 'OPEN'),
            (lambda r: r['ac02']['SALES']['actions'][0].__setitem__('verdict', 'DIFFERS'), 'interactions', 'FAIL'),
        ]
        for change, check, status in cases:
            run = run_all_green()
            change(run)
            res = A.evaluate(run, 'SALES')
            self.assertEqual(by(res)[check]['status'], status, (check, status, by(res)[check]))
            self.assertFalse(res['ready_to_sign'])

    def test_not_applicable_without_filters_or_actions(self):
        run = run_all_green()
        d = run['result']['ir']['dashboards'][0]
        d['filters'], d['actions'] = [], []
        run.pop('sl06'), run.pop('ac02')
        res = A.evaluate(run, 'SALES')
        self.assertEqual((by(res)['numbers_filter_states']['status'], by(res)['interactions']['status']),
                         ('NOT_APPLICABLE', 'NOT_APPLICABLE'))
        self.assertTrue(res['ready_to_sign'])

    def test_unknown_dashboard(self):
        self.assertEqual(A.evaluate(run_all_green(), 'Nope')['verdict'], 'UNKNOWN_DASHBOARD')


class RenderedCheckFile(unittest.TestCase):
    def test_load(self):
        data = json.dumps([{'dashboard': 'SALES', 'zones': 56, 'problems': [], 'errors': []}]).encode()
        self.assertEqual(A.load_rendered_check(data)['SALES']['zones'], 56)
        for bad in (b'{}', b'[{"dashboard": "S"}]', b'not json'):
            with self.assertRaises(ValueError):
                A.load_rendered_check(bad)


class Report(unittest.TestCase):
    def test_report_section_escaped(self):
        run = run_all_green()
        run['result']['ir']['dashboards'][0]['name'] = '<b>SALES</b>'
        for k in ('exact',):
            run[k] = {('<b>SALES</b>', 'KPI'): {'status': 'Verified'}}
        html = A.report_section(run)
        self.assertIn('Acceptance', html)
        self.assertNotIn('<b>SALES</b>', html)
        self.assertIn('NOT_ACCEPTED', html)

    def test_wired_into_report_console_and_sl06(self):
        self.assertIn('v3_acceptance', (ROOT / 'v3_evidence.py').read_text(encoding='utf-8').split('def report_html')[1])
        self.assertIn('v3_acceptance', (ROOT / 'v3_console.py').read_text(encoding='utf-8'))
        src = (ROOT / 'v3_filter_states.py').read_text(encoding='utf-8')
        self.assertIn("'dashboard': dash.get('name')", src)


if __name__ == '__main__':
    unittest.main()
