"""AC02 (E19, milestone M7): interaction proof. Code plans one selection per dashboard filter action (the most frequent
member of the source field); the Tableau capture script selects the same marks on the source sheet
(selectMarksByValueAsync), records every sheet, clears the selection (Reset) and records again; the app replays the
selection through its own action state (dashboard_actions.state_key + predicates) and every sheet is proved. The sheet
plan path applies the active action predicates exactly like the renderer. Written BEFORE the code (tests first)."""
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import v3_actions_proof as AP  # noqa: E402

SALES = ROOT / 'Workbooks' / 'Sales Dashboard Ver 01.twbx'


class _Payload:
    r = None

    @classmethod
    def get(cls):
        if cls.r is None:
            from v3_local import prepare_payload
            cls.r = prepare_payload(SALES.read_bytes(), SALES.name)
        return cls.r


class Plan(unittest.TestCase):
    def test_one_selection_per_ready_filter_action_on_sales(self):
        import engine
        from v3_local import preview_context
        r = _Payload.get()
        dash = r['ir']['dashboards'][0]
        with preview_context(r['ir'], dict(r['bindings'])):
            plan = AP.plan_actions(dash, engine)
            again = AP.plan_actions(dash, engine)
        self.assertEqual(plan, again)
        self.assertEqual(plan['schema'], AP.PLAN_SCHEMA)
        by = {a['action_id']: a for a in plan['actions']}
        self.assertEqual(set(by), {a['id'] for a in dash['actions'] if a['status'] == 'READY'})
        country = next(a for a in plan['actions'] if a['field'] == 'Country')
        self.assertEqual((country['source'], len(country['values'])), ('Ctry_KPI', 1))
        self.assertEqual(country['tableau_field'], 'Country')
        month = next(a for a in plan['actions'] if a['field'] == 'Order Date')
        self.assertTrue(isinstance(month['values'][0], int) and 1 <= month['values'][0] <= 12)
        self.assertEqual(month['tableau_field'], 'MONTH(Order Date)')
        self.assertIn(month['tableau_values'][0], AP.MONTHS)


class SheetPlanAppliesActions(unittest.TestCase):
    def test_sheet_where_includes_the_active_action_like_the_renderer(self):
        import dashboard_actions as DA
        import engine
        import v3_sheet_plan as P
        from v3_local import preview_context
        r = _Payload.get()
        dash = r['ir']['dashboards'][0]
        action = next(a for a in dash['actions'] if a['caption'] == 'Country')
        target = next(s for s in dash['sheets'] if s['name'] != action['source'] and s.get('worksheet_semantics')
                      and s.get('datasource_id') == action['datasource_id'])
        with preview_context(r['ir'], dict(r['bindings'])):
            with engine.field_scope(target):
                before = P.sheet_where(target, engine)
            key = DA.state_key(engine.IR, action)
            engine.st.session_state[key] = ['USA']
            try:
                with engine.field_scope(target):
                    after = P.sheet_where(target, engine)
            finally:
                engine.st.session_state.pop(key, None)
        self.assertNotIn("'USA'", before)
        self.assertIn("'USA'", after)


class Capture(unittest.TestCase):
    def test_load(self):
        good = {'schema': AP.CAPTURE_SCHEMA, 'dashboard': 'SALES',
                'default': {'sheets': {}}, 'reset': {'sheets': {}},
                'actions': [{'action_id': 'a1', 'values': ['USA'], 'capture': {'sheets': {}}}]}
        self.assertEqual(AP.load_capture(json.dumps(good).encode())['dashboard'], 'SALES')
        for bad in (dict(good, schema='x'), dict(good, actions='x'), {k: v for k, v in good.items() if k != 'reset'},
                    dict(good, actions=[{'action_id': 'a1', 'capture': {}}])):
            with self.assertRaises(ValueError):
                AP.load_capture(json.dumps(bad).encode())

    def test_script_selects_records_and_clears(self):
        body = (ROOT / 'tools' / 'tableau_sheet_capture.js').read_text(encoding='utf-8').split('async function captureActions')[1]
        for part in ('selectMarksByValueAsync', 'clearSelectedMarksAsync', AP.CAPTURE_SCHEMA, 'reset'):
            self.assertIn(part, body)


