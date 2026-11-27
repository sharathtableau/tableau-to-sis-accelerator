"""EN01 run and evidence store: append-only, never blocks a run, no Cortex error text, approvals name the
person, a reopened run shows the verdicts it recorded."""
from collections import defaultdict
from pathlib import Path
import json
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import v3_ai as A
import v3_store as ST

ROOT = Path(__file__).resolve().parents[1]
LEAK = 'service text that must not leak'


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def collect(self):
        return self.rows


class StoreSession:
    """In-memory stand-in for the evidence tables: records every INSERT, answers the store's SELECTs."""

    def __init__(self, fail_insert=False, ready=True):
        self.tables, self.sql_text, self.fail_insert, self.is_ready = defaultdict(list), [], fail_insert, ready

    def sql(self, text, params=None):
        t = ' '.join(text.split())
        self.sql_text.append(t)
        if 'INFORMATION_SCHEMA.TABLES' in t:
            return _Rows([[len(ST.TABLES) if self.is_ready else 3]])
        if t.startswith('SELECT CURRENT_USER()'):
            return _Rows([['SF_TESTER']])
        if t.startswith('INSERT INTO'):
            if self.fail_insert:
                raise RuntimeError(LEAK)
            table = t.split()[2].split('.')[-1]
            self.tables[table] += [json.loads(p) for p in params]
            return _Rows([])
        if 'V_RUN_LATEST' in t:
            out = []
            hidden = {h['RUN_ID'] for h in self.tables['HIDDEN_RUNS']} if 'HIDDEN_RUNS' in t else set()
            for r in self.tables['RUNS']:
                if r['RUN_ID'] in hidden:
                    continue
                snaps = [s for s in self.tables['RUN_SNAPSHOTS'] if s['RUN_ID'] == r['RUN_ID'] and s['PART_NO'] == 0]
                if snaps:
                    s = max(snaps, key=lambda x: x['SNAPSHOT_NO'])
                    out.append([r['RUN_ID'], r['WORKBOOK_NAME'], r['ORIGIN'], r['STARTED_AT'], r['STARTED_BY'],
                                s['SNAPSHOT_NO'], s['SNAPSHOT_AT'], s['FAILED_STAGE']])
            return _Rows(out)
        if 'CONTENT_GZ_B64' in t:
            rid = params[0]
            snaps = [s for s in self.tables['RUN_SNAPSHOTS'] if s['RUN_ID'] == rid]
            done = [s['SNAPSHOT_NO'] for s in snaps if s['PART_NO'] == 0]
            if not done:
                return _Rows([])
            no = max(done)
            rows = sorted((s for s in snaps if s['SNAPSHOT_NO'] == no), key=lambda s: s['PART_NO'])
            return _Rows([[s['SNAPSHOT_NO'], s['PART_NO'], s['PARTS'], s['SHA256'], s['CONTENT_GZ_B64']] for s in rows])
        if t.startswith('SELECT (SELECT COALESCE'):
            rid = params[0]

            def mx(table, col):
                return max([r[col] for r in self.tables[table] if r['RUN_ID'] == rid] or [0])
            return _Rows([[mx('RUN_EVENTS', 'EVENT_NO'), mx('CORTEX_CALLS', 'CALL_NO'), mx('SHEET_RESULTS', 'PASS_NO'),
                           mx('CORTEX_PROPOSALS', 'PROPOSAL_NO'), mx('PROOFS', 'PROOF_NO')]])
        raise AssertionError('unexpected SQL from the store: ' + t[:120])


class CortexSession:
    def __init__(self, response=None, fail=False):
        self.response, self.fail = response, fail

    def sql(self, text, params=None):
        if self.fail:
            raise RuntimeError(LEAK)
        return _Rows([[json.dumps({'structured_output': [{'raw_message': self.response}],
                                   'usage': {'prompt_tokens': 120, 'completion_tokens': 30}})]])


