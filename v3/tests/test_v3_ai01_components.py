"""AI01: every Cortex review finding links to real components. Written before the code.
The model picks a priority from the closed list; CODE attaches the components that priority is about (untranslated
calculations by name, datasources with relationships / joins, blocked sheets, authored actions), so a finding always
cites components that exist and the model never names an id. The support verdicts are not changed."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import migration_review as MR  # noqa: E402

IR = {'dashboards': [{'name': 'D', 'actions': [{'id': 'a1', 'name': 'Filter 1', 'status': 'READY'}],
                      'sheets': [{'name': 'S1', 'support': {'status': 'UNSUPPORTED'}, 'datasource_id': 'ds'},
                                 {'name': 'S2', 'support': {'status': 'SUPPORTED'}, 'datasource_id': 'ds'}]}],
      'field_scopes': {'ds': {'caption': 'Orders', 'calc_drops': {'Calc A': 'reason'}}}}


class Components(unittest.TestCase):
    def test_components_per_priority(self):
        c = MR.components(IR)
        self.assertEqual(c['repair_calculations'], ['calculation: Calc A (Orders)'])
        self.assertEqual(c['repair_visual_contracts'], ['sheet: D / S1'])
        self.assertEqual(c['verify_interactions'], ['action: D / Filter 1'])
        self.assertEqual(c['compare_results'], ['sheet: D / S1', 'sheet: D / S2'])

    def test_reviewed_finding_carries_them_and_verdicts_stay(self):
        import copy
        before = copy.deepcopy(IR)
        orig = MR.request
        MR.request = lambda *a, **k: {'choice': 'repair_calculations', 'reason': 'one calculation is untranslated'}
        try:
            out = MR.review(IR, object(), 'm', None)
        finally:
            MR.request = orig
        self.assertEqual(out['status'], 'REVIEWED')
        self.assertEqual(out['components'], ['calculation: Calc A (Orders)'])
        self.assertEqual(IR, before)


if __name__ == '__main__':
    unittest.main()
