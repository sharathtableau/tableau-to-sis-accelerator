"""Release 21b-B: Cortex proposes the existing Snowflake table for a datasource; code checks; a person approves."""
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import v3_ai
import v3_cortex_mapping as MAP

FIELDS = [{'physical': 'Order ID', 'datatype': 'string', 'caption': 'Order ID', 'internal': 'Order ID'},
          {'physical': 'Order Date', 'datatype': 'date', 'caption': 'Order Date', 'internal': 'Order Date'},
          {'physical': 'Sales', 'datatype': 'real', 'caption': 'Sales', 'internal': 'Sales'}]

CATALOG = [('SANDBOX', 'ORDERS_ARCHIVE', 'ORDER_NUMBER', 'TEXT', None),
           ('SANDBOX', 'ORDERS_ARCHIVE', 'ORDERED_ON', 'DATE', None),
           ('SANDBOX', 'ORDERS_ARCHIVE', 'REVENUE', 'NUMBER', 2),
           ('SANDBOX', 'ORDERS_ARCHIVE', 'ORDER_DATE', 'DATE', None),
           ('PIPELINE_DEMO', 'SALES_FACT', 'ORDER_ID', 'TEXT', None),
           ('PIPELINE_DEMO', 'SALES_FACT', 'ORDER_DATE', 'DATE', None),
           ('PIPELINE_DEMO', 'SALES_FACT', 'SALES', 'FLOAT', None),
           ('PIPELINE_DEMO', 'WEATHER', 'CITY', 'TEXT', None),
           ('PUBLIC', 'mixedCase', 'ORDER_ID', 'TEXT', None)]


class _Row(dict):
    def as_dict(self):
        return dict(self)

    def __getitem__(self, k):
        return list(self.values())[k] if isinstance(k, int) else dict.__getitem__(self, k)


class FakeSF:
    def __init__(self):
        self.log, self.views = [], {}
        self.cols = {}
        for sch, tab, col, typ, scale in CATALOG:
            self.cols.setdefault(f'WBR_DB.{sch}.{tab}', {})[col] = MAP._sf_type(typ, scale)

    def sql(self, text, params=None):
        self.log.append(text)
        fake = self

        class R:
            def collect(self_inner):
                t = text.strip()
                if 'INFORMATION_SCHEMA.COLUMNS' in t:
                    return [_Row({'TABLE_SCHEMA': s, 'TABLE_NAME': n, 'COLUMN_NAME': c, 'DATA_TYPE': d, 'NUMERIC_SCALE': sc})
                            for s, n, c, d, sc in CATALOG]
                if 'INFORMATION_SCHEMA.SCHEMATA' in t:
                    return [_Row({'N': 1})]
                m = re.match(r'SELECT COUNT\(\*\) AS N FROM (\S+)$', t)
                if m:
                    return [_Row({'N': 9994})]
                m = re.match(r"SHOW VIEWS LIKE '([^%]+)%'", t)
                if m:
                    return [_Row({'name': n.split('.')[-1], 'comment': v['comment']}) for n, v in fake.views.items()
                            if n.split('.')[-1].startswith(m.group(1))]
                if t.startswith('SHOW TABLES LIKE'):
                    return []
                m = re.match(r"CREATE VIEW (\S+) COMMENT = '(.*?)' AS SELECT (.*) FROM (\S+)$", t, re.S)
                if m:
                    assert m.group(1) not in fake.views
                    src = fake.cols[m.group(4)]
                    cols = {}
                    for part in m.group(3).split(', '):
                        col, alias = re.match(r'"(.+)" AS (\S+)', part).groups()
                        cols[alias] = src[col]
                    fake.views[m.group(1)] = {'comment': m.group(2), 'cols': cols}
                    return []
                m = re.match(r'DESCRIBE (TABLE|VIEW) (\S+)', t)
                if m:
                    if m.group(1) == 'TABLE' and m.group(2) in fake.views:
                        raise RuntimeError('not a table')
                    cols = fake.views[m.group(2)]['cols'] if m.group(1) == 'VIEW' else fake.cols[m.group(2)]
                    return [_Row({'name': c, 'type': ty, 'kind': 'COLUMN'}) for c, ty in cols.items()]
                if t.endswith('LIMIT 0'):
                    return []
                raise AssertionError('unexpected SQL: ' + t[:100])
        return R()


