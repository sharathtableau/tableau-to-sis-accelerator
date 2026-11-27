"""SL06 (milestone M7): validation across filter states. Code picks a small deterministic set of quick-filter states
per dashboard; the Tableau capture script applies each state with the Embedding API and records every sheet; the app
draws the same state through the engine's own dashboard-filter clause; v3_exact_loop.prove compares each sheet in each
state. Nothing is Verified across states unless every state matches. Written BEFORE the code (tests first)."""
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import v3_filter_states as F  # noqa: E402

SS = ROOT / 'Workbooks' / 'Superstore_Tableau2024_3.twbx'


def _payload():
    from v3_local import prepare_payload
    return prepare_payload(SS.read_bytes(), SS.name)


class Plan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = _payload()
        cls.ir = cls.r['ir']

    def _plan(self, dash_name):
        import engine
        from v3_local import preview_context
        dash = next(d for d in self.ir['dashboards'] if d['name'] == dash_name)
        with preview_context(self.ir, dict(self.r['bindings'])):
            return dash, F.plan_states(dash, engine)

    def test_states_are_deterministic_categorical_and_bounded(self):
        dash, plan = self._plan('Order Details')
        self.assertEqual(plan['schema'], F.PLAN_SCHEMA)
        self.assertEqual(plan['dashboard'], 'Order Details')
        states = plan['states']
        self.assertTrue(0 < len(states) <= F.MAX_FILTERS * F.VALUES_PER_FILTER)
        cats = {f['caption'] for f in dash['filters'] if f['kind'] == 'categorical'}
        self.assertTrue(all(s['field'] in cats and len(s['values']) == 1 for s in states))
        self.assertEqual(len({s['id'] for s in states}), len(states))
        self.assertEqual(plan, self._plan('Order Details')[1])                     # same input, same plan
        region = [s['values'][0] for s in states if s['field'] == 'Region']
        self.assertTrue(region and all(v in ('Central', 'East', 'South', 'West') for v in region))

    def test_dashboard_without_quick_filters_has_no_states(self):
        _dash, plan = self._plan('Commission Model')
        self.assertEqual(plan['states'], [])

    def test_state_part_is_the_engine_dashboard_clause(self):
        import engine
        from v3_local import preview_context
        dash = next(d for d in self.ir['dashboards'] if d['name'] == 'Order Details')
        with preview_context(self.ir, dict(self.r['bindings'])):
            part = F.state_part(dash, {'field': 'Region', 'values': ["O'Hare"]}, engine)
        self.assertEqual(part['caption'], 'Region')
        self.assertTrue(part['governs'])
        self.assertIn("= 'O''Hare'", part['clause'])                                   # quote escaped
        with preview_context(self.ir, dict(self.r['bindings'])):
            self.assertIsNone(F.state_part(dash, {'field': 'Not A Filter', 'values': ['x']}, engine))


class Capture(unittest.TestCase):
    def _bundle(self, **over):
        sheet = {'columns': ['Region', 'SUM(Sales)'], 'rows': [['East', '1']], 'total_rows': 1}
        b = {'schema': F.CAPTURE_SCHEMA, 'dashboard': 'Order Details', 'view': '/t/x/views/W/V',
             'default': {'schema': 'tableau-sheet-capture/1', 'dashboard': 'Order Details', 'sheets': {'S': sheet}},
             'states': [{'id': 's1', 'field': 'Region', 'values': ['East'],
                         'capture': {'schema': 'tableau-sheet-capture/1', 'dashboard': 'Order Details',
                                     'sheets': {'S': sheet}}}]}
        b.update(over)
        return json.dumps(b).encode()

    def test_load_validates_the_bundle(self):
        got = F.load_capture(self._bundle())
        self.assertEqual([s['id'] for s in got['states']], ['s1'])
        for bad in (b'{}', self._bundle(schema='x'), self._bundle(states='x'),
                    self._bundle(states=[{'id': 's1', 'field': 'Region', 'values': 'East', 'capture': {}}])):
            with self.assertRaises(ValueError):
                F.load_capture(bad)

    def test_capture_script_applies_restores_and_writes_the_schema(self):
        js = (ROOT / 'tools' / 'tableau_sheet_capture.js').read_text(encoding='utf-8')
        body = js.split('async function captureStates')[1]
        for part in ('applyFilterAsync', 'getFiltersAsync', 'clearFilterAsync', F.CAPTURE_SCHEMA, 'replace'):
            self.assertIn(part, body)


