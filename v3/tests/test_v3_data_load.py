"""Release 21a: load a workbook's own data into Snowflake after approval; find it again by fingerprint."""
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pandas as pd
import tableau_parser
import v3_data_load as DL
from v3_source_binding import validate_table

CLOUD = ROOT / 'Workbooks' / 'cloud_20261010'


def wb(name):
    p = CLOUD / name
    return p.read_bytes(), tableau_parser.build_ir(str(p), scoped=True), tableau_parser.load_twb_xml(str(p))


class _Row(dict):
    def as_dict(self):
        return dict(self)

    def __getitem__(self, k):
        return list(self.values())[k] if isinstance(k, int) else dict.__getitem__(self, k)


class FakeSnowflake:
    """Tables with columns, types, row counts and comments; enough SQL for v3_data_load and validate_table."""
    def __init__(self):
        self.tables, self.log, self.writes, self.unmatched = {}, [], [], 0

    def write_pandas(self, df, name, database, schema, auto_create_table, overwrite, quote_identifiers):
        assert not overwrite and auto_create_table and not quote_identifiers
        fqn = f'{database}.{schema}.{name}'
        assert fqn not in self.tables, 'never writes over an existing table'
        def typ(s):
            d = str(s.dtype)
            return ('NUMBER(38,0)' if d in ('Int64', 'int64') else 'FLOAT' if d.startswith('float')
                    else 'BOOLEAN' if d == 'bool' else 'VARCHAR(16777216)')
        self.tables[fqn] = {'cols': {c: typ(df[c]) for c in df.columns}, 'rows': len(df), 'comment': ''}
        self.writes.append(fqn)

    def sql(self, text, params=None):
        self.log.append(text)
        fake = self

        class R:
            def collect(self_inner):
                t = text.strip()
                m = re.match(r"SHOW TABLES IN SCHEMA (\S+)$", t)
                if m:
                    return [_Row({'name': n.split('.')[-1], 'comment': v['comment']}) for n, v in fake.tables.items()
                            if n.startswith(m.group(1) + '.')]
                m = re.match(r"SHOW TABLES LIKE '([^%]+)%' IN SCHEMA (\S+)", t)
                if m:
                    return [_Row({'name': n.split('.')[-1], 'comment': v['comment']}) for n, v in fake.tables.items()
                            if n.startswith(m.group(2) + '.') and n.split('.')[-1].startswith(m.group(1))]
                if 'INFORMATION_SCHEMA.SCHEMATA' in t:
                    return [_Row({'N': 1})]
                m = re.match(r'DESCRIBE TABLE (\S+)', t)
                if m:
                    return [_Row({'name': c, 'type': ty}) for c, ty in fake.tables[m.group(1)]['cols'].items()]
                m = re.match(r'SELECT COUNT\(\*\) AS N FROM (\S+)$', t)
                if m:
                    return [_Row({'N': fake.tables[m.group(1)]['rows']})]
                m = re.match(r"COMMENT ON TABLE (\S+) IS '(.*)'$", t, re.S)
                if m:
                    fake.tables[m.group(1)]['comment'] = m.group(2).replace("''", "'")
                    return []
                m = re.match(r'ALTER TABLE (\S+) ADD COLUMN (\S+) (\S+)', t)
                if m:
                    fake.tables[m.group(1)]['cols'][m.group(2)] = m.group(3)
                    return []
                m = re.match(r'ALTER TABLE (\S+) DROP COLUMN (\S+)', t)
                if m:
                    del fake.tables[m.group(1)]['cols'][m.group(2)]
                    return []
                m = re.match(r'ALTER TABLE (\S+) RENAME COLUMN (\S+) TO (\S+)', t)
                if m:
                    cols = fake.tables[m.group(1)]['cols']
                    cols[m.group(3)] = cols.pop(m.group(2))
                    return []
                m = re.match(r'SELECT COUNT\(\*\) AS N, COUNT\(\S+\) AS NONNULL, COUNT\(DISTINCT \S+\) AS UNIQUE_KEYS FROM (\S+)$', t)
                if m:
                    n = fake.tables[m.group(1)]['rows']
                    return [_Row({'N': n, 'NONNULL': n, 'UNIQUE_KEYS': n})]
                if ' LEFT JOIN ' in t and t.startswith('SELECT COUNT(*) AS N FROM ') and 'IS NULL' in t:
                    return [_Row({'N': fake.unmatched})]
                m = re.match(r'SELECT COUNT\(\*\) AS N FROM \(SELECT .*? FROM (\S+) f\b', t, re.S)
                if m:
                    return [_Row({'N': fake.tables[m.group(1)]['rows']})]
                if t.startswith(('UPDATE ', 'SELECT * FROM')):
                    return []
                raise AssertionError('unexpected SQL: ' + t[:120])
        return R()


class LocateAndPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ss = wb('Superstore_Tableau2024_3.twbx')
        cls.gs = wb('Global_Sales_Dashboard.twbx')
        cls.r1 = wb('R1_Test_Upload_-_Superstore.twbx')

    def test_bundled_excel_csv_and_sheet_relationships_fit_before_any_write(self):
        raw, ir, root = self.ss
        plans = {c: DL.plan(raw, root, ir, c) for c in ('Sales Target', 'Sales Commission', 'Sample - Superstore')}
        self.assertTrue(all(p['ok'] for p in plans.values()), plans)
        self.assertEqual((plans['Sales Target']['rows'], plans['Sales Commission']['rows']), (4603, 41))
        sss = plans['Sample - Superstore']                                  # release 21b: one table per logical table
        self.assertEqual(sss['mode'], 'entities')
        self.assertEqual([(e['caption'], e['target'], e['rows']) for e in sss['entities']],
                         [('Orders', 'ORDERS', 10194), ('People', 'PEOPLE', 4), ('Returns', 'RETURNS', 296)])
        self.assertEqual(sss['notes'], ['Returns: 504 exact duplicate rows removed'])

    def test_global_sales_files_load_and_snowflake_joins_are_named_for_release_24(self):
        raw, ir, root = self.gs
        plans = {s['caption']: DL.plan(raw, root, ir, s['caption']) for s in ir['field_scopes'].values()}
        self.assertTrue(plans['CALLS']['ok'] and plans['SALES']['ok'])
        joined = next(p for c, p in plans.items() if c.startswith('SAMPLE_SUPER_STORE_ORDERS'))
        self.assertEqual(joined['kind'], 'none')
        self.assertIn('release 24', joined['reason'])

    def test_extracts_are_routed_to_the_laptop_loader_with_a_fingerprint(self):
        raw, ir, root = self.r1
        p = DL.plan(raw, root, ir, 'Sample - Superstore')
        self.assertEqual(p['kind'], 'extract')
        self.assertFalse(p['ok'])
        self.assertIn('load_workbook_data.py', p['reason'])
        self.assertRegex(p['sha256'], r'^[0-9a-f]{64}$')

    def test_tableau_generated_fields_are_not_required_columns(self):
        raw, ir, root = self.ss
        did = DL.locate(raw, root, ir, 'Sample - Superstore')['datasource_id']
        self.assertFalse(any(str(f['internal']).startswith(':') for f in DL.fields_for(ir, did)))


class LaptopExtractTests(unittest.TestCase):
    """The laptop loader decodes .hyper with the same rules; extracts drop the fields Tableau did not use."""
    @unittest.skipUnless(__import__('importlib').util.find_spec('tableauhyperapi'), 'tableauhyperapi not installed')
    def test_extracts_plan_with_unused_fields_listed_not_required(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('lwd', ROOT / 'tools' / 'load_workbook_data.py')
        lwd = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lwd)
        for name, cap, rows in (('Fil_Test.twbx', 'Sample - Superstore (2)', None),
                                ('R1_Test_Upload_-_Superstore.twbx', 'Sample - Superstore', 10194)):
            raw, ir, root = wb(name)
            loc = DL.locate(raw, root, ir, cap)
            p = DL.plan(raw, root, ir, cap, decoded=lwd.decode_extract(raw, loc, root))
            self.assertTrue(p['ok'], p.get('reason'))
            if rows:
                self.assertEqual(p['rows'], rows)
        self.assertTrue(any('Not in the extract' in n for n in DL.plan(*[wb('Fil_Test.twbx')[i] for i in (0, 2, 1)],
                        'Sample - Superstore (2)', decoded=lwd.decode_extract(
                            wb('Fil_Test.twbx')[0], DL.locate(wb('Fil_Test.twbx')[0], wb('Fil_Test.twbx')[2],
                                                              wb('Fil_Test.twbx')[1], 'Sample - Superstore (2)'),
                            wb('Fil_Test.twbx')[2]))['notes']))

    def test_excel_and_csv_stay_strict(self):
        df = pd.DataFrame({'Sales': [1.0]})
        with self.assertRaises(ValueError):
            DL.shape(df, [{'physical': 'Profit', 'datatype': 'real', 'internal': 'Profit'}], extract=False)
        _df, _f, kept, absent = DL.shape(df, [{'physical': 'Profit', 'datatype': 'real', 'internal': 'Profit'}],
                                         extract=True)
        self.assertEqual((kept, absent), ([], ['PROFIT']))