def _run():
    proof = {'chosen': {'ranked': [{'id': 'lod_key_grain', 'reason': 'FIXED per customer'}], 'summary': 's',
                        'proposed_fix': {'hypothesis': 'lod_key_grain', 'lod_key': 'CUSTOMER_ID', 'explanation': 'e'}},
             'results': [{'Cortex rank': 1, 'Hypothesis': 'FIXED values counted once per LOD key', 'id': 'lod_key_grain',
                          'Cortex reason': 'FIXED per customer', 'Test': "Cortex's proposed fix", 'Result': 'Proven',
                          'Evidence': '21 of 21 values match', 'fix_key': 'CUSTOMER_ID', 'sql': 'SELECT 1',
                          'engine_sql': 'SELECT 2'},
                         {'Cortex rank': 'Not ranked', 'Hypothesis': 'Rounding', 'id': 'rounding',
                          'Cortex reason': '—', 'Test': 'Fact', 'Result': 'Ruled out', 'Evidence': 'gap too large'}],
             'conclusion': 'Proven by code'}
    return {'name': 'Any book', 'origin': 'Tableau Cloud', 'states': {'Discovery': 'done'},
            'events': [('Discovery complete', '1 datasource', 'done')], 'details': {}, 'failed': None,
            'result': {'ir': {'dashboards': [{'name': 'Main', 'sheets': [{'name': 'KPI'}]}],
                              'ai_migration_review': {'title': 'Review', 'reason': 'ok'}},
                       'assessment': {'sheets': [{'dashboard': 'Main', 'sheet': 'KPI', 'state': 'UNVERIFIED'}],
                                      'issues': []}},
            'bindings': {'D': 'DB.S.T'}, 'missing': [],
            'calc_xl': {'score': {'Agree': 1}, 'rows': [
                {'Calculation': 'Profit ratio', 'Tableau formula': 'SUM([P])/SUM([S])', 'Engine translation': 'x',
                 'Cortex translation': 'SUM(P)/SUM(S)', 'Result': 'Agree', 'Evidence': 'equal'},
                {'Calculation': 'Flag', 'Tableau formula': 'IF', 'Engine translation': 'y',
                 'Cortex translation': 'DROP', 'Result': 'Rejected by allowlist', 'Evidence': 'not allowed'}]},
            'proofs': {'KPI': proof},
            'sheet_reference': {'meta': 'capture', 'results': {
                'KPI': ({'Status': 'Difference found', 'Matched values': 0, 'Mismatched values': 21}, [
                    {'Key': '2024-01-01', 'Tableau': 1.0, 'App': 2.0, 'Match': False}])}},
            'sheet_values': {('Main', 'KPI'): {'rows': [1, 2], 'total': 2}},
            'capture_bytes': b'{"sheets": {}}'}


def _source():
    return {'origin': 'tableau-cloud', 'name': 'Any book', 'server_url': 'https://t.example', 'site_content_url': 's',
            'workbook_id': 'w-1'}


def _all_text(sess):
    return json.dumps(sess.tables, default=str) + ' '.join(sess.sql_text)


