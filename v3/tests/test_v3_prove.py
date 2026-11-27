"""Cause proof (AI06.3 slice): typed menu, Cortex ranking filtered, code tests are read-only and
reproduce the per-LOD-key grain on real SQL execution (DuckDB), verdicts never touched."""
from pathlib import Path
import json
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import v3_prove as P

RAW = ("SELECT DATE_TRUNC('day', EVENT_DATE) AS X, "
       "AVG(DATEDIFF('day', MIN(CASE WHEN EVENT_TYPE = 'order' THEN EVENT_DATE END) "
       "OVER (PARTITION BY CUSTOMER_ID), MAX(EVENT_DATE) OVER (PARTITION BY CUSTOMER_ID))) AS VAL "
       "FROM EVENTS WHERE EVENT_DATE >= DATE '2025-01-01' GROUP BY 1 ORDER BY 1")


def _hoisted():
    import engine
    return engine._hoist_windows(RAW)


class _Session:
    def __init__(self, response):
        self.response, self.calls = response, []

    def sql(self, text, params=None):
        self.calls.append((text, params))
        session = self

        class R:
            def collect(self_inner):
                return [[json.dumps(session.response)]]
        return R()


class SqlRewrite(unittest.TestCase):
    def test_not_applicable_without_hoisted_fixed(self):
        self.assertIsNone(P.lod_grain_sql("SELECT REGION AS X, SUM(SALES) AS VAL FROM T GROUP BY 1"))
        self.assertIsNone(P.lod_grain_sql(RAW))                       # not hoisted yet
        self.assertIsNone(P.lod_grain_sql("SELECT 1; DROP TABLE T"))

    def test_rewrite_shape_and_read_only(self):
        h = _hoisted()
        self.assertIsNotNone(h)
        out = P.lod_grain_sql(h)
        self.assertIsNotNone(out)
        self.assertTrue(P.read_only(out))
        self.assertIn('SELECT DISTINCT', out)
        self.assertIn('CUSTOMER_ID AS __K0__', out)
        self.assertIn('__G0__ AS X', out)
        self.assertIn('GROUP BY 1', out)
        self.assertEqual(P.partition_keys(h), ['CUSTOMER_ID'])

    def test_executes_per_key_grain(self):
        try:
            import duckdb
        except ImportError:
            self.skipTest('duckdb not installed')
        con = duckdb.connect()
        con.execute("CREATE TABLE EVENTS (CUSTOMER_ID INT, EVENT_DATE TIMESTAMP, EVENT_TYPE VARCHAR)")
        # customer 1: three events on 2025-01-10, span 0->10 days; customer 2: one event that day, span 4 days
        con.execute("""INSERT INTO EVENTS VALUES
            (1, '2025-01-05', 'order'), (1, '2025-01-10', 'view'), (1, '2025-01-10', 'view'),
            (1, '2025-01-10', 'order'), (1, '2025-01-15', 'view'),
            (2, '2025-01-06', 'order'), (2, '2025-01-10', 'view')""")
        h = _hoisted()
        row_grain = dict(con.execute(h).fetchall())
        key_grain = dict(con.execute(P.lod_grain_sql(h)).fetchall())
        day = [k for k in row_grain if str(k).startswith('2025-01-10')][0]
        # row grain: customer 1 counted 3x (10 days), customer 2 once (4 days) -> 34/4 = 8.5
        self.assertAlmostEqual(row_grain[day], 8.5)
        # LOD-key grain: each customer once -> (10 + 4) / 2 = 7
        self.assertAlmostEqual(key_grain[day], 7.0)


    def test_nested_windows_two_hoist_layers(self):
        try:
            import duckdb
        except ImportError:
            self.skipTest('duckdb not installed')
        import engine
        first = "MIN(CASE WHEN EVENT_TYPE = 'order' THEN EVENT_DATE END) OVER (PARTITION BY CUSTOMER_ID)"
        second = (f"MIN(CASE WHEN EVENT_TYPE = 'order' AND EVENT_DATE > ({first}) THEN EVENT_DATE END) "
                  "OVER (PARTITION BY CUSTOMER_ID)")
        raw = (f"SELECT DATE_TRUNC('day', EVENT_DATE) AS X, AVG(DATEDIFF('day', {first}, {second})) AS VAL "
               "FROM EVENTS GROUP BY 1")
        con = duckdb.connect()
        con.execute("CREATE TABLE EVENTS (CUSTOMER_ID INT, EVENT_DATE TIMESTAMP, EVENT_TYPE VARCHAR)")
        con.execute("""INSERT INTO EVENTS VALUES
            (1, '2025-01-01', 'order'), (1, '2025-01-11', 'order'), (1, '2025-01-20', 'view'),
            (1, '2025-01-20', 'view'), (2, '2025-01-02', 'order'), (2, '2025-01-06', 'order'),
            (2, '2025-01-20', 'view')""")
        sql = raw
        for _ in range(3):                      # what engine.q does on the window-in-aggregate error
            try:
                con.execute(sql).fetchall()
                break
            except Exception:
                sql = engine._hoist_windows(sql)
        rows = dict(con.execute(sql).fetchall())
        keyed = dict(con.execute(P.lod_grain_sql(sql)).fetchall())
        day = [k for k in rows if str(k).startswith('2025-01-20')][0]
        self.assertAlmostEqual(rows[day], (10 + 10 + 4) / 3)   # customer 1 counted twice
        self.assertAlmostEqual(keyed[day], (10 + 4) / 2)       # each customer once


