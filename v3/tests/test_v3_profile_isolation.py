"""A migration filename cannot select curated SQL, colors or UI semantics."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
import engine
import calc_translator
from test_v3_field_identity import workbook, parse
from v3_local import preview_context


class ProfileIsolationTests(unittest.TestCase):
    def test_filename_and_display_names_cannot_select_profile(self):
        for shape in ('bar', 'kpi'):
            for renamed in (False, True):
                with tempfile.TemporaryDirectory() as folder:
                    root = workbook(shape=shape, rename=renamed)
                    if renamed:
                        for ds in root.findall('./datasources/datasource'):
                            ds.set('caption', 'Different alias ' + ds.get('name'))
                    ir = parse(root, folder)
                    bindings = {}
                    for i, scope in enumerate(ir['field_scopes'].values()):
                        path = Path(folder) / f'input{i}.csv'
                        path.write_text(f'Place{i},Value\nA,3\nB,7\n')
                        bindings[scope['caption']] = str(path)
                    for filename in ('Superstore.twb', 'World Indicators.twb',
                                     'unseen.twb', 'authored/anything.twb', ''):
                        with self.subTest(shape=shape, renamed=renamed, filename=filename):
                            candidate = deepcopy(ir)
                            candidate['source_file'] = filename
                            with patch.object(config, 'profile_for', side_effect=AssertionError('filename routing')):
                                engine.configure(candidate)
                            with preview_context(candidate, bindings):
                                    self.assertEqual(config.PROFILE.MEASURE_LIBRARY, {})
                                    self.assertEqual(engine.cat_colors('Region', ['Europe', 'Asia']), (None, None))
                                    for i, sheet in enumerate(candidate['dashboards'][0]['sheets']):
                                        with engine.field_scope(sheet):
                                            measure = engine.resolve_measure(engine.tbl(sheet), sheet['name'],
                                                {'caption': 'Result renamed' if renamed else 'Result', 'agg': 'usr'})
                                            result = engine.q(f"SELECT {measure['sql']} AS V FROM {engine.tbl(sheet)}")
                                            self.assertEqual(result.iloc[0, 0], (21, 101)[i])

    def test_scoped_unplaced_parameters_do_not_depend_on_authored_prefix(self):
        saved = engine.IR
        saved_params = engine.PARAM_DEFS
        try:
            engine.PARAM_DEFS = {'Example parameter': 1}
            for filename in ('anything.twb', 'authored/anything.twb', ''):
                engine.IR = {'field_scopes': {}, 'source_file': filename}
                with patch.object(engine, '_placed_params', side_effect=AssertionError('invented controls')):
                    engine._render_param_controls()
        finally:
            engine.IR = saved
            engine.PARAM_DEFS = saved_params


if __name__ == '__main__':
    unittest.main()

