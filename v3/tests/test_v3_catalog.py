"""Release 20b (SL01): estate catalogue — parsing, fingerprints, routes, storage, Cortex descriptions, page, tool."""
import collections
import json
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

import v3_catalog as C
import v3_store as ST

CLOUD = ROOT / 'Workbooks' / 'cloud_20261010'
HAVE_CLOUD = (CLOUD / 'Superstore_Tableau2024_3.twbx').exists()


def ref(parts):
    return 'COL(' + parts[-1].upper().replace(' ', '_') + ')'


class FakeResult:
    def __init__(self, sess, q, params):
        self.sess, self.q, self.params = sess, q, params or []

    def collect(self):
        s, q = self.sess, self.q
        s.log.append((q, self.params))
        if 'INFORMATION_SCHEMA.TABLES' in q:
            return [(len(self.params) - 1 if s.ready else 0,)]
        if q.startswith('SELECT WORKBOOK_SHA256'):
            sha = s.known.get(self.params[0])
            return [(sha,)] if sha else []
        if q.startswith('INSERT INTO'):
            table = re.match(r'INSERT INTO [\w.]+\.(\w+) ', q).group(1)
            if table in s.fail_on:
                raise RuntimeError('insert failed')
            rows = [json.loads(p) for p in self.params]
            s.inserts[table].extend(rows)
            s.order.append(table)
            if table == 'CAT_WORKBOOKS':
                for r in rows:
                    if r['STATUS'] == 'harvested':
                        s.known[r['WORKBOOK_KEY']] = r['WORKBOOK_SHA256']
            return []
        return []


class FakeSession:
    def __init__(self, ready=True):
        self.ready, self.log, self.known, self.order = ready, [], {}, []
        self.inserts = collections.defaultdict(list)
        self.fail_on = set()

    def sql(self, q, params=None):
        return FakeResult(self, q, params)


def harvest_cloud(sess=None):
    h = C.Harvest(sess, 'test', 'cloud')
    files = C.folder_workbooks(str(CLOUD))
    h.start(len(files))
    for f in files:
        C.harvest_file(h, str(CLOUD), f)
    return h


class NormaliseTests(unittest.TestCase):
    def test_spacing_case_comments_and_quotes_do_not_change_the_fingerprint(self):
        a = C.normalise('sum( [Profit] ) // ratio\n / SUM([Sales])', ref)
        b = C.normalise('SUM([Profit])/sum([Sales])', ref)
        self.assertEqual(a, b)
        self.assertEqual(a, 'SUM(COL(PROFIT))/SUM(COL(SALES))')
        self.assertEqual(C.normalise('IF [a]="x y" THEN 1 END', ref), C.normalise("if [a] = 'x y' then 1 end", ref))

    def test_string_literals_keep_their_case_and_spacing(self):
        self.assertIn("'Same  Day'", C.normalise('IF [Ship Mode] = "Same  Day" THEN 0 END', ref))
        self.assertNotEqual(C.normalise('[a]="East"', ref), C.normalise('[a]="EAST"', ref))

    def test_expanded_calculations_need_no_brackets_when_they_are_one_term(self):
        self.assertEqual(C._wrap('COL(SALES)'), 'COL(SALES)')
        self.assertEqual(C._wrap('SUM(COL(A))'), 'SUM(COL(A))')
        self.assertEqual(C._wrap('{FIXED COL(A):SUM(COL(B))}'), '{FIXED COL(A):SUM(COL(B))}')
        self.assertEqual(C._wrap('COL(A)+1'), '(COL(A)+1)')
        self.assertEqual(C._wrap('SUM(COL(A))/SUM(COL(B))'), '(SUM(COL(A))/SUM(COL(B)))')

    def test_kinds(self):
        self.assertEqual(C.kind_of('SUM(COL(A))', ''), 'aggregate')
        self.assertEqual(C.kind_of('{FIXED COL(A):SUM(COL(B))}', ''), 'lod')
        self.assertEqual(C.kind_of('RUNNING_SUM(SUM(COL(A)))', ''), 'table_calc')
        self.assertEqual(C.kind_of("IF COL(A)>0 THEN 'x' END", ''), 'row')
        self.assertEqual(C.kind_of('1', '', has_table_calc=True), 'table_calc')