class Facts(unittest.TestCase):
    def test_rounding_ruled_out_when_gaps_exceed_precision(self):
        detail = [{'Key': 'Oct 1', 'Measure': 'M', 'Tableau': '60', 'App': '58.7', 'Match': False},
                  {'Key': 'Oct 2', 'Measure': 'M', 'Tableau': '79', 'App': '77.6', 'Match': False},
                  {'Key': 'Oct 3', 'Measure': 'M', 'Tableau': '70', 'App': '70', 'Match': True}]
        f = P.rounding_fact(detail)
        self.assertTrue(f['refuted'])
        self.assertEqual(f['status'], 'Ruled out')

    def test_rounding_possible_when_within_precision(self):
        f = P.rounding_fact([{'Tableau': '60', 'App': '60.4', 'Match': False}])
        self.assertFalse(f['refuted'])


def _run():
    return {'name': 'Book', 'binding_rows': [{'Datasource': 'D', 'Binding': 'Relationship model'}],
            'result': {'ir': {'calcs': {'Avg Days': {'sql': 'AVG(MIN(X) OVER (PARTITION BY CUSTOMER_ID))'}},
                              'dashboards': [{'name': 'Main', 'sheets': [{'name': 'S', 'y': {'caption': 'Avg Days'}}]}]}}}


DETAIL = [{'Key': 'Oct 1', 'Measure': 'Avg Days', 'Tableau': '60', 'App': '58.7', 'Match': False},
          {'Key': 'Oct 3', 'Measure': 'Avg Days', 'Tableau': '70', 'App': '70', 'Match': True}]


