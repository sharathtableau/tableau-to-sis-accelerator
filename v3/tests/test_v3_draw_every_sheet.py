"""Release 18: every run draws every sheet; one draw rule for the console, report and EN07 gate; results in EN01."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import v3_console as VC
import v3_evidence as EV
import v3_store as ST


class DrawRuleTests(unittest.TestCase):
    def test_render_status(self):
        none = {'error': [], 'warning': []}
        self.assertEqual(EV.render_status('ValueError: x', True, 5, none)[0], 'Error')
        self.assertEqual(EV.render_status(None, True, 5, {'error': ['boom'], 'warning': []})[0], 'Error')
        status, note = EV.render_status(None, False, None, {'error': [], 'warning': ['S: measure not convertible']})
        self.assertEqual(status, 'Error')
        self.assertIn('warning instead of its content', note)
        self.assertEqual(EV.render_status(None, True, 4, {'error': [], 'warning': ['Filter R: could not load']})[0],
                         'Yes (filter warning)')
        self.assertEqual(EV.render_status(None, True, 4, {'error': [], 'warning': ['S: note']})[0], 'Yes')
        self.assertEqual(EV.render_status(None, True, 0, none)[0], 'Yes (0 rows)')
        self.assertEqual(EV.render_status(None, False, None, none)[0], 'Yes (text/HTML only)')

    def test_messages_shown_while_drawing_are_recorded_and_passed_through(self):
        import streamlit as st
        seen = []
        with patch.object(st, 'warning', side_effect=lambda *a, **k: seen.append(a[0])):
            with EV._ShownMessages() as said:
                st.warning('Sheet: nothing convertible to show')
            st.warning('after')
        self.assertEqual(said['warning'], ['Sheet: nothing convertible to show'])
        self.assertEqual(seen, ['Sheet: nothing convertible to show', 'after'])     # still shown, then restored


class EveryRunDrawsTests(unittest.TestCase):
    def test_without_a_tableau_reference_the_run_still_draws_every_sheet(self):
        run = {'result': {'ir': {'dashboards': [{'name': 'D', 'sheets': [{'name': 'A'}, {'name': 'B'}]}]}}}

        def checks(r, sf):
            r['sheet_checks'] = [{'Renders on data': 'Yes'}, {'Renders on data': 'Error'}]
        with patch('v3_evidence.sheet_checks', side_effect=checks) as drawn, \
                patch.object(VC, '_compare_capture') as compare:
            steps = list(VC._auto_validation(run, lambda t: None))
        drawn.assert_called_once()
        compare.assert_not_called()
        self.assertEqual([t for t, _x, _o in steps], ['Rendering every sheet on Snowflake'])
        summary = VC._auto_validation_summary(run)
        self.assertIn('1 of 2 sheets draw on Snowflake, 1 with an error', summary)
        self.assertIn('no Tableau reference loaded yet', summary)


class StoreTests(unittest.TestCase):
    def test_draw_results_saved_once_per_change_and_failures_view_exists(self):
        from test_v3_store import StoreSession, _run, _source
        ST._READY.clear()
        sess, run = StoreSession(), _run()
        run['sheet_checks'] = [{'Dashboard': 'Main', 'Sheet': 'KPI', 'Kind': 'kpi', 'Engine status': 'UNVERIFIED',
                                'Renders on data': 'Error', 'Rows': None, 'Output': '—',
                                'Note': 'The sheet showed a warning instead of its content: x'}]
        with patch('backend._active_session', return_value=sess):
            ST.start(run, _source())
            ST.checkpoint(run)
            ST.checkpoint(run)
            run['sheet_checks'][0].update({'Renders on data': 'Yes', 'Rows': 3, 'Note': ''})
            ST.checkpoint(run)
        rows = sess.tables['SHEET_RENDERS']
        self.assertEqual([(r['PASS_NO'], r['RENDERS']) for r in rows], [(1, 'Error'), (2, 'Yes')])
        ddl = ST.ddl()
        self.assertIn('V_FAILURES', ddl)
        self.assertIn("'Sheet does not draw'", ddl)
        self.assertIn('CREATE TABLE IF NOT EXISTS ' + ST.SCHEMA + '.SHEET_RENDERS', ddl)


if __name__ == '__main__':
    unittest.main()
