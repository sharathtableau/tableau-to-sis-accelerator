"""DM7: the data-model close-out (E02, E04 composite keys, E14 account at runtime, EN19). Written BEFORE the code.

E04  A relationship on several fields (<expression op='AND'> of equalities) is joined on ALL of them:
       * the extract flatten (init_workbook.flatten_tables) keeps fact grain, refuses a repeated or null combination;
       * the live binding (v3_model_binding) compiles ON a AND b, checks the combination is unique and non-null,
         and the Semantic View declares PRIMARY KEY (a, b) / REFERENCES on both columns.
     Consumers that join on one key still refuse a composite relationship in strict mode (no partial model).
     Proof: E-Commerce live relationship model (Events-Customers on customer_id AND country) and synthetic frames.
E14  A package records the account its bindings were verified in; the app refuses to run them in another one.
EN19 A live source that is not Snowflake refuses with a reason that names the replication route.
E02  A placed filter with no source sheet and no datasource never reads the built-in default table.
4.2  A live union of Snowflake tables is one read-only UNION ALL (columns by name, a member's missing column NULL,
     'Table Name' = the member), verified before binding.
6.3  A reserved-word column (ORDER, USER, ...) gets the engine name ORDER_ everywhere; a live table with one is read
     through a verified one-table projection (proof: GOLD CRM Scorecard Cohort Analysis's 'user' column).
6.4  A live non-Snowflake source gets a replication kit: landing-table DDL from the workbook metadata and the steps."""
import copy
import json
from pathlib import Path
import sys
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import init_workbook as IW  # noqa: E402

REL = ET.fromstring(
    "<g><relationship><expression op='AND'>"
    "<expression op='='><expression op='[REGION]'/><expression op='[REGION (Targets)]'/></expression>"
    "<expression op='='><expression op='[YEAR]'/><expression op='[YEAR (Targets)]'/></expression>"
    "</expression><first-end-point object-id='Sales_0123456789ABCDEF01'/><second-end-point object-id='Targets_FEDCBA9876543210FE'/>"
    "</relationship></g>")


class Parse(unittest.TestCase):
    def test_composite_only_when_asked(self):
        with self.assertRaises(ValueError):
            IW.parse_relationships(REL, strict=True)                   # one-key consumers: still refused
        r = IW.parse_relationships(REL, strict=True, composite=True)
        self.assertEqual(r, [{'first': 'Sales_0123456789ABCDEF01', 'second': 'Targets_FEDCBA9876543210FE', 'lkey': 'REGION', 'rkey': 'REGION',
                              'lkeys': ['REGION', 'YEAR'], 'rkeys': ['REGION', 'YEAR']}])


class Flatten(unittest.TestCase):
    def frames(self):
        import pandas as pd
        fact = pd.DataFrame({'REGION': ['E', 'E', 'W', 'W', 'N'], 'YEAR': [2023, 2024, 2023, 2024, 2024],
                             'SALES': [10.0, 20.0, 30.0, 40.0, 50.0]})
        dim = pd.DataFrame({'REGION': ['E', 'E', 'W'], 'YEAR': [2023, 2024, 2024], 'TARGET': [1.0, 2.0, 4.0]})
        return {'Sales_0123456789ABCDEF01': fact, 'Targets_FEDCBA9876543210FE': dim}

    def test_joined_on_every_key_at_fact_grain(self):
        rels = IW.parse_relationships(REL, strict=True, composite=True)
        ev = {}
        df, _ = IW.flatten_tables(self.frames(), rels, strict=True, evidence=ev)
        self.assertEqual(len(df), 5)
        self.assertEqual(float(df['SALES'].sum()), 150.0)                # no inflation
        got = {(r.REGION, r.YEAR): r.TARGET for r in df.itertuples()}
        self.assertEqual(got[('E', 2023)], 1.0)
        self.assertEqual(got[('E', 2024)], 2.0)
        self.assertEqual(got[('W', 2024)], 4.0)
        self.assertTrue(got[('W', 2023)] != got[('W', 2023)])            # no match on the pair = null, not W-2024
        self.assertIn('REGION (Targets)', df.columns)                   # the dimension's own key, as Tableau names it
        self.assertEqual(ev['edges'][0]['unmatched_rows'], 2)

    def test_repeated_combination_refuses(self):
        import pandas as pd
        f = self.frames()
        f['Targets_FEDCBA9876543210FE'] = pd.concat([f['Targets_FEDCBA9876543210FE'], f['Targets_FEDCBA9876543210FE'].iloc[[0]].assign(TARGET=9.0)])
        with self.assertRaises(ValueError) as cm:
            IW.flatten_tables(f, IW.parse_relationships(REL, strict=True, composite=True), strict=True)
        self.assertIn('repeats', str(cm.exception))