class Orchestration(unittest.TestCase):
    def test_menu_enforced_and_code_decides(self):
        run = _run()
        sheet = run['result']['ir']['dashboards'][0]['sheets'][0]
        response = {'ranked': [{'id': 'display_rounding', 'reason': 'small gaps'},
                               {'id': 'invent_new_cause', 'reason': 'x'},
                               {'id': 'lod_key_grain', 'reason': 'FIXED on customer'},
                               {'id': 'null_handling', 'reason': 'maybe'}], 'summary': 's'}
        calls = []

        def fake_render(run, dash, sheet, entry, transform, snowflake):
            calls.append(transform)
            if transform is None:
                return {'Status': 'Difference found', 'Matched values': 1, 'Mismatched values': 1}, [], [], None
            return ({'Status': 'Match', 'Matched values': 2, 'Mismatched values': 0}, [],
                    [{'engine_sql': 'SELECT a', 'tested_sql': 'SELECT b'}], None)
        out = P.prove(_Session(response), run, {'View': 'S'}, DETAIL, sheet, 'Main', {}, render=fake_render)
        ids = [r['id'] for r in out['results']]
        self.assertEqual(ids, ['display_rounding', 'lod_key_grain', 'null_handling'])
        self.assertEqual(out['chosen']['rejected_ids'], ['invent_new_cause'])
        self.assertEqual(out['results'][0]['Result'], 'Ruled out')       # code overrules Cortex's first pick
        self.assertEqual(out['results'][1]['Result'], 'Proven')
        self.assertEqual(out['results'][2]['Result'], 'Untested')
        self.assertTrue(out['harness']['reproduces_app'])
        self.assertIn('Ready for a reviewer', out['conclusion'])
        self.assertIn('Cortex identified this cause (ranked #2)', out['conclusion'])
        self.assertEqual(calls, [None, P.lod_grain_sql])
        self.assertEqual(run['ai_calls'][-1]['Status'], 'Completed') if run.get('ai_calls') else None

    def test_code_tests_lod_grain_even_if_cortex_skips_it(self):
        run = _run()
        sheet = run['result']['ir']['dashboards'][0]['sheets'][0]

        def fake_render(run, dash, sheet, entry, transform, snowflake):
            if transform is None:
                return {'Status': 'Difference found', 'Matched values': 1, 'Mismatched values': 1}, [], [], None
            return ({'Status': 'Match', 'Matched values': 2, 'Mismatched values': 0}, [],
                    [{'engine_sql': 'SELECT a', 'tested_sql': 'SELECT b'}], None)
        out = P.prove(_Session({'ranked': [{'id': 'display_rounding', 'reason': 'r'},
                                           {'id': 'date_grain', 'reason': 'r'}], 'summary': ''}),
                      run, {'View': 'S'}, DETAIL, sheet, 'Main', {}, render=fake_render)
        lod = next(r for r in out['results'] if r['id'] == 'lod_key_grain')
        self.assertEqual(lod['Cortex rank'], 'Not ranked')
        self.assertEqual(lod['Result'], 'Proven')
        self.assertIn('Cortex did not rank this cause', out['conclusion'])

    def test_not_applicable_when_no_rewrite(self):
        run = _run()
        sheet = run['result']['ir']['dashboards'][0]['sheets'][0]

        def fake_render(run, dash, sheet, entry, transform, snowflake):
            return {'Status': 'Difference found', 'Matched values': 1, 'Mismatched values': 1}, [], [], None
        out = P.prove(_Session({'ranked': [{'id': 'lod_key_grain', 'reason': 'r'}], 'summary': ''}),
                      run, {'View': 'S'}, DETAIL, sheet, 'Main', {}, render=fake_render)
        self.assertEqual(out['results'][0]['Result'], 'Not applicable')
        self.assertIn('No hypothesis was proven', out['conclusion'])


class ConfidenceCap(unittest.TestCase):
    def test_diagnosis_never_high_without_a_check(self):
        import v3_ai as A
        run = _run()
        out = A.diagnose(_Session({'likely_causes': ['rounding'], 'checks_to_run': ['c'], 'confidence': 'high'}),
                         run, {'View': 'S'}, DETAIL, run['result']['ir']['dashboards'][0]['sheets'][0])
        self.assertEqual(out['confidence'], 'medium')
        self.assertTrue(out['confidence_capped'])

    def test_touchpoints_label_unproven_adaptation(self):
        import v3_ai as A
        status = {name: st for _s, name, _w, st, _t in A.TOUCHPOINTS}
        self.assertEqual(status['Refused-sheet adaptation proposals'], 'built')
        self.assertEqual(status['Cause proof'], 'live')


