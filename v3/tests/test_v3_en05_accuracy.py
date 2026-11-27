"""EN05 / AI08: Cortex accuracy per release on a fixed, seeded set. Written before the code.
  * Cause seeds: one built-in case per cause-proof hypothesis (lod_key_grain, display_rounding, filter_vs_fixed_order,
    null_handling, date_grain, aggregation_type) -- the values, formulas and code facts of a difference whose cause is
    known. Cortex ranks hypotheses through the production choose(); a hit = the seeded cause ranked first (top-1) or
    in the first three (top-3). Each case is asked twice: repeatable = same top answer both times (EN38).
  * Translation seeds: Tableau formulas with a reference Snowflake expression and a small inline data set. Cortex
    translates through the production CF05 propose(); agreement = code runs both on the same rows in ONE read-only
    query over VALUES (never a table, never model SQL alone).
  * The result is one row per run (release, model, rates, calls, tokens, credits), stored in an owner-created table;
    the gate compares it with the previous release: a drop of more than 10 points in any rate is REGRESSION.
  * Workbook-free: seeds are generic constructs, no workbook or sheet names."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import v3_accuracy as AC  # noqa: E402
import v3_prove  # noqa: E402


class Seeds(unittest.TestCase):
    def test_one_cause_seed_per_hypothesis(self):
        self.assertEqual(sorted(s['seeded'] for s in AC.CAUSE_SEEDS), sorted(v3_prove.MENU))
        for s in AC.CAUSE_SEEDS:
            self.assertTrue(s['detail'] and any(not d['Match'] for d in s['detail']), s['id'])

    def test_translation_seeds_are_valid_reference_expressions(self):
        import v3_cortex_calc as CC
        for t in AC.TRANSLATION_SEEDS:
            ok, why = CC.validate_expression(t['reference'], AC.COLUMNS)
            self.assertTrue(ok, (t['id'], why))
        self.assertGreaterEqual(len(AC.TRANSLATION_SEEDS), 6)

    def test_values_relation_is_read_only_and_bound_to_the_seed_columns(self):
        from v3_prove import read_only
        rel = AC.values_relation()
        self.assertTrue(read_only('SELECT 1 FROM ' + rel))
        for c in AC.COLUMNS:
            self.assertIn(c, rel)


class Scoring(unittest.TestCase):
    def test_cause_scoring_top1_top3_and_repeatability(self):
        answers = {}

        def chooser(case, attempt):
            # right first for half the cases, second for the rest; the last case changes its answer on retry
            other = next(h for h in v3_prove.MENU if h != case['seeded'])
            first = case['seeded'] if case['id'] in ('lod', 'round', 'filter') else other
            ranked = [first, case['seeded'] if first != case['seeded'] else other]
            if case['id'] == 'agg' and attempt == 2:
                ranked = list(reversed(ranked))
            return {'ranked': [{'id': h} for h in ranked]}
        out = AC.score_causes(chooser)
        self.assertEqual(out['cases'], 6)
        self.assertAlmostEqual(out['top1'], 3 / 6)
        self.assertAlmostEqual(out['top3'], 1.0)
        self.assertAlmostEqual(out['repeatable'], 5 / 6)

    def test_translation_scoring_uses_one_read_only_query_per_formula(self):
        queries = []

        class Row(dict):
            def as_dict(self):
                return dict(self)

        class S:
            def sql(self, q, params=None):
                queries.append(q)

                class R:
                    def collect(self):
                        agg = 'ENGINE_VALUE' in q
                        return [Row(ENGINE_VALUE=1.0, CORTEX_VALUE=1.0) if agg else Row(ROWS_COMPARED=4, ROWS_DIFFERENT=0)]
                return R()

        def ask(prompt, schema):
            t = next(t for t in AC.TRANSLATION_SEEDS if f'"calculation": "{t["id"]}"' in prompt)
            if t['id'] == AC.TRANSLATION_SEEDS[0]['id']:
                return {'sql_expression': 'SALES; DROP TABLE X', 'level': t['level'], 'notes': ''}
            return {'sql_expression': t['reference'], 'level': t['level'], 'notes': ''}
        out = AC.score_translations(S(), ask)
        n = len(AC.TRANSLATION_SEEDS)
        self.assertEqual(out['cases'], n)
        self.assertAlmostEqual(out['agree'], (n - 1) / n)              # the injected one is refused, never run
        self.assertEqual(len(queries), n - 1)
        self.assertTrue(all(q.lstrip().upper().startswith('SELECT') and 'VALUES' in q for q in queries))
        self.assertFalse(any('DROP' in q for q in queries))


class Gate(unittest.TestCase):
    def test_regression_blocks(self):
        prev = {'release': 'r37', 'cause_top1': 0.8, 'cause_top3': 1.0, 'translation_agree': 0.9, 'repeatable': 1.0}
        ok = dict(prev, release='r38', cause_top1=0.75)
        bad = dict(prev, release='r38', translation_agree=0.7)
        self.assertEqual(AC.gate(ok, prev)['status'], 'PASS')
        g = AC.gate(bad, prev)
        self.assertEqual(g['status'], 'REGRESSION')
        self.assertIn('translation_agree', g['reasons'][0])
        self.assertEqual(AC.gate(ok, None)['status'], 'FIRST_RUN')

    def test_storage_is_bound_and_setup_never_replaces(self):
        sql = '\n'.join(AC.setup_sql('WBR_OWNER'))
        self.assertIn('CREATE TABLE IF NOT EXISTS WBR_DB.CX_EVAL.CORTEX_ACCURACY', sql)
        self.assertNotIn('OR REPLACE', sql)
        seen = {}

        class S:
            def sql(self, q, params=None):
                seen['q'], seen['p'] = q, params

                class R:
                    def collect(self):
                        return []
                return R()
        self.assertEqual(AC.save(S(), {'release': 'r38', 'model': 'm', 'cause_top1': 0.5}), 'OK')
        self.assertIn('INSERT INTO WBR_DB.CX_EVAL.CORTEX_ACCURACY', seen['q'])
        self.assertNotIn('r38', seen['q'])
        self.assertIn('r38', seen['p'])

    def test_no_workbook_names_and_shown_in_console(self):
        src = (ROOT / 'v3_accuracy.py').read_text(encoding='utf-8')
        for name in ('Superstore', 'ECommerce', 'E-Commerce', 'Regional', 'Hospitality'):
            self.assertNotIn(name, src)
        self.assertIn('v3_accuracy', (ROOT / 'v3_console.py').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
