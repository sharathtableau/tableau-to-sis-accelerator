"""File/extract datasources reuse a table that already exists in Snowflake, verified, never written."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import v3_console as VC


def _run():
    ir = {'field_scopes': {'ds1': {'caption': 'Sample - Superstore'}, 'ds2': {'caption': 'Sales Target'}},
          'fields': {'a': {'datasource_id': 'ds1', 'physical': 'SALES', 'datatype': 'real'},
                     'b': {'datasource_id': 'ds2', 'physical': 'TARGET', 'datatype': 'real'}}}
    return {'result': {'ir': ir}, 'bindings': {'Sample - Superstore': None, 'Sales Target': None},
            'missing': ['Sample - Superstore', 'Sales Target'],
            'binding_rows': [{'Datasource': 'Sample - Superstore'}, {'Datasource': 'Sales Target'}]}


class AutoBindTests(unittest.TestCase):
    def test_existing_tables_are_bound_after_verification_and_missing_ones_stay_explicit(self):
        run = _run()
        found = {'SAMPLE_SUPERSTORE': ('WBR_DB.PIPELINE_DEMO.SAMPLE_SUPERSTORE', 'existing')}
        with patch.object(VC, '_runtime_sf', return_value=True), \
                patch.object(VC.backend, '_active_session', return_value=object()), \
                patch('pipeline.resolve_existing_table', side_effect=lambda s, db, sc, t: found.get(t, (None, None))), \
                patch('v3_source_binding.validate_table', side_effect=lambda s, t, f: t) as check:
            bound = VC._auto_bind(run)
        self.assertEqual(bound, ['Sample - Superstore'])
        self.assertEqual(run['bindings']['Sample - Superstore'], 'WBR_DB.PIPELINE_DEMO.SAMPLE_SUPERSTORE')
        self.assertEqual(run['missing'], ['Sales Target'])
        self.assertEqual(check.call_count, 1)
        self.assertIn('auto-detected', run['binding_rows'][0]['Binding'])

    def test_a_table_that_fails_the_check_is_not_bound(self):
        run = _run()
        with patch.object(VC, '_runtime_sf', return_value=True), \
                patch.object(VC.backend, '_active_session', return_value=object()), \
                patch('pipeline.resolve_existing_table', return_value=('WBR_DB.PIPELINE_DEMO.X', 'existing')), \
                patch('v3_source_binding.validate_table', side_effect=ValueError('missing physical column SALES')):
            self.assertEqual(VC._auto_bind(run), [])
        self.assertEqual(len(run['missing']), 2)
        self.assertIn('missing physical column', run['auto_bind_notes']['Sample - Superstore'])

    def test_a_table_with_engine_column_names_is_reused(self):
        """Release 23: tables made by the original accelerator use SALES_TARGET for 'Sales Target'."""
        run = _run()
        run['result']['ir']['fields']['b']['physical'] = 'Sales Target'
        calls = []

        def check(s, t, f):
            calls.append([x['physical'] for x in f])
            if any(' ' in x['physical'] for x in f):
                raise ValueError('missing physical column Sales Target')
            return t
        found = {'SALES_TARGET': ('WBR_DB.PIPELINE_DEMO.SALES_TARGET', 'existing')}
        with patch.object(VC, '_runtime_sf', return_value=True), \
                patch.object(VC.backend, '_active_session', return_value=object()), \
                patch('pipeline.resolve_existing_table', side_effect=lambda s, db, sc, t: found.get(t, (None, None))), \
                patch('v3_source_binding.validate_table', side_effect=check):
            bound = VC._auto_bind(run)
        self.assertEqual(bound, ['Sales Target'])
        self.assertEqual(calls, [['Sales Target'], ['SALES_TARGET']])
        self.assertIn('engine column name', run['binding_rows'][1]['Status'])

    def test_engine_names_still_need_every_field(self):
        run = _run()
        run['result']['ir']['fields']['b']['physical'] = 'Sales Target'
        found = {'SALES_TARGET': ('WBR_DB.PIPELINE_DEMO.SALES_TARGET', 'existing')}
        with patch.object(VC, '_runtime_sf', return_value=True), \
                patch.object(VC.backend, '_active_session', return_value=object()), \
                patch('pipeline.resolve_existing_table', side_effect=lambda s, db, sc, t: found.get(t, (None, None))), \
                patch('v3_source_binding.validate_table', side_effect=ValueError('missing physical column X')):
            self.assertEqual(VC._auto_bind(run), [])
        self.assertIn('missing physical column', run['auto_bind_notes']['Sales Target'])

    def test_local_preview_never_queries(self):
        run = _run()
        with patch.object(VC, '_runtime_sf', return_value=False):
            self.assertEqual(VC._auto_bind(run), [])


if __name__ == '__main__':
    unittest.main()