class CalcInventory(unittest.TestCase):
    def test_classify_and_summarise_in_two_statements(self):
        import v3_ai as A

        class S:
            def __init__(self):
                self.calls = []

            def sql(self, text, params=None):
                self.calls.append((text, params))

                class R:
                    def collect(_self):
                        if 'AI_CLASSIFY' in text:
                            return [('A', json.dumps({'labels': ['aggregate measure']})),
                                    ('B', json.dumps({'labels': ['made-up label']}))]
                        return [('Mostly aggregate measures.',)]
                return R()
        session, run = S(), {}
        out = A.classify_calculations(session, run, [('A', 'SUM(X)'), ('B', 'MIN(Y) OVER (PARTITION BY K)'), ('C', '')])
        self.assertEqual([r['Cortex category'] for r in out['rows']], ['aggregate measure', 'unclassified'])
        self.assertEqual(out['summary'], 'Mostly aggregate measures.')
        self.assertEqual(len(session.calls), 2)
        self.assertIn('AI_CLASSIFY', session.calls[0][0])
        self.assertIn('AI_AGG', session.calls[1][0])
        self.assertNotIn('SUM(X)', session.calls[0][0])          # calc text only as bound parameters
        self.assertTrue(all(str(r.get('Token source', '')).startswith('Estimated') for r in run['ai_log']))


class AppliedFixTests(unittest.TestCase):
    def test_fix_rewrites_only_while_its_sheet_renders(self):
        import engine
        from unittest.mock import patch
        hoisted = _hoisted()
        seen = []
        with patch.object(engine, '_q_exec', side_effect=lambda q: seen.append(q) or 'ok'):
            engine.ACTIVE_QUERY_FIX = None
            engine._exec_fixed(hoisted)
            self.assertEqual(seen[-1], hoisted)                      # no fix: engine query unchanged
            engine.ACTIVE_QUERY_FIX = 'lod_key_grain'
            engine._exec_fixed(hoisted)
            self.assertEqual(seen[-1], P.lod_grain_sql(hoisted))     # fix: the proven rewrite
            engine._exec_fixed('SELECT REGION AS X, SUM(S) AS V FROM T GROUP BY 1')
            self.assertEqual(seen[-1], 'SELECT REGION AS X, SUM(S) AS V FROM T GROUP BY 1')  # not applicable
            engine.ACTIVE_QUERY_FIX = 'not_a_fix'
            engine._exec_fixed(hoisted)
            self.assertEqual(seen[-1], hoisted)
        engine.ACTIVE_QUERY_FIX = None

    def test_render_sheet_scopes_the_fix(self):
        import engine
        from unittest.mock import patch
        states = []
        with patch.object(engine, '_render_sheet_contained', side_effect=lambda s, w: states.append(engine.ACTIVE_QUERY_FIX)):
            engine.render_sheet({'name': 'A', 'query_fix': 'lod_key_grain'}, '')
            engine.render_sheet({'name': 'B'}, '')
            engine.render_sheet({'name': 'C', 'query_fix': 'DROP TABLE'}, '')
        self.assertEqual(states, ['lod_key_grain', None, None])
        self.assertIsNone(engine.ACTIVE_QUERY_FIX)

    def test_fallback_when_fixed_query_fails(self):
        import engine
        from unittest.mock import patch
        hoisted = _hoisted()
        calls = []

        def fake(q):
            calls.append(q)
            if q != hoisted:
                raise RuntimeError('boom')
            return 'ok'
        with patch.object(engine, '_q_exec', side_effect=fake):
            engine.ACTIVE_QUERY_FIX = 'lod_key_grain'
            self.assertEqual(engine._exec_fixed(hoisted), 'ok')
        engine.ACTIVE_QUERY_FIX = None
        self.assertEqual(calls[0], hoisted)                         # engine's own query ran first
        self.assertEqual(calls[-1], P.lod_grain_sql(hoisted))       # the rewrite was tried, failed, ignored

    def test_applied_fix_gives_exactly_what_the_proof_gave(self):
        """Release 7: proof and fix share one mechanism, end to end through engine.q on real SQL."""
        try:
            import duckdb
        except ImportError:
            self.skipTest('duckdb not installed')
        import engine
        from unittest.mock import patch
        con = duckdb.connect()
        con.execute("CREATE TABLE EVENTS (CUSTOMER_ID INT, EVENT_DATE TIMESTAMP, EVENT_TYPE VARCHAR)")
        con.execute("""INSERT INTO EVENTS VALUES
            (1, '2025-01-05', 'order'), (1, '2025-01-10', 'view'), (1, '2025-01-10', 'view'),
            (1, '2025-01-10', 'order'), (1, '2025-01-15', 'view'),
            (2, '2025-01-06', 'order'), (2, '2025-01-10', 'view')""")

        def run(sql):
            return con.execute(sql).fetchall()

        cached = engine._q_exec

        def values():
            cached.clear()
            return dict(engine.q(RAW))
        with patch.object(engine, 'run_sql', side_effect=run):
            as_migrated = values()
            applied = []
            with P._query_rewrite(P.TRANSFORMS['lod_key_grain'], applied):
                proven = values()
            sheet = {'name': 'Avg. Days to 2nd Purchase', 'query_fix': 'lod_key_grain'}
            fixed = {}
            with patch.object(engine, '_render_sheet_contained', side_effect=lambda s, w: fixed.update(values())):
                engine.render_sheet(sheet, [])
            after_undo = values()
        day = [k for k in as_migrated if str(k).startswith('2025-01-10')][0]
        self.assertAlmostEqual(as_migrated[day], 8.5)
        self.assertTrue(applied)
        self.assertAlmostEqual(proven[day], 7.0)
        self.assertEqual(fixed, proven)                              # what was proven is what ships
        self.assertEqual(after_undo, as_migrated)                    # other renders untouched
        rec = engine.LAST_FIX_RENDER['Avg. Days to 2nd Purchase']
        self.assertGreaterEqual(rec['Queries rewritten'], 1)
        self.assertEqual(rec['Rewrites failed'], 0)

    def test_rewritten_query_is_not_rewritten_again(self):
        once = P.lod_grain_sql(_hoisted())
        self.assertIsNone(P.lod_grain_sql(once))

    def test_fix_lands_on_every_copy_of_the_sheet(self):
        import v3_console as VC
        a, b, other = {'name': 'S'}, {'name': 'S'}, {'name': 'T'}
        run = {'result': {'ir': {'dashboards': [{'name': 'D1', 'sheets': [a, other]}, {'name': 'D2', 'sheets': [b]}]}},
               'events': [], 'missing': ['x']}
        VC._apply_fix(run, 'S', a, {'id': 'lod_key_grain', 'Hypothesis': 'h', 'Evidence': 'e'})
        self.assertEqual([a.get('query_fix'), b.get('query_fix'), other.get('query_fix')],
                         ['lod_key_grain', 'lod_key_grain', None])
        VC._undo_fix(run, 'S', a)
        self.assertFalse(a.get('query_fix') or b.get('query_fix'))