@unittest.skipUnless(HAVE_CLOUD, 'Cloud test set not present')
class CloudSetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = harvest_cloud()
        cls.parsed = {r['WORKBOOK_NAME']: r['_parsed'] for r in cls.h.results if r.get('_parsed')}

    def test_every_cloud_workbook_is_catalogued(self):
        self.assertEqual(self.h.counts['failed'], 0, [r for r in self.h.results if r['STATUS'] == 'failed'])
        self.assertEqual(self.h.counts['harvested'], len(C.folder_workbooks(str(CLOUD))))

    def test_profit_ratio_is_one_metric_across_superstore_workbooks(self):
        fps = collections.defaultdict(set)
        for name, p in self.parsed.items():
            for m in p['metrics']:
                if m['NAME'] == 'Profit Ratio':
                    fps[m['METRIC_FP']].add(name)
        self.assertEqual(len(fps), 1, fps)
        books = next(iter(fps.values()))
        self.assertTrue({'Superstore_Tableau2024_3', 'R1_Test_Upload_-_Superstore', 'Fil_Test'} <= books, books)

    def test_sheet_aggregation_and_calculation_of_the_same_formula_share_a_fingerprint(self):
        p = self.parsed['Superstore_Tableau2024_3']
        by_name = {m['NAME']: m['METRIC_FP'] for m in p['metrics'] if m['ORIGIN_KIND'] == 'sheet aggregation'}
        self.assertIn('Sales', by_name)
        self.assertEqual(by_name['Sales'], C.fp('SUM(COL(SALES))'))

    def test_every_measure_on_a_sheet_links_to_a_metric(self):
        for name, p in self.parsed.items():
            known = {m['METRIC_FP'] for m in p['metrics']}
            for u in p['usage']:
                if u['IS_MEASURE']:
                    self.assertIn(u['METRIC_FP'], known | {c['FORMULA_FP'] for c in p['calculations']}, (name, u))
            self.assertTrue(any(u['IS_MEASURE'] for u in p['usage']), name)

    def test_usage_names_dashboards_and_filters(self):
        p = self.parsed['Superstore_Tableau2024_3']
        self.assertTrue(any(u['DASHBOARDS'] for u in p['usage']))
        self.assertTrue(any(u['SHELF'] == 'filter' for u in p['usage']))
        self.assertTrue({u['SHELF'] for u in p['usage']} & {'rows', 'cols'})

    def test_routes_are_the_migration_routes(self):
        routes = {(n, d['DATASOURCE']): d['ROUTE'] for n, p in self.parsed.items() for d in p['datasources']}
        self.assertEqual(routes[('ECommerce_Live_Relationship_Events_Customers_Products',
                                 'E-Commerce Dataset (LIVE Snowflake join)')], 'bind_model')
        self.assertEqual(routes[('Sales_Dashboard_Ver_01', 'VIZLEAP_SALES (SANDBOX.VIZLEAP_SALES) (BI)')], 'bind_snowflake')
        # DM3: custom SQL is bound through a verified read-only plan (was 'release24': named, not supported)
        self.assertEqual(routes[('Regional_Analysis', 'Data Using Custom-SQL')], 'bind_model')
        self.assertEqual(routes[('Global_Sales_Dashboard', 'CALLS')], 'load')
        self.assertEqual(routes[('Fil_Test', 'Sample - Superstore (2)')], 'laptop')
        self.assertEqual(routes[('Superstore_Tableau2024_3', 'Sample - Superstore')], 'load')

    def test_extract_held_on_tableau_routes_to_the_laptop_loader(self):
        """A Cloud download without data: the extract is on Tableau, so the route is the laptop loader."""
        import io, zipfile
        raw = (CLOUD / 'Fil_Test.twbx').read_bytes()
        out = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(raw)) as src, zipfile.ZipFile(out, 'w') as dst:
            for n in src.namelist():
                if not n.lower().endswith(('.hyper', '.tde', '.xls', '.xlsx', '.csv')):
                    dst.writestr(n, src.read(n))
        p = C.parse_workbook(out.getvalue(), 'Fil_Test.twbx')
        self.assertEqual([d['ROUTE'] for d in p['datasources']], ['laptop'])
        self.assertIn('extract', p['datasources'][0]['ROUTE_REASON'])

    def test_route_follows_source_candidates_and_locate(self):
        import tableau_parser
        import v3_data_load as DL
        import v3_source_binding
        for f in C.folder_workbooks(str(CLOUD)):
            raw = Path(f).read_bytes()
            root = tableau_parser.load_twb_xml(f)
            ir = tableau_parser.build_ir(f, scoped=True)
            cand = v3_source_binding.source_candidates(root, ir)
            for did, scope in ir['field_scopes'].items():
                r, _why = C.route(raw, root, ir, did, scope['caption'], cand)
                if cand[did].get('model_candidate') or cand[did].get('candidate'):
                    self.assertIn(r, ('bind_model', 'bind_snowflake'))
                else:
                    kind = DL.locate(raw, root, ir, scope['caption'])['kind']
                    self.assertEqual(r, {'file': 'load', 'union': 'load', 'extract': 'laptop'}.get(kind, r), (f, did))

    def test_original_formula_and_translated_sql_are_kept(self):
        calcs = [c for p in self.parsed.values() for c in p['calculations']]
        self.assertTrue(all(c['FORMULA'] is not None and c['FORMULA_FP'] for c in calcs))
        self.assertTrue(any(c['TRANSLATION'] == 'translated' and c['TRANSLATED_SQL'] for c in calcs))
        lod = next(c for c in calcs if c['KIND'] == 'lod')
        self.assertIn('{FIXED', lod['NORMALISED'])


