"""Construct tests independent of business captions or the golden workbooks."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import calc_translator as CT
import codegen
import engine
import findings
import ir_contract as contract
import tableau_parser as TP
import backend
import pandas as pd


def sheet(name="Arbitrary metric"):
    return {"name": name, "kind": "table", "dims": [], "measures": []}


def book(names=("Alpha", "Beta"), direction="vert"):
    return {"dashboards": [{"name": "Unseen dashboard", "title": "Unseen dashboard",
             "sheets": [sheet(n) for n in names],
             "layout": {"dir": direction, "children": [{"sheet": n} for n in names]}}]}


class UI:
    def __init__(self):
        self.warnings = []
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def columns(self, weights): return [self for _ in weights]
    def warning(self, message): self.warnings.append(message)
    def __getattr__(self, name): return lambda *args, **kwargs: None


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        findings.clear()

    def test_names_and_layout_shapes_do_not_change_validity(self):
        for names, direction in [(('A', 'B'), 'vert'), (('Renamed 17', '患者数'), 'horz')]:
            with self.subTest(names=names):
                ir = book(names, direction)
                self.assertEqual(contract.validate_ir(ir), [])
                self.assertIn('run(IR)', codegen.build(ir))
                self.assertEqual(contract.assess_ir(ir)['state'], 'NEEDS_REVIEW')

    def test_invalid_layout_rejected_before_codegen(self):
        for bad in ({'dir': 'vert'}, {'sheet': 'missing'}, {'children': [], 'dir': 'diagonal'},
                    {'text': 'hello', 'children': []}, {'sheet': 'Alpha', 'h': float('nan')}):
            ir = book()
            ir['dashboards'][0]['layout']['children'][0] = bad
            with self.subTest(bad=bad), self.assertRaises(contract.IRValidationError):
                codegen.build(ir)
            self.assertTrue(contract.validate_ir(ir)[0].path.startswith('dashboards[0].layout'))

    def test_unknown_kind_never_dispatches_to_table(self):
        bad = dict(sheet(), kind='future-viz')
        with patch.object(engine, 'st', UI()), patch.object(engine, 'r_table') as table:
            engine.render_sheet(bad, '')
            table.assert_not_called()
        self.assertTrue(any(f['severity'] == 'BLOCKER' for f in findings.all_findings()))

    def test_novel_mark_is_unsupported_in_two_shelf_shapes(self):
        for name, shelves in [('Unseen', '<rows>[federated.a].[sum:X:qk]</rows>'),
                               ('Renamed', '<cols>[federated.a].[none:Group:nk]</cols>')]:
            ws = ET.fromstring(f'<worksheet name="{name}"><table>{shelves}'
                               '<panes><pane><mark class="FutureDensityV9"/></pane></panes>'
                               '</table></worksheet>')
            result = TP.infer(ws, {})
            self.assertEqual(result['kind'], 'unsupported')
            ir = book()
            ir['dashboards'][0]['sheets'][0] = dict(result, name='Alpha')
            self.assertEqual(contract.assess_ir(ir)['state'], 'BLOCKED')

    def test_parser_failure_retains_sheet_placeholder(self):
        with patch.object(TP, '_infer_core', side_effect=ValueError('novel shelf')):
            result = TP.infer(ET.fromstring('<worksheet name="Unfamiliar"/>'), {})
        self.assertEqual(result['name'], 'Unfamiliar')
        self.assertEqual(result['support']['status'], 'INVALID')

    def test_preparation_failure_does_not_stop_sibling_in_two_layouts(self):
        for direction in ('vert', 'horz'):
            ui = UI()
            visited = []
            def prepare(s):
                if s['name'] == 'Alpha': raise ValueError('unhandled filter preparation')
                return s
            with patch.object(engine, 'st', ui), patch.object(engine, '_apply_drill', side_effect=prepare), \
                 patch.object(engine, '_apply_sheet_filters', return_value=''), \
                 patch.object(engine, 'r_table', side_effect=lambda s, w: visited.append(s['name'])):
                ir = book(direction=direction)['dashboards'][0]
                engine._render_layout(ir['layout'], {s['name']: s for s in ir['sheets']}, '')
            self.assertEqual(visited, ['Beta'])
            self.assertTrue(ui.warnings)

    def test_invalid_layout_leaf_keeps_siblings(self):
        for direction in ('vert', 'horz'):
            ir = book(direction=direction)['dashboards'][0]
            ir['layout']['children'].insert(1, {'dir': 'vert'})
            visited = []
            with patch.object(engine, 'st', UI()), \
                 patch.object(engine, 'render_sheet', side_effect=lambda s, w: visited.append(s['name'])):
                engine._render_layout(ir['layout'], {s['name']: s for s in ir['sheets']}, '')
            self.assertEqual(visited, ['Alpha', 'Beta'])

    def test_shared_dashboard_failure_does_not_stop_next_dashboard(self):
        visited = []
        def draw(d):
            if d['name'] == 'bad': raise ValueError('control failure')
            visited.append(d['name'])
        with patch.object(engine, 'st', UI()), patch.object(engine, '_render_dashboard_impl', side_effect=draw):
            for name in ('bad', 'healthy'): engine.render_dashboard({'name': name})
        self.assertEqual(visited, ['healthy'])

    def test_dependency_failure_propagates_without_null(self):
        for name in ('Bad', 'Renamed dependency'):
            defs = {name: '{INCLUDE [A]: SUM({FIXED [B]: SUM([X])})}', 'Parent': f'[{name}] + 1'}  # untranslatable
            self.assertEqual(CT.translate_formula('[Parent] * 2', calc_defs=defs), (None, False))
        self.assertEqual(CT.translate_formula('[Loop]', calc_defs={'Loop': '[Loop]+1'}), (None, False))
        self.assertIsNotNone(CT.translate_formula('[Good] + 1', calc_defs={'Good': '[X]*2'})[0])

    def test_missing_parameter_refuses_and_valid_parameter_survives(self):
        for name in ('Unknown', 'Renamed parameter'):
            formula = f'[Parameters].[{name}] + 1'
            self.assertEqual(CT.translate_formula(formula), (None, False))
            self.assertIn('__PARAM_', CT.translate_formula(formula, params={name: '2'})[0])

    def test_untranslated_calcs_block_assessment(self):
        ir = book()
        ir['calc_drops'] = {'Broken': '[Missing]'}
        self.assertEqual(contract.assess_ir(ir)['state'], 'BLOCKED')

    def test_normalized_column_collision_never_discards_data(self):
        for columns in (['Order-ID', 'Order ID'], ['a b', 'A_B']):
            frame = pd.DataFrame([[1, 999]], columns=columns)
            with self.subTest(columns=columns), self.assertRaisesRegex(ValueError, 'Ambiguous physical'):
                backend._normalize_columns(frame)
            self.assertEqual(len(frame.columns), 2)
        self.assertEqual(list(backend._normalize_columns(pd.DataFrame({'Value': [3]})).columns), ['VALUE'])

    def test_registry_points_to_real_runtime_handlers(self):
        self.assertTrue(all(callable(getattr(engine, name)) for name in contract.CHART_HANDLERS.values()))


if __name__ == '__main__':
    unittest.main()