class EvidenceTests(unittest.TestCase):
    def test_referenced_fixed_formula_reaches_cortex_and_facts(self):
        run = {'name': 'Book', 'binding_rows': [{'Binding': 'Relationship model'}],
               'result': {'calc_formulas': {'Avg Days': 'AVG([Days to 2nd])',
                                            'Days to 2nd': '{FIXED [customer_id]: MIN([d])}'},
                          'ir': {'calcs': {'Avg Days': {'sql': 'AVG(DAYS_X)'},
                                           'Days to 2nd': {'sql': 'MIN(D) OVER (PARTITION BY CUSTOMER_ID)'}},
                                 'dashboards': [{'name': 'Main', 'sheets': [{'name': 'S', 'y': {'caption': 'Avg Days'}}]}]}}}
        sheet = run['result']['ir']['dashboards'][0]['sheets'][0]
        self.assertEqual(P.sheet_calcs(run, sheet), ['Avg Days', 'Days to 2nd'])
        self.assertIn('CUSTOMER_ID', P.facts(run, sheet, DETAIL)['fixed_lod_keys_in_sheet_calcs'])
        s = _Session({'ranked': [{'id': 'lod_key_grain', 'reason': 'FIXED per customer'}], 'summary': ''})
        P.choose(s, run, {'View': 'S'}, DETAIL, sheet, P.facts(run, sheet, DETAIL))
        prompt = json.dumps(s.calls[-1][1], default=str)
        self.assertIn('FIXED [customer_id]', prompt)