class StoreTests(unittest.TestCase):
    def setUp(self):
        ST._READY.clear()
        ST._SF_USER.clear()

    def test_setup_script_is_generated_from_the_table_definitions(self):
        self.assertEqual((ROOT / 'sql' / 'en01_evidence_store.sql').read_text(), ST.ddl())
        script = ST.ddl()
        for name in list(ST.TABLES) + list(ST.VIEWS):
            self.assertIn(f'{ST.SCHEMA}.{name}', script)
        self.assertNotIn('DROP ', script.upper())
        self.assertNotIn('GRANT ', script.upper())
        for table, (_c, cols) in ST.TABLES.items():
            sql = ST.insert_sql(table, 2)
            self.assertTrue(sql.startswith(f'INSERT INTO {ST.SCHEMA}.{table} ('))
            self.assertEqual(sql.count('(?)'), 2)
            for c, _t in cols:
                self.assertIn(f'v:"{c}"', sql)

    def test_without_a_store_nothing_is_written_and_nothing_changes(self):
        run = _run()
        with patch('backend._active_session', return_value=None):
            self.assertIsNone(ST.start(run, _source()))
            ST.checkpoint(run)
            self.assertIsNone(ST.approve(run, 'apply_fix', 'KPI'))
        self.assertNotIn('_store', run)
        sess = StoreSession(ready=False)
        with patch('backend._active_session', return_value=sess):
            self.assertIsNone(ST.start(run, _source()))
        self.assertFalse(sess.tables)

    def test_checkpoint_appends_each_kind_of_evidence_once(self):
        sess, run = StoreSession(), _run()
        with patch('backend._active_session', return_value=sess):
            ST.start(run, _source())
            ST.note_workbook(run, b'workbook bytes')
            A.call(CortexSession({'answer': 'ok'}), run, 'Validation', 'Ask', 'prompt text', schema={'type': 'object'})
            ST.checkpoint(run, 'Validation')
            first = {k: len(v) for k, v in sess.tables.items() if v}
            ST.checkpoint(run)                                   # nothing new: nothing written
            self.assertEqual({k: len(v) for k, v in sess.tables.items() if v}, first)
            run['events'].insert(0, ('Proven fix applied', 'KPI', 'done'))
            ST.checkpoint(run)
        t = sess.tables
        self.assertEqual(len(t['RUNS']), 1)
        self.assertEqual(t['RUNS'][0]['SOURCE_REF'], 'https://t.example/s/w-1')
        self.assertEqual(len(t['RUNS'][0]['WORKBOOK_SHA256']), 64)
        self.assertEqual([e['EVENT_NO'] for e in t['RUN_EVENTS']], [1, 2])
        call = t['CORTEX_CALLS'][-1]
        self.assertEqual((call['INPUT_TOKENS'], call['OUTPUT_TOKENS'], call['FUNCTION']), (120, 30, 'AI_COMPLETE'))
        self.assertEqual(call['RESPONSE'], {'answer': 'ok'})
        self.assertEqual(len(call['PROMPT_SHA256']), 64)
        self.assertNotIn('prompt text', _all_text(sess))          # prompts are kept only as a hash
        kinds = {(p['KIND'], p['SUBJECT']): p for p in t['CORTEX_PROPOSALS']}
        self.assertTrue(kinds[('calc_translation', 'Profit ratio')]['PASSED_CODE_CHECK'])
        self.assertFalse(kinds[('calc_translation', 'Flag')]['PASSED_CODE_CHECK'])
        self.assertEqual(kinds[('cause_ranking', 'KPI')]['CODE_VERDICT'], 'Ranked cause proven (#1)')
        self.assertEqual(kinds[('proposed_fix', 'KPI')]['CODE_VERDICT'], 'Proven')
        self.assertIsNone(kinds[('workbook_review', 'workbook')]['PASSED_CODE_CHECK'])
        self.assertEqual(len(t['PROOFS']), 2)
        self.assertEqual(len({p['PROOF_KEY'] for p in t['PROOFS']}), 1)
        self.assertEqual([(s['SHEET'], s['STATUS'], s['PASS_NO']) for s in t['SHEET_RESULTS']],
                         [('KPI', 'Difference found', 1)])
        self.assertGreaterEqual(len([s for s in t['RUN_SNAPSHOTS'] if s['PART_NO'] == 0]), 1)
        for text in sess.sql_text:                             # the store only inserts and reads
            self.assertTrue(text.startswith(('INSERT INTO ' + ST.SCHEMA, 'SELECT')), text[:60])

    def test_a_failed_save_never_breaks_the_run_and_never_shows_service_text(self):
        sess, run = StoreSession(fail_insert=True), _run()
        with patch('backend._active_session', return_value=sess):
            ST.start(run, _source())
            ST.checkpoint(run, 'Discovery')
            self.assertIsNone(ST.approve(run, 'apply_fix', 'KPI'))
            ST.fix(run, None, 'KPI', 'applied')
            ST.deployed(run, None, error=RuntimeError(LEAK))
        level, text = ST.status(run)
        self.assertEqual(level, 'warn')
        self.assertIn('RuntimeError', text)
        self.assertNotIn(LEAK, text)
        self.assertNotIn(LEAK, json.dumps(run['_store']))

    def test_cortex_failure_is_stored_as_the_exception_class_only(self):
        sess, run = StoreSession(), _run()
        with patch('backend._active_session', return_value=sess):
            ST.start(run, _source())
            with self.assertRaises(A.CortexUnavailable):
                A.call(CortexSession(fail=True), run, 'Validation', 'Diagnose', 'p', schema={'type': 'object'})
            ST.checkpoint(run)
        failed = [c for c in sess.tables['CORTEX_CALLS'] if c['STATUS'] == 'Failed']
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]['NOTE'], 'RuntimeError')
        self.assertIsNone(failed[0]['RESPONSE'])
        self.assertNotIn(LEAK, _all_text(sess))

    def test_activity_rows_on_screen_do_not_show_store_fields(self):
        run = _run()
        A.call(CortexSession({'answer': 'ok'}), run, 'Validation', 'Ask', 'p', schema={'type': 'object'})
        for row in A.activity(run):
            self.assertFalse([k for k in row if str(k).startswith('_')])

    def test_approval_names_the_person_and_links_fix_and_deployment(self):
        sess, run = StoreSession(), _run()
        with patch('backend._active_session', return_value=sess), \
                patch('v3_store.viewer', return_value=('sharath@example.com', 'st.user.email')):
            ST.start(run, _source())
            aid = ST.approve(run, 'apply_fix', 'KPI', {'fix_key': 'CUSTOMER_ID'})
            ST.fix(run, aid, 'KPI', 'applied', 'lod_key_grain', 'CUSTOMER_ID',
                   ST.proof_key(run['proofs']['KPI']), {'Proposed by': 'Cortex (ranked #1)'})
            did = ST.approve(run, 'deploy', 'WBR_DB.S.APP')
            ST.deployed(run, did, {'identifier': 'WBR_DB.S.APP', 'build_sha256': 'b' * 64, 'files': 9})
        a = {x['ACTION']: x for x in sess.tables['APPROVALS']}
        self.assertEqual(a['apply_fix']['APPROVED_BY'], 'sharath@example.com')
        self.assertEqual(a['apply_fix']['SNOWFLAKE_USER'], 'SF_TESTER')
        self.assertEqual(a['apply_fix']['IDENTITY_SOURCE'], 'st.user.email')
        self.assertTrue(a['apply_fix']['APPROVED_AT'])
        f = sess.tables['FIXES'][0]
        self.assertEqual((f['APPROVAL_ID'], f['FIX_KEY']), (aid, 'CUSTOMER_ID'))
        self.assertEqual(f['PROOF_KEY'], sess.tables['PROOFS'][0]['PROOF_KEY'])
        self.assertEqual(sess.tables['DEPLOYMENTS'][0]['APPROVAL_ID'], did)
        # Without a Streamlit viewer, the Snowflake user is recorded and the source says so.
        with patch('backend._active_session', return_value=sess), patch('v3_store.viewer', return_value=(None, None)):
            ST.approve(run, 'undo_fix', 'KPI')
        last = sess.tables['APPROVALS'][-1]
        self.assertEqual((last['APPROVED_BY'], last['IDENTITY_SOURCE']), ('SF_TESTER', 'CURRENT_USER()'))

    def test_snapshot_round_trip_keeps_tuples_tuple_keys_and_bytes(self):
        run = _run()
        run['package'] = {'data': b'zip', 'build': 'x'}
        text, _size = ST.dumps(run)
        back = ST.loads(text)
        self.assertNotIn('package', back)
        expected = {k: v for k, v in run.items() if k != 'package'}
        self.assertEqual(back, expected)
        self.assertIsInstance(back['events'][0], tuple)
        self.assertIn(('Main', 'KPI'), back['sheet_values'])
        self.assertEqual(back['capture_bytes'], run['capture_bytes'])

    def test_reopened_run_has_the_recorded_verdicts_and_saves_nothing_twice(self):
        sess, run = StoreSession(), _run()
        with patch('backend._active_session', return_value=sess), patch('v3_store.PART_CHARS', 200):
            ST.start(run, _source())
            ST.checkpoint(run, 'Validation', reason='run complete')
            parts = [s for s in sess.tables['RUN_SNAPSHOTS'] if s['SNAPSHOT_NO'] == 1]
            self.assertGreater(len(parts), 1)
            self.assertEqual(parts[-1]['PART_NO'], 0)                 # part 0 written last
            listed = ST.recent_runs(sess)
            self.assertEqual(listed[0]['RUN_ID'], run['_store']['run_id'])
            before = {k: len(v) for k, v in sess.tables.items() if v}
            back = ST.reopen(sess, run['_store']['run_id'])
            ST.checkpoint(back)
            self.assertEqual({k: len(v) for k, v in sess.tables.items() if v}, before)
            back['events'].insert(0, ('Run reopened', 'x', 'done'))
            ST.checkpoint(back)
        self.assertEqual(back['sheet_reference']['results']['KPI'][0]['Status'], 'Difference found')
        self.assertEqual(back['proofs']['KPI']['conclusion'], 'Proven by code')
        self.assertEqual(sess.tables['RUN_EVENTS'][-1]['EVENT_NO'], 2)
        self.assertEqual(back['_store']['run_id'], run['_store']['run_id'])

    def test_incomplete_or_altered_snapshot_is_refused(self):
        sess, run = StoreSession(), _run()
        with patch('backend._active_session', return_value=sess):
            ST.start(run, _source())
            ST.checkpoint(run)
            rid = run['_store']['run_id']
            sess.tables['RUN_SNAPSHOTS'][-1]['CONTENT_GZ_B64'] = sess.tables['RUN_SNAPSHOTS'][-1]['CONTENT_GZ_B64'][:-8]
            with self.assertRaises(ValueError):
                ST.reopen(sess, rid)

    def test_no_workbook_or_sheet_names_in_the_store(self):
        source = (ROOT / 'v3_store.py').read_text().lower()
        for name in ('ecommerce', 'e-commerce', 'superstore', 'days to 2nd', 'customer_id', 'sales dashboard'):
            self.assertNotIn(name, source)


