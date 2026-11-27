"""Release 22a (SL03): rationalisation — expressions, groups, code-run comparison, one class per metric, Cortex
checks, storage, report, page, laptop tool, and the catalogue bindings it relies on."""
import collections
import io
import json
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

import pandas as pd

import v3_catalog as C
import v3_rationalise as R
import v3_store as ST

CLOUD = ROOT / 'Workbooks' / 'cloud_20261010'
HAVE_CLOUD = (CLOUD / 'Superstore_Tableau2024_3.twbx').exists()


def harvest():
    h = C.Harvest(None, 'test', 'cloud')
    for f in C.folder_workbooks(str(CLOUD)):
        C.harvest_file(h, str(CLOUD), f)
    return h


def frames(h, extra=None):
    parts = collections.defaultdict(list)
    wbs = []
    for r in h.results:
        if r['STATUS'] != 'harvested':
            continue
        wbs.append({'WORKBOOK_KEY': r['WORKBOOK_KEY'], 'WORKBOOK_NAME': r['WORKBOOK_NAME'], 'TOTAL_VIEWS': 5,
                    'HARVEST_ID': r['HARVEST_ID'], 'SCOPE': 'cloud'})
        x = {'WORKBOOK_KEY': r['WORKBOOK_KEY'], 'HARVEST_ID': r['HARVEST_ID'], 'WORKBOOK_NAME': r['WORKBOOK_NAME'],
             'SCOPE': 'cloud'}
        for name, part in (('metrics', 'metrics'), ('usage', 'usage'), ('calculations', 'calculations'),
                           ('fields', 'fields'), ('bindings', 'bindings')):
            parts[name] += [dict(row, **x) for row in r['_parsed'][part]]
    for k, rows in (extra or {}).items():
        parts[k] += rows
    out = {k: pd.DataFrame(v) for k, v in parts.items()}
    out['workbooks'] = pd.DataFrame(wbs)
    out['descriptions'] = pd.DataFrame(columns=['METRIC_FP', 'BUSINESS_NAME', 'INTENT'])
    return out


class Sess:
    """Fake Snowflake: catalogue/loaded-table metadata, a handler for the comparison query, captured inserts."""
    def __init__(self, handler=None, columns=None, ready=True):
        self.handler, self.ready = handler, ready
        self.columns = columns if columns is not None else {
            'ORDERS': ['ORDER_ID', 'ORDER_DATE', 'SALES', 'PROFIT', 'QUANTITY', 'DISCOUNT', 'REGION', 'CUSTOMER_NAME',
                       'CATEGORY']}
        self.inserts, self.queries = collections.defaultdict(list), []

    def sql(self, q, params=None):
        return Res(self, q, params or [])


class Res:
    def __init__(self, s, q, p):
        self.s, self.q, self.p = s, q, p

    def collect(self):
        s, q = self.s, self.q
        if q.startswith('INSERT INTO'):
            t = re.match(r'INSERT INTO [\w.]+\.(\w+) ', q).group(1)
            s.inserts[t] += [json.loads(x) for x in self.p]
            return []
        if 'INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = ? AND TABLE_NAME IN' in q:
            return [(len(self.p) - 1 if s.ready else 0,)]
        if 'INFORMATION_SCHEMA.TABLES' in q:
            return [(t,) for t in s.columns]
        if 'INFORMATION_SCHEMA.COLUMNS' in q:
            types = {'ORDER_DATE': 'DATE', 'REGION': 'TEXT', 'CUSTOMER_NAME': 'TEXT', 'CATEGORY': 'TEXT'}
            return [('PIPELINE_DEMO', t, c, types.get(c, 'NUMBER')) for t, cols in s.columns.items() for c in cols]
        if q.startswith('SELECT * FROM (SELECT \'total\''):
            s.queries.append(q)
            return s.handler(q) if s.handler else []
        return []


def rows_for(q, differ=False):
    n = len(re.findall(r' AS M\d+', q.split(' UNION ALL ')[0]))
    base = [('total', None), ('month', '2024-01'), ('month', '2024-02'), ('dimension', 'East'), ('dimension', 'West')]
    out = []
    for i, (g, k) in enumerate(base):
        vals = [0.12 + i / 100] * n
        if differ and n > 1:
            vals[1] = vals[0] * 2
        out.append((g, k, *vals))
    return out