class ProposedFixTests(unittest.TestCase):
    def _go(self, proposal):
        run = _run()
        sheet = run['result']['ir']['dashboards'][0]['sheets'][0]
        used = []

        def fake_render(run, dash, sheet, entry, transform, snowflake):
            used.append(transform)
            if transform is None:
                return {'Status': 'Difference found', 'Matched values': 1, 'Mismatched values': 1}, [], [], None
            return ({'Status': 'Match', 'Matched values': 2, 'Mismatched values': 0}, [],
                    [{'engine_sql': 'SELECT a', 'tested_sql': 'SELECT b'}], None)
        resp = {'ranked': [{'id': 'lod_key_grain', 'reason': 'FIXED per customer repeated on rows'}], 'summary': 's',
                'proposed_fix': proposal}
        return P.prove(_Session(resp), run, {'View': 'S'}, DETAIL, sheet, 'Main', {}, render=fake_render), used

    def test_cortex_proposed_fix_is_what_code_tests(self):
        out, used = self._go({'hypothesis': 'lod_key_grain', 'lod_key': 'customer_id', 'explanation': 'x'})
        self.assertEqual(getattr(used[1], 'key', None), 'customer_id')        # Cortex's key, not the general rewrite
        lod = next(r for r in out['results'] if r['id'] == 'lod_key_grain')
        self.assertEqual(lod['fix_key'], 'customer_id')
        self.assertIn("Cortex's proposed fix", lod['Test'])
        self.assertIn('Cortex identified this cause (ranked #1) and proposed the fix', out['conclusion'])

    def test_proposed_key_not_in_facts_is_refused(self):
        out, used = self._go({'hypothesis': 'lod_key_grain', 'lod_key': 'PRODUCT_ID', 'explanation': 'x'})
        self.assertIs(used[1], P.lod_grain_sql)                                # general test instead
        lod = next(r for r in out['results'] if r['id'] == 'lod_key_grain')
        self.assertIn('refused', lod['Cortex reason'])
        self.assertIsNone(lod.get('fix_key'))

    def test_key_limited_rewrite_leaves_other_keys_alone(self):
        q = _hoisted()
        self.assertIsNotNone(P.lod_grain_for('CUSTOMER_ID')(q))
        self.assertEqual(P.lod_grain_for('CUSTOMER_ID')(q), P.lod_grain_sql(q))
        self.assertIsNone(P.lod_grain_for('PRODUCT_ID')(q))
        import query_fixes as QF
        self.assertIs(QF.transform_for('lod_key_grain'), QF.lod_grain_sql)
        self.assertEqual(QF.transform_for('lod_key_grain', 'customer_id').key, 'customer_id')


class SheetEvidenceTests(unittest.TestCase):
    def test_workbook_metadata_does_not_pull_every_calculation(self):
        run = {'result': {'calc_formulas': {'Avg Days': 'AVG([Days to 2nd])', 'Days to 2nd': '{FIXED [customer_id]: MIN([d])}',
                                            '1': '1', 'Unused': 'SUM([x])'},
                          'ir': {'calcs': {'Avg Days': {'sql': 'AVG(X)'}, 'Days to 2nd': {'sql': 'MIN(D) OVER (PARTITION BY CUSTOMER_ID)'},
                                           '1': {'sql': '1'}, 'Unused': {'sql': 'SUM(X)'}}}}}
        sheet = {'name': 'S', 'y': {'caption': 'Avg Days', 'internal': 'Avg Days'}, 'filters': [{'value': '1'}],
                 'worksheet_semantics': {'everything': ['Unused', '1', 'Avg Days', 'Days to 2nd']}}
        self.assertEqual(P.sheet_calcs(run, sheet), ['Avg Days', 'Days to 2nd'])
        self.assertEqual(P.tableau_fixed_keys(run, sheet), ['customer_id'])