ECOM = ROOT / 'Workbooks' / 'ECommerce_Live_Relationship_Events_Customers_Products.twbx'


def composite_ds():
    import tableau_parser as TP
    root = TP.load_twb_xml(str(ECOM))
    ds = next(d for d in root.findall('./datasources/datasource') if d.find('.//relationships/relationship') is not None)
    ds = copy.deepcopy(ds)
    for rel in ds.iter():
        if isinstance(rel.tag, str) and rel.tag.rsplit('.', 1)[-1] == 'relationship':
            expr = rel.find('expression')
            ops = [e.get('op') for e in expr]
            if ops == ['[customer_id]', '[customer_id (Customers)]']:
                pair = copy.deepcopy(expr)
                pair[0].set('op', '[country]')
                pair[1].set('op', '[country (Customers)]')
                both = ET.Element('expression', {'op': 'AND'})
                both.append(copy.deepcopy(expr))
                both.append(pair)
                rel.remove(expr)
                rel.insert(0, both)
    return ds


class FakeSession:
    def __init__(self, n=10, columns=('CUSTOMER_ID', 'COUNTRY')):
        self.seen, self.n, self.columns = [], n, columns

    def sql(self, text, params=None):
        self.seen.append(text)
        outer = self

        class R:
            def collect(_):
                if text.startswith('DESCRIBE TABLE'):
                    return [{'NAME': n, 'TYPE': 'VARCHAR'} for n in outer.columns]
                if 'CURRENT_ACCOUNT()' in text:
                    return [{'LOCATOR': 'X', 'ACCOUNT_NAME': 'ACCT', 'ORG': 'ORG', 'REGION': 'R'}]
                if ' IS NULL' in text:
                    return [{'N': 0}]
                return [{'N': outer.n, 'NONNULL': outer.n, 'UNIQUE_KEYS': outer.n}]
        return R()