def variant_rows(h):
    """A second 'Profit Ratio' with a different formula, in another workbook (same name, different formula)."""
    r = next(r for r in h.results if r['WORKBOOK_NAME'] == 'Sales_Dashboard_Ver_01')
    x = {'WORKBOOK_KEY': r['WORKBOOK_KEY'], 'HARVEST_ID': r['HARVEST_ID'], 'WORKBOOK_NAME': r['WORKBOOK_NAME'], 'SCOPE': 'cloud'}
    text = 'SUM(COL(PROFIT))/SUM(COL(DISCOUNT))'
    fp = C.fp(text)
    metric = dict(x, METRIC_FP=fp, NAME='Profit Ratio', ORIGIN_KIND='calculation', KIND='aggregate',
                  EXPRESSION='SUM([Profit])/SUM([Discount])', NORMALISED=text, DATASOURCE_ID='dsx', DATASOURCE='X',
                  SOURCE_KEY='k', FIELD='Calc_PR2')
    calc = dict(x, DATASOURCE_ID='dsx', FIELD='Calc_PR2', KIND='aggregate', TRANSLATED_SQL='sum(PROFIT)/NULLIF(sum(DISCOUNT), 0)')
    use = dict(x, WORKSHEET='KPI', DATASOURCE_ID='dsx', FIELD='Calc_PR2', SHELF='text', METRIC_FP=fp)
    return fp, {'metrics': [metric], 'calculations': [calc], 'usage': [use]}


class ExpressionTests(unittest.TestCase):
    calcs = {('w', 'd', 'C1'): {'KIND': 'aggregate', 'TRANSLATED_SQL': 'sum(PROFIT)/NULLIF(sum(SALES), 0)'},
             ('w', 'd', 'C2'): {'KIND': 'row', 'TRANSLATED_SQL': 'SALES*2'},
             ('w', 'd', 'C3'): {'KIND': 'aggregate', 'TRANSLATED_SQL': 'SUM(SALES)/__PARAM_QUOTA__'},
             ('w', 'd', 'C4'): {'KIND': 'aggregate', 'TRANSLATED_SQL': 'SUM(SALES); DROP TABLE X'}}
    fields = {('w', 'd', 'Sales'): {'PHYSICAL': 'Sales'}, ('w', 'd', 'Customer Name'): {'PHYSICAL': 'Customer Name'}}

    def m(self, **k):
        return dict({'WORKBOOK_KEY': 'w', 'DATASOURCE_ID': 'd', 'KIND': 'aggregate', 'ORIGIN_KIND': 'sheet aggregation'}, **k)

    def test_sheet_aggregations_and_calculations(self):
        e = lambda **k: R.expression(self.m(**k), self.calcs, self.fields)
        self.assertEqual(e(FIELD='Sales', NORMALISED='SUM(COL(SALES))'), ('SUM(SALES)', None))
        self.assertEqual(e(FIELD='Customer Name', NORMALISED='COUNTD(COL(CUSTOMER_NAME))'), ('COUNT(DISTINCT CUSTOMER_NAME)', None))
        self.assertEqual(e(FIELD='C2', NORMALISED='AVG(COL(SALES)*2)'), ('AVG((SALES*2))', None))
        self.assertEqual(e(FIELD='C1', ORIGIN_KIND='calculation', NORMALISED='x')[0], 'sum(PROFIT)/NULLIF(sum(SALES), 0)')

    def test_what_is_not_run(self):
        e = lambda **k: R.expression(self.m(**k), self.calcs, self.fields)[1]
        self.assertEqual(e(FIELD='C3', ORIGIN_KIND='calculation', NORMALISED='x'), 'uses a parameter')
        self.assertEqual(e(FIELD='C4', ORIGIN_KIND='calculation', NORMALISED='x'), 'expression not allowed')
        self.assertIn('level of detail', e(FIELD='C1', KIND='lod', ORIGIN_KIND='calculation', NORMALISED='x'))
        self.assertIn('ATTR', e(FIELD='Sales', NORMALISED='ATTR(COL(SALES))'))
        self.assertEqual(e(FIELD='Missing', ORIGIN_KIND='calculation', NORMALISED='x'), 'not translated')
        self.assertIn('level of detail', R._blocked('SUM(X) OVER (PARTITION BY Y)'))
        self.assertIsNone(R._blocked("CASE WHEN REGION = 'Select' THEN 1 END"))       # a word inside a string is fine


