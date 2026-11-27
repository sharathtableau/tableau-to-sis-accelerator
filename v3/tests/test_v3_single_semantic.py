"""Single-table datasources get a Semantic View over the bound table, for Ask the data only."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import v3_semantic as SV
import v3_ask
from v3_package import validate_ask

TABLE = 'SANDBOX.BI.VIZLEAP_SALES'
CAP = 'VIZLEAP_SALES (SANDBOX.VIZLEAP_SALES) (BI)'
DESCRIBE = [('ORDER_ID', 'NUMBER(38,0)'), ('ORDER_DATE', 'DATE'), ('COUNTRY', 'VARCHAR(100)'),
            ('PAYMENT_TYPE', 'VARCHAR(50)'), ('SALES', 'NUMBER(12,2)'), ('QUANTITY', 'NUMBER(38,0)'),
            ('PROFIT', 'FLOAT'), ('PAYLOAD', 'VARIANT'), ('Product Name', 'VARCHAR(200)')]
IR = {'calcs': {'Profit Ratio': {'agg_ready': True, 'sql': 'SUM(PROFIT) / NULLIF(SUM(SALES), 0)'},
                'Param calc': {'agg_ready': True, 'sql': 'SUM(SALES) * __PARAM_X'},
                'Other ds': {'agg_ready': True, 'sql': 'SUM(REVENUE)'}}}


class _Row:
    def __init__(self, d):
        self.d = d

    def as_dict(self):
        return dict(self.d)


class _Session:
    def __init__(self, drift=False, describe_table_fails=False):
        self.log, self.drift, self.describe_table_fails = [], drift, describe_table_fails

    def sql(self, text):
        self.log.append(text)
        s = self

        class R:
            def collect(self_inner):
                if text.startswith('DESCRIBE TABLE') and s.describe_table_fails:
                    raise RuntimeError('not a table')
                if text.startswith('DESCRIBE'):
                    return [_Row({'name': n, 'type': t, 'kind': 'COLUMN'}) for n, t in DESCRIBE]
                if text.startswith('CREATE OR REPLACE SEMANTIC VIEW'):
                    return []
                if 'COUNT(*) AS N FROM (SELECT DISTINCT' in text:
                    return [_Row({'N': 5000 + (1 if s.drift and 'SEMANTIC_VIEW(' in text else 0)})]
                if text.startswith('SELECT SUM('):
                    return [_Row({'TOTAL_SALES': 1000.5, 'TOTAL_QUANTITY': 42, 'TOTAL_PROFIT': 99.25})]
                if 'SEMANTIC_VIEW(' in text and ' METRICS ' in text:
                    return [_Row({'TOTAL_SALES': 1000.5, 'TOTAL_QUANTITY': 42, 'TOTAL_PROFIT': 99.25})]
                return [_Row({'V': 1})]
        return R()


class SingleTableSemanticTests(unittest.TestCase):
    def test_spec_reads_columns_and_skips_unmodelled_types(self):
        spec = SV.single_spec(_Session(), TABLE)
        self.assertEqual(spec['table'], TABLE)
        self.assertNotIn('PAYLOAD', [c['column'] for c in spec['columns']])
        kinds = {c['column']: c['datatype'] for c in spec['columns']}
        self.assertEqual((kinds['ORDER_ID'], kinds['SALES'], kinds['ORDER_DATE']), ('integer', 'real', 'date'))

    def test_view_fallback_when_binding_is_a_view(self):
        session = _Session(describe_table_fails=True)
        SV.single_spec(session, TABLE)
        self.assertTrue(any(q.startswith('DESCRIBE VIEW') for q in session.log))

    def test_rejects_non_object_binding(self):
        for bad in ('C:/data/sales.csv', 'SALES', "A.B.C; DROP TABLE X"):
            with self.assertRaises(ValueError):
                SV.single_spec(_Session(), bad)

    def test_model_metrics_and_readable_name(self):
        sem = SV.single_model(SV.single_spec(_Session(), TABLE), IR, CAP)
        names = {m['name'] for m in sem['metrics']}
        self.assertTrue({'TOTAL_SALES', 'TOTAL_QUANTITY', 'TOTAL_PROFIT', 'DISTINCT_ORDER_ID', 'PROFIT_RATIO'} <= names)
        self.assertNotIn('TOTAL_ORDER_ID', names)                 # identifiers are counted, not summed
        self.assertFalse(any('PARAM' in m['expr'] or 'REVENUE' in m['expr'] for m in sem['metrics']))
        self.assertTrue(sem['name'].startswith('WBR_DB.TABLEAU_ACCELERATOR_V3.VIZLEAP_SALES_SANDBOX_VIZLEAP_SALES_BI_SVLAB_'))
        quoted = next(d for d in sem['dimensions'] if d['column'] == 'Product Name')
        self.assertEqual(quoted['name'], 'PRODUCT_NAME')
        ddl = SV.ddl(sem)
        self.assertIn('AS "Product Name"', ddl)
        self.assertIn('data source table ' + TABLE, ddl)
        self.assertNotIn('RELATIONSHIPS', ddl)

    def test_relationship_ddl_comment_unchanged(self):
        from tests.test_v3_semantic import plan
        self.assertIn('relationship model: ECommerce', SV.ddl(SV.model(plan(), None, 'ECommerce')))

    def test_create_verifies_and_returns_ask_entry(self):
        rec = SV.create_single(_Session(), SV.single_spec(_Session(), TABLE), IR, CAP)
        self.assertTrue(rec['verified'])
        self.assertEqual(rec['mode'], 'single_table')
        self.assertEqual({c['Check'] for c in rec['checks']},
                         {'Distinct rows', 'Total SALES', 'Total QUANTITY', 'Total PROFIT'})
        entry = rec['ask']
        self.assertEqual(entry['semantic_view'], rec['name'])
        self.assertEqual(SV.check_single(entry, TABLE), entry)

    def test_not_equal_means_no_ask(self):
        rec = SV.create_single(_Session(drift=True), SV.single_spec(_Session(), TABLE), None, CAP)
        self.assertFalse(rec['verified'])
        self.assertIsNone(rec['ask'])

    def test_entry_bound_to_exact_table_and_name(self):
        entry = SV.create_single(_Session(), SV.single_spec(_Session(), TABLE), None, CAP)['ask']
        with self.assertRaises(ValueError):
            SV.check_single(entry, 'SANDBOX.BI.OTHER')
        with self.assertRaises(ValueError):
            SV.check_single(dict(entry, semantic_view='WBR_DB.TABLEAU_ACCELERATOR_V3.X_SV_00000000'), TABLE)
        with self.assertRaises(ValueError):
            SV.check_single(dict(entry, columns=entry['columns'][:-1]), TABLE)
        with self.assertRaises(ValueError):
            validate_ask({CAP: {'tables': {}}}, {CAP: entry})      # relationship binding cannot carry it

    def test_ask_tab_lists_single_table_view_with_suggestions(self):
        entry = SV.create_single(_Session(), SV.single_spec(_Session(), TABLE), None, CAP)['ask']
        plans = v3_ask.semantic_plans({CAP: TABLE}, {CAP: entry})
        self.assertEqual(list(plans), [CAP])
        ideas = v3_ask.suggestions(plans[CAP])
        self.assertTrue(ideas and all(i.startswith('What is the total') or i.startswith('What is the distinct')
                                      for i in ideas))
        self.assertEqual(v3_ask.semantic_plans({CAP: TABLE}), {})

    def test_bundle_unchanged_without_ask_and_carries_ask_when_present(self):
        import v3_package
        src = {}

        def fake_archive(files, main, target, provenance):
            src['app'] = files['app.py'].decode()
            src['prov'] = provenance
            return b'', 'x'
        orig = v3_package.archive, v3_package.validate_bindings
        v3_package.archive = fake_archive
        v3_package.validate_bindings = lambda ir, b: dict(b)
        try:
            import ir_contract
            keep = ir_contract.require_valid_ir, ir_contract.assess_ir
            ir_contract.require_valid_ir = lambda ir: None
            ir_contract.assess_ir = lambda ir: {}
            try:
                ir = {'fields': {'a': 1}, 'field_scopes': {'x': 1}}
                v3_package.workbook_bundle(ir, {CAP: TABLE})
                self.assertTrue(src['app'].rstrip().endswith("run_candidate(PAYLOAD['ir'], PAYLOAD['bindings'])"))
                self.assertNotIn('ask_semantic_views', src['prov'])
                entry = SV.create_single(_Session(), SV.single_spec(_Session(), TABLE), None, CAP)['ask']
                v3_package.workbook_bundle(ir, {CAP: TABLE}, ask={CAP: entry})
                self.assertIn("PAYLOAD['ask']", src['app'])
                self.assertEqual(src['prov']['ask_semantic_views'], {CAP: entry['semantic_view']})
            finally:
                ir_contract.require_valid_ir, ir_contract.assess_ir = keep
        finally:
            v3_package.archive, v3_package.validate_bindings = orig


class AskChartTests(unittest.TestCase):
    def test_deployed_answer_shape_gets_a_bar_chart(self):
        import datetime, decimal
        rows = [{'BILLING_CYCLE': c, 'START_DATE': datetime.datetime(2024, 4, 22), 'END_DATE': datetime.datetime(2025, 10, 21),
                 'TOTAL_NET_REVENUE_USD': decimal.Decimal(v)} for c, v in
                (('Annual', '28106790.05'), ('Monthly', '2786335.25'), ('One-time', '939156.18'))]
        self.assertEqual(v3_ask.chart_spec(rows), ('bar', 'BILLING_CYCLE', 'TOTAL_NET_REVENUE_USD'))

    def test_time_series_and_no_chart_cases(self):
        import datetime
        rows = [{'MONTH': datetime.date(2025, m, 1), 'TOTAL_SALES': m * 10} for m in (1, 2, 3)]
        self.assertEqual(v3_ask.chart_spec(rows), ('line', 'MONTH', 'TOTAL_SALES'))
        self.assertIsNone(v3_ask.chart_spec([{'TOTAL_SALES': 5}]))
        self.assertIsNone(v3_ask.chart_spec([{'A': 'x', 'B': 'y', 'N': 1}, {'A': 'x', 'B': 'z', 'N': 2}]))


if __name__ == '__main__':
    unittest.main()