@unittest.skipUnless(ECOM.exists(), 'corpus workbook')
class LiveBinding(unittest.TestCase):
    def test_composite_live_plan(self):
        import v3_model_binding as MB
        plan = MB.build(composite_ds())
        key = next(k for k in plan['keys'] if k.get('pairs'))
        self.assertEqual(key['pairs'], [['CUSTOMER_ID', 'CUSTOMER_ID'], ['COUNTRY', 'COUNTRY']])
        sql = MB.compile_query(plan)
        self.assertIn('d0."CUSTOMER_ID" AND f."COUNTRY" = d0."COUNTRY"', sql)
        json.dumps(plan)                                              # a plan stays plain data

    def test_composite_key_is_checked_as_a_whole(self):
        import v3_model_binding as MB
        plan = MB.build(composite_ds())
        for f in plan['fields'].values():
            f['datatype'] = 'string'
        s = FakeSession(columns=sorted({f['remote'] for f in plan['fields'].values()}))
        plan['server'] = 'org-acct.snowflakecomputing.com'
        MB.validate(s, plan, None)
        q = next(t for t in s.seen if 'UNIQUE_KEYS' in t)
        self.assertIn('COUNT(DISTINCT "CUSTOMER_ID", "COUNTRY")', q)
        self.assertIn('COUNT_IF("CUSTOMER_ID" IS NOT NULL AND "COUNTRY" IS NOT NULL)', q)

    def test_live_lookup_with_repeating_keys_refuses_at_binding(self):
        """DM4's open case: a live relationship model whose lookup keys repeat AND whose columns differ within a key
        is refused (never inflated). DM8: constant per key is read as one row per key (test_v3_dm8)."""
        import tableau_parser as TP
        import v3_model_binding as MB
        root = TP.load_twb_xml(str(ECOM))
        ds = next(d for d in root.findall('./datasources/datasource')
                  if d.find('.//relationships/relationship') is not None)
        plan = MB.build(ds)
        for f in plan['fields'].values():
            f['datatype'] = 'string'
        plan['server'] = 'org-acct.snowflakecomputing.com'

        class Dup(FakeSession):
            def sql(self, text, params=None):
                r = super().sql(text, params)
                if 'UNIQUE_KEYS' in text:
                    class R:
                        def collect(_):
                            return [{'N': 10, 'NONNULL': 10, 'UNIQUE_KEYS': 7}]
                    return R()
                return r
        with self.assertRaises(ValueError) as cm:
            MB.validate(Dup(columns=sorted({f['remote'] for f in plan['fields'].values()})), plan, None)
        self.assertIn('KEY_REPEATS_VARYING', str(cm.exception))

    def test_semantic_view_declares_both_columns(self):
        import v3_model_binding as MB
        import v3_semantic as SV
        plan = MB.build(composite_ds())
        sem = SV.model(plan, caption='Events')
        ddl = SV.ddl(sem)
        self.assertIn('PRIMARY KEY (CUSTOMER_ID, COUNTRY)', ddl)
        self.assertIn('(CUSTOMER_ID, COUNTRY) REFERENCES', ddl)
        rel = SV.relation_sql(sem, plan, 'per_table')
        self.assertIn(' AND ', rel.split('LEFT JOIN')[1])

    def test_single_key_plans_are_unchanged(self):
        import tableau_parser as TP
        import v3_model_binding as MB
        root = TP.load_twb_xml(str(ECOM))
        ds = next(d for d in root.findall('./datasources/datasource')
                  if d.find('.//relationships/relationship') is not None)
        plan = MB.build(ds)
        self.assertFalse(any('pairs' in k for k in plan['keys']))


class Account(unittest.TestCase):
    def test_package_records_the_account_and_refuses_bad_values(self):
        import v3_package as P
        src = (ROOT / 'v3_package.py').read_text(encoding='utf-8')
        self.assertIn("account=PAYLOAD['account']", src)
        with self.assertRaises(ValueError):
            P.workbook_bundle({'dashboards': [], 'datasources': ['A'], 'fields': {'x': {}}, 'field_scopes': {'d': {}}},
                              {'A': 'DB.S.T'}, account="evil'; DROP")

    def test_runtime_refuses_another_account_and_runs_in_its_own(self):
        import tempfile
        from unittest.mock import patch
        from streamlit.runtime.scriptrunner import StopException
        import v3_runtime
        sys.path.insert(0, str(ROOT / 'tests'))
        from test_v3_package import fixture, parse
        with tempfile.TemporaryDirectory() as folder:
            ir = parse(fixture(), folder)
            bindings = {cap: 'DB.SCHEMA.T' for cap in ir['datasources']}
            base = [patch('backend._active_session', return_value=FakeSession()), patch('v3_local._validate_table_once'),
                    patch.object(v3_runtime.st, 'caption'), patch.object(v3_runtime.st, 'title'),
                    patch.object(v3_runtime.st, 'set_page_config')]
            for p in base:
                p.start()
            try:
                with patch.object(v3_runtime.st, 'error') as err, \
                        patch.object(v3_runtime.st, 'stop', side_effect=StopException), \
                        patch.object(v3_runtime, 'render_workbook') as run:
                    with self.assertRaises(StopException):
                        v3_runtime.run_candidate(ir, bindings, account='other-acct.snowflakecomputing.com')
                    run.assert_not_called()
                    self.assertIn('BINDING_ACCOUNT_MISMATCH', err.call_args[0][0])
                with patch.object(v3_runtime, 'render_workbook') as run:
                    v3_runtime.run_candidate(ir, bindings, account='org-acct.snowflakecomputing.com')
                    run.assert_called_once()
            finally:
                for p in base:
                    p.stop()

    def test_runtime_stops_in_another_account(self):
        import v3_runtime
        import v3_source_binding as SB
        self.assertEqual(SB.account_host(FakeSession()), 'org-acct.snowflakecomputing.com')
        src = (ROOT / 'v3_runtime.py').read_text(encoding='utf-8')
        self.assertIn('BINDING_ACCOUNT_MISMATCH', src)
        self.assertIn('account=None', src.split('def run_candidate')[1].split(':')[0])