class CompareTests(unittest.TestCase):
    def test_tolerance_strings_and_nulls(self):
        self.assertTrue(R.same(1.0, 1.0 + 1e-12))
        self.assertTrue(R.same(1e9, 1e9 + 0.5))
        self.assertFalse(R.same(1.0, 1.01))
        self.assertTrue(R.same(None, None))
        self.assertFalse(R.same(None, 0))
        self.assertTrue(R.same('A', 'A'))

    def test_clusters(self):
        rows = [('total', None, 1, 1, 2), ('month', 'x', 3, 3, 3)]
        clusters, diffs = R.compare(rows, 3)
        self.assertEqual(clusters, [[0, 1], [2]])
        self.assertEqual(len(diffs), 1)
        self.assertEqual(R.compare([('total', None, 5, 5)], 2), ([[0, 1]], []))


class GroupAndClassTests(unittest.TestCase):
    def test_name_groups_use_calculations_only(self):
        ms = [{'ORIGIN_KIND': 'calculation', 'NAME': 'Margin', 'METRIC_FP': 'a'},
              {'ORIGIN_KIND': 'calculation', 'NAME': ' margin ', 'METRIC_FP': 'b'},
              {'ORIGIN_KIND': 'calculation', 'NAME': 'Margin', 'METRIC_FP': 'a'},
              {'ORIGIN_KIND': 'sheet aggregation', 'NAME': 'Sales', 'METRIC_FP': 'c'},
              {'ORIGIN_KIND': 'sheet aggregation', 'NAME': 'Sales', 'METRIC_FP': 'd'}]
        g = R.name_groups(ms)
        self.assertEqual([x['members'] for x in g], [['a', 'b']])

    def test_one_class_per_metric_with_precedence(self):
        info = {fp: {'sheets': s, 'no_views': nv, 'workbooks': w} for fp, s, nv, w in
                [('a', 2, False, 1), ('b', 1, False, 1), ('c', 0, False, 3), ('d', 3, True, 2), ('e', 4, False, 2),
                 ('f', 1, False, 1), ('g', 1, False, 1), ('h', 1, False, 1)]}
        groups = [{'id': 'G1', 'source': 'same name', 'verdict': 'conflicting', 'members': ['a', 'b']},
                  {'id': 'G2', 'source': 'cortex', 'verdict': 'differs', 'members': ['f', 'e']},
                  {'id': 'G3', 'source': 'cortex', 'verdict': 'equivalent', 'members': ['g', 'h']},
                  {'id': 'G4', 'source': 'cortex', 'verdict': 'not_testable', 'members': ['f', 'g'], 'reason': 'x'}]
        cls = {fp: v[0] for fp, v in R.classify(info, groups).items()}
        self.assertEqual(cls, {'a': 'conflicting', 'b': 'conflicting', 'c': 'stale', 'd': 'stale', 'e': 'identical',
                               'f': 'unique', 'g': 'equivalent', 'h': 'equivalent'})
        self.assertEqual(set(cls), set(info))


class RelationTests(unittest.TestCase):
    def test_choose_prefers_a_relation_bound_to_a_member(self):
        pool = [{'kind': 'loaded', 'label': 'L', 'columns': {'A': 'NUMBER', 'B': 'NUMBER'}, 'owners': set()},
                {'kind': 'table', 'label': 'T', 'columns': {'A': 'NUMBER', 'B': 'NUMBER', 'C': 'TEXT'}, 'owners': {('w', 'd')}},
                {'kind': 'table', 'label': 'U', 'columns': {'A': 'NUMBER'}, 'owners': {('w', 'd')}}]
        self.assertEqual(R.choose(pool, ['A', 'B'], {('w', 'd')})['label'], 'T')
        self.assertEqual(R.choose(pool, ['A', 'B'], {('x', 'y')})['label'], 'L')
        self.assertIsNone(R.choose(pool, ['Z'], set()))

    def test_comparison_query_is_one_read_only_select(self):
        rel = {'kind': 'model', 'relation': 'SELECT f."a" AS A FROM DB.S.T f', 'columns': {}}
        q = R.test_sql(rel, ['SUM(A)', 'AVG(A)'], 'ORDER_DATE', 'Region Name')
        self.assertTrue(q.startswith("SELECT * FROM (SELECT 'total'"))
        self.assertIn('FROM (SELECT f."a" AS A FROM DB.S.T f) r', q)
        self.assertIn("DATE_TRUNC('MONTH', r.ORDER_DATE)", q)
        self.assertIn('TO_VARCHAR(r."Region Name")', q)
        self.assertEqual(q.count(' UNION ALL '), 2)
        self.assertIsNone(re.search(r'\b(INSERT|UPDATE|DELETE|CREATE|DROP|MERGE)\b', q))

    def test_grains_use_the_most_used_columns(self):
        rel = {'columns': {'ORDER_DATE': 'DATE', 'SHIP_DATE': 'DATE', 'REGION': 'TEXT', 'CITY': 'TEXT', 'SALES': 'NUMBER'}}
        self.assertEqual(R.grains(rel, {}, {'SHIP_DATE': 3, 'ORDER_DATE': 1, 'CITY': 2, 'REGION': 5}), ('SHIP_DATE', 'REGION'))
        self.assertEqual(R.grains({'columns': {'X': 'NUMBER'}}, {}, {}), (None, None))


