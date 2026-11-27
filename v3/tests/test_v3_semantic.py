"""Semantic View from a Tableau relationship plan: model, DDL, guarded attach, query routing, Analyst guard."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import v3_semantic as SV
from v3_model_binding import compile_query

E, C, P = 'Events_1', 'Customers_1', 'Products_1'


def plan():
    f = lambda owner, remote, dtype, output: {'owner': owner, 'remote': remote, 'datatype': dtype, 'output': output}
    return {'version': 'fact-tree-binding/1', 'datasource_id': 'federated.x', 'server': 's', 'root': E,
            'tables': {E: {'table': 'DB.S.EVENTS', 'caption': 'Events'},
                       C: {'table': 'DB.S.CUSTOMERS', 'caption': 'Customers'},
                       P: {'table': 'DB.S.PRODUCTS', 'caption': 'Products'}},
            'fields': {'event_id': f(E, 'EVENT_ID', 'string', 'EVENT_ID'),
                       'customer_id': f(E, 'CUSTOMER_ID', 'string', 'CUSTOMER_ID'),
                       'product_id': f(E, 'PRODUCT_ID', 'string', 'PRODUCT_ID'),
                       'revenue': f(E, 'REVENUE', 'real', 'REVENUE'),
                       'quantity': f(E, 'QUANTITY', 'integer', 'QUANTITY'),
                       'customer_id (customers)': f(C, 'CUSTOMER_ID', 'string', 'CUSTOMER_ID_CUSTOMERS_'),
                       'segment': f(C, 'SEGMENT', 'string', 'SEGMENT'),
                       'product_id (products)': f(P, 'PRODUCT_ID', 'string', 'PRODUCT_ID_PRODUCTS_'),
                       'category': f(P, 'CATEGORY', 'string', 'CATEGORY'),
                       'base_price': f(P, 'BASE_PRICE', 'real', 'BASE_PRICE')},
            'keys': [{'first': E, 'second': C, 'left': 'CUSTOMER_ID', 'right': 'CUSTOMER_ID'},
                     {'first': E, 'second': P, 'left': 'PRODUCT_ID', 'right': 'PRODUCT_ID'}]}


IR = {'calcs': {'Net Revenue': {'agg_ready': True, 'sql': 'SUM(REVENUE)'},
                'Rev per Unit': {'agg_ready': True, 'sql': 'SUM(REVENUE) / NULLIF(SUM(QUANTITY), 0)'},
                'Param calc': {'agg_ready': True, 'sql': 'SUM(REVENUE) * __PARAM_X'},
                'Segment calc': {'agg_ready': True, 'sql': 'COUNT(DISTINCT SEGMENT)'}}}


class _Row:
    def __init__(self, d):
        self.d = d

    def as_dict(self):
        return dict(self.d)

    def __getitem__(self, i):
        return list(self.d.values())[i]


_SF_TYPE = {'string': 'VARCHAR(100)', 'real': 'FLOAT', 'integer': 'NUMBER(38,0)'}


def describe_rows(fqn, p=None):
    """DESCRIBE TABLE answer for the test plan's tables (release 20a builds shared views from the tables)."""
    p = p or plan()
    owner = next(o for o, t in p['tables'].items() if t['table'] == fqn)
    cols = {f['remote']: f['datatype'] for f in p['fields'].values() if f['owner'] == owner}
    return [_Row({'name': c, 'type': _SF_TYPE[t], 'kind': 'COLUMN'}) for c, t in sorted(cols.items())]