class Prove(unittest.TestCase):
    IR = {'dashboards': [{'name': 'Order Details', 'filters': [{'caption': 'Region', 'kind': 'categorical'}],
                          'sheets': [{'name': 'S', 'worksheet_semantics': {'occurrences': [{}]}},
                                     {'name': 'T', 'worksheet_semantics': {'occurrences': [{}]}}]}]}
    CAP = {'schema': F.CAPTURE_SCHEMA, 'dashboard': 'Order Details',
           'default': {'sheets': {'S': {'rows': []}, 'T': {'rows': []}}},
           'states': [{'id': 's1', 'field': 'Region', 'values': ['East'], 'capture': {'sheets': {'S': {'rows': [1]}, 'T': {'rows': [1]}}}},
                      {'id': 's2', 'field': 'Region', 'values': ['West'], 'capture': {'sheets': {'S': {'rows': [2]}}}}]}

    def test_every_sheet_in_every_state_with_the_state_clause(self):
        seen = []

        def prover(sheet, entry, parts):
            seen.append((sheet['name'], parts[0]['clause'] if parts else None))
            ok = not (sheet['name'] == 'T' and parts)
            return {'status': 'Verified' if ok else 'Not matched'}

        part_for = lambda dash, state: {'clause': f"REGION = '{state['values'][0]}'", 'governs': True}
        out = F.prove_states(self.IR, self.CAP, prover, part_for)
        self.assertIn(('S', None), seen)                                                # default state proved too
        self.assertIn(('S', "REGION = 'West'"), seen)
        by = {r['sheet']: r for r in out['rows']}
        self.assertEqual(by['S']['verdict'], 'VERIFIED_ALL_STATES')
        self.assertEqual((by['S']['states_checked'], by['S']['states_verified']), (3, 3))
        self.assertEqual(by['T']['verdict'], 'DIFFERS_IN_A_STATE')
        self.assertEqual(by['T']['cells']['s2'], 'NOT_CAPTURED')                        # absent, never assumed equal
        self.assertEqual(out['summary'], {'sheets': 2, 'verified_all_states': 1, 'states': 2})

    def test_unknown_filter_state_is_reported_not_skipped(self):
        cap = dict(self.CAP, states=[dict(self.CAP['states'][0], field='Ship Mode')])
        out = F.prove_states(self.IR, cap, lambda s, e, p: {'status': 'Verified'}, lambda d, s: None)
        self.assertEqual({r['cells']['s1'] for r in out['rows']}, {'FILTER_NOT_IN_APP'})
        self.assertTrue(all(r['verdict'] != 'VERIFIED_ALL_STATES' for r in out['rows']))

    def test_prover_failure_is_a_code(self):
        def boom(s, e, p):
            raise RuntimeError('secret service text')
        out = F.prove_states(self.IR, self.CAP, boom, lambda d, s: {'clause': 'x'})
        self.assertNotIn('secret', json.dumps(out))
        self.assertTrue(all(v.startswith('PROOF_FAILED:') for r in out['rows'] for v in r['cells'].values()
                            if v != 'NOT_CAPTURED'))


class Console(unittest.TestCase):
    def test_validation_tab_has_the_block(self):
        src = (ROOT / 'v3_console.py').read_text(encoding='utf-8')
        self.assertIn('v3_filter_states', src)
        self.assertIn('F.render(', src)


if __name__ == '__main__':
    unittest.main()