@unittest.skipUnless(HAVE_CLOUD, 'Cloud test set not present')
class RunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = harvest()

    def test_catalogue_records_the_snowflake_relation(self):
        b = {r['WORKBOOK_NAME']: r['_parsed']['bindings'] for r in self.h.results}
        self.assertEqual(b['Sales_Dashboard_Ver_01'][0]['KIND'], 'table')
        self.assertEqual(b['Sales_Dashboard_Ver_01'][0]['RELATION'], 'SANDBOX.BI.VIZLEAP_SALES')
        model = b['ECommerce_Live_Relationship_Events_Customers_Products'][0]
        self.assertEqual(model['KIND'], 'model')
        self.assertTrue(model['RELATION'].startswith('SELECT ') and ' LEFT JOIN ' in model['RELATION'])
        self.assertIn('CUSTOMER_ID', model['COLUMNS'])
        self.assertEqual(b['Fil_Test'], [])

    def test_conflict_is_proved_by_numbers_and_explained(self):
        fp2, extra = variant_rows(self.h)
        sess = Sess(handler=lambda q: rows_for(q, differ=True))
        explained = {'explanation': 'One divides profit by sales, the other by quantity.', 'recommended': 'd1',
                     'reason': 'Used on most sheets.'}
        with patch('v3_ai.call', return_value=explained) as call, patch.object(R, 'cortex_groups', return_value=[]):
            run = R.Run(sess, 'cloud', write_rows=True)
            counts = run.execute(frames(self.h, extra))
        g = next(g for g in run.groups if g['source'] == 'same name')
        self.assertEqual(g['verdict'], 'conflicting')
        self.assertIn(fp2, g['members'])
        self.assertEqual(g['relation_label'], 'WBR_DB.PIPELINE_DEMO.ORDERS (loaded)')
        self.assertTrue(g['differences'])
        self.assertTrue(g['CORTEX_PASSED_CHECK'])
        self.assertIn(g['RECOMMENDED_FP'], g['members'])
        self.assertEqual(call.call_count, 1)
        self.assertEqual(sum(counts.values()), len(run.info))
        metrics = sess.inserts['RAT_METRICS']
        self.assertEqual(len(metrics), len(run.info))
        self.assertEqual(len({m['METRIC_FP'] for m in metrics}), len(metrics))       # exactly one class each
        self.assertEqual({m['CLASS'] for m in metrics if m['METRIC_FP'] in g['members']}, {'conflicting'})
        self.assertEqual([r['STATUS'] for r in sess.inserts['RAT_RUNS']], ['started', 'finished'])
        self.assertTrue(any('sum(PROFIT)/NULLIF(sum(DISCOUNT), 0) AS M' in q for q in sess.queries))

    def test_same_numbers_make_an_equivalent_group(self):
        fp2, extra = variant_rows(self.h)
        sess = Sess(handler=lambda q: rows_for(q, differ=False))
        with patch.object(R, 'cortex_groups', return_value=[]):
            run = R.Run(sess, 'cloud', cortex=False)
            run.execute(frames(self.h, extra))
        self.assertEqual(run.classes[fp2][0], 'equivalent')

    def test_cortex_groups_cannot_create_conflicts(self):
        sess = Sess(handler=lambda q: rows_for(q, differ=True))
        info_fp = C.fp('SUM(COL(PROFIT))/SUM(COL(SALES))')
        sales = C.fp('SUM(COL(SALES))')
        proposed = [{'source': 'cortex', 'name': 'Profit Ratio', 'members': sorted([info_fp, sales]), 'reason': 'both money'}]
        with patch.object(R, 'cortex_groups', return_value=proposed), patch('v3_ai.call') as call:
            run = R.Run(sess, 'cloud')
            run.execute(frames(self.h))
        self.assertEqual(run.groups[0]['verdict'], 'differs')
        self.assertNotEqual(run.classes[info_fp][0], 'conflicting')
        call.assert_not_called()                                   # no explanation for a 'differs' group

    def test_not_testable_reasons(self):
        fp2, extra = variant_rows(self.h)
        sess = Sess(handler=lambda q: rows_for(q), columns={'OTHER': ['X']})
        fr = frames(self.h, extra)
        fr['bindings'] = pd.DataFrame(columns=fr['bindings'].columns)      # no bound table or model either
        with patch.object(R, 'cortex_groups', return_value=[]):
            run = R.Run(sess, 'cloud', cortex=False)
            run.execute(fr)
        g = next(g for g in run.groups if g['source'] == 'same name')
        self.assertEqual(g['verdict'], 'not_testable')
        self.assertIn('PROFIT', g['reason'])

        def boom(q):
            raise RuntimeError('SQL compilation error: secret detail')
        sess = Sess(handler=boom)
        with patch.object(R, 'cortex_groups', return_value=[]):
            run = R.Run(sess, 'cloud', cortex=False)
            run.execute(frames(self.h, extra))
        g = next(g for g in run.groups if g['source'] == 'same name')
        self.assertEqual(g['verdict'], 'not_testable')
        self.assertIn('RuntimeError', g['reason'])
        self.assertNotIn('secret detail', g['reason'])

    def test_explanation_is_checked(self):
        g = {'members': ['a', 'b'], 'name': 'M', 'differences': [{'grain': 'total', 'key': None, 'values': {'a': 0.12, 'b': 0.24}}]}
        info = {'a': {'names': ['M'], 'expression': 'x', 'workbooks': 1, 'sheets': 1, 'views': 3},
                'b': {'names': ['M'], 'expression': 'y', 'workbooks': 1, 'sheets': 2, 'views': 9}}
        with patch('v3_ai.call', return_value={'explanation': 'A is 0.12 and B is 0.24.', 'recommended': 'd2', 'reason': 'More sheets.'}):
            self.assertTrue(R.explain(None, {}, g, info)['CORTEX_PASSED_CHECK'])
        with patch('v3_ai.call', return_value={'explanation': 'A is 77.5 higher.', 'recommended': 'd1', 'reason': 'r'}):
            self.assertFalse(R.explain(None, {}, g, info)['CORTEX_PASSED_CHECK'])
        with patch('v3_ai.call', return_value={'explanation': 'x', 'recommended': 'd9', 'reason': 'r'}):
            self.assertFalse(R.explain(None, {}, g, info)['CORTEX_PASSED_CHECK'])

    def test_cortex_grouping_keeps_only_valid_groups(self):
        info = {f'f{i}': {'fp': f'f{i}', 'names': [f'M{i}'], 'name': f'M{i}', 'expression': 'SUM([X])'} for i in range(4)}
        answer = {'groups': [{'ids': ['x1', 'x2'], 'reason': 'same'}, {'ids': ['x3'], 'reason': 'alone'},
                             {'ids': ['x1', 'x99'], 'reason': 'unknown id'}]}
        with patch('v3_ai.call', return_value=answer) as call:
            out = R.cortex_groups(object(), {'ai_log': []}, info, {})
        self.assertEqual([g['members'] for g in out], [['f0', 'f1']])
        prompt = call.call_args[0][4]
        self.assertIn('untrusted data', prompt)