class ConsoleReopenTests(unittest.TestCase):
    def test_reopened_run_renders_every_page_with_its_recorded_verdicts(self):
        from streamlit.testing.v1 import AppTest
        from test_v3_console import CATALOG, _packaged, ConsoleTests
        app = str(ROOT / 'v3_console.py')
        with patch('tableau_server.parse_site_url', return_value={'server_url': 'https://example.test', 'site_content_url': 's'}), \
                patch('tableau_server.list_site_contents', return_value=CATALOG), \
                patch('tableau_server.fetch_workbook_by_id', return_value={'filename': 'remote.twbx', 'bytes': _packaged(), 'workbook_id': 'w-1'}), \
                patch('pipeline.onboard'), patch('pipeline.deploy_model_view'):
            at = AppTest.from_file(app, default_timeout=60).run()
            ConsoleTests._run_cloud(None, at)
            self.assertFalse(at.exception)
            original = at.session_state['v3c_run']
        restored = ST.loads(ST.dumps(original)[0])
        again = AppTest.from_file(app, default_timeout=60)
        again.session_state['v3c_run'] = restored
        again.run()
        for page in ('New migration', 'Inventory', 'Preview', 'Validation', 'Cortex AI', 'Deploy'):
            again.radio(key='v3c_page').set_value(page).run()
            self.assertFalse(again.exception, page)
        run = again.session_state['v3c_run']
        self.assertEqual(run['states'], original['states'])
        self.assertEqual([r['state'] for r in run['result']['assessment']['sheets']],
                         [r['state'] for r in original['result']['assessment']['sheets']])

    def test_overview_lists_saved_runs_and_reopen_restores_one(self):
        from streamlit.testing.v1 import AppTest
        app = str(ROOT / 'v3_console.py')
        sess, run = StoreSession(), _run()
        with patch('backend._active_session', return_value=sess):
            ST.start(run, _source())
            ST.checkpoint(run, 'Validation', reason='run complete')
            at = AppTest.from_file(app, default_timeout=60).run()
            self.assertFalse(at.exception)
            button = next(b for b in at.button if b.label == 'Reopen')
            button.click().run()
            self.assertFalse(at.exception)
            back = at.session_state['v3c_run']
            self.assertEqual(back['_store']['run_id'], run['_store']['run_id'])
            self.assertEqual(back['sheet_reference']['results']['KPI'][0]['Status'], 'Difference found')
            self.assertEqual(back['events'][0][0], 'Run reopened')
            self.assertTrue(any('Saved in Snowflake' in c.value for c in at.caption))
        # the reopen event was appended to the same run, numbered after the saved ones
        self.assertEqual(sess.tables['RUN_EVENTS'][-1]['TITLE'], 'Run reopened')
        self.assertEqual(len(sess.tables['RUNS']), 1)


