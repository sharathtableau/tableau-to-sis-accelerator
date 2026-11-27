"""CF01 Cortex contribution scorecard. Written before the code.
For each place Cortex proposes something, the scorecard counts what Cortex proposed, what passed code's check, what
code rejected and what still waits for evidence (Tableau values, a referee). It is read from the run's own records
(never from model text) by a closed table of rules, so its numbers match the stored results. The Cortex AI page and
the migration report show it; it never changes a verdict."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import v3_scorecard as SC  # noqa: E402

RUN = {
    'calc_xl': {'rows': [{'Result': 'Agree'}, {'Result': 'Agree'}, {'Result': 'Disagree'},
                         {'Result': 'Rejected by allowlist'}, {'Result': 'Cortex declined'},
                         {'Result': 'Not comparable'}]},
    'cf05': {'rows': [{'state': 'AGREES_WITH_ENGINE'}, {'state': 'VERIFIED'}, {'state': 'REJECTED_BY_CODE'},
                      {'state': 'DISAGREES_WITH_ENGINE'}, {'state': 'PROPOSED'}, {'state': 'NOT_ATTEMPTED'}]},
    'cx3': {'rows': [{'state': 'VERIFIED'}, {'state': 'NOT_MATCHED'}, {'state': 'NO_TABLEAU_REFERENCE'},
                     {'state': 'CORTEX_DECLINED'}]},
    'proofs': {'S1': {'results': [{'Cortex rank': 1, 'Result': 'Proven'}, {'Cortex rank': 2, 'Result': 'Not proven'}]},
               'S2': {'results': [{'Cortex rank': 1, 'Result': 'Not proven'},
                                  {'Cortex rank': 'Not ranked', 'Result': 'Proven'}]},
               'S3': {'results': [{'Cortex rank': 'Not ranked', 'Result': 'Not applicable'}]}},
    'cx4': {'S4': {'results': [{'Cortex rank': 1, 'Result': 'Proven'}]}, 'S5': {'error': 'CX4_FAILED:X'}},
    'cortex_models': {'A': {'verified': True, 'rejected': [{'Item': 'metric x'}]}, 'B': {'verified': False}},
    'cortex_mappings': {'A': {'ok': True}, 'B': {'ok': False}, 'C': {'ok': False, 'error': True}},
    'cx5': {'rows': [{'brief_status': 'OK'}, {'brief_status': 'REJECTED_BY_CODE'}, {'brief_status': None}]},
    'cx8': [{'rows': [{'id': 'R1'}, {'id': 'R2'}], 'dropped': {'quote not in the document': 3}}],
    'ai_log': [{'Step': 'Parsing', 'Status': 'Completed'}, {'Step': 'Parsing', 'Status': 'Completed'},
               {'Step': 'Validation', 'Status': 'Failed'}, {'Step': 'Report', 'Status': 'Not run'}],
}


def by(rows):
    return {r['Decision']: r for r in rows}


class Scorecard(unittest.TestCase):
    def test_every_decision_counted_by_the_rules(self):
        s = by(SC.scorecard(RUN))
        self.assertEqual(s['Calculation cross-check'], dict(s['Calculation cross-check'], **{
            'Cortex proposed': 4, 'Passed code check': 2, 'Rejected by code': 2, 'Awaiting evidence': 0}))
        cf = s['First calculation translation (CF05)']
        self.assertEqual((cf['Cortex proposed'], cf['Passed code check'], cf['Rejected by code'],
                          cf['Awaiting evidence']), (5, 2, 2, 1))
        cx3 = s['Hard calculation plans (CX3)']
        self.assertEqual((cx3['Cortex proposed'], cx3['Passed code check'], cx3['Rejected by code'],
                          cx3['Awaiting evidence']), (3, 1, 1, 1))
        pr = s['Cause proof (top-ranked cause)']
        self.assertEqual((pr['Cortex proposed'], pr['Passed code check'], pr['Rejected by code']), (2, 1, 1))
        self.assertEqual(s['Diagnosis agent (CX4)']['Passed code check'], 1)
        self.assertEqual(s['Diagnosis agent (CX4)']['Cortex proposed'], 1)
        sm = s['Semantic model design']
        self.assertEqual((sm['Cortex proposed'], sm['Passed code check'], sm['Rejected by code']), (2, 1, 1))
        mp = s['Table mapping']
        self.assertEqual((mp['Cortex proposed'], mp['Passed code check'], mp['Rejected by code']), (2, 1, 1))
        rp = s['Repair briefs (CX5)']
        self.assertEqual((rp['Cortex proposed'], rp['Passed code check'], rp['Rejected by code']), (2, 1, 1))
        dc = s['Document requirements (CX8)']
        self.assertEqual((dc['Cortex proposed'], dc['Passed code check'], dc['Rejected by code']), (5, 2, 3))

    def test_stage_and_rate(self):
        s = by(SC.scorecard(RUN))
        self.assertEqual(s['Calculation cross-check']['Stage'], 'Parsing')
        self.assertEqual(s['Cause proof (top-ranked cause)']['Stage'], 'Validation')
        self.assertEqual(s['Calculation cross-check']['Pass rate'], '50%')
        self.assertEqual(s['Hard calculation plans (CX3)']['Pass rate'], '50%')      # 1 of 2 decided

    def test_absent_records_give_no_rows_and_totals_add_up(self):
        self.assertEqual(SC.scorecard({}), [])
        rows = SC.scorecard(RUN)
        tot = SC.totals(rows)
        self.assertEqual(tot['Cortex proposed'], sum(r['Cortex proposed'] for r in rows))
        self.assertEqual(tot['Passed code check'], sum(r['Passed code check'] for r in rows))

    def test_calls_per_stage_from_the_call_log(self):
        c = SC.calls_by_stage(RUN)
        self.assertEqual(c, {'Parsing': 2, 'Validation': 1})       # 'Not run' calls are not calls

    def test_shown_on_the_cortex_page_and_in_the_report(self):
        import inspect
        import v3_evidence
        self.assertIn('v3_scorecard', inspect.getsource(v3_evidence.report_html))
        self.assertIn('v3_scorecard', (ROOT / 'v3_console.py').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
