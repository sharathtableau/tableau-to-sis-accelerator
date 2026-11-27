"""CF04 model per job + EN03 Cortex cost controls. Written before the code.
  * Every console Cortex call (v3_ai.call) belongs to a job tier from a closed table: 'light' jobs (descriptions,
    glossary, summaries, labels, extraction) use the light model; everything else (calculations, causes, repair plans,
    semantic model design, mapping) uses the reasoning model. Unknown purposes are 'reasoning' (never cheaper by guess).
    An explicit model passed by the caller always wins. Both models are set by the owner (V3_CORTEX_MODEL,
    V3_CORTEX_MODEL_LIGHT; 'off' = the reasoning model for every job).
  * A light model the account does not have is not fatal: the call is retried once with the reasoning model and the
    fallback is logged (code CORTEX_MODEL_FALLBACK).
  * A run has a token ceiling and an AI-credit ceiling (V3_CORTEX_MAX_TOKENS_PER_RUN, V3_CORTEX_MAX_CREDITS_PER_RUN).
    Before a call, code estimates it (prompt chars / 4 + max_tokens). A call that would cross a ceiling is not made:
    it is logged 'Not run' with CORTEX_TOKEN_CEILING / CORTEX_COST_CEILING and the caller gets a clear stop message
    that names the setting to raise. No Snowflake call happens.
  * Cost per stage (Parsing, Data model, Validation, Report, ...) in the console and the migration report."""
from pathlib import Path
import json
import os
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import v3_ai as A  # noqa: E402
import v3_usage as U  # noqa: E402


class Rows(list):
    pass


class Session:
    """Records AI_COMPLETE calls; answers with a detailed response (usage reported)."""
    def __init__(self, fail_models=(), usage=(1000, 200)):
        self.calls, self.fail_models, self.usage = [], set(fail_models), usage

    def sql(self, text, params=None):
        model = params[0]
        self.calls.append(model)
        if model in self.fail_models:
            raise RuntimeError('unknown model')
        body = {'choices': [{'messages': '{"ok": true}'}],
                'usage': {'prompt_tokens': self.usage[0], 'completion_tokens': self.usage[1]}}
        outer = self

        class Q:
            def collect(self):
                return [(json.dumps(body),)]
        return Q()


class Env:
    def __init__(self, **kw):
        self.kw = kw

    def __enter__(self):
        self.old = {k: os.environ.get(k) for k in self.kw}
        for k, v in self.kw.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


SCHEMA = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}}


class ModelPerJob(unittest.TestCase):
    def test_tiers_from_the_closed_table(self):
        self.assertEqual(A.job_tier('Data model', 'Semantic View descriptions and synonyms'), 'light')
        self.assertEqual(A.job_tier('Report', 'Executive summary'), 'light')
        self.assertEqual(A.job_tier('Catalogue', 'Metric names and descriptions'), 'light')
        self.assertEqual(A.job_tier('Parsing', 'CF05 first translation'), 'reasoning')
        self.assertEqual(A.job_tier('Validation', 'Cause proof: rank hypotheses — Sheet 1'), 'reasoning')
        self.assertEqual(A.job_tier('Validation', 'CX5 repair plan'), 'reasoning')
        self.assertEqual(A.job_tier('Anything', 'a purpose nobody listed'), 'reasoning')

    def test_model_for_each_tier_and_explicit_wins(self):
        with Env(V3_CORTEX_MODEL_LIGHT='light-x', V3_CORTEX_MODEL='strong-y'):
            self.assertEqual(A.model_for('Report', 'Executive summary'), 'light-x')
            self.assertEqual(A.model_for('Parsing', 'CF05 first translation'), 'strong-y')
            self.assertEqual(A.model_for('Report', 'Executive summary', model='chosen'), 'chosen')
        with Env(V3_CORTEX_MODEL_LIGHT='off', V3_CORTEX_MODEL='strong-y'):
            self.assertEqual(A.model_for('Report', 'Executive summary'), 'strong-y')

    def test_call_uses_the_tier_model_and_logs_it(self):
        s, run = Session(), {}
        with Env(V3_CORTEX_MODEL_LIGHT='light-x', V3_CORTEX_MODEL='strong-y'):
            A.call(s, run, 'Report', 'Executive summary', 'p', SCHEMA)
            A.call(s, run, 'Parsing', 'CF05 first translation', 'p', SCHEMA)
        self.assertEqual(s.calls, ['light-x', 'strong-y'])
        self.assertEqual([e['Model'] for e in A.log(run)], ['light-x', 'strong-y'])
        self.assertEqual([e.get('Tier') for e in A.log(run)], ['light', 'reasoning'])

    def test_missing_light_model_falls_back_once(self):
        s, run = Session(fail_models={'light-x'}), {}
        with Env(V3_CORTEX_MODEL_LIGHT='light-x', V3_CORTEX_MODEL='strong-y'):
            out = A.call(s, run, 'Report', 'Executive summary', 'p', SCHEMA)
        self.assertEqual(out, {'ok': True})
        self.assertEqual(s.calls, ['light-x', 'strong-y'])
        first, second = A.log(run)
        self.assertEqual(first['Status'], 'Failed')
        self.assertIn('CORTEX_MODEL_FALLBACK', first['Note'])
        self.assertEqual((second['Status'], second['Model']), ('Completed', 'strong-y'))

    def test_reasoning_model_failure_is_not_retried(self):
        s, run = Session(fail_models={'strong-y'}), {}
        with Env(V3_CORTEX_MODEL='strong-y'):
            with self.assertRaises(A.CortexUnavailable):
                A.call(s, run, 'Parsing', 'CF05 first translation', 'p', SCHEMA)
        self.assertEqual(s.calls, ['strong-y'])