class ShapeTests(unittest.TestCase):
    F = [{'physical': 'Order Date', 'datatype': 'date', 'internal': 'Order Date'},
         {'physical': 'Postal Code', 'datatype': 'string', 'internal': 'Postal Code'},
         {'physical': 'Quantity', 'datatype': 'integer', 'internal': 'Quantity'},
         {'physical': 'Sales', 'datatype': 'real', 'internal': 'Sales'}]

    def test_names_follow_the_engine_and_types_follow_the_workbook(self):
        df = pd.DataFrame({'Order Date': ['2024-01-05', None], 'Postal Code': [42420.0, None],
                           'Quantity': [2, 3], 'Sales': ['1,200.5', '3']})
        out, fixes, kept, absent = DL.shape(df, self.F)
        self.assertEqual((len(kept), absent), (4, []))
        self.assertEqual(list(out.columns), ['ORDER_DATE', 'POSTAL_CODE', 'QUANTITY', 'SALES'])
        self.assertEqual(out['POSTAL_CODE'].tolist(), ['42420', None])
        self.assertEqual(out['ORDER_DATE'].tolist(), ['2024-01-05', None])
        self.assertEqual(fixes, {'ORDER_DATE': 'DATE'})
        self.assertEqual(str(out['QUANTITY'].dtype), 'Int64')
        self.assertEqual(out['SALES'].tolist(), [1200.5, 3.0])

    def test_problems_stop_the_load_and_are_all_listed(self):
        df = pd.DataFrame({'Order Date': ['2024-01-05'], 'Quantity': [2.5], 'Sales': [1.0]})
        with self.assertRaises(ValueError) as ctx:
            DL.shape(df, self.F)
        self.assertIn('missing column POSTAL_CODE', str(ctx.exception))
        self.assertIn('QUANTITY: expected whole numbers', str(ctx.exception))


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.raw, self.ir, self.root = wb('Superstore_Tableau2024_3.twbx')

    def test_load_creates_a_tagged_table_that_binds(self):
        sf = FakeSnowflake()
        rec = DL.load(sf, self.raw, self.root, self.ir, 'Sales Commission', who='tester')
        self.assertEqual(rec['table'], 'WBR_DB.PIPELINE_DEMO.SALES_COMMISSION')
        self.assertEqual((rec['action'], rec['rows']), ('loaded', 41))
        comment = sf.tables[rec['table']]['comment']
        self.assertTrue(comment.startswith('v3load data_sha256='))          # release 23: data fingerprint first
        self.assertIn(' src_sha256=' + rec['sha256'], comment)
        self.assertIn('via=app by=tester', comment)

    def test_same_file_is_reused_without_loading_again(self):
        sf = FakeSnowflake()
        DL.load(sf, self.raw, self.root, self.ir, 'Sales Target')
        again = DL.load(sf, self.raw, self.root, self.ir, 'Sales Target')
        self.assertEqual(again['action'], 'reused')
        self.assertEqual(len(sf.writes), 1)
        loc = DL.locate(self.raw, self.root, self.ir, 'Sales Target')
        self.assertEqual(DL.find_loaded(sf, 'Sales Target', loc['sha256']), 'WBR_DB.PIPELINE_DEMO.SALES_TARGET')

    def test_existing_table_is_never_replaced_a_new_version_is_made(self):
        sf = FakeSnowflake()
        sf.tables['WBR_DB.PIPELINE_DEMO.SALES_TARGET'] = {'cols': {'X': 'VARCHAR'}, 'rows': 1, 'comment': ''}
        rec = DL.load(sf, self.raw, self.root, self.ir, 'Sales Target')
        self.assertEqual(rec['table'], 'WBR_DB.PIPELINE_DEMO.SALES_TARGET_V2')
        self.assertEqual(rec['action'], 'loaded as new version')
        self.assertEqual(sf.tables['WBR_DB.PIPELINE_DEMO.SALES_TARGET']['cols'], {'X': 'VARCHAR'})

    def test_date_columns_become_dates_so_the_binding_check_passes(self):
        sf = FakeSnowflake()
        rec = DL.load(sf, self.raw, self.root, self.ir, 'Sample - Superstore')
        orders = next(t['table'] for t in rec['table']['tables'].values() if t['caption'] == 'Orders')
        self.assertEqual(sf.tables[orders]['cols']['ORDER_DATE'], 'DATE')
        self.assertEqual(rec['rows'], 10194)