GOOD = {'table': 'WBR_DB.SANDBOX.ORDERS_ARCHIVE', 'reason': 'Same orders, renamed columns',
        'mappings': [{'field': 'Order ID', 'column': 'ORDER_NUMBER'}, {'field': 'Order Date', 'column': 'ORDERED_ON'},
                     {'field': 'Sales', 'column': 'REVENUE'}]}


class CandidateTests(unittest.TestCase):
    def test_catalog_skips_quoted_objects_and_ranks_by_matching_columns(self):
        cat = MAP.catalog(FakeSF())
        self.assertNotIn('WBR_DB.PUBLIC.mixedCase', cat)
        self.assertEqual(cat['WBR_DB.SANDBOX.ORDERS_ARCHIVE'][2]['type'], 'NUMBER(38,2)')
        ranked = list(MAP.rank(FIELDS, cat))
        self.assertEqual(ranked[0], 'WBR_DB.PIPELINE_DEMO.SALES_FACT')        # 3 exact matches
        self.assertIn('WBR_DB.SANDBOX.ORDERS_ARCHIVE', ranked)
        self.assertNotIn('WBR_DB.PIPELINE_DEMO.WEATHER', ranked)

    def test_cortex_can_only_answer_from_the_closed_lists(self):
        cands = MAP.rank(FIELDS, MAP.catalog(FakeSF()))
        sch = MAP._schema(FIELDS, cands)
        self.assertEqual(sch['properties']['table']['enum'][-1], 'NONE')
        self.assertEqual(set(sch['properties']['table']['enum'][:-1]), set(cands))
        self.assertIn('ORDER_NUMBER', sch['properties']['mappings']['items']['properties']['column']['enum'])
        self.assertFalse(sch['additionalProperties'])


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.sf = FakeSF()
        self.cands = MAP.rank(FIELDS, MAP.catalog(self.sf))

    def test_renamed_columns_pass_but_need_a_mapping_view(self):
        rec = MAP.check(self.sf, GOOD, FIELDS, self.cands)
        self.assertTrue(rec['ok'])
        self.assertFalse(rec['identity'])
        self.assertEqual(rec['rows'], 9994)

    def test_same_names_bind_the_table_as_it_is(self):
        prop = {'table': 'WBR_DB.PIPELINE_DEMO.SALES_FACT', 'reason': '', 'mappings': [
            {'field': 'Order ID', 'column': 'ORDER_ID'}, {'field': 'Order Date', 'column': 'ORDER_DATE'},
            {'field': 'Sales', 'column': 'SALES'}]}
        rec = MAP.check(self.sf, prop, FIELDS, self.cands)
        self.assertTrue(rec['ok'] and rec['identity'])
        fqn, action = MAP.apply(self.sf, 'Orders', rec, FIELDS)
        self.assertEqual((fqn, action), ('WBR_DB.PIPELINE_DEMO.SALES_FACT', 'bound to the table as it is'))
        self.assertFalse(any(q.startswith('CREATE') for q in self.sf.log))

    def test_every_wrong_answer_is_refused(self):
        cases = [dict(GOOD, table='WBR_DB.PIPELINE_DEMO.WEATHER'),                          # not a candidate
                 dict(GOOD, table='NONE'),
                 dict(GOOD, mappings=GOOD['mappings'][:2]),                                  # Sales unmapped
                 dict(GOOD, mappings=[{'field': 'Order ID', 'column': 'ORDER_NUMBER'},
                                      {'field': 'Order Date', 'column': 'REVENUE'},          # number for a date
                                      {'field': 'Sales', 'column': 'REVENUE'}]),
                 dict(GOOD, mappings=[{'field': 'Order ID', 'column': 'ORDER_ID'},           # not in this table
                                      {'field': 'Order Date', 'column': 'ORDERED_ON'},
                                      {'field': 'Sales', 'column': 'REVENUE'}])]
        for prop in cases:
            rec = MAP.check(self.sf, prop, FIELDS, self.cands)
            self.assertFalse(rec['ok'], prop)
            with self.assertRaises(ValueError):
                MAP.apply(self.sf, 'Orders', rec, FIELDS)
        self.assertFalse(any(q.startswith('CREATE') for q in self.sf.log))


