"""EN29: Cortex evaluation runs in Snowflake AI Observability (laptop tool; never in the deployed app). Written first.
The documented path is the TruLens SDK (trulens-core, trulens-connectors-snowflake, trulens-providers-cortex >= 2.1.2):
an External Agent per app version, runs that execute as tasks. The tool sends the EN05 seeded set as one run per
release; scores come from OUR code (the same scorer as EN05), never from an LLM judge. Offline-testable parts:
  * dataset(): one row per seed with input and ground truth (no workbook names, no data rows);
  * metric functions: cause top-1 and translation agreement, deterministic;
  * the app function calls the production paths (v3_prove.choose / CF05 propose) through injected callables;
  * the tool lives in tools/ (not in the deployed bundle) and imports TruLens only when run;
  * owner SQL / privileges printed (CREATE EXTERNAL AGENT, EXECUTE TASK, SNOWFLAKE.CORTEX_USER)."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools')]
import en29_observability as OB  # noqa: E402
import v3_accuracy as AC  # noqa: E402


class Dataset(unittest.TestCase):
    def test_one_row_per_seed(self):
        rows = OB.dataset()
        self.assertEqual(len(rows), len(AC.CAUSE_SEEDS) + len(AC.TRANSLATION_SEEDS))
        self.assertEqual({r['kind'] for r in rows}, {'cause', 'translation'})
        cause = next(r for r in rows if r['kind'] == 'cause')
        self.assertIn(cause['ground_truth'], {s['seeded'] for s in AC.CAUSE_SEEDS})

    def test_metrics_are_deterministic(self):
        self.assertEqual(OB.cause_top1('{"top": "lod_key_grain"}', 'lod_key_grain'), 1.0)
        self.assertEqual(OB.cause_top1('{"top": "date_grain"}', 'lod_key_grain'), 0.0)
        self.assertEqual(OB.cause_top1('not json', 'lod_key_grain'), 0.0)
        self.assertEqual(OB.translation_agree('{"result": "Agree"}', ''), 1.0)
        self.assertEqual(OB.translation_agree('{"result": "REJECTED_BY_CODE"}', ''), 0.0)

    def test_app_uses_the_production_paths(self):
        app = OB.App(chooser=lambda case, attempt: {'ranked': [{'id': case['seeded']}]},
                     translate=lambda t: 'Agree')
        rows = OB.dataset()
        c = next(r for r in rows if r['kind'] == 'cause')
        t = next(r for r in rows if r['kind'] == 'translation')
        self.assertEqual(OB.cause_top1(app.answer(c['input']), c['ground_truth']), 1.0)
        self.assertEqual(OB.translation_agree(app.answer(t['input']), t['ground_truth']), 1.0)

    def test_not_in_the_bundle_and_trulens_is_lazy(self):
        yml = (ROOT / 'snowflake.yml').read_text(encoding='utf-8')
        self.assertNotIn('en29_observability', yml)
        src = (ROOT / 'tools' / 'en29_observability.py').read_text(encoding='utf-8')
        self.assertNotIn('\nimport trulens', src)
        self.assertNotIn('\nfrom trulens', src)

    def test_owner_setup_names_the_privileges(self):
        text = '\n'.join(OB.owner_setup('WBR_OWNER'))
        for p in ('CREATE EXTERNAL AGENT', 'EXECUTE TASK', 'SNOWFLAKE.CORTEX_USER', 'trulens-connectors-snowflake'):
            self.assertIn(p, text)


if __name__ == '__main__':
    unittest.main()