class EntityTests(unittest.TestCase):
    """Release 21b-A: one table per logical table, shared across workbooks, modelled from the authored keys."""
    def setUp(self):
        self.raw, self.ir, self.root = wb('Superstore_Tableau2024_3.twbx')

    def test_each_logical_table_loads_once_and_the_plan_is_a_loaded_relationship_model(self):
        from v3_model_binding import compile_query, validate
        sf = FakeSnowflake()
        rec = DL.load(sf, self.raw, self.root, self.ir, 'Sample - Superstore', who='t')
        plan = rec['table']
        self.assertEqual(plan['source'], 'loaded')
        self.assertEqual(sorted(t['table'] for t in plan['tables'].values()),
                         ['WBR_DB.PIPELINE_DEMO.ORDERS', 'WBR_DB.PIPELINE_DEMO.PEOPLE', 'WBR_DB.PIPELINE_DEMO.RETURNS'])
        self.assertIn('LEFT JOIN WBR_DB.PIPELINE_DEMO.PEOPLE', compile_query(plan))
        self.assertIn('data_sha256=', sf.tables['WBR_DB.PIPELINE_DEMO.ORDERS']['comment'])
        self.assertEqual(validate(sf, plan)['fact_rows'], 10194)     # loaded plans skip the authored-account rule

    def test_same_data_from_another_workbook_reuses_the_tables(self):
        sf = FakeSnowflake()
        DL.load(sf, self.raw, self.root, self.ir, 'Sample - Superstore')
        n = len(sf.writes)
        loc = DL.locate(self.raw, self.root, self.ir, 'Sample - Superstore')
        frames = __import__('v3_data_entities').frames_for(self.raw, loc)
        other = dict(loc, sha256='f' * 64)                          # same data, different source file
        rec = __import__('v3_data_entities').load(sf, self.raw, self.root, self.ir, 'Sample - Superstore', other,
                                                  frames, False)
        self.assertEqual(len(sf.writes), n)
        self.assertIn('reused ORDERS', rec['action'])
        self.assertIn('src_sha256=' + 'f' * 64, sf.tables['WBR_DB.PIPELINE_DEMO.ORDERS']['comment'])

    def test_hosted_app_finds_the_model_by_source_fingerprint(self):
        import v3_data_entities as ENT
        sf = FakeSnowflake()
        DL.load(sf, self.raw, self.root, self.ir, 'Sample - Superstore')
        loc = DL.locate(self.raw, self.root, self.ir, 'Sample - Superstore')
        plan = ENT.find_model(sf, self.root, self.ir, 'Sample - Superstore', loc['sha256'])
        self.assertEqual(plan['source'], 'loaded')
        self.assertIsNone(ENT.find_model(FakeSnowflake(), self.root, self.ir, 'Sample - Superstore', loc['sha256']))

    def test_unmatched_fact_rows_are_counted_not_refused(self):
        import v3_data_entities as ENT
        sf = FakeSnowflake()
        rec = DL.load(sf, self.raw, self.root, self.ir, 'Sample - Superstore')
        sf.unmatched = 9194
        notes = ENT.check(sf, rec['table'])['notes']
        self.assertTrue(any('9,194 Orders rows have no' in n for n in notes))

    @unittest.skipUnless(__import__('importlib').util.find_spec('tableauhyperapi'), 'tableauhyperapi not installed')
    def test_excel_and_hyper_copies_of_the_same_data_share_tables_and_keep_dates(self):
        import importlib.util
        import v3_data_entities as ENT
        spec = importlib.util.spec_from_file_location('lwd', ROOT / 'tools' / 'load_workbook_data.py')
        lwd = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lwd)
        shas = []
        for name, extract in (('Superstore_Tableau2024_3.twbx', False), ('R1_Test_Upload_-_Superstore.twbx', True)):
            raw, ir, root = wb(name)
            loc = DL.locate(raw, root, ir, 'Sample - Superstore')
            frames = ENT.frames_for(raw, loc, lwd.decode_extract(raw, loc, root) if extract else None)
            shaped, _n, _o, _d = ENT.shape(root, ir, loc, frames, extract)
            shas.append({e['caption']: e['sha'] for e in shaped.values()})
            orders = next(e['frame'] for e in shaped.values() if e['caption'] == 'Orders')
            self.assertEqual(orders['ORDER_DATE'].isna().sum(), 0)        # hyper dates are real dates
        self.assertEqual(shas[0], shas[1])