@unittest.skipUnless(HAVE_CLOUD, 'Cloud test set not present')
class StorageTests(unittest.TestCase):
    def test_detail_rows_are_written_before_the_workbook_row(self):
        sess = FakeSession()
        h = C.Harvest(sess, 'test', 'cloud')
        C.harvest_file(h, str(CLOUD), str(CLOUD / 'Fil_Test.twbx'))
        self.assertEqual(sess.order[-1], 'CAT_WORKBOOKS')
        self.assertTrue({'CAT_DATASOURCES', 'CAT_CALCULATIONS', 'CAT_METRICS', 'CAT_USAGE', 'CAT_FIELDS'} <= set(sess.order))
        keys = {r['WORKBOOK_KEY'] for t in ('CAT_DATASOURCES', 'CAT_USAGE') for r in sess.inserts[t]}
        self.assertEqual(keys, {sess.inserts['CAT_WORKBOOKS'][0]['WORKBOOK_KEY']})

    def test_unchanged_workbook_is_not_parsed_again(self):
        sess = FakeSession()
        C.harvest_file(C.Harvest(sess, 'test', 'cloud'), str(CLOUD), str(CLOUD / 'Fil_Test.twbx'))
        before = len(sess.inserts['CAT_USAGE'])
        h2 = C.Harvest(sess, 'test', 'cloud')
        with patch.object(C, 'parse_workbook', side_effect=AssertionError('parsed again')):
            row = C.harvest_file(h2, str(CLOUD), str(CLOUD / 'Fil_Test.twbx'))
        self.assertEqual(row['STATUS'], 'unchanged')
        self.assertEqual(len(sess.inserts['CAT_USAGE']), before)
        self.assertEqual(h2.counts['unchanged'], 1)

    def test_a_broken_workbook_is_recorded_as_failed_and_the_harvest_continues(self):
        sess = FakeSession()
        h = C.Harvest(sess, 'test', 'cloud')
        bad = h.workbook(b'not a workbook', 'x.twb', {'WORKBOOK_KEY': 'file:x', 'WORKBOOK_NAME': 'x', 'PROJECT': 'p',
                                                       'ORIGIN': 'folder', 'SOURCE_REF': {}})
        good = C.harvest_file(h, str(CLOUD), str(CLOUD / 'Fil_Test.twbx'))
        self.assertEqual(bad['STATUS'], 'failed')
        self.assertTrue(bad['ERROR_CLASS'])
        self.assertEqual(good['STATUS'], 'harvested')
        self.assertEqual(h.counts, {'found': 0, 'harvested': 1, 'unchanged': 0, 'failed': 1})

    def test_harvest_rows_and_insert_statements(self):
        sess = FakeSession()
        h = C.Harvest(sess, 'test', 'cloud')
        h.start(1)
        h.finish(describe_metrics=False)
        self.assertEqual([r['STATUS'] for r in sess.inserts['CAT_HARVESTS']], ['started', 'finished'])
        q = C.insert_sql('CAT_USAGE', 2)
        self.assertEqual(q.count('(?)'), 2)
        self.assertTrue(all(c in q for c, _t in C.TABLES['CAT_USAGE'][1]))
        self.assertFalse(any(re.match(r'\s*(UPDATE|DELETE|DROP|CREATE|MERGE)', s, re.I) for s, _p in sess.log))


