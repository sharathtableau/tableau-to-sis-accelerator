"""EN32: Cortex model lifecycle. Written before the code.
A model can be retired, renamed or not offered in the account's region. Then:
  * a call that fails at Snowflake (not a malformed answer) tries the next model of its chain once: the chosen model,
    then the reasoning model (for light jobs), then the owner's V3_CORTEX_FALLBACK_MODELS (comma separated, in order);
    each try is logged (CORTEX_MODEL_FALLBACK) and counts against the run budget;
  * a model that failed at Snowflake in this run is skipped for the rest of the run (no repeated failing calls);
  * a malformed / cut-off answer is NOT a model problem: no fallback (callers keep their own single retry);
  * without a fallback list a reasoning-model failure is reported as before;
  * the Cortex AI page names the models that were unavailable and the model used instead."""
from pathlib import Path
import json
import os
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import v3_ai as A  # noqa: E402

SCHEMA = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}}


class Session:
    def __init__(self, fail_models=(), bad_answer=False):
        self.calls, self.fail_models, self.bad = [], set(fail_models), bad_answer

    def sql(self, text, params=None):
        model = params[0]
        self.calls.append(model)
        if model in self.fail_models:
            raise RuntimeError('model unavailable')
        body = 'not json' if self.bad else json.dumps({'choices': [{'messages': '{"ok": true}'}],
                                                       'usage': {'prompt_tokens': 10, 'completion_tokens': 2}})

        class Q:
            def collect(self):
                return [(body,)]
        return Q()


class Env:
    def __init__(self, **kw):
        self.kw = kw

    def __enter__(self):
        self.old = {k: os.environ.get(k) for k in self.kw}
        for k, v in self.kw.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def __exit__(self, *a):
        for k, v in self.old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


class Lifecycle(unittest.TestCase):
    def test_reasoning_model_falls_back_to_the_owner_list(self):
        s, run = Session(fail_models={'old-model'}), {}
        with Env(V3_CORTEX_MODEL='old-model', V3_CORTEX_FALLBACK_MODELS='new-model, other'):
            out = A.call(s, run, 'Parsing', 'CF05 first translation', 'p', SCHEMA)
        self.assertEqual(out, {'ok': True})
        self.assertEqual(s.calls, ['old-model', 'new-model'])
        self.assertIn('CORTEX_MODEL_FALLBACK', A.log(run)[0]['Note'])
        self.assertEqual(A.log(run)[1]['Model'], 'new-model')

    def test_light_chain_is_light_then_reasoning_then_list(self):
        s, run = Session(fail_models={'light-x', 'strong-y'}), {}
        with Env(V3_CORTEX_MODEL_LIGHT='light-x', V3_CORTEX_MODEL='strong-y', V3_CORTEX_FALLBACK_MODELS='spare'):
            A.call(s, run, 'Report', 'Executive summary', 'p', SCHEMA)
        self.assertEqual(s.calls, ['light-x', 'strong-y', 'spare'])

    def test_a_failed_model_is_skipped_for_the_rest_of_the_run(self):
        s, run = Session(fail_models={'old-model'}), {}
        with Env(V3_CORTEX_MODEL='old-model', V3_CORTEX_FALLBACK_MODELS='new-model'):
            A.call(s, run, 'Parsing', 'CF05 first translation', 'p', SCHEMA)
            A.call(s, run, 'Parsing', 'CF05 first translation', 'q', SCHEMA)
        self.assertEqual(s.calls, ['old-model', 'new-model', 'new-model'])
        self.assertEqual(A.unavailable_models(run), ['old-model'])

    def test_a_malformed_answer_is_not_a_model_problem(self):
        s, run = Session(bad_answer=True), {}
        with Env(V3_CORTEX_MODEL='m1', V3_CORTEX_FALLBACK_MODELS='m2'):
            with self.assertRaises(A.CortexUnavailable):
                A.call(s, run, 'Parsing', 'CF05 first translation', 'p', SCHEMA)
        self.assertEqual(s.calls, ['m1'])
        self.assertEqual(A.unavailable_models(run), [])

    def test_no_list_means_no_retry(self):
        s, run = Session(fail_models={'m1'}), {}
        with Env(V3_CORTEX_MODEL='m1', V3_CORTEX_FALLBACK_MODELS=None):
            with self.assertRaises(A.CortexUnavailable):
                A.call(s, run, 'Parsing', 'CF05 first translation', 'p', SCHEMA)
        self.assertEqual(s.calls, ['m1'])

    def test_every_model_failing_reports_once_per_model(self):
        s, run = Session(fail_models={'m1', 'm2'}), {}
        with Env(V3_CORTEX_MODEL='m1', V3_CORTEX_FALLBACK_MODELS='m2'):
            with self.assertRaises(A.CortexUnavailable):
                A.call(s, run, 'Parsing', 'CF05 first translation', 'p', SCHEMA)
            with self.assertRaises(A.CortexUnavailable):
                A.call(s, run, 'Parsing', 'CF05 first translation', 'p', SCHEMA)
        self.assertEqual(s.calls, ['m1', 'm2'])                 # second call: both known unavailable, no call
        self.assertIn('CORTEX_NO_MODEL_AVAILABLE', A.log(run)[-1]['Note'])

    def test_shown_on_the_cortex_page(self):
        self.assertIn('unavailable_models(', (ROOT / 'v3_console.py').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