class Ceilings(unittest.TestCase):
    def test_token_ceiling_stops_before_the_call(self):
        s, run = Session(usage=(5000, 1000)), {}
        with Env(V3_CORTEX_MAX_TOKENS_PER_RUN='8000', V3_CORTEX_MAX_CREDITS_PER_RUN='100'):
            A.call(s, run, 'Parsing', 'CF05 first translation', 'p', SCHEMA, max_tokens=500)    # 6,000 used
            with self.assertRaises(A.CortexUnavailable) as cm:
                A.call(s, run, 'Parsing', 'CF05 first translation', 'x' * 4000, SCHEMA, max_tokens=1500)
        self.assertEqual(len(s.calls), 1)                                   # no Snowflake call for the stopped one
        msg = str(cm.exception)
        self.assertIn('V3_CORTEX_MAX_TOKENS_PER_RUN', msg)
        last = A.log(run)[-1]
        self.assertEqual(last['Status'], 'Not run')
        self.assertIn('CORTEX_TOKEN_CEILING', last['Note'])

    def test_cost_ceiling_stops_before_the_call(self):
        s, run = Session(usage=(100000, 20000)), {}
        model = 'claude-sonnet-4-5'                     # a model with a known rate
        with Env(V3_CORTEX_MAX_TOKENS_PER_RUN='10000000', V3_CORTEX_MAX_CREDITS_PER_RUN='0.3', V3_CORTEX_MODEL=model):
            A.call(s, run, 'Parsing', 'CF05 first translation', 'p', SCHEMA)      # 0.18 + 0.18 = 0.36 credits
            with self.assertRaises(A.CortexUnavailable) as cm:
                A.call(s, run, 'Parsing', 'CF05 first translation', 'p', SCHEMA)
        self.assertEqual(len(s.calls), 1)
        self.assertIn('V3_CORTEX_MAX_CREDITS_PER_RUN', str(cm.exception))
        self.assertIn('CORTEX_COST_CEILING', A.log(run)[-1]['Note'])

    def test_limits_are_reported(self):
        with Env(V3_CORTEX_MAX_TOKENS_PER_RUN='1234', V3_CORTEX_MAX_CREDITS_PER_RUN='0.5'):
            lim = A.limits({})
        self.assertEqual((lim['tokens'], lim['credits']), (1234, 0.5))
        with Env(V3_CORTEX_MAX_TOKENS_PER_RUN='junk', V3_CORTEX_MAX_CREDITS_PER_RUN=None):
            lim = A.limits({})
        self.assertEqual((lim['tokens'], lim['credits']), (A.DEFAULT_MAX_TOKENS, A.DEFAULT_MAX_CREDITS))


class CostPerStage(unittest.TestCase):
    def test_stage_totals(self):
        rows = [{'Step': 'Parsing', 'Model': 'm1', 'Status': 'Completed', 'Input tokens': 100, 'Output tokens': 10,
                 'AI credits': 0.001, 'Cost USD': 0.002},
                {'Step': 'Parsing', 'Model': 'm2', 'Status': 'Completed', 'Input tokens': 50, 'Output tokens': 5,
                 'AI credits': 0.0005, 'Cost USD': 0.001},
                {'Step': 'Report', 'Model': 'm1', 'Status': 'Not run', 'Note': 'CORTEX_TOKEN_CEILING'},
                {'Step': 'Validation', 'Model': 'm1', 'Status': 'Failed'}]
        out = {r['Stage']: r for r in U.stage_totals(rows)}
        self.assertEqual(out['Parsing']['Calls'], 2)
        self.assertEqual((out['Parsing']['Tokens in'], out['Parsing']['Tokens out']), (150, 15))
        self.assertAlmostEqual(out['Parsing']['Cost USD'], 0.003)
        self.assertEqual(out['Parsing']['Models'], 'm1, m2')
        self.assertEqual(out['Report']['Stopped by ceiling'], 1)
        self.assertEqual(out['Validation']['Failed'], 1)

    def test_report_has_cost_per_stage(self):
        import inspect
        import v3_evidence
        self.assertIn('stage_totals', inspect.getsource(v3_evidence.report_html))

    def test_console_has_cost_per_stage(self):
        src = (ROOT / 'v3_console.py').read_text(encoding='utf-8')
        self.assertIn('stage_totals', src)
        self.assertIn('limits(', src)


if __name__ == '__main__':
    unittest.main()