class _Session:
    """Records SQL; profiles return the same numbers unless `drift` is set; DDL may be rejected."""
    def __init__(self, drift=False, reject=0, combined_fails=False, views=None, role='WBR_OWNER', blocked=()):
        self.sql_log, self.drift, self.reject = [], drift, reject
        self.combined_fails = combined_fails
        self.views, self.role, self.blocked = dict(views or {}), role, set(blocked)

    def sql(self, text):
        self.sql_log.append(text)
        session = self

        class R:
            def collect(self_inner):
                if text.startswith('SELECT CURRENT_ROLE()'):
                    return [_Row({'R': session.role})]
                if text.startswith('SHOW SEMANTIC VIEWS'):
                    like = text.split("LIKE '")[1].split("%'")[0]
                    return [_Row({'name': n, 'owner': o[0], 'comment': o[1]}) for n, o in session.views.items()
                            if n.startswith(like)]
                if text.startswith('DESCRIBE TABLE'):
                    return describe_rows(text.split()[-1])
                if text.startswith('CREATE OR REPLACE SEMANTIC VIEW'):
                    name = text.split()[5].split('.')[-1]
                    if name in session.blocked:
                        raise RuntimeError(f'SQL compilation error: Object {name} already exists.')
                    if session.reject:
                        session.reject -= 1
                        raise RuntimeError('invalid metric')
                    session.views.setdefault(name, (session.role, 'Generated'))
                    return []
                if session.combined_fails and 'FROM SEMANTIC_VIEW(' in text and 'LEFT JOIN (' not in text:
                    raise RuntimeError('facts and dimensions must be in the same logical table')
                if 'COUNT(*) AS ROWS_' in text:
                    rows = 100 + (1 if session.drift and 'SEMANTIC_VIEW(' in text else 0)
                    return [_Row({'ROWS_': rows, 'S_REVENUE': 1201846.0, 'S_QUANTITY': 10694, 'D_CUSTOMER_ID': 67})]
                return [_Row({'V': 1})]
        return R()