class LiveOther(unittest.TestCase):
    def test_non_snowflake_live_source_names_the_route(self):
        import v3_source_binding as SB
        x = ET.fromstring("<workbook><datasources><datasource name='federated.a' caption='A'>"
                          "<connection class='federated'><named-connections><named-connection name='sqlserver.1'>"
                          "<connection class='sqlserver' server='db.local' dbname='Sales'/></named-connection>"
                          "</named-connections><relation connection='sqlserver.1' name='T' table='[dbo].[T]' "
                          "type='table'/></connection></datasource></datasources></workbook>")
        out = SB.source_candidates(x, {'field_scopes': {'federated.a': {'caption': 'A'}}})
        self.assertIn('replicate it into Snowflake', out['federated.a']['reason'])
        self.assertIn('sqlserver', out['federated.a']['reason'])


class DefaultTable(unittest.TestCase):
    def test_filter_without_a_source_never_reads_the_default_table(self):
        import engine
        import v3_filter_states as FS
        old = engine.IR
        engine.IR = {'field_scopes': {'d': {'caption': 'A'}}}
        try:
            self.assertEqual(FS._resolve({'sheets': []}, {'caption': 'Region'}, engine), (None, None))
        finally:
            engine.IR = old


UNION_DS = (b"<datasource name='federated.u' caption='Orders all years'><connection class='federated'>"
            b"<named-connections><named-connection name='snowflake.x'><connection class='snowflake' "
            b"server='org-acct.snowflakecomputing.com' dbname='WBR_DB' schema='S'/></named-connection>"
            b"</named-connections><relation all='true' name='Orders' type='union'>"
            b"<relation connection='snowflake.x' name='ORDERS_2023' table='[WBR_DB].[S].[ORDERS_2023]' type='table'/>"
            b"<relation connection='snowflake.x' name='ORDERS_2024' table='[WBR_DB].[S].[ORDERS_2024]' type='table'/>"
            b"</relation><metadata-records>"
            + b"".join(b"<metadata-record class='column'><remote-name>%s</remote-name><local-name>[%s]</local-name>"
                       b"<parent-name>[Orders]</parent-name><local-type>%s</local-type></metadata-record>" % (r, l, t)
                       for r, l, t in ((b'ORDER_ID', b'ORDER_ID', b'string'), (b'SALES', b'SALES', b'real'),
                                       (b'COUPON', b'COUPON', b'string'), (b'Table Name', b'Table Name', b'string')))
            + b"</metadata-records></connection></datasource>")


class LiveUnion(unittest.TestCase):
    def session(self):
        class S(FakeSession):
            def sql(self, text, params=None):
                self.seen.append(text)
                if text.startswith('DESCRIBE TABLE WBR_DB.S.ORDERS_2023'):
                    rows_ = [{'NAME': 'ORDER_ID', 'TYPE': 'VARCHAR'}, {'NAME': 'SALES', 'TYPE': 'FLOAT'}]
                elif text.startswith('DESCRIBE TABLE'):
                    rows_ = [{'NAME': 'ORDER_ID', 'TYPE': 'VARCHAR'}, {'NAME': 'SALES', 'TYPE': 'FLOAT'},
                             {'NAME': 'COUPON', 'TYPE': 'VARCHAR'}]
                elif 'CURRENT_ACCOUNT()' in text:
                    rows_ = [{'LOCATOR': 'X', 'ACCOUNT_NAME': 'ACCT', 'ORG': 'ORG', 'REGION': 'R'}]
                else:
                    rows_ = [{'N': 7}]

                class R:
                    def collect(_):
                        return rows_
                return R()
        return S()

    def test_union_plan_and_sql(self):
        import v3_model_binding as MB
        import v3_physical_binding as PB
        plan = MB.build_live(ET.fromstring(UNION_DS))
        self.assertEqual(plan['kind'], 'live-union')
        done = PB.complete_union(self.session(), plan)
        self.assertEqual(done['missing'], {'ORDERS_2023': ['COUPON']})
        sql = MB.compile_query(done)
        self.assertIn("SELECT \"ORDER_ID\", \"SALES\", NULL AS \"COUPON\", 'ORDERS_2023' AS \"Table Name\" FROM WBR_DB.S.ORDERS_2023", sql)
        self.assertIn(" UNION ALL SELECT \"ORDER_ID\", \"SALES\", \"COUPON\", 'ORDERS_2024'", sql)
        self.assertIn('q."Table Name" AS TABLE_NAME', sql)
        out = MB.validate(self.session(), plan, None)
        self.assertEqual(out['missing_columns'], {'ORDERS_2023': ['COUPON']})

    def test_routed_through_source_binding(self):
        import v3_source_binding as SB
        root = ET.fromstring(b"<workbook><datasources>" + UNION_DS + b"</datasources></workbook>")
        out = SB.source_candidates(root, {'field_scopes': {'federated.u': {'caption': 'Orders all years'}}})
        self.assertEqual(out['federated.u']['model_candidate']['kind'], 'live-union')

    def test_member_in_another_connection_refuses(self):
        import v3_model_binding as MB
        bad = UNION_DS.replace(b"connection='snowflake.x' name='ORDERS_2024'", b"connection='other' name='ORDERS_2024'")
        with self.assertRaises(ValueError):
            MB.build_live(ET.fromstring(bad))