class DdlTests(unittest.TestCase):
    def test_setup_script_creates_every_catalogue_table_and_view(self):
        sql = ST.ddl()
        for t in C.TABLES:
            self.assertIn(f'CREATE TABLE IF NOT EXISTS {ST.SCHEMA}.{t} (', sql)
        for v in C.VIEWS:
            self.assertIn(f'CREATE OR REPLACE VIEW {ST.SCHEMA}.{v} ', sql)
        self.assertLess(sql.index('CAT_WORKBOOKS ('), sql.index('V_CAT_WORKBOOKS COMMENT'))
        self.assertEqual((ROOT / 'sql' / 'en01_evidence_store.sql').read_text(), sql)

    def test_not_ready_without_tables(self):
        self.assertFalse(C.ready(None))
        self.assertFalse(C.ready(FakeSession(ready=False)))
        self.assertTrue(C.ready(FakeSession()))


ITEMS = [{'METRIC_FP': f'fp{i}', 'NAMES': [f'Metric {i}'], 'EXPRESSION': 'SUM([Sales]) * 0.75', 'KIND': 'aggregate',
          'DATASOURCES': ['Orders']} for i in range(90)]


def good_answer(ids, bad=None):
    out = []
    for i in ids:
        a = {'id': i, 'business_name': 'Adjusted sales', 'description': 'Sales scaled by 0.75.', 'intent': 'revenue'}
        out.append(dict(a, **(bad or {}).get(i, {})))
    return {'metrics': out}


class DescribeTests(unittest.TestCase):
    def _call(self, answers):
        calls = []

        def fake(sess, run, step, purpose, prompt, schema=None, model=None, max_tokens=1500):
            import v3_ai
            v3_ai.record(run, step, purpose, 'm', 'Completed')
            ids = schema['properties']['metrics']['items']['properties']['id']['enum']
            calls.append((prompt, ids))
            return answers(ids)
        return calls, fake

    def test_batches_cap_and_code_check(self):
        sess = FakeSession()
        h = C.Harvest(sess, 'test', 'cloud')
        bad = {'m1': {'intent': 'magic'}, 'm2': {'description': 'Sales above the 1,250,000 target.'},
               'm3': {'business_name': 'x' * 90}}
        calls, fake = self._call(lambda ids: dict(good_answer(ids, bad), metrics=good_answer(ids, bad)['metrics'] + [
            {'id': 'm999', 'business_name': 'n', 'description': 'd', 'intent': 'other'}]))
        with patch('v3_ai.call', side_effect=fake):
            passed = C.describe(sess, h, max_calls=2, items=ITEMS)
        self.assertEqual(len(calls), 2)                                  # 90 items, 40 per call, capped at 2 calls
        rows = sess.inserts['CAT_METRIC_DESCRIPTIONS']
        self.assertEqual(len(rows), 80)
        self.assertEqual(passed, 80 - 6)
        failed = {r['CODE_CHECK'] for r in rows if not r['PASSED_CODE_CHECK']}
        self.assertTrue(any('intent' in f for f in failed))
        self.assertTrue(any('numbers' in f for f in failed))
        self.assertTrue(any('80' in f for f in failed))
        self.assertIn('untrusted data', calls[0][0])
        self.assertIn('SUM([Sales]) * 0.75', calls[0][0])

    def test_cortex_failure_leaves_the_catalogue_complete_and_no_service_text(self):
        import v3_ai
        sess = FakeSession()
        h = C.Harvest(sess, 'test', 'cloud')
        h.start(0)
        with patch('v3_ai.call', side_effect=v3_ai.CortexUnavailable('Cortex request failed (X).')), \
                patch.object(C, 'pending', return_value=ITEMS[:3]):
            h.finish()
        self.assertEqual(h.described, 0)
        self.assertEqual(sess.inserts['CAT_HARVESTS'][-1]['STATUS'], 'finished')
        self.assertNotIn('CAT_METRIC_DESCRIPTIONS', sess.inserts)

    def test_check_rules(self):
        item = {'formula': 'SUM([Sales])/1000'}
        self.assertTrue(C.check(item, {'business_name': 'Sales (thousands)', 'description': 'Sales in thousands (1000).',
                                       'intent': 'revenue'})[0])
        self.assertFalse(C.check(item, {'business_name': '', 'description': 'd', 'intent': 'revenue'})[0])
        self.assertFalse(C.check(item, {'business_name': 'n', 'description': 'd', 'intent': 'sales'})[0])