class SemanticViewTests(unittest.TestCase):
    def test_model_follows_the_authored_relationships(self):
        sem = SV.model(plan(), IR, 'ECommerce')
        self.assertEqual([t['alias'] for t in sem['tables'] if t['role'] == 'fact'], ['events'])
        self.assertEqual({(r['from'], r['from_key'], r['to']) for r in sem['relationships']},
                         {('events', 'CUSTOMER_ID', 'customers'), ('events', 'PRODUCT_ID', 'products')})
        self.assertEqual({t['alias']: t['primary_key'] for t in sem['tables']},
                         {'events': None, 'customers': 'CUSTOMER_ID', 'products': 'PRODUCT_ID'})
        self.assertEqual(sem['facts'], [])          # every column is a dimension (row-level reads)
        numeric = sorted(d['name'] for d in sem['dimensions'] if d['numeric'])
        self.assertEqual(numeric, ['BASE_PRICE', 'QUANTITY', 'REVENUE'])
        self.assertNotIn('TOTAL_BASE_PRICE', [m['name'] for m in sem['metrics']])   # dimension-table attribute
        names = [m['name'] for m in sem['metrics']]
        self.assertIn('TOTAL_REVENUE', names)
        self.assertIn('DISTINCT_CUSTOMER_ID', names)
        self.assertIn('REV_PER_UNIT', names)
        self.assertNotIn('NET_REVENUE', names)              # same expression as TOTAL_REVENUE
        skipped = {s['calculation'] for s in sem['skipped']}
        self.assertEqual(skipped, {'Param calc', 'Segment calc'})
        self.assertTrue(sem['name'].startswith('WBR_DB.TABLEAU_ACCELERATOR_V3.ECOMMERCE_SVLAB_'))

    def test_ddl_has_all_blocks_and_escapes_text(self):
        sem = SV.model(plan(), IR, "O'Brien")
        text = SV.ddl(sem)
        self.assertNotIn('FACTS (', text)
        for block in ('TABLES (', 'RELATIONSHIPS (', 'DIMENSIONS (', 'METRICS ('):
            self.assertIn(block, text)
        self.assertIn('events_to_customers AS events (CUSTOMER_ID) REFERENCES customers', text)
        self.assertIn("O''Brien", text)

    def test_relation_reads_through_the_view_with_model_column_names(self):
        p = plan()
        sem = SV.model(p)
        sql = SV.relation_sql(sem, p)
        self.assertTrue(sql.startswith('SELECT EVENT_ID, CUSTOMER_ID'))
        self.assertIn('FROM SEMANTIC_VIEW(' + sem['name'] + ' DIMENSIONS ', sql)
        self.assertNotIn(' FACTS ', sql)
        self.assertIn('events.REVENUE', sql)
        per = SV.relation_sql(sem, p, 'per_table')
        self.assertIn('FROM (SELECT * FROM SEMANTIC_VIEW(' + sem['name'] + ' DIMENSIONS events.', per)
        self.assertIn('LEFT JOIN (SELECT * FROM SEMANTIC_VIEW(' + sem['name'] + ' DIMENSIONS customers.', per)
        self.assertIn('ON events.CUSTOMER_ID = customers.CUSTOMER_ID_CUSTOMERS_', per)
        self.assertIn('ON events.PRODUCT_ID = products.PRODUCT_ID_PRODUCTS_', per)

    def test_create_attaches_only_when_equal(self):
        rec = SV.create(_Session(), plan(), IR, 'ECommerce')
        self.assertTrue(rec['verified'])
        self.assertTrue(all(c['Equal'] for c in rec['checks']))
        bound = SV.attach(plan(), rec)
        self.assertEqual(bound['semantic_view'], rec['name'])
        compile_query(bound)                                   # still a valid binding plan
        bad = SV.create(_Session(drift=True), plan(), IR, 'ECommerce')
        self.assertFalse(bad['verified'])
        with self.assertRaises(ValueError):
            SV.attach(plan(), bad)

    def test_rejected_calculation_metrics_fall_back_without_blocking(self):
        rec = SV.create(_Session(reject=1), plan(), IR, 'ECommerce')
        self.assertTrue(rec['verified'])
        ddl_attempts = [a for a in rec['attempts'] if not a['Attempt'].startswith('read')]
        self.assertEqual([a['Result'] == 'created' for a in ddl_attempts], [False, True])
        self.assertNotIn('REV_PER_UNIT', rec['ddl'])
        self.assertIn('TOTAL_REVENUE', rec['ddl'])

    def test_per_table_reading_is_used_when_the_combined_query_is_refused(self):
        rec = SV.create(_Session(combined_fails=True), plan(), IR, 'ECommerce')
        self.assertTrue(rec['verified'])
        self.assertEqual(rec['mode'], 'per_table')
        bound = SV.attach(plan(), rec)
        self.assertEqual(bound['semantic_mode'], 'per_table')
        self.assertIn('LEFT JOIN (SELECT * FROM SEMANTIC_VIEW(', SV.bound_relation(bound))
        bound['semantic_mode'] = 'free sql'
        with self.assertRaises(ValueError):
            SV.bound_relation(bound)

    def test_bound_relation_rejects_a_foreign_view_name(self):
        rec = SV.create(_Session(), plan(), IR, 'ECommerce')
        bound = SV.attach(plan(), rec)
        self.assertIn('SEMANTIC_VIEW(' + rec['name'], SV.bound_relation(bound))
        bound['semantic_view'] = 'OTHER_DB.X.Y'
        with self.assertRaises(ValueError):
            SV.bound_relation(bound)

    def test_dashboard_queries_route_through_the_view(self):
        from v3_model_binding import replace_bindings, table_name
        p = plan()
        rec = SV.create(_Session(), p, IR, 'ECommerce')
        bound = SV.attach(p, rec)
        sources = {'ECommerce': {'model_plan': bound, 'table': table_name(bound)}}
        sql = replace_bindings('SELECT SUM(REVENUE) FROM ' + table_name(bound), sources)
        self.assertIn('SEMANTIC_VIEW(' + rec['name'], sql)
        plain = {'ECommerce': {'model_plan': p, 'table': table_name(p)}}
        self.assertNotIn('SEMANTIC_VIEW', replace_bindings('SELECT 1 FROM ' + table_name(p), plain))

    def test_analyst_sql_runs_only_as_single_read_only_select(self):
        s = _Session()
        SV.run_read_only(s, 'WITH a AS (SELECT 1) SELECT * FROM a;')
        self.assertTrue(s.sql_log[-1].startswith('SELECT * FROM (WITH a AS'))
        for bad in ('DROP TABLE X', 'SELECT 1; DELETE FROM X', 'CREATE TABLE T AS SELECT 1', 'CALL P()'):
            with self.assertRaises(ValueError):
                SV.run_read_only(s, bad)

    def test_analyst_response_parsing(self):
        sql, text = SV._parse_analyst({'message': {'content': [{'type': 'text', 'text': 'Revenue by category'},
                                                               {'type': 'sql', 'statement': 'SELECT 1'}]}})
        self.assertEqual((sql, text), ('SELECT 1', 'Revenue by category'))


