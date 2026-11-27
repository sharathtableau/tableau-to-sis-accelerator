"""Release 20a (SL02, EN40): one shared Semantic View per set of tables and keys; reuse, ownership guard."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import v3_semantic as SV
import v3_sv_shared as SH
from v3_model_binding import replace_bindings
from test_v3_semantic import plan, IR, E, C, P, _Session


def other_workbook():
    """Same tables and keys, different Tableau captions, field names and field selection."""
    p = deepcopy(plan())
    p['datasource_id'] = 'federated.other'
    for oid, cap in ((E, 'Web events'), (C, 'Clients'), (P, 'Catalogue')):
        p['tables'][oid]['caption'] = cap
    p['fields'] = {k: v for k, v in p['fields'].items() if k not in ('base_price', 'segment')}
    p['fields']['Net Revenue (USD)'] = p['fields'].pop('revenue')
    p['fields']['Net Revenue (USD)']['output'] = 'NET_REVENUE_USD_'
    return p


class FingerprintTests(unittest.TestCase):
    def test_same_tables_and_keys_give_one_name_whatever_the_workbook_uses(self):
        self.assertEqual(SH.fingerprint(plan()), SH.fingerprint(other_workbook()))
        self.assertEqual(SH.base_name(plan()), SH.base_name(other_workbook()))
        self.assertRegex(SH.base_name(plan()), r'^WBR_DB\.TABLEAU_ACCELERATOR_V3\.EVENTS_SVLAB_F[0-9A-F]{10}$')

    def test_different_keys_or_tables_give_a_different_name(self):
        p = plan()
        p['keys'][0] = dict(p['keys'][0], left='SEGMENT_ID')
        self.assertNotEqual(SH.fingerprint(p), SH.fingerprint(plan()))
        q = plan()
        q['tables'][P] = dict(q['tables'][P], table='DB.S.PRODUCTS_V2')
        self.assertNotEqual(SH.fingerprint(q), SH.fingerprint(plan()))


class ReuseTests(unittest.TestCase):
    def test_second_workbook_on_the_same_tables_creates_no_view(self):
        session = _Session()
        first = SH.ensure(session, plan(), 'ECommerce')
        self.assertEqual(first['decision']['action'], 'created')
        self.assertTrue(first['verified'])
        creates = [q for q in session.sql_log if q.startswith('CREATE')]
        self.assertEqual(len(creates), 1)
        second = SH.ensure(session, other_workbook(), 'Web analytics')
        self.assertEqual(second['decision']['action'], 'reused')
        self.assertEqual(second['name'], first['name'])
        self.assertTrue(second['verified'])
        self.assertEqual(len([q for q in session.sql_log if q.startswith('CREATE')]), 1)   # still one
        self.assertEqual(second['map']['NET_REVENUE_USD_'], 'events.REVENUE')

    def test_shared_view_has_every_table_column_and_no_workbook_calculations(self):
        rec = SH.ensure(_Session(), other_workbook(), 'Web analytics')
        cols = {(d['table'], d['column']) for d in rec['model']['dimensions']}
        self.assertIn(('products', 'BASE_PRICE'), cols)            # not used by this workbook, still in the view
        self.assertTrue(all(m['source'] == 'generated' for m in rec['model']['metrics']))
        names = [d['name'] for d in rec['model']['dimensions']]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn('CUSTOMERS_CUSTOMER_ID', names)              # same column name in two tables

    def test_unproven_view_is_never_attached(self):
        rec = SH.ensure(_Session(drift=True), plan(), 'ECommerce')
        self.assertFalse(rec['verified'])
        with self.assertRaises(ValueError):
            SH.attach(plan(), rec)

    def test_dashboard_queries_read_through_the_shared_view(self):
        rec = SH.ensure(_Session(), other_workbook(), 'Web analytics')
        bound = SH.attach(other_workbook(), rec)
        rel = SV.bound_relation(bound)
        self.assertIn('SEMANTIC_VIEW(' + rec['name'] + ' DIMENSIONS ', rel)
        self.assertIn('REVENUE AS NET_REVENUE_USD_', rel)
        sql = replace_bindings('SELECT SUM(NET_REVENUE_USD_) FROM M_TABLE', {'x': {'model_plan': bound, 'table': 'M_TABLE'}})
        self.assertIn('FROM (SELECT ', sql)
        self.assertIn('SEMANTIC_VIEW(', sql)


class OwnershipGuardTests(unittest.TestCase):
    def test_view_owned_by_another_role_never_fails_the_run(self):
        base = SH.base_name(plan()).split('.')[-1]
        session = _Session(views={base: ('DS_BASIC', 'Generated')}, drift=False)
        session.drift = False
        # The DS_BASIC view does not reproduce this model (reads through it differ), so it cannot be reused.
        orig = session.sql

        def sql(text, *a, **k):
            if f'SEMANTIC_VIEW(WBR_DB.TABLEAU_ACCELERATOR_V3.{base} ' in text and 'COUNT(*) AS ROWS_' in text:
                raise RuntimeError('SQL access control error: insufficient privileges')
            return orig(text, *a, **k)
        session.sql = sql
        rec = SH.ensure(session, plan(), 'ECommerce')
        self.assertTrue(rec['verified'])
        self.assertTrue(rec['name'].endswith(base + '_R2'))
        blocked = rec['decision']['blocked'][0]
        self.assertEqual(blocked['Owner'], 'DS_BASIC')
        self.assertIn('GRANT OWNERSHIP ON SEMANTIC VIEW', blocked['Grant that clears it'])
        self.assertIn('USE ROLE DS_BASIC', blocked['Grant that clears it'])

    def test_invisible_view_that_cannot_be_replaced_moves_to_the_next_name(self):
        base = SH.base_name(plan()).split('.')[-1]
        rec = SH.ensure(_Session(blocked={base}), plan(), 'ECommerce')
        self.assertTrue(rec['verified'])
        self.assertTrue(rec['name'].endswith('_R2'))
        self.assertEqual(rec['decision']['blocked'][0]['View'], base)

    def test_certified_view_is_never_replaced(self):
        base = SH.base_name(plan()).split('.')[-1]
        session = _Session(views={base: ('WBR_OWNER', 'Shared view [CERTIFIED]')}, drift=True)
        rec = SH.ensure(session, plan(), 'ECommerce')
        self.assertNotIn(f'CREATE OR REPLACE SEMANTIC VIEW WBR_DB.TABLEAU_ACCELERATOR_V3.{base}\n',
                         '\n'.join(q.split('\n')[0] + '\n' for q in session.sql_log))
        self.assertEqual(rec['decision']['blocked'][0]['Reason'], 'certified view; never replaced')

    def test_definition_errors_are_not_mistaken_for_ownership(self):
        with self.assertRaises(RuntimeError):
            SH.ensure(_Session(reject=3), plan(), 'ECommerce')


class RuntimeGuardTests(unittest.TestCase):
    def test_runtime_accepts_only_names_derived_from_the_plan(self):
        rec = SH.ensure(_Session(), plan(), 'ECommerce')
        bound = SH.attach(plan(), rec)
        for ok_name in (rec['name'], rec['name'] + '_R3', rec['name'] + '_CORTEX'):
            SV.bound_relation(dict(bound, semantic_view=ok_name))
        for bad in ('WBR_DB.TABLEAU_ACCELERATOR_V3.OTHER_SVLAB_F0123456789', rec['name'] + '_X', 'WBR_DB.PUBLIC.EVENTS'):
            with self.assertRaises(ValueError):
                SV.bound_relation(dict(bound, semantic_view=bad))

    def test_runtime_rejects_a_tampered_mapping(self):
        bound = SH.attach(plan(), SH.ensure(_Session(), plan(), 'ECommerce'))
        for bad in ({}, dict(bound['semantic_map'], REVENUE='events.REVENUE; DROP'),
                    dict(bound['semantic_map'], EXTRA='events.REVENUE')):
            with self.assertRaises(ValueError):
                SV.bound_relation(dict(bound, semantic_map=bad))

    def test_older_payloads_keep_working(self):
        rec = SV.create(_Session(), plan(), IR, 'ECommerce')
        bound = SV.attach(plan(), rec)
        self.assertIn('SEMANTIC_VIEW(' + rec['name'], SV.bound_relation(bound))


class RegistryTests(unittest.TestCase):
    def test_decision_is_recorded_on_the_run_and_summarised(self):
        import v3_store
        run = {'result': {'ir': {'workbook': 'ECommerce'}}}
        rec = SH.ensure(_Session(), plan(), 'ECommerce')
        v3_store.log_semantic(run, SH.log_rows(run, 'ECommerce', rec, 'relationship'))
        row = run['semantic_registry'][0]
        self.assertEqual((row['ACTION'], row['KIND'], row['VERIFIED']), ('created', 'relationship', True))
        self.assertEqual(sorted(row['TABLES']), ['DB.S.CUSTOMERS', 'DB.S.EVENTS', 'DB.S.PRODUCTS'])
        self.assertIn('Any later workbook on the same tables and keys reuses it', SH.summary(rec))
        self.assertIn('SEMANTIC_VIEW_EVENTS', v3_store.ddl())
        self.assertIn('V_SEMANTIC_VIEW_REGISTRY', v3_store.ddl())


if __name__ == '__main__':
    unittest.main()