class BindingCheckTests(unittest.TestCase):
    def test_engine_names_accepted_and_generated_fields_skipped(self):
        sf = FakeSnowflake()
        sf.tables['DB.S.T'] = {'cols': {'ORDER_DATE': 'DATE', 'SALES': 'FLOAT'}, 'rows': 1, 'comment': ''}
        fields = [{'physical': 'Order Date', 'datatype': 'date', 'internal': 'Order Date'},
                  {'physical': 'Sales', 'datatype': 'real', 'internal': 'Sales'},
                  {'physical': ':Measure Names', 'datatype': 'string', 'internal': ':Measure Names'}]
        self.assertEqual(DL.check_loaded(sf, 'DB.S.T', fields), 'DB.S.T')            # loaded: engine names
        with self.assertRaises(ValueError):
            validate_table(sf, 'DB.S.T', fields)          # authored Snowflake tables: exact names, unchanged
        sf.tables['DB.S.T']['cols']['SALES'] = 'VARCHAR(10)'
        with self.assertRaises(ValueError):
            DL.check_loaded(sf, 'DB.S.T', fields)           # a wrong type is still refused


class ConsoleTests(unittest.TestCase):
    def _run(self):
        raw, ir, _root = wb('Superstore_Tableau2024_3.twbx')
        caps = ['Sales Target', 'Sales Commission', 'Sample - Superstore']
        return {'result': {'ir': ir, 'digest': 'd' * 64, 'source_name': 'Superstore_Tableau2024_3.twbx'},
                '_raw': raw, 'bindings': {c: None for c in caps}, 'missing': list(caps),
                'binding_rows': [{'Datasource': c} for c in caps], 'states': {'Data model': 'review'}, 'events': []}

    def test_plans_then_approved_loads_bind_and_record_the_approval(self):
        import v3_console as VC
        import v3_store
        sf, run = FakeSnowflake(), self._run()
        with patch.object(VC, '_runtime_sf', return_value=True), \
                patch.object(VC.backend, '_active_session', return_value=sf), \
                patch.object(v3_store, 'approve', return_value='A1') as approve, \
                patch.object(VC, '_auto_semantic', return_value=('done', 'Semantic View created')) as sv, \
                patch.object(VC, '_build', return_value=('done', 'Candidate package', {'data': b'', 'build': 'x'})):
            self.assertEqual(VC._auto_loaded(run), [])
            self.assertEqual(sf.writes, [])                                    # planning writes nothing
            self.assertTrue(all(p['ok'] for p in run['load_plans'].values()))
            self.assertIn('3 can be loaded from the workbook after approval', VC._missing_summary(run))
            done, errors = VC._approve_loads(run, list(run['missing']))
        self.assertEqual(errors, [])
        self.assertEqual(len(done), 3)
        approve.assert_called_once()
        self.assertEqual(approve.call_args[0][1], 'load_data')
        self.assertEqual(run['missing'], [])
        self.assertEqual(run['states']['Data model'], 'done')
        sv.assert_called_once()                       # the shared Semantic View follows the last binding
        self.assertEqual(run['bindings']['Sales Commission'], 'WBR_DB.PIPELINE_DEMO.SALES_COMMISSION')
        self.assertEqual(len(run['data_loads']), 3)

    def test_second_run_binds_by_fingerprint_without_loading(self):
        import v3_console as VC
        import v3_store
        sf = FakeSnowflake()
        first = self._run()
        with patch.object(VC, '_runtime_sf', return_value=True), \
                patch.object(VC.backend, '_active_session', return_value=sf), \
                patch.object(v3_store, 'approve', return_value='A1'), \
                patch.object(VC, '_build', return_value=('done', 'x', None)):
            VC._auto_loaded(first)
            VC._approve_loads(first, list(first['missing']))
            writes = len(sf.writes)
            second = self._run()
            found = VC._auto_loaded(second)
        self.assertEqual(sorted(found), sorted(['Sales Target', 'Sales Commission', 'Sample - Superstore']))
        self.assertEqual(len(sf.writes), writes)
        self.assertEqual(second['missing'], [])

    def test_same_data_loaded_from_another_file_binds_without_a_load_card(self):
        """Release 23: data the app can read itself is matched by its data fingerprint before any load card."""
        import v3_console as VC
        sf = FakeSnowflake()
        raw, ir, root = wb('Superstore_Tableau2024_3.twbx')
        real = DL.locate
        for n, cap in enumerate(('Sales Target', 'Sales Commission', 'Sample - Superstore')):
            with patch.object(DL, 'locate', side_effect=lambda *a, **k: dict(real(*a, **k), sha256=str(n + 1) * 64)):
                DL.load(sf, raw, root, ir, cap)                 # loaded earlier from "another file"
        writes, seen = len(sf.writes), len(sf.log)
        run = self._run()
        with patch.object(VC, '_runtime_sf', return_value=True), \
                patch.object(VC.backend, '_active_session', return_value=sf), \
                patch.object(VC, '_auto_semantic', return_value=('done', 'Semantic View reused')), \
                patch.object(VC, '_build', return_value=('done', 'x', None)):
            found = VC._auto_loaded(run)
        self.assertEqual(sorted(found), ['Sales Commission', 'Sales Target', 'Sample - Superstore'])
        self.assertEqual(len(sf.writes), writes)
        self.assertEqual(run['missing'], [])
        self.assertFalse(run.get('load_plans'))
        rows = {r['Datasource']: r for r in run['binding_rows']}
        self.assertIn('same data', rows['Sales Target']['Binding'])
        self.assertEqual(rows['Sample - Superstore']['Binding'], 'Relationship model over loaded tables (same data)')
        self.assertFalse([q for q in sf.log[seen:] if q.startswith(('COMMENT ON', 'CREATE', 'ALTER', 'INSERT', 'UPDATE'))])

    def test_one_file_feeding_two_datasources_never_cross_binds(self):
        """Two datasources reading one Excel file share a source fingerprint: each binds only its own table."""
        sf = FakeSnowflake()
        sf.tables['WBR_DB.PIPELINE_DEMO.SALES_TARGET'] = {'cols': {}, 'rows': 1, 'comment': 'v3load src_sha256=' + 'a' * 64}
        self.assertIsNone(DL.find_loaded(sf, 'Sales Commission', 'a' * 64))
        self.assertEqual(DL.find_loaded(sf, 'Sales Target', 'a' * 64), 'WBR_DB.PIPELINE_DEMO.SALES_TARGET')
        sf.tables['WBR_DB.PIPELINE_DEMO.ORDERS_V2'] = {'cols': {}, 'rows': 1,
                                                      'comment': 'v3load data_sha256=x src_sha256=' + 'b' * 64 + '@SALES_COMMISSION'}
        self.assertEqual(DL.find_loaded(sf, 'Sales Commission', 'b' * 64), 'WBR_DB.PIPELINE_DEMO.ORDERS_V2')
        self.assertIsNone(DL.find_loaded(sf, 'Sales Target', 'b' * 64))

    def test_nothing_loaded_means_load_cards_as_before(self):
        sf = FakeSnowflake()
        raw, ir, root = wb('Superstore_Tableau2024_3.twbx')
        self.assertIsNone(DL.find_reusable(sf, raw, root, ir, 'Sales Target'))
        self.assertIsNone(DL.find_reusable(sf, raw, root, ir, 'Sample - Superstore'))
        r1, irr, rootr = wb('R1_Test_Upload_-_Superstore.twbx')
        self.assertIsNone(DL.find_reusable(sf, r1, rootr, irr, 'Sales Target'))     # an extract: the app cannot read it
        self.assertEqual(sf.writes, [])

    def test_saved_runs_do_not_keep_the_workbook_bytes(self):
        import v3_store
        self.assertIn('_raw', v3_store.NOT_IN_SNAPSHOT)