class ApplyTests(unittest.TestCase):
    def test_mapping_view_is_code_built_never_replaces_and_is_reused(self):
        sf = FakeSF()
        cands = MAP.rank(FIELDS, MAP.catalog(sf))
        rec = MAP.check(sf, GOOD, FIELDS, cands)
        fqn, action = MAP.apply(sf, 'Orders', rec, FIELDS, who='tester')
        self.assertEqual((fqn, action), ('WBR_DB.PIPELINE_DEMO.ORDERS_MAP', 'created the mapping view'))
        ddl = next(q for q in sf.log if q.startswith('CREATE'))
        self.assertTrue(ddl.startswith('CREATE VIEW '))
        self.assertNotIn('OR REPLACE', ddl)
        self.assertIn('"ORDER_NUMBER" AS ORDER_ID', ddl)
        self.assertIn('FROM WBR_DB.SANDBOX.ORDERS_ARCHIVE', ddl)
        again = MAP.apply(sf, 'Orders', rec, FIELDS)
        self.assertEqual(again[1], 'reused the mapping view')
        other = MAP.check(sf, dict(GOOD, mappings=[dict(m) for m in GOOD['mappings']]), FIELDS, cands)
        other['pairs'][1]['Column'] = 'ORDER_DATE'                                        # a different mapping
        self.assertEqual(MAP.apply(sf, 'Orders', other, FIELDS)[0], 'WBR_DB.PIPELINE_DEMO.ORDERS_MAP_V2')


class SuggestTests(unittest.TestCase):
    def test_cortex_failure_shows_no_error_text(self):
        def boom(*a, **k):
            raise v3_ai.CortexUnavailable('Cortex request failed (RuntimeError).')
        with patch.object(v3_ai, 'call', side_effect=boom):
            rec = MAP.suggest(FakeSF(), {}, 'Orders', FIELDS)
        self.assertFalse(rec['ok'])
        self.assertEqual(rec['verdict'], 'Cortex request failed (RuntimeError).')

    def test_no_candidates_means_no_cortex_call(self):
        with patch.object(v3_ai, 'call') as call:
            rec = MAP.suggest(FakeSF(), {}, 'Weather', [{'physical': 'Wind Speed', 'datatype': 'real'}])
        call.assert_not_called()
        self.assertIn('No table in WBR_DB', rec['verdict'])


class ConsoleTests(unittest.TestCase):
    def _run(self):
        ir = {'field_scopes': {'ds1': {'caption': 'Orders'}},
              'fields': {f['physical']: dict(f, datasource_id='ds1', is_calculation=False) for f in FIELDS}}
        return {'result': {'ir': ir, 'digest': 'e' * 64}, 'bindings': {'Orders': None}, 'missing': ['Orders'],
                'binding_rows': [{'Datasource': 'Orders'}], 'states': {'Data model': 'review'}, 'events': []}

    def test_ask_check_approve_bind_and_record(self):
        import v3_console as VC
        import v3_store
        sf, run = FakeSF(), self._run()
        with patch.object(VC.backend, '_active_session', return_value=sf), \
                patch.object(v3_ai, 'call', return_value=GOOD), \
                patch.object(v3_store, 'approve', return_value='A9') as approve, \
                patch.object(VC, '_build', return_value=('done', 'Candidate', None)), \
                patch.object(VC.st, 'spinner'):
            VC._ask_mappings(run, ['Orders'])
            self.assertTrue(run['cortex_mappings']['Orders']['ok'])
            self.assertEqual(run['missing'], ['Orders'])                       # nothing bound before approval
            VC._apply_mapping(run, 'Orders')
        self.assertEqual(approve.call_args[0][1], 'map_table')
        self.assertEqual(run['bindings']['Orders'], 'WBR_DB.PIPELINE_DEMO.ORDERS_MAP')
        self.assertEqual(run['missing'], [])
        row = next(r for r in v3_store.proposal_rows(run) if r['KIND'] == 'table_mapping')
        self.assertTrue(row['PASSED_CODE_CHECK'] and row['CHANGES_APP'])


if __name__ == '__main__':
    unittest.main()
