import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from coverage_v3 import summarize, reason_families


class CoverageTests(unittest.TestCase):
    def test_duplicate_reasons_count_once_per_sheet_but_families_overlap(self):
        reason = "Occurrence adapter for 'table' is not implemented; Date-grain occurrences require a date-aware adapter; Date-grain occurrences require a date-aware adapter"
        result = summarize([{'file': 'unseen.twb', 'sheets': [
            {'state': 'UNSUPPORTED', 'reason': reason},
            {'state': 'UNSUPPORTED', 'reason': reason}]}])
        self.assertEqual(result['sheet_occurrences'], 2)
        self.assertEqual({r['family']: r['sheet_occurrences'] for r in result['families']},
                         {'renderer_contract': 2, 'date_grain': 2})
        self.assertTrue(all(r['only_reported_family'] == 0 for r in result['families']))

    def test_unknown_clauses_and_intake_failure_are_never_lost(self):
        self.assertEqual(reason_families('future construct'), {'unclassified'})
        report = summarize([{'error': 'bad archive'}, {'sheets': [
            {'state': 'PARTIAL', 'reason': "Occurrence adapter missing; future construct"}, {}]}])
        self.assertEqual(report['intake_failures'], 1)
        self.assertEqual(report['blocked_or_partial_records'], 2)
        self.assertEqual(report['states'], {'PARTIAL': 1, 'UNKNOWN': 1})
        self.assertIn('unclassified', [b['family'] for b in report['families']])

    def test_pass_flags_do_not_turn_diagnostics_into_release_evidence(self):
        report = summarize([{'state': 'PASS', 'data_verified': True, 'visual_verified': True,
                             'sheets': [{'state': 'UNVERIFIED'}]}])
        self.assertEqual(report['release_readiness'], 'NOT_ESTABLISHED')
        self.assertTrue(all(v == 'NOT_EVALUATED' for v in report['verification'].values()))
        self.assertEqual(report['families'], [])

    def test_renaming_workbooks_does_not_change_priority_counts(self):
        first = summarize([{'file': 'known.twb', 'sheets': [{'state': 'INVALID', 'reason': 'missing field'}]}])
        second = summarize([{'file': 'never_seen.twb', 'sheets': [{'state': 'INVALID', 'reason': 'missing field'}]}])
        self.assertEqual(first['families'][0]['family'], second['families'][0]['family'])
        self.assertEqual(first['families'][0]['sheet_occurrences'], second['families'][0]['sheet_occurrences'])


if __name__ == '__main__': unittest.main()