class MigrationStepTests(unittest.TestCase):
    """The migration run itself creates and attaches the view; no button needed."""
    def _run(self):
        p = plan()
        return {'result': {'ir': IR}, 'bindings': {'ECommerce': p},
                'binding_rows': [{'Datasource': 'ECommerce', 'Binding': 'Relationship model'}]}

    def test_auto_semantic_attaches_when_equal(self):
        from unittest.mock import patch
        import v3_console as VC
        run = self._run()
        with patch.object(VC, '_runtime_sf', return_value=True), \
                patch.object(VC.backend, '_active_session', return_value=_Session()), \
                patch.object(SV, 'cortex_enricher', return_value=None):
            status, detail = VC._auto_semantic(run)
        self.assertEqual(status, 'done', detail)
        # Release 20a: shared view named from the fact table and the tables/keys fingerprint.
        self.assertRegex(run['bindings']['ECommerce']['semantic_view'],
                         r'^WBR_DB\.TABLEAU_ACCELERATOR_V3\.EVENTS_SVLAB_F[0-9A-F]{10}$')
        self.assertEqual(run['bindings']['ECommerce']['semantic_mode'], 'shared')
        self.assertEqual(run['binding_rows'][0]['Binding'], 'Snowflake Semantic View')

    def test_auto_semantic_keeps_model_when_not_equal_or_failing(self):
        from unittest.mock import patch
        import v3_console as VC
        for session in (_Session(drift=True), _Session(reject=3)):
            run = self._run()
            with patch.object(VC, '_runtime_sf', return_value=True), \
                    patch.object(VC.backend, '_active_session', return_value=session), \
                    patch.object(SV, 'cortex_enricher', return_value=None):
                status, detail = VC._auto_semantic(run)
            self.assertEqual(status, 'review')
            self.assertNotIn('semantic_view', run['bindings']['ECommerce'])

    def test_local_run_does_not_write(self):
        import v3_console as VC
        run = self._run()
        from unittest.mock import patch
        with patch.object(VC, '_runtime_sf', return_value=False):
            status, _ = VC._auto_semantic(run)
        self.assertEqual(status, 'review')
        self.assertNotIn('semantic', run)


class AskTheDataTests(unittest.TestCase):
    """Deployed app 'Ask the data' tab: only on a verified view, guarded SQL, starter questions."""
    def test_tab_only_for_name_checked_views_and_suggests_questions(self):
        import v3_ask
        rec = SV.create(_Session(), plan(), IR, 'ECommerce')
        bound = SV.attach(plan(), rec)
        self.assertEqual(list(v3_ask.semantic_plans({'ECommerce': bound, 'Other': 'DB.S.T'})), ['ECommerce'])
        self.assertEqual(v3_ask.semantic_plans({'ECommerce': plan()}), {})
        forged = dict(bound, semantic_view='EVIL.DB.VIEW')
        with self.assertRaises(ValueError):
            v3_ask.semantic_plans({'ECommerce': forged})
        ideas = v3_ask.suggestions(bound)
        self.assertTrue(ideas and all(q.startswith('What is the ') for q in ideas))
        self.assertTrue(any('category' in q or 'segment' in q for q in ideas))

    def test_answer_runs_only_read_only_sql(self):
        from unittest.mock import patch
        import v3_ask
        s = _Session()
        with patch.object(SV, 'ask_analyst', return_value=('SELECT 1 AS V', 'ok')):
            a = v3_ask._answer(s, 'V', 'q')
        self.assertEqual(a['rows'], [{'V': 1}])
        with patch.object(SV, 'ask_analyst', return_value=('DELETE FROM T', 'bad')):
            a = v3_ask._answer(s, 'V', 'q')
        self.assertIsNone(a['rows'])
        self.assertEqual(a['error'], 'SQL_REFUSED:ValueError')          # AG03: codes only on screen


if __name__ == '__main__':
    unittest.main()


class ReadableNameTests(unittest.TestCase):
    def test_name_comes_from_the_tableau_datasource_caption_and_survives_attach(self):
        rec = SV.create(_Session(), plan(), IR, 'E-Commerce Dataset (LIVE Snowflake join)')
        self.assertRegex(rec['name'], r'^WBR_DB\.TABLEAU_ACCELERATOR_V3\.ECOMMERCE_DATASET_LIVE_SNOWFLAKE_JOIN_SVLAB_[0-9A-F]{8}$')
        bound = SV.attach(plan(), rec)
        self.assertIn('SEMANTIC_VIEW(' + rec['name'], SV.bound_relation(bound))
        bound['semantic_label'] = 'OTHER; DROP'
        with self.assertRaises(ValueError):
            SV.bound_relation(bound)
