"""CF07 / RL1 / RL3: Cortex proposes sheet-plan edits for sheets the exact loop could not match. Written before code.
  * Cortex sees the plan (fields, aggregations, date parts, addressing), the comparison with Tableau (what differs)
    and Tableau's column headers, and proposes at most 3 candidates, each a list of EDITS from a closed menu:
    set_agg, set_date_part, set_addressing, set_mark. It never writes a plan or SQL.
  * Code applies the edits to a copy of the plan; an unknown op, field or value rejects the whole candidate.
  * Each candidate is proven by the exact loop exactly like the authored plan: only equality with Tableau's values
    makes it Verified; a person applies it with the existing approval. A changed mark is labelled Approximated.
  * Not-matched sheets only; at most max_sheets per click; Cortex failures are status codes."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import v3_plan_proposals as PP  # noqa: E402
import v3_sheet_plan as P  # noqa: E402

PLAN = {'schema': P.SCHEMA, 'mark': 'bar', 'sort': [], 'aggregate': True, 'source': 'authored', 'fields': [
    {'id': 'd', 'caption': 'Order Date', 'channel': 'cols', 'kind': 'dim', 'discrete': True, 'date_part': 'year'},
    {'id': 's', 'caption': 'Sales', 'channel': 'rows', 'kind': 'measure', 'agg': 'sum', 'discrete': False,
     'date_part': 'none', 'table_calc': {'type': 'CumTotal', 'addressing': 'table'}}]}


class Edits(unittest.TestCase):
    def test_closed_edits_apply_to_a_copy(self):
        new, problems = PP.apply_edits(PLAN, [{'op': 'set_agg', 'field_id': 's', 'value': 'avg'},
                                              {'op': 'set_date_part', 'field_id': 'd', 'value': 'quarter'},
                                              {'op': 'set_addressing', 'field_id': 's', 'value': 'rows'}], 1)
        self.assertEqual(problems, [])
        self.assertEqual((new['fields'][1]['agg'], new['fields'][0]['date_part'],
                          new['fields'][1]['table_calc']['addressing']), ('avg', 'quarter', 'rows'))
        self.assertEqual(new['source'], 'cortex:1')
        self.assertEqual(PLAN['fields'][1]['agg'], 'sum')                       # original untouched

    def test_anything_outside_the_menu_rejects_the_candidate(self):
        for bad in ({'op': 'set_sql', 'field_id': 's', 'value': 'DROP'},
                    {'op': 'set_agg', 'field_id': 'zz', 'value': 'avg'},
                    {'op': 'set_agg', 'field_id': 's', 'value': 'median_of_everything'},
                    {'op': 'set_date_part', 'field_id': 's', 'value': 'year'},           # not a dimension
                    {'op': 'set_addressing', 'field_id': 'd', 'value': 'rows'},          # no table calc
                    {'op': 'set_mark', 'value': 'hexbin'}):
            new, problems = PP.apply_edits(PLAN, [bad], 1)
            self.assertIsNone(new, bad)
            self.assertTrue(problems)

    def test_mark_change_is_labelled_approximated(self):
        new, _p = PP.apply_edits(PLAN, [{'op': 'set_mark', 'value': 'line'}], 2)
        self.assertEqual(new['mark'], 'line')
        self.assertTrue(new.get('approximated'))


class Propose(unittest.TestCase):
    def test_candidates_from_cortex_are_checked(self):
        seen = {}

        def ask(prompt, schema):
            seen['p'] = prompt
            return {'candidates': [
                {'edits': [{'op': 'set_agg', 'field_id': 's', 'value': 'avg'}], 'reason': 'avg'},
                {'edits': [{'op': 'drop_table', 'field_id': 's', 'value': 'x'}], 'reason': 'bad'},
                {'edits': [{'op': 'set_addressing', 'field_id': 's', 'value': 'rows'}], 'reason': 'addr'},
                {'edits': [{'op': 'set_mark', 'value': 'line'}], 'reason': 'extra beyond 3'}]}
        out = PP.propose(PLAN, {'status': 'Not matched', 'reasons': ['3 values differ'], 'matched': 2,
                                'different': 3}, ['YEAR(Order Date)', 'SUM(Sales)'], ask)
        self.assertEqual([c['source'] for c in out['candidates']], ['cortex:1', 'cortex:3'])
        self.assertEqual(out['rejected'], 1)
        self.assertIn('3 values differ', seen['p'])
        self.assertIn('SUM(Sales)', seen['p'])
        self.assertNotIn('SELECT', seen['p'])

    def test_cortex_failure_is_a_code(self):
        def ask(prompt, schema):
            raise RuntimeError('service said something long')
        out = PP.propose(PLAN, {'status': 'Not matched'}, [], ask)
        self.assertEqual(out['candidates'], [])
        self.assertEqual(out['status'], 'CORTEX_FAILED:RuntimeError')

    def test_only_not_matched_sheets_are_sent(self):
        run = {'exact': {('D', 'A'): {'status': 'Verified', 'plan': PLAN}, ('D', 'B'): {'status': 'Not matched',
                                                                                         'plan': PLAN},
                         ('D', 'C'): {'status': 'Not planned', 'plan': None}}}
        self.assertEqual(PP.targets(run, 10), [('D', 'B')])

    def test_wired_into_the_console(self):
        self.assertIn('v3_plan_proposals', (ROOT / 'v3_console.py').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