class Prove(unittest.TestCase):
    IR = {'dashboards': [{'name': 'SALES', 'actions': [{'id': 'a1', 'status': 'READY', 'source': 'Ctry_KPI'},
                                                       {'id': 'a2', 'status': 'BLOCKED', 'source': 'X', 'reason': 'r'}],
                          'sheets': [{'name': 'Ctry_KPI', 'worksheet_semantics': {'o': 1}},
                                     {'name': 'Total', 'worksheet_semantics': {'o': 1}}]}]}
    CAP = {'schema': AP.CAPTURE_SCHEMA, 'dashboard': 'SALES',
           'default': {'sheets': {'Ctry_KPI': {}, 'Total': {}}}, 'reset': {'sheets': {'Ctry_KPI': {}, 'Total': {}}},
           'actions': [{'action_id': 'a1', 'values': ['USA'], 'capture': {'sheets': {'Ctry_KPI': {}, 'Total': {}}}}]}

    def test_every_sheet_after_each_action_and_after_reset(self):
        seen = []

        def prover(sheet, entry, selection):
            seen.append((sheet['name'], json.dumps(selection)))
            return {'status': 'Verified'}
        out = AP.prove_actions(self.IR, self.CAP, prover)
        self.assertEqual(out['dashboard'], 'SALES')
        self.assertIn(('Total', json.dumps({'a1': ['USA']})), seen)
        self.assertIn(('Total', json.dumps({})), seen)                                  # reset = no selection
        by = {a['id']: a for a in out['actions']}
        self.assertEqual(by['a1']['verdict'], 'VERIFIED')
        self.assertEqual(by['reset']['verdict'], 'VERIFIED')
        self.assertEqual(by['a2']['verdict'], 'NOT_SUPPORTED_IN_APP')

    def test_a_differing_sheet_or_missing_capture_fails_the_action(self):
        out = AP.prove_actions(self.IR, self.CAP,
                               lambda s, e, sel: {'status': 'Not matched' if sel and s['name'] == 'Total' else 'Verified'})
        self.assertEqual({a['id']: a['verdict'] for a in out['actions']}['a1'], 'DIFFERS')
        cap = dict(self.CAP, actions=[])
        self.assertEqual({a['id']: a['verdict'] for a in AP.prove_actions(self.IR, cap, lambda *a: {'status': 'Verified'})
                          ['actions']}['a1'], 'NOT_CAPTURED')

    def test_failure_is_a_code(self):
        def boom(*a):
            raise RuntimeError('secret')
        out = AP.prove_actions(self.IR, self.CAP, boom)
        self.assertNotIn('secret', json.dumps(out))


class Acceptance(unittest.TestCase):
    def test_ac01_reads_the_actions_by_id_and_fails_blocked_ones(self):
        import v3_acceptance as A
        dash = {'name': 'SALES', 'filters': [], 'sheets': [{'name': 'S', 'worksheet_semantics': {'o': 1}}],
                'actions': [{'id': 'a1', 'status': 'READY'}]}
        run = {'result': {'ir': {'dashboards': [dash]}}, 'exact': {('SALES', 'S'): {'status': 'Verified'}},
               'rendered_checks': {'SALES': {'zones': 3, 'problems': [], 'errors': []}},
               'cx2': {'SALES': {'status': 'OK', 'findings': [], 'evaluation': []}},
               'ac02': {'SALES': {'status': 'OK', 'actions': [{'id': 'a1', 'verdict': 'VERIFIED'},
                                                              {'id': 'reset', 'verdict': 'VERIFIED'}]}}}
        self.assertTrue(A.evaluate(run, 'SALES')['ready_to_sign'])
        run['ac02']['SALES']['actions'][1]['verdict'] = 'DIFFERS'
        self.assertFalse(A.evaluate(run, 'SALES')['ready_to_sign'])                    # reset counts too


if __name__ == '__main__':
    unittest.main()