class HiddenRunsTests(unittest.TestCase):
    """Release 23b: Saved runs is collapsed; a person can remove runs from it (the evidence stays)."""
    def setUp(self):
        ST._READY.clear()
        ST._REG_READY.clear()

    def _ready(self):
        real = ST.registry_ready
        return patch.object(ST, 'registry_ready', side_effect=lambda s, t='SEMANTIC_VIEW_EVENTS': t == 'HIDDEN_RUNS' or real(s, t))

    def test_removed_runs_leave_the_list_and_keep_their_evidence(self):
        sess, run = StoreSession(), _run()
        with patch('backend._active_session', return_value=sess), self._ready():
            ST.start(run, _source())
            ST.checkpoint(run, 'Validation', reason='run complete')
            rid = run['_store']['run_id']
            self.assertEqual([r['RUN_ID'] for r in ST.recent_runs(sess)], [rid])
            self.assertEqual(ST.hide_runs(sess, [rid]), 1)
            self.assertEqual(ST.recent_runs(sess), [])
        self.assertEqual(sess.tables['HIDDEN_RUNS'][0]['RUN_ID'], rid)
        self.assertEqual(len(sess.tables['RUNS']), 1)                      # nothing deleted
        self.assertFalse([t for t in sess.sql_text if t.startswith(('DELETE', 'UPDATE', 'DROP'))])

    def test_without_the_table_nothing_is_written(self):
        sess = StoreSession()
        self.assertEqual(ST.hide_runs(sess, ['r1']), 0)
        self.assertNotIn('HIDDEN_RUNS', sess.tables)

    def test_overview_list_is_collapsed_and_remove_works(self):
        from streamlit.testing.v1 import AppTest
        sess, run = StoreSession(), _run()
        with patch('backend._active_session', return_value=sess), self._ready():
            ST.start(run, _source())
            ST.checkpoint(run, 'Validation', reason='run complete')
            at = AppTest.from_file(str(ROOT / 'v3_console.py'), default_timeout=60).run()
            self.assertFalse(at.exception)
            box = next(e for e in at.expander if e.label.startswith('Saved runs'))
            self.assertEqual(box.label, 'Saved runs (1)')
            self.assertFalse(box.proto.expanded)
            next(b for b in at.button if b.label == 'Remove').click().run()
            self.assertFalse(at.exception)
            self.assertFalse([e for e in at.expander if e.label.startswith('Saved runs')])
        self.assertEqual(len(sess.tables['HIDDEN_RUNS']), 1)


if __name__ == '__main__':
    unittest.main()