GOLD_COHORT = sorted((ROOT / 'Workbooks' / '_powerbi_project_sweep').glob('GOLD CRM Scorecard Cohort Analysis*.twbx'))


class ReservedWords(unittest.TestCase):
    def test_engine_names_never_reserved(self):
        from calc_translator import to_phys
        self.assertEqual([to_phys(x) for x in ('Order', 'user', 'Group', 'Order Date', 'Region')],
                         ['ORDER_', 'USER_', 'GROUP_', 'ORDER_DATE', 'REGION'])

    @unittest.skipUnless(GOLD_COHORT, 'corpus workbook')
    def test_live_table_with_a_reserved_column_reads_through_a_projection(self):
        import tableau_parser as TP
        import v3_model_binding as MB
        import v3_source_binding as SB
        path = str(GOLD_COHORT[0])
        out = SB.source_candidates(TP.load_twb_xml(path), TP.build_ir(path, scoped=True))
        plan = next(v['model_candidate'] for v in out.values() if v.get('model_candidate'))
        self.assertEqual(plan['kind'], 'projection')
        sql = MB.compile_query(plan)
        self.assertIn('t0."user" AS USER_', sql)
        self.assertNotIn(' JOIN ', sql)


class ReplicationKit(unittest.TestCase):
    def test_landing_ddl_and_steps(self):
        import v3_intake
        x = ET.fromstring(b"<workbook><datasources><datasource name='federated.a' caption='Sales (SQL Server)'>"
                          b"<connection class='federated'><named-connections><named-connection name='s'>"
                          b"<connection class='sqlserver' server='db'/></named-connection></named-connections>"
                          b"<metadata-records><metadata-record class='column'><remote-name>Order</remote-name>"
                          b"<local-name>[Order]</local-name><local-type>integer</local-type></metadata-record>"
                          b"<metadata-record class='column'><remote-name>Amount</remote-name><local-name>[Amount]"
                          b"</local-name><local-type>real</local-type></metadata-record></metadata-records>"
                          b"</connection></datasource></datasources></workbook>")
        kits = v3_intake.replication_kit(x)
        self.assertEqual(kits[0]['Landing table'], 'WBR_DB.V3_LANDING.SALES_SQL_SERVER')
        self.assertIn('CREATE TABLE IF NOT EXISTS WBR_DB.V3_LANDING.SALES_SQL_SERVER', kits[0]['ddl'])
        self.assertIn('ORDER_ NUMBER(38,0)', kits[0]['ddl'])
        self.assertIn('AMOUNT FLOAT', kits[0]['ddl'])
        self.assertNotIn('OR REPLACE', kits[0]['ddl'])
        self.assertEqual(len(kits[0]['steps']), 4)
        with self.assertRaises(ValueError):
            v3_intake.replication_kit(x, schema='x; DROP')


if __name__ == '__main__':
    unittest.main()
