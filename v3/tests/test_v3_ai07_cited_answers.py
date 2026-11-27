"""AI07: the operator assistant answers with citations code can check. Written before the code.
  * The answer cites evidence keys (top-level or dotted paths, e.g. sheet_assessment or semantic_views.Orders).
  * Code resolves every citation against the run's evidence: unknown citations are listed, and an answer that claims
    to be answerable but cites nothing that exists is labelled UNCITED.
  * The evidence now also carries the Cortex scorecard, exact-plan results, applied fixes, verified queries and the
    CF03 evaluation, so the assistant can answer about them (no credentials, no source rows).
  * Advisory: nothing here changes a verdict."""
from pathlib import Path
import json
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import v3_ai as A  # noqa: E402

RUN = {'name': 'WB', 'states': {'Parsing': 'done'},
       'result': {'ir': {'dashboards': [{'name': 'D', 'sheets': [{'name': 'S'}]}]},
                  'assessment': {'sheets': [{'dashboard': 'D', 'sheet': 'S', 'state': 'PARTIAL', 'reason': 'r'}]}},
       'semantic': {'Orders': {'name': 'V', 'verified': True, 'checks': []}},
       'applied_fixes': {'S': {'Sheet': 'S', 'Fix': 'FIXED values counted once per LOD key'}},
       'cf03': {'rows': [{'Result': 'Correct'}], 'sql_correctness': 1.0}}


class Citations(unittest.TestCase):
    def test_resolve(self):
        ev = A.evidence(RUN)
        ok, unknown = A.check_citations(['sheet_assessment', 'semantic_views.Orders', 'nope', 'semantic_views.X'], ev)
        self.assertEqual(ok, ['sheet_assessment', 'semantic_views.Orders'])
        self.assertEqual(unknown, ['nope', 'semantic_views.X'])

    def test_evidence_has_the_cortex_results(self):
        ev = A.evidence(RUN)
        for k in ('cortex_scorecard', 'applied_fixes', 'ask_the_data_evaluation', 'exact_plans', 'verified_queries'):
            self.assertIn(k, ev)
        self.assertEqual(ev['ask_the_data_evaluation']['sql_correctness'], 1.0)

    def test_ask_labels_uncited_answers(self):
        class S:
            def __init__(self, cited):
                self.cited = cited

            def sql(self, q, params=None):
                body = json.dumps({'choices': [{'messages': json.dumps(
                    {'answer': 'S is partial.', 'evidence_used': self.cited, 'answerable_from_evidence': True})}]})

                class R:
                    def collect(self):
                        return [(body,)]
                return R()
        good = A.ask(S(['sheet_assessment']), dict(RUN, ai_log=[]), 'Is S supported?')
        self.assertEqual(good['citation_status'], 'CITED')
        bad = A.ask(S(['made_up_key']), dict(RUN, ai_log=[]), 'Is S supported?')
        self.assertEqual(bad['citation_status'], 'UNCITED')
        self.assertEqual(bad['unknown_citations'], ['made_up_key'])

    def test_console_shows_the_status(self):
        self.assertIn('citation_status', (ROOT / 'v3_console.py').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