class ProjectTests(unittest.TestCase):
    def test_project_tree_includes_sub_projects_and_paths(self):
        projects = [{'id': 'a', 'name': 'Top'}, {'id': 'b', 'name': 'Child', 'parent_project_id': 'a'},
                    {'id': 'c', 'name': 'Grand', 'parent_project_id': 'b'}, {'id': 'd', 'name': 'Other'}]
        ids, path = C.project_tree(projects, 'a')
        self.assertEqual(ids, {'a', 'b', 'c'})
        self.assertEqual(path('c'), 'Top / Child / Grand')
        self.assertEqual(C.project_tree(projects, 'a', include_sub=False)[0], {'a'})

    def test_view_counts_are_read_from_usage_statistics(self):
        import xml.etree.ElementTree as ET
        xml = ('<tsResponse xmlns="http://tableau.com/api"><views><view id="v1" name="Overview" contentUrl="x">'
               '<usage totalViewCount="42"/></view><view id="v2" name="Detail"/></views></tsResponse>')

        class Client:
            site_id = 's'

            def _get_paginated(self, path, tag):
                self.path = path
                return ET.fromstring(xml).findall('.//{http://tableau.com/api}view')
        c = Client()
        out = C.views_with_usage(c, 'w1')
        self.assertIn('includeUsageStatistics=true', c.path)
        self.assertEqual([v['total_views'] for v in out], [42, None])


