"""Console Cortex layer: logged by step, budgeted, schema-parsed, grounded; evidence module honest."""
from pathlib import Path
import json
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import v3_ai as A
import v3_evidence as EV


class _Session:
    def __init__(self, response, fail=False):
        self.response, self.fail, self.calls = response, fail, []

    def sql(self, text, params=None):
        self.calls.append((text, params))
        if self.fail:
            raise RuntimeError('service text that must not leak')
        session = self

        class R:
            def collect(self_inner):
                return [[json.dumps(session.response)]]
        return R()


def _run():
    return {'name': 'Book', 'origin': 'Tableau Cloud', 'states': {'Discovery': 'done'},
            'details': {'Discovery': '1 datasource'}, 'binding_rows': [{'Datasource': 'D', 'Snowflake objects': 'DB.S.T'}],
            'result': {'ir': {'dashboards': [{'name': 'Main', 'sheets': [{'name': 'KPI'}]}],
                              'ai_migration_review': {'title': 'Review', 'reason': 'ok'},
                              'ai_request_trace': [{'request_id': 'r:1', 'status': 'RESPONSE_VALIDATED', 'attempted': True,
                                                    'model_id': 'm', 'elapsed_ms': 5, 'error_code': None},
                                                   {'request_id': 'r:2', 'status': 'REFUSED', 'attempted': True,
                                                    'model_id': 'm', 'elapsed_ms': 3, 'error_code': 'INVALID_RESPONSE'}],
                              'ai_assistance': [{'sheet': 'KPI', 'request_ids': ['r:2']}]},
                       'assessment': {'sheets': [{'dashboard': 'Main', 'sheet': 'KPI', 'state': 'UNVERIFIED'}],
                                      'untranslated_calculations': [], 'issues': []}},
            'reference': {'rows': [{'View': 'Main', 'Sheet': 'KPI', 'Status': 'Match'}],
                          'details': {'Main': [{'Key': 'Books', 'Measure': 'Sales', 'Tableau': '4,430,337.4',
                                                'App': '4430337.36', 'Match': True}]}, 'at': 'now'}}


class CortexLayerTests(unittest.TestCase):
    def test_trace_rows_attribute_parsing_calls_to_purposes(self):
        rows = A.trace_rows(_run()['result']['ir'])
        self.assertEqual([r['Purpose'] for r in rows],
                         ['Workbook migration review', 'Refused-sheet adaptation proposal — KPI'])
        self.assertTrue(all(r['Step'] == 'Parsing' for r in rows))
        self.assertIn('INVALID_RESPONSE', rows[1]['Status'])

    def test_call_is_logged_parameterised_and_schema_parsed(self):
        run = _run()
        session = _Session({'answer': 'Books matched at 4,430,337.4', 'evidence_used': ['tableau_comparison'],
                            'answerable_from_evidence': True})
        out = A.ask(session, run, 'What matched?')
        self.assertEqual(out['ungrounded_numbers'], [])
        text, params = session.calls[0]
        self.assertIn('AI_COMPLETE', text)
        self.assertNotIn('What matched', text)          # question travels as a bind parameter only
        self.assertIn('What matched', params[1])
        self.assertEqual(A.activity(run)[-1]['Step'], 'Cortex AI')
        self.assertEqual(A.activity(run)[-1]['Status'], 'Completed')

    def test_ungrounded_numbers_are_flagged(self):
        run = _run()
        session = _Session({'summary': 'Revenue 9,999,999 matched; Books 4,430,337.4.', 'verified': [],
                            'open_risks': [], 'next_steps': []})
        out = A.executive_summary(session, run)
        self.assertEqual(out['ungrounded_numbers'], ['9,999,999'])

    def test_failures_and_budget_never_leak_service_text(self):
        run = _run()
        with self.assertRaises(A.CortexUnavailable) as ctx:
            A.ask(_Session({}, fail=True), run, 'q')
        self.assertNotIn('service text', str(ctx.exception))
        self.assertEqual(A.log(run)[-1]['Status'], 'Failed')
        run['ai_log'] = [{'attempted': True}] * A.MAX_CONSOLE_CALLS
        session = _Session({'answer': 'x', 'evidence_used': [], 'answerable_from_evidence': True})
        with self.assertRaises(A.CortexUnavailable):
            A.ask(session, run, 'q')
        self.assertEqual(session.calls, [])
        with self.assertRaises(A.CortexUnavailable):
            A.ask(None, _run(), 'q')

    def test_invalid_schema_response_is_rejected(self):
        class Bad(_Session):
            def sql(self, text, params=None):
                class R:
                    def collect(self_inner):
                        return [['not json']]
                return R()
        with self.assertRaises(A.CortexUnavailable):
            A.ask(Bad({}), _run(), 'q')