class StoreAndReportTests(unittest.TestCase):
    def test_setup_sql_has_the_rationalisation_and_binding_objects(self):
        sql = ST.ddl()
        for t in list(R.TABLES) + ['CAT_BINDINGS']:
            self.assertIn(f'CREATE TABLE IF NOT EXISTS {ST.SCHEMA}.{t} (', sql)
        for v in list(R.VIEWS) + ['V_CAT_BINDINGS', 'V_CAT_FIELDS']:
            self.assertIn(f'CREATE OR REPLACE VIEW {ST.SCHEMA}.{v} ', sql)
        self.assertEqual((ROOT / 'sql' / 'en01_evidence_store.sql').read_text(), sql)

    def test_excel_report(self):
        import openpyxl
        m = pd.DataFrame([{'RUN_ID': 'r', 'METRIC_FP': 'a', 'NAME': 'M', 'CLASS': 'conflicting', 'WORKBOOKS': 2}])
        g = pd.DataFrame([{'RUN_ID': 'r', 'GROUP_ID': 'G1', 'NAME': 'M', 'DIFFERENCES': json.dumps(
            [{'grain': 'total', 'key': None, 'values': {'a': 1, 'b': 2}}])}])
        wb = openpyxl.load_workbook(io.BytesIO(R.excel({'SCOPE': 's', 'RUN_ID': 'r'}, m, g)))
        self.assertEqual(wb.sheetnames, ['Summary', 'Metrics', 'Groups', 'Differences'])
        self.assertEqual(wb['Differences'].max_row, 3)


