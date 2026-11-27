"""Cortex token usage and cost recorded per call, from Snowflake's own show_details response."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import v3_ai
import v3_usage as U


class _Row(tuple):
    pass


class _Session:
    def __init__(self, value):
        self.value, self.sql_log = value, []

    def sql(self, text, params=None):
        self.sql_log.append(text)
        value = self.value

        class R:
            def collect(self_inner):
                return [_Row((value,))]
        return R()


DETAILED_JSON = json.dumps({'created': 1, 'model': 'claude-sonnet-4-5',
                            'structured_output': [{'raw_message': {'items': []}, 'type': 'json'}],
                            'usage': {'completion_tokens': 3500, 'prompt_tokens': 4000, 'total_tokens': 7500}})
DETAILED_TEXT = json.dumps({'choices': [{'messages': 'A short answer.'}], 'model': 'claude-sonnet-4-5',
                            'usage': {'completion_tokens': 300, 'prompt_tokens': 1000, 'total_tokens': 1300}})


class UnwrapTests(unittest.TestCase):
    def test_structured_and_text_and_legacy(self):
        self.assertEqual(U.unwrap(DETAILED_JSON), ({'items': []}, {'input': 4000, 'output': 3500}))
        self.assertEqual(U.unwrap(DETAILED_TEXT), ('A short answer.', {'input': 1000, 'output': 300}))
        self.assertEqual(U.unwrap('{"choice": "a"}'), ('{"choice": "a"}', None))      # no details: unchanged
        self.assertEqual(U.unwrap('plain text'), ('plain text', None))

    def test_cost_from_rates(self):
        self.assertAlmostEqual(U.credits('claude-sonnet-4-5', 4000, 3500), 0.0387)
        self.assertIsNone(U.credits('unknown-model', 10, 10))
        c, usd = U.analyst_cost(1)
        self.assertGreater(c, 0)
        self.assertGreater(usd, 0)


class CallTests(unittest.TestCase):
    def test_schema_call_records_reported_tokens_and_cost(self):
        run, session = {}, _Session(DETAILED_JSON)
        out = v3_ai.call(session, run, 'Data model', 'Semantic View descriptions and synonyms', 'p',
                         schema={'type': 'object'}, model='claude-sonnet-4-5', max_tokens=4000)
        self.assertEqual(out, {'items': []})
        self.assertIn('show_details => TRUE', session.sql_log[0])
        e = run['ai_log'][-1]
        self.assertEqual((e['Input tokens'], e['Output tokens']), (4000, 3500))
        self.assertEqual(e['Token source'], 'Reported by Snowflake')
        self.assertAlmostEqual(e['Cost USD'], round(0.0387 * U.credit_usd(), 4))

    def test_text_call_and_legacy_response(self):
        run = {}
        self.assertEqual(v3_ai.call(_Session(DETAILED_TEXT), run, 'Report', 'Executive summary', 'p'), 'A short answer.')
        self.assertEqual(run['ai_log'][-1]['Input tokens'], 1000)
        legacy = v3_ai.call(_Session('"Plain"'), run, 'Report', 'Executive summary', 'p')
        self.assertEqual(legacy, 'Plain')
        self.assertNotIn('Input tokens', run['ai_log'][-1])

    def test_intake_trace_and_vision_estimates_and_totals(self):
        run = {'result': {'ir': {'ai_migration_review': {'status': 'REVIEWED'},
                                 'ai_request_trace': [{'request_id': 'r1', 'model_id': 'claude-sonnet-4-5', 'attempted': True,
                                                       'status': 'RESPONSE_VALIDATED', 'elapsed_ms': 9,
                                                       'usage': {'input': 1000, 'output': 300}}]}}}
        png = b'\x89PNG\r\n\x1a\n' + b'\x00\x00\x00\rIHDR' + (1500).to_bytes(4, 'big') + (900).to_bytes(4, 'big') + b'\x00' * 8
        v3_ai.note_vision(run, 'Dashboard', {'tokens': 3000, 'errors': [], 'tableau_description': 'a',
                                             'app_description': 'b'}, 'claude-opus-5', images=(png, png))
        rows = v3_ai.activity(run)
        self.assertEqual(rows[0]['Input tokens'], 1000)
        vision = [r for r in rows if r['Step'] == 'Validation']
        self.assertEqual(len(vision), 3)
        self.assertTrue(all(r['Token source'].startswith('Estimated') for r in vision))
        self.assertEqual(vision[0]['Input tokens'], 1500 * 900 // 750 + 500)
        t = U.totals(rows)
        self.assertEqual(t['calls'], 4)
        self.assertEqual(t['estimated_calls'], 3)
        self.assertGreater(t['usd'], 0)


if __name__ == '__main__':
    unittest.main()
