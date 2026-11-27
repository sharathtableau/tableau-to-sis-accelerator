"""DM8: relationship models bind with Tableau's relationship semantics. Written BEFORE the code (tests first).

Found live (Regional Analysis, 26 Nov): 'Data using Relationships' (Orders -> People on Region, Orders -> Returns
on Order ID) showed 'Needs explicit binding' with no reason, so no Semantic View was built. The binding check was
stricter than Tableau in two ways, and the Returns table trips both:
  1. a related table whose key repeats was refused ('Dimension keys are null or duplicate') -- Superstore Returns
     lists an Order ID once per returned line;
  2. a fact key with no related row was refused ('Unmatched relationship keys...') -- most orders were not
     returned. Tableau keeps every fact row and shows Null for the related fields.
Rule (generic, code-verified, nothing written to Snowflake):
  * unmatched fact keys and null related keys are kept (LEFT JOIN, as Tableau); the counts are recorded;
  * a related table whose key repeats but whose columns are constant per key (checked by a query) is read as one
    row per key (SELECT DISTINCT ... WHERE key IS NOT NULL) -- the fact grain is unchanged and every dimension
    value equals Tableau's; a table with a column that varies per key, or one of whose numeric fields the workbook
    aggregates as a measure, is refused with a reason naming the table and the field;
  * the fact row count check stays: the model can never multiply fact rows;
  * the reason reaches the binding row on the Data model page (a code, never a Cortex text);
  * the shared Semantic View covers the tables it can model exactly; a one-row-per-key table stays in the
    dashboard query and the reason is recorded.
The local preview already reads such a table per table (DM4: init_workbook leaves it out of the flatten and a
relationship query reads it), so this rule is for the live Snowflake binding and the loaded-tables check.
Proof: the Superstore Orders / People / Returns data (Sample - Superstore.xls) behind the Regional Analysis model,
on DuckDB; the extract-metadata fix on Regional Analysis.twbx and Retails Dashboard; a corpus audit that no live
Snowflake datasource is refused statically."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
import v3_model_binding as MB  # noqa: E402

REGIONAL = ROOT / 'Workbooks' / 'cloud_20261010' / 'Regional_Analysis.twb'
XLS = ROOT / 'Workbooks' / 'cloud_20261010' / 'Sample - Superstore.xls'


class DuckSession:
    """A read-only Snowflake stand-in over DuckDB: the same SQL the binding check sends."""
    def __init__(self, con):
        self.con, self.seen = con, []

    def sql(self, text, params=None):
        self.seen.append(text)
        con = self.con

        class R:
            def collect(_):
                if 'CURRENT_ACCOUNT()' in text:
                    return [{'LOCATOR': 'X', 'ACCOUNT_NAME': 'C2GPARTNERS', 'ORG': 'WB19670', 'REGION': 'R'}]
                if text.startswith('DESCRIBE TABLE '):
                    out = con.execute('DESCRIBE ' + text[len('DESCRIBE TABLE '):]).fetchall()
                    return [{'NAME': r[0], 'TYPE': {'DOUBLE': 'FLOAT', 'BIGINT': 'NUMBER(38,0)'}.get(r[1], r[1])}
                            for r in out]
                cur = con.execute(text)
                names = [d[0] for d in cur.description]
                return [dict(zip(names, r)) for r in cur.fetchall()]
        return R()


def superstore(con, returns=None):
    import pandas as pd
    con.execute("ATTACH ':memory:' AS SANDBOX")
    con.execute('CREATE SCHEMA SANDBOX.DS')
    x = pd.ExcelFile(XLS)
    o = pd.read_excel(x, 'Orders')
    o.columns = ['ROW_ID', 'ORDER_ID', 'ORDER_DATE', 'SHIP_DATE', 'SHIP_MODE', 'CUSTOMER_ID', 'CUSTOMER_NAME',
                 'SEGMENT', 'COUNTRY', 'CITY', 'STATE', 'POSTAL_CODE', 'REGION', 'PRODUCT_ID', 'CATEGORY',
                 'SUB_CATEGORY', 'PRODUCT_NAME', 'SALES', 'QUANTITY', 'DISCOUNT', 'PROFIT']
    o['ORDER_DATE'] = pd.to_datetime(o['ORDER_DATE']).dt.date
    o['SHIP_DATE'] = pd.to_datetime(o['SHIP_DATE']).dt.date
    o['POSTAL_CODE'] = pd.to_numeric(o['POSTAL_CODE'], errors='coerce').astype('Int64')
    p = pd.read_excel(x, 'People')
    p.columns = ['REGIONAL_MANAGER', 'REGION']
    r = pd.read_excel(x, 'Returns') if returns is None else returns
    r.columns = ['RETURNED', 'ORDER_ID']
    r['RETURNED'] = r['RETURNED'].astype(str).str.lower().isin(['yes', 'true'])
    for name, df in (('ORDERS', o), ('PEOPLE', p), ('RETURNS', r)):
        con.register('df_' + name, df)
        con.execute(f'CREATE TABLE SANDBOX.DS.SAMPLE_SUPER_STORE_{name} AS SELECT * FROM df_{name}')
    return o, p, r


def regional_plan():
    import tableau_parser as TP
    root = TP.load_twb_xml(str(REGIONAL))
    ds = next(d for d in root.findall('./datasources/datasource')
              if d.find('.//relationships/relationship') is not None)
    return MB.build(ds), root


@unittest.skipUnless(REGIONAL.exists() and XLS.exists(), 'corpus workbook and data')
class Regional(unittest.TestCase):
    def setUp(self):
        import duckdb
        self.con = duckdb.connect()
        self.o, self.p, self.r = superstore(self.con)
        self.s = DuckSession(self.con)
        self.plan, _root = regional_plan()

    def test_returns_repeat_and_most_orders_are_unmatched(self):
        r = self.r
        self.assertLess(r['ORDER_ID'].nunique(), len(r))                   # the key repeats
        self.assertLess(self.o['ORDER_ID'].isin(r['ORDER_ID']).mean(), 0.5)  # most orders have no return

    def test_binds_at_fact_grain_with_tableau_values(self):
        checks = MB.validate(self.s, self.plan, None)
        returns = next(oid for oid, t in self.plan['tables'].items() if t['table'].endswith('RETURNS'))
        self.assertTrue(self.plan['tables'][returns].get('distinct'))       # read as one row per Order ID
        self.assertEqual(checks['fact_rows'], len(self.o))
        self.assertEqual(checks['output_rows'], len(self.o))
        edge = next(e for e in checks['edges'] if e['table'].endswith('RETURNS'))
        self.assertEqual(edge['key_rows'], len(self.r))
        self.assertEqual(edge['distinct_keys'], self.r['ORDER_ID'].nunique())
        self.assertGreater(edge['unmatched_fact_keys'], 0)
        # the values Tableau shows: every order kept; RETURNED true exactly for returned orders, Null otherwise
        df = self.con.execute(MB.compile_query(self.plan)).df()
        self.assertEqual(len(df), len(self.o))
        self.assertAlmostEqual(float(df['SALES'].sum()), float(self.o['SALES'].sum()), places=4)
        returned = set(self.r['ORDER_ID'])
        want = self.o['ORDER_ID'].isin(returned)
        self.assertEqual(int(df['RETURNED'].fillna(False).astype(bool).sum()), int(want.sum()))
        self.assertEqual(int(df['RETURNED'].isna().sum()), int((~want).sum()))
        self.assertEqual(int(df['REGIONAL_MANAGER'].isna().sum()), 0)

    def test_a_varying_column_is_refused_with_a_reason(self):
        import duckdb
        import pandas as pd
        con = duckdb.connect()
        bad = pd.DataFrame({'Returned': ['Yes', 'No'], 'Order ID': ['CA-2016-152156', 'CA-2016-152156']})
        superstore(con, returns=bad)
        with self.assertRaises(ValueError) as cm:
            MB.validate(DuckSession(con), regional_plan()[0], None)
        self.assertIn('SAMPLE_SUPER_STORE_RETURNS', str(cm.exception))
        self.assertIn('RETURNED', str(cm.exception))
        self.assertTrue(MB.reason_code(cm.exception).startswith('KEY_REPEATS_VARYING'))

    def test_a_measure_on_a_repeating_table_is_refused(self):
        import tableau_parser as TP
        ir = TP.build_ir(str(REGIONAL), scoped=True)
        returns_fields = [f for f in ir['fields'].values() if f.get('internal') in ('RETURNED',)]
        for f in returns_fields:
            f['role'] = 'measure'
        with self.assertRaises(ValueError) as cm:
            MB.validate(self.s, self.plan, ir, measures=[f['internal'] for f in returns_fields])
        self.assertTrue(MB.reason_code(cm.exception).startswith('KEY_REPEATS_MEASURE'))

    def test_semantic_view_leaves_the_one_row_per_key_table_out(self):
        import v3_semantic as SV
        MB.validate(self.s, self.plan, None)
        pruned, left_out = SV.exact_part(self.plan)
        self.assertEqual([t['caption'] for t in left_out], ['SAMPLE_SUPER_STORE_RETURNS'])
        self.assertEqual(len(pruned['tables']), 2)
        self.assertTrue(all(not t.get('distinct') for t in pruned['tables'].values()))
        self.assertEqual(len(self.con.execute(MB.compile_query(pruned)).df()), len(self.o))

    def test_plain_plans_are_unchanged(self):
        import duckdb
        import pandas as pd
        con = duckdb.connect()
        superstore(con, returns=pd.DataFrame({'Returned': ['Yes'], 'Order ID': ['CA-2016-152156']}))
        plan = regional_plan()[0]
        sql_before = MB.compile_query(plan)
        MB.validate(DuckSession(con), plan, None)
        self.assertFalse(any(t.get('distinct') for t in plan['tables'].values()))
        self.assertEqual(MB.compile_query(plan), sql_before)


class ExtractMetadata(unittest.TestCase):
    """A live relationship datasource with an extract: the extract's own metadata records (under its hyper
    connection) name their owner by object id, and the plan was refused ('Physical field owner is absent from
    graph'). Only the live connection's records decide ownership now. Two workbooks."""
    def test_twbx_with_extract_builds(self):
        import tableau_parser as TP
        for name, caption in (('Regional Analysis.twbx', 'Data using Relationships'),
                              ('Retails Dashboard.twbx', 'VIZLEAP_CUSTOMER')):
            path = next((ROOT / 'Workbooks').rglob(name), None)
            if path is None:
                continue
            root = TP.load_twb_xml(str(path))
            ds = next(d for d in root.findall('./datasources/datasource')
                      if (d.get('caption') or '').startswith(caption))
            plan = MB.build(ds)
            self.assertGreaterEqual(len(plan['tables']), 2, name)
            self.assertTrue(all(f['owner'] in plan['tables'] for f in plan['fields'].values()))


class CorpusAudit(unittest.TestCase):
    """Guard: every live Snowflake datasource in the corpus gets a binding plan from the workbook alone; the only
    static refusals left are sources that are not Snowflake (file / extract / other databases: loaded or
    replicated) and the two hand-made R9 / R10 fixtures whose metadata is ambiguous on purpose."""
    ALLOWED = ('Automatic binding requires exactly one authored Snowflake connection', 'Live ',
               'Ambiguous physical field metadata')

    def test_no_live_snowflake_source_is_refused_statically(self):
        import tableau_parser as TP
        import v3_source_binding as SB
        seen, bad = 0, []
        for f in sorted((ROOT / 'Workbooks').rglob('*.twb*')):
            if f.suffix == '.twbr':
                continue
            try:
                ir, root = TP.build_ir(str(f), scoped=True), TP.load_twb_xml(str(f))
            except Exception:
                continue
            for item in SB.source_candidates(root, ir).values():
                seen += 1
                reason = item.get('reason')
                if reason and not reason.startswith(self.ALLOWED):
                    bad.append((f.name, item['caption'], reason))
                if reason and reason.startswith('Ambiguous') and not f.name.startswith(('R9_', 'R10_')):
                    bad.append((f.name, item['caption'], reason))
        if not seen:
            self.skipTest('corpus')
        self.assertEqual(bad, [])


class Codes(unittest.TestCase):
    def test_reason_codes(self):
        self.assertEqual(MB.reason_code(ValueError('KEY_REPEATS_VARYING: T.X')), 'KEY_REPEATS_VARYING')
        self.assertEqual(MB.reason_code(ValueError('Source account identity mismatch')), 'SOURCE_ACCOUNT_MISMATCH')
        self.assertEqual(MB.reason_code(RuntimeError('boom')), 'BINDING_CHECK_FAILED')

    def test_console_shows_the_reason(self):
        import v3_console as C
        result = {'source_reports': [{'caption': 'D', 'reason': 'KEY_REPEATS_VARYING: T repeats its key and T.X '
                                                                'differs within a key'},
                                     {'caption': 'E', 'reason': '002003 (02000): SQL compilation error: secret'}]}
        self.assertEqual(C._binding_reason(result, 'D'),
                         ' — KEY_REPEATS_VARYING: T repeats its key and T.X differs within a key')
        self.assertEqual(C._binding_reason(result, 'E'), ' — BINDING_CHECK_FAILED')     # never a Snowflake message
        self.assertEqual(C._binding_reason(result, 'F'), '')


if __name__ == '__main__':
    unittest.main()