class ReuseByDataTests(unittest.TestCase):
    """Release 23 (live finding 31 Oct): R1 and R1-V2 hold the same data in .hyper files that differ in bytes;
    each load made a new _V3/_V4 table. Single tables are now recognised by their data, like 21b tables."""
    def setUp(self):
        self.raw, self.ir, self.root = wb('Superstore_Tableau2024_3.twbx')

    def _other_file(self, sha):
        real = DL.locate
        return patch.object(DL, 'locate', side_effect=lambda *a, **k: dict(real(*a, **k), sha256=sha))

    def test_same_data_from_another_file_reuses_the_table(self):
        sf = FakeSnowflake()
        first = DL.load(sf, self.raw, self.root, self.ir, 'Sales Target')
        self.assertIn('data_sha256=', sf.tables[first['table']]['comment'])
        with self._other_file('e' * 64):
            again = DL.load(sf, self.raw, self.root, self.ir, 'Sales Target')
        self.assertEqual(again['table'], first['table'])
        self.assertEqual(again['action'], 'reused SALES_TARGET (same data)')
        self.assertEqual(len(sf.writes), 1)
        comment = sf.tables[first['table']]['comment']
        self.assertIn('src_sha256=' + first['sha256'], comment)
        self.assertIn('src_sha256=' + 'e' * 64, comment)
        self.assertEqual(DL.find_loaded(sf, 'Sales Target', 'e' * 64), first['table'])   # the app finds it too

    def test_same_data_is_found_under_any_name_and_the_own_name_is_preferred(self):
        sf = FakeSnowflake()
        sf.tables['WBR_DB.PIPELINE_DEMO.OTHER'] = {'cols': {}, 'rows': 1, 'comment': 'v3load data_sha256=abc src_sha256=1'}
        self.assertEqual(DL.find_by_data(sf, 'abc', 'Sales Target')[0], 'WBR_DB.PIPELINE_DEMO.OTHER')
        sf.tables['WBR_DB.PIPELINE_DEMO.SALES_TARGET_V2'] = {'cols': {}, 'rows': 1, 'comment': 'v3load data_sha256=abc'}
        self.assertEqual(DL.find_by_data(sf, 'abc', 'Sales Target')[0], 'WBR_DB.PIPELINE_DEMO.SALES_TARGET_V2')
        self.assertIsNone(DL.find_by_data(sf, 'ab', 'Sales Target'))                       # whole fingerprint only
        sf.tables['WBR_DB.PIPELINE_DEMO.NOT_OURS'] = {'cols': {}, 'rows': 1, 'comment': 'data_sha256=zzz'}
        self.assertIsNone(DL.find_by_data(sf, 'zzz'))                                       # untagged tables ignored

    def test_different_data_still_gets_a_new_version(self):
        sf = FakeSnowflake()
        sf.tables['WBR_DB.PIPELINE_DEMO.SALES_TARGET'] = {'cols': {'X': 'VARCHAR'}, 'rows': 1,
                                                         'comment': 'v3load data_sha256=' + '0' * 64}
        rec = DL.load(sf, self.raw, self.root, self.ir, 'Sales Target')
        self.assertEqual(rec['table'], 'WBR_DB.PIPELINE_DEMO.SALES_TARGET_V2')



if __name__ == '__main__':
    unittest.main()