class DiagnosisEvidenceTests(unittest.TestCase):
    def test_diagnosis_sends_sheet_axes_calcs_and_data_model(self):
        run = _run()
        run['result']['ir']['calcs'] = {'Avg Days': {'sql': 'AVG(MIN(X) OVER (PARTITION BY CUSTOMER_ID))'}}
        sheet = {'name': 'KPI', 'kind': 'dtbar', 'x': {'caption': 'Event Date'}, 'y': {'caption': 'Avg Days'}}
        session = _Session({'likely_causes': ['grain'], 'checks_to_run': ['c'], 'confidence': 'medium'})
        out = A.diagnose(session, run, {'View': 'KPI', 'Sheet': 'KPI'},
                         [{'Key': 'd', 'Tableau': '80', 'App': '81.5', 'Match': False}], sheet)
        self.assertEqual(out['confidence'], 'medium')
        sent = ' '.join(map(str, session.calls[0][1]))
        for token in ('PARTITION BY CUSTOMER_ID', 'Event Date', 'data_model', 'DB.S.T'):
            self.assertIn(token, sent)


class EvidenceTests(unittest.TestCase):
    def test_report_marks_missing_evidence_as_not_run_and_never_claims_acceptance(self):
        run = _run()
        run.pop('reference')
        html = EV.report_html(run, None, 'v', A.activity(run)).decode()
        self.assertIn('Sheet checks were not run', html)
        self.assertIn('Tableau values not yet compared', html)
        self.assertIn('Whole-workbook acceptance', html)
        self.assertNotIn('<script', html)

    def test_report_escapes_workbook_text(self):
        run = _run()
        run['name'] = '<script>alert(1)</script>'
        html = EV.report_html(run, [], 'v', []).decode()
        self.assertNotIn('<script>alert', html)

    def test_merge_statuses_attaches_tableau_and_visual_results(self):
        run = _run()
        run['sheet_checks'] = [{'Dashboard': 'Main', 'Sheet': 'KPI', 'Renders on data': 'Yes',
                                'Tableau values': '—', 'Visual (Cortex)': '—'},
                               {'Dashboard': 'Main', 'Sheet': 'Other', 'Renders on data': 'Yes',
                                'Tableau values': '—', 'Visual (Cortex)': '—'}]
        run['visual'] = {'Main': {'verdict': 'PASS'}}
        rows = EV.merge_statuses(run)
        self.assertEqual(rows[0]['Tableau values'], 'Match')
        self.assertEqual(rows[1]['Tableau values'], 'Not in published view data')
        self.assertEqual(rows[0]['Visual (Cortex)'], 'Consistent')

    def test_visual_compare_logs_vision_calls_and_keeps_images(self):
        run = _run()
        run['bindings'] = {}
        run['result'].update(cloud_server_url='https://x', cloud_site_content_url='s', cloud_workbook_id='w')
        views = [{'view': 'Main', 'id': 'v', 'png': b'TPNG', 'error': None},
                 {'view': 'Gone', 'id': 'g', 'png': None, 'error': 'boom'}]
        vision = {'verdict': 'BUG', 'explanation': 'Bars differ', 'tableau_description': 'a',
                  'app_description': 'b', 'errors': []}
        with patch('tableau_server.pull_all_view_images', return_value=views), \
                patch.object(EV, 'app_dashboard_png', return_value=(b'APNG', [{'sheet': 'KPI', 'rendered': False}])), \
                patch('parity.vision_validate_dashboard', return_value=vision) as v:
            out = EV.visual_compare(run, object(), True)
        self.assertEqual(out['Main']['verdict'], 'BUG')
        self.assertEqual(out['Main']['omitted'], ['KPI'])
        self.assertEqual(v.call_args.args[1], EV.VISION_STAGE)
        self.assertIn('No dashboard', out['Gone']['error'])
        steps = [r for r in A.activity(run) if r['Step'] == 'Validation']
        self.assertEqual(len(steps), 3)


if __name__ == '__main__':
    unittest.main()
