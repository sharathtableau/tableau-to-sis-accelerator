"""Cortex-designed Semantic View (lab): typed proposal only, code validates, separate name, same proof."""
from pathlib import Path
import json
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import v3_cortex_model as CM
import v3_semantic as SV
from test_v3_semantic import plan, IR, E, C, P, _Session as SemSession


def good():
    return {'fact_table': E,
            'tables': [{'table_id': E, 'primary_key': 'EVENT_ID', 'description': 'Events', 'synonyms': []},
                       {'table_id': C, 'primary_key': 'CUSTOMER_ID', 'description': 'Customers', 'synonyms': ['clients']},
                       {'table_id': P, 'primary_key': 'PRODUCT_ID', 'description': 'Products', 'synonyms': []}],
            'relationships': [{'from_table': E, 'from_column': 'CUSTOMER_ID', 'to_table': C, 'to_column': 'CUSTOMER_ID'},
                              {'from_table': E, 'from_column': 'PRODUCT_ID', 'to_table': P, 'to_column': 'PRODUCT_ID'}],
            'columns': [{'table_id': E, 'column': 'REVENUE', 'description': 'Net revenue of the event',
                         'synonyms': ['sales', 'income']}],
            'metrics': [{'name': 'Total Revenue', 'table_id': E, 'aggregation': 'SUM', 'column': 'REVENUE',
                         'description': 'Sum of revenue', 'synonyms': ['revenue']},
                        {'name': 'Customers', 'table_id': E, 'aggregation': 'COUNT_DISTINCT', 'column': 'CUSTOMER_ID',
                         'description': 'Distinct customers', 'synonyms': []},
                        {'name': 'Bad', 'table_id': C, 'aggregation': 'SUM', 'column': 'SEGMENT',
                         'description': 'x', 'synonyms': []},
                        {'name': 'Ghost', 'table_id': E, 'aggregation': 'SUM', 'column': 'NOPE',
                         'description': 'x', 'synonyms': []}],
            'design_notes': 'Events is the fact table.'}


class _Both(SemSession):
    """AI_COMPLETE calls (with params) return the proposal; everything else behaves like the SV double."""
    def __init__(self, proposal, **kw):
        super().__init__(**kw)
        self.proposal = proposal

    def sql(self, text, params=None):
        if params is not None and 'AI_COMPLETE' in text:
            proposal = self.proposal

            class R:
                def collect(_self):
                    return [[json.dumps(proposal)]]
            return R()
        return super().sql(text)


class CompileTests(unittest.TestCase):
    def test_valid_design_compiles_to_separate_view_and_rejects_bad_items(self):
        sem, rejected, cmp = CM.compile_model(plan(), good(), IR, 'ECommerce')
        self.assertTrue(sem['name'].endswith('_CORTEX'))
        self.assertNotEqual(sem['name'], SV.model(plan(), IR, 'ECommerce')['name'])
        self.assertEqual(len(sem['relationships']), 2)
        self.assertEqual(cmp['same'], 2)
        self.assertTrue(cmp['fact_matches_tableau'])
        exprs = {m['name']: m['expr'] for m in sem['metrics']}
        self.assertEqual(exprs['TOTAL_REVENUE'], 'SUM(events.REVENUE)')
        self.assertTrue(exprs['CUSTOMERS'].startswith('COUNT(DISTINCT'))
        self.assertEqual({r['Item'] for r in rejected}, {'metric BAD', 'metric Ghost'})
        ddl = SV.ddl(sem)
        self.assertIn('designed by Cortex', ddl)
        self.assertNotIn('NOPE', ddl)

    def test_wrong_relationship_is_rejected_or_reported(self):
        p = good()
        p['relationships'][1] = {'from_table': E, 'from_column': 'QUANTITY', 'to_table': P, 'to_column': 'BASE_PRICE'}
        sem, rejected, cmp = CM.compile_model(plan(), p, IR, 'ECommerce')
        self.assertEqual(cmp['same'], 1)
        self.assertTrue(cmp['missing'])

    def test_cortex_never_sees_the_keys(self):
        ev = json.dumps(CM.evidence(plan(), IR))
        self.assertNotIn('"keys"', ev)
        self.assertNotIn('"left"', ev)


class BindingTests(unittest.TestCase):
    def test_cortex_view_attachable_only_for_same_plan(self):
        rec = CM.design(_Both(good()), {}, plan(), IR, 'ECommerce')
        bound = SV.attach(plan(), rec)
        self.assertTrue(bound['semantic_view'].endswith('_CORTEX'))
        self.assertIn('SEMANTIC_VIEW(', SV.bound_relation(bound))
        with self.assertRaises(ValueError):
            SV.bound_relation(dict(bound, semantic_view=bound['semantic_view'] + 'X'))


class AutoRunTests(unittest.TestCase):
    def test_run_step_attaches_only_verified_design(self):
        import v3_console as VC
        from unittest.mock import patch
        for drift, expect in ((False, True), (True, False)):
            run = {'result': {'ir': IR}, 'bindings': {'ECommerce': plan()}, 'semantic': {},
                   'binding_rows': [{'Datasource': 'ECommerce'}]}
            with patch.object(VC, '_runtime_sf', return_value=True), \
                 patch.object(VC.backend, '_active_session', return_value=_Both(good(), drift=drift)), \
                 patch.object(VC, '_relationship_plans', return_value={'ECommerce': plan()}), \
                 patch.object(SV, 'cortex_enricher', return_value=None):
                VC._auto_semantic(run)              # release 20a: the Cortex variant aligns with the shared view
                ok, text = VC._auto_cortex_model(run)
            self.assertEqual(ok, expect)
            self.assertEqual(str(run['bindings']['ECommerce'].get('semantic_view', '')).endswith('_CORTEX'), expect)


class DesignTests(unittest.TestCase):
    def test_design_creates_and_verifies_with_same_proof(self):
        run = {}
        rec = CM.design(_Both(good()), run, plan(), IR, 'ECommerce')
        self.assertTrue(rec['verified'])
        self.assertTrue(rec['name'].endswith('_CORTEX'))
        self.assertTrue(rec['checks'])
        self.assertEqual(run['ai_log'][-1]['Status'], 'Completed')

    def test_design_not_verified_when_profiles_differ(self):
        rec = CM.design(_Both(good(), drift=True), {}, plan(), IR, 'ECommerce')
        self.assertFalse(rec['verified'])


if __name__ == '__main__':
    unittest.main()