class PageTests(unittest.TestCase):
    def _page(self, found, ready=True):
        from streamlit.testing.v1 import AppTest

        script = (f'import sys\nsys.path.insert(0, {str(ROOT)!r})\nimport v3_rationalise_ui\n'
                  "v3_rationalise_ui.render(object(), 'cloud')\n")
        with patch.object(R, 'ready', return_value=ready), patch.object(R, 'latest', return_value=found):
            at = AppTest.from_string(script, default_timeout=30).run()
        return at

    def test_report_renders(self):
        m = pd.DataFrame([{'METRIC_FP': 'a', 'NAME': 'Margin', 'CLASS': 'conflicting', 'REASON': 'r', 'WORKBOOKS': 1,
                           'SHEETS': 2, 'TABLEAU_VIEWS': 3, 'EXPRESSION': 'SUM([P])/SUM([S])', 'GROUP_ID': 'G1'},
                          {'METRIC_FP': 'b', 'NAME': 'Margin', 'CLASS': 'conflicting', 'REASON': 'r', 'WORKBOOKS': 1,
                           'SHEETS': 1, 'TABLEAU_VIEWS': 0, 'EXPRESSION': 'SUM([P])/SUM([Q])', 'GROUP_ID': 'G1'},
                          {'METRIC_FP': 'c', 'NAME': 'Sales', 'CLASS': 'identical', 'REASON': 'r', 'WORKBOOKS': 4,
                           'SHEETS': 9, 'TABLEAU_VIEWS': 5, 'EXPRESSION': 'SUM([Sales])', 'GROUP_ID': None}])
        g = pd.DataFrame([{'GROUP_ID': 'G1', 'NAME': 'Margin', 'SOURCE': 'same name', 'MEMBERS': json.dumps(['a', 'b']),
                           'VERDICT': 'conflicting', 'RELATION': 'WBR_DB.PIPELINE_DEMO.ORDERS (loaded)',
                           'GRAINS': json.dumps(['total']), 'DIFFERENCES': json.dumps([{'grain': 'total', 'key': None,
                                                                                         'values': {'a': 0.1, 'b': 0.2}}]),
                           'EXPLANATION': 'Different denominators.', 'CORTEX_REASON': 'More sheets.', 'RECOMMENDED_FP': 'a',
                           'CORTEX_PASSED_CHECK': True, 'CODE_CHECK': 'ok', 'REASON': None}])
        at = self._page(({'RUN_ID': 'R1', 'RECORDED_AT': '2026-10-31', 'METRICS': 3, 'GROUPS_COMPARED': 1, 'SCOPE': 'cloud'}, m, g))
        self.assertFalse(at.exception, at.exception)
        tiles = {x.label: x.value for x in at.metric}
        self.assertEqual(tiles['Conflicting'], '2')
        self.assertEqual(tiles['Identical'], '1')
        self.assertTrue(any('Different denominators' in x.value for x in at.markdown))
        self.assertTrue(any('recommended by Cortex' in x.value for x in at.markdown))

    def test_without_tables_asks_for_setup(self):
        at = self._page(None, ready=False)
        self.assertFalse(at.exception)
        self.assertTrue(any('set-up SQL' in w.value for w in at.warning))



class NoNameLogicTests(unittest.TestCase):
    def test_no_workbook_or_metric_names_in_code(self):
        src = ''.join((ROOT / f).read_text(encoding='utf-8') for f in ('v3_rationalise.py', 'v3_rationalise_ui.py'))
        for name in ('Superstore', 'ECommerce', 'Profit Ratio', 'Fil_Test', 'VIZLEAP', 'ORDERS'):
            self.assertNotIn(name, src)


if __name__ == '__main__':
    unittest.main()
