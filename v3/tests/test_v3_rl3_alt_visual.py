"""RL3 / AI05: when a sheet fails to draw, Cortex proposes an alternate visual. Written before the code.
  * Targets: sheets whose render check is Error or Refused (sheet_checks), and that have a workbook plan.
  * Cortex sees the plan's fields (caption, kind, aggregation, channel), the authored mark and the failure CODE, and
    proposes at most 2 alternates from a closed menu: mark in {bar, line, area, point, text} and channel moves for
    fields ALREADY on the plan. It cannot add, drop or re-aggregate a field, so the level of detail and every value
    stay the workbook plan's.
  * Code applies the moves to a copy, validates the plan, and draws it on the sheet's own result (build check). The
    first candidate that draws is offered; if none does (or Cortex is unavailable), code falls back to a table of the
    same result. Either way it is labelled "Alternate visual — approximated drawing, values from the workbook plan".
  * A person applies it (the sheet keeps engine_kind and is marked PARTIAL with the reason); nothing is automatic in
    the deployed app. Status codes only."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import v3_alt_visual as AV  # noqa: E402
import v3_sheet_plan as P  # noqa: E402

PLAN = {'schema': P.SCHEMA, 'mark': 'pie', 'sort': [], 'aggregate': True, 'fields': [
    {'id': 'd', 'caption': 'Region', 'channel': 'color', 'kind': 'dim', 'discrete': True, 'date_part': 'none'},
    {'id': 'm', 'caption': 'Sales', 'channel': 'size', 'kind': 'measure', 'agg': 'sum', 'discrete': False,
     'date_part': 'none'}]}


def draws_unless(*bad_marks):
    return lambda plan, df: plan['mark'] not in bad_marks


class Candidates(unittest.TestCase):
    def test_moves_apply_to_a_copy_and_keep_the_fields(self):
        alt, problems = AV.apply_moves(PLAN, 'bar', [{'field_id': 'd', 'channel': 'cols'},
                                                     {'field_id': 'm', 'channel': 'rows'}], 1)
        self.assertEqual(problems, [])
        self.assertEqual(alt['mark'], 'bar')
        self.assertEqual([(f['id'], f['channel'], f.get('agg')) for f in alt['fields']],
                         [('d', 'cols', None), ('m', 'rows', 'sum')])
        self.assertEqual(PLAN['mark'], 'pie')
        self.assertTrue(alt['approximated'])
        self.assertEqual(alt['source'], 'cortex-alt:1')

    def test_outside_the_menu_is_refused(self):
        for mark, moves in (('hexbin', []), ('bar', [{'field_id': 'zz', 'channel': 'rows'}]),
                            ('bar', [{'field_id': 'm', 'channel': 'sql'}])):
            alt, problems = AV.apply_moves(PLAN, mark, moves, 1)
            self.assertIsNone(alt)
            self.assertTrue(problems)


class Choose(unittest.TestCase):
    def test_first_cortex_candidate_that_draws(self):
        seen = {}

        def ask(prompt, schema):
            seen['p'] = prompt
            return {'candidates': [
                {'mark': 'line', 'moves': [{'field_id': 'd', 'channel': 'cols'}, {'field_id': 'm', 'channel': 'rows'}],
                 'reason': 'trend'},
                {'mark': 'bar', 'moves': [{'field_id': 'd', 'channel': 'cols'}, {'field_id': 'm', 'channel': 'rows'}],
                 'reason': 'parts of a whole as bars'}]}
        out = AV.choose(PLAN, 'RENDER_FAILED:ValueError', ask, df=[{'x': 1}], draws=draws_unless('line'))
        self.assertEqual(out['state'], 'ALTERNATE')
        self.assertEqual(out['plan']['mark'], 'bar')
        self.assertEqual(out['source'], 'cortex-alt:2')
        self.assertIn('RENDER_FAILED:ValueError', seen['p'])
        self.assertIn('approximated', out['label'].lower())

    def test_table_fallback_when_nothing_draws_or_cortex_fails(self):
        out = AV.choose(PLAN, 'X', lambda p, s: {'candidates': [{'mark': 'line', 'moves': [], 'reason': ''}]},
                        df=[{'x': 1}], draws=draws_unless('line', 'bar', 'area', 'point'))
        self.assertEqual((out['state'], out['plan']['mark'], out['source']), ('TABLE_FALLBACK', 'text', 'code'))
        out = AV.choose(PLAN, 'X', lambda p, s: (_ for _ in ()).throw(RuntimeError('detail')), df=[], draws=draws_unless())
        self.assertEqual(out['state'], 'TABLE_FALLBACK')
        self.assertEqual(out['cortex'], 'CORTEX_FAILED:RuntimeError')
        self.assertNotIn('detail', str(out))

    def test_targets_are_failed_sheets(self):
        run = {'sheet_checks': [{'Dashboard': 'D', 'Sheet': 'A', 'Renders on data': 'Yes'},
                                {'Dashboard': 'D', 'Sheet': 'B', 'Renders on data': 'Error', 'Note': 'boom'},
                                {'Dashboard': 'D', 'Sheet': 'C', 'Renders on data': 'Refused'}]}
        self.assertEqual(AV.targets(run), [('D', 'B', 'RENDER_ERROR'), ('D', 'C', 'REFUSED')])


class Apply(unittest.TestCase):
    def test_person_applies_and_the_sheet_says_so(self):
        sheet = {'name': 'B', 'kind': 'pie'}
        alt = dict(PLAN, mark='bar', approximated=True, source='cortex-alt:1')
        with self.assertRaises(ValueError):
            AV.apply(sheet, {'state': 'ALTERNATE', 'plan': alt}, approved_by='')
        AV.apply(sheet, {'state': 'ALTERNATE', 'plan': alt, 'label': AV.LABEL}, approved_by='Owner', refresh=False)
        self.assertEqual(sheet['sheet_plan']['mark'], 'bar')
        self.assertEqual(sheet['engine_kind'], 'pie')
        self.assertEqual(sheet['support']['status'], 'PARTIAL')
        self.assertIn('Alternate visual', sheet['support']['reason'])
        self.assertEqual(sheet['alternate_visual']['approved_by'], 'Owner')

    def test_console_offers_it(self):
        self.assertIn('v3_alt_visual', (ROOT / 'v3_console.py').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