class PageAndToolTests(unittest.TestCase):
    def test_page_without_snowflake_says_so(self):
        from streamlit.testing.v1 import AppTest
        at = AppTest.from_file(str(ROOT / 'v3_console.py'), default_timeout=30).run()
        at.radio(key='v3c_page').set_value('Estate catalogue').run()
        self.assertFalse(at.exception)
        self.assertTrue(any('hosted Snowflake session' in i.value for i in at.info))

    def test_page_without_tables_asks_for_the_setup_script(self):
        from streamlit.testing.v1 import AppTest
        at = AppTest.from_file(str(ROOT / 'v3_console.py'), default_timeout=30).run()
        with patch('backend._active_session', return_value=FakeSession(ready=False)):
            at.radio(key='v3c_page').set_value('Estate catalogue').run()
        self.assertFalse(at.exception)
        self.assertTrue(any('en01_evidence_store.sql' in w.value for w in at.warning))

    @unittest.skipUnless(HAVE_CLOUD, 'Cloud test set not present')
    def test_laptop_tool_plan_only_writes_nothing(self):
        import importlib.util
        import io
        import contextlib
        spec = importlib.util.spec_from_file_location('harvest_catalogue', ROOT / 'tools' / 'harvest_catalogue.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        out = io.StringIO()
        with patch('pipeline.snow_session', side_effect=AssertionError('no Snowflake in plan-only')), \
                contextlib.redirect_stdout(out):
            code = mod.main(['--folder', str(CLOUD), '--plan-only'])
        self.assertEqual(code, 0)
        self.assertIn('Plan only: nothing written.', out.getvalue())
        self.assertIn('distinct metrics', out.getvalue())


def frames_from(h, cloud_key=None):
    """The catalogue views as the app reads them (lists as JSON text, like Snowflake VARIANTs)."""
    import pandas as pd
    wb, parts = [], collections.defaultdict(list)
    for r in h.results:
        if r['STATUS'] != 'harvested':
            continue
        origin = 'tableau-cloud' if r['WORKBOOK_KEY'] == cloud_key else 'folder'
        ref = {'workbook_id': 'w-9', 'server_url': 'https://example.test', 'site_content_url': 's'} if origin == 'tableau-cloud' else {}
        wb.append(dict({k: v for k, v in r.items() if k != '_parsed'}, ORIGIN=origin, SOURCE_REF=json.dumps(ref),
                       HARVESTED_AT=r['RECORDED_AT'], LAST_SEEN=r['RECORDED_AT'], TOTAL_VIEWS=7))
        extra = {'WORKBOOK_KEY': r['WORKBOOK_KEY'], 'HARVEST_ID': r['HARVEST_ID'], 'SCOPE': r['SCOPE'],
                 'PROJECT': r['PROJECT'], 'WORKBOOK_NAME': r['WORKBOOK_NAME']}
        for view, part in (('V_CAT_DATASOURCES', 'datasources'), ('V_CAT_METRICS', 'metrics'),
                           ('V_CAT_USAGE', 'usage'), ('V_CAT_CALCULATIONS', 'calculations')):
            for row in r['_parsed'][part]:
                parts[view].append({k: (json.dumps(v) if isinstance(v, list) else v) for k, v in dict(row, **extra).items()})
    out = {'V_CAT_WORKBOOKS': pd.DataFrame(wb)}
    out.update({k: pd.DataFrame(v) for k, v in parts.items()})
    return out


@unittest.skipUnless(HAVE_CLOUD, 'Cloud test set not present')
class PageWithDataTests(unittest.TestCase):
    def test_results_render_and_open_a_workbook_in_new_migration(self):
        import pandas as pd
        from streamlit.testing.v1 import AppTest
        h = harvest_cloud()
        key = next(r['WORKBOOK_KEY'] for r in h.results if r['WORKBOOK_NAME'] == 'Fil_Test')
        frames = frames_from(h, cloud_key=key)
        sess = FakeSession()
        with patch('backend._active_session', return_value=sess), \
                patch.object(C, 'scopes', return_value=['cloud']), \
                patch.object(C, 'frame', side_effect=lambda s, view, scope=None, limit=0: frames[view]), \
                patch.object(C, 'descriptions', return_value=pd.DataFrame(columns=['METRIC_FP', 'BUSINESS_NAME',
                                                                                  'DESCRIPTION', 'INTENT', 'MODEL'])), \
                patch.object(C, 'pending', return_value=[]), \
                patch.object(C, 'harvests', return_value=pd.DataFrame()), \
                patch.object(C, 'failures', return_value=pd.DataFrame()):
            at = AppTest.from_file(str(ROOT / 'v3_console.py'), default_timeout=60).run()
            at.radio(key='v3c_page').set_value('Estate catalogue').run()
            self.assertFalse(at.exception, at.exception)
            tiles = {m.label: m.value for m in at.metric}
            self.assertEqual(tiles['Workbooks'], str(len(frames['V_CAT_WORKBOOKS'])))
            self.assertGreater(int(tiles['Shared by 2+ workbooks']), 10)
            fp = C.fp('SUM(COL(PROFIT))/SUM(COL(SALES))')
            at.selectbox(key='v3cat-pick').set_value(fp).run()
            self.assertFalse(at.exception, at.exception)
            at.text_input(key='v3cat-q').set_value('profit').run()
            self.assertFalse(at.exception, at.exception)
            at.selectbox(key='v3cat-migrate').set_value(key).run()
            next(b for b in at.button if b.label == 'Open in New migration').click().run()
            self.assertFalse(at.exception, at.exception)
            src = at.session_state['v3c_source']
            self.assertEqual((src['origin'], src['workbook_id'], src['name']), ('tableau-cloud', 'w-9', 'Fil_Test'))
            self.assertEqual(at.radio(key='v3c_page').value, 'New migration')


class NoNameLogicTests(unittest.TestCase):
    def test_no_workbook_or_sheet_names_in_the_catalogue_code(self):
        src = (ROOT / 'v3_catalog.py').read_text(encoding='utf-8') + (ROOT / 'v3_catalog_ui.py').read_text(encoding='utf-8')
        for name in ('Superstore', 'ECommerce', 'Regional', 'Fil_Test', 'Global Sales', 'VIZLEAP', 'Profit Ratio'):
            self.assertNotIn(name, src)



class OptionalBindingsTests(unittest.TestCase):
    @unittest.skipUnless(HAVE_CLOUD, 'Cloud test set not present')
    def test_harvest_works_before_the_release_22_table_exists(self):
        class NoBindings(FakeSession):
            def sql(self, q, params=None):
                if 'TABLE_NAME IN' in q and params and 'CAT_BINDINGS' in params:
                    return FakeResult(FakeSession(ready=False), q, params)
                return FakeResult(self, q, params)
        sess = NoBindings()
        self.assertTrue(C.ready(sess))
        row = C.harvest_file(C.Harvest(sess, 'test', 'cloud'), str(CLOUD), str(CLOUD / 'Sales_Dashboard_Ver_01.twbx'))
        self.assertEqual(row['STATUS'], 'harvested')
        self.assertNotIn('CAT_BINDINGS', sess.inserts)
        sess2 = FakeSession()
        C.harvest_file(C.Harvest(sess2, 'test', 'cloud'), str(CLOUD), str(CLOUD / 'Sales_Dashboard_Ver_01.twbx'))
        self.assertEqual(sess2.inserts['CAT_BINDINGS'][0]['RELATION'], 'SANDBOX.BI.VIZLEAP_SALES')


if __name__ == '__main__':
    unittest.main()