class GrainFactTests(unittest.TestCase):
    def test_rows_per_key_fact_is_read_only_and_stated(self):
        class S:
            queries = []

            def sql(self, q):
                S.queries.append(q)

                class F:
                    columns = ['EVENT_DATE', 'CUSTOMER_ID', 'X']

                    def collect(_s):
                        return [{'POINTS': 21, 'AVG_ROWS_PER_KEY': 2.4, 'MAX_ROWS_PER_KEY': 6, 'POINTS_WITH_REPEATS': 21}.values()]
                return F()
        run = {'bindings': {'D': 'DB.S.EVENTS'}}
        sheet = {'datasource': 'D', 'x': {'field_id': 'fed::event_date', 'datepart': 'tdy'}}
        g = P.grain_fact(S(), run, sheet, ['CUSTOMER_ID'])
        self.assertEqual(g['status'], 'computed')
        self.assertEqual(g['points_with_more_rows_than_keys'], 21)
        self.assertIn("DATE_TRUNC('day', EVENT_DATE)", S.queries[-1])
        self.assertTrue(P.read_only(S.queries[-1]))
        self.assertIn('repeated', g['text'])
        self.assertEqual(P.grain_fact(None, run, sheet, ['CUSTOMER_ID'])['status'], 'not computed')


class RealWorkbookEvidence(unittest.TestCase):
    def test_ecommerce_avg_days_evidence_is_small_and_has_the_fixed_formula(self):
        wb = Path(__file__).resolve().parents[1] / 'Workbooks' / 'ECommerce_Live_Relationship_Events_Customers_Products.twbx'
        if not wb.exists():
            self.skipTest('ECommerce workbook not in this checkout')
        import v3_local
        res = v3_local.prepare_payload(wb.read_bytes(), wb.name)
        sheet = next(s for d in res['ir']['dashboards'] for s in d['sheets'] if s.get('name') == 'Avg. Days to 2nd Purchase')
        run = {'result': res}
        names = P.sheet_calcs(run, sheet)
        self.assertLess(len(names), 25)
        self.assertTrue(any('FIXED' in (res['calc_formulas'].get(n) or '') for n in names), names)
        self.assertIn('customer_id', [k.lower() for k in P.tableau_fixed_keys(run, sheet)])


class R14Tests(unittest.TestCase):
    def test_grain_fact_scoped_to_the_chart_points(self):
        class S:
            queries = []

            def sql(self, q):
                S.queries.append(q)

                class F:
                    columns = ['EVENT_DATE', 'CUSTOMER_ID']

                    def collect(_s):
                        return [{'POINTS': 3, 'AVG_ROWS_PER_KEY': 1.5, 'MAX_ROWS_PER_KEY': 2, 'POINTS_WITH_REPEATS': 2,
                                 'DIFF_POINTS': 1, 'DIFF_AVG_ROWS_PER_KEY': 2.0, 'ONE': 1}.values()]
                return F()
        detail = [{'Key': 'October 18, 2025', 'Match': False}, {'Key': 'October 17, 2025', 'Match': True},
                  {'Key': 'October 16, 2025', 'Match': True}]
        g = P.grain_fact(S(), {'bindings': {'D': 'DB.S.E'}},
                         {'datasource': 'D', 'x': {'field_id': 'f::event_date', 'datepart': 'tdy'}}, ['CUSTOMER_ID'], detail)
        q = S.queries[-1]
        self.assertIn("DATE '2025-10-16'", q)
        self.assertIn("B::DATE IN (DATE '2025-10-18')", q)
        self.assertEqual(g['differing_points'], 1)
        self.assertIn('on the 3 chart points', g['text'])
        self.assertTrue(P.read_only(q))

    def test_cut_off_cortex_answer_is_retried_once(self):
        good = {'ranked': [{'id': 'lod_key_grain', 'reason': 'r'}], 'summary': 's'}

        class S:
            n = 0

            def sql(self, text, params=None):
                S.n += 1

                class R:
                    def collect(_s):
                        return [['{"ranked": [{"id": "lod_'] if S.n == 1 else [json.dumps(good)]]
                return R()
        run = _run()
        sheet = run['result']['ir']['dashboards'][0]['sheets'][0]
        out = P.choose(S(), run, {'View': 'S'}, DETAIL, sheet, P.facts(run, sheet, DETAIL))
        self.assertEqual(S.n, 2)
        self.assertEqual(out['ranked'][0]['id'], 'lod_key_grain')


if __name__ == '__main__':
    unittest.main()
