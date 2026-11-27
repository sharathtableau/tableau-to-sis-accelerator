"""Structural adapter proofs, independent of workbook filenames/captions."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine
import ir_contract
from test_v3_field_identity import workbook, parse, UI
from v3_local import preview_context


class PanelUI(UI):
    def container(self, **kwargs): return self


def multi_measure(rename=False, reverse=False, vertical=False):
    root = workbook(rename=rename)
    for idx, ws in enumerate(root.findall('./worksheets/worksheet')):
        tokens = [f'[federated.source_{idx}].[sum:Value:qk]',
                  f'[federated.source_{idx}].[avg:Value:qk]']
        if reverse: tokens.reverse()
        ws.find('./table/cols').text = ' + '.join(tokens)
        if vertical:
            cols, rows = ws.find('./table/cols'), ws.find('./table/rows')
            cols.text, rows.text = rows.text, cols.text
    return root


class AdapterTests(unittest.TestCase):
    def test_multi_measure_numeric_grain_and_negative_domains(self):
        for rename, reverse in ((False, False), (True, True)):
            with self.subTest(rename=rename, reverse=reverse), tempfile.TemporaryDirectory() as folder:
                ir = parse(multi_measure(rename, reverse), folder)
                self.assertEqual(ir_contract.validate_ir(ir), [])
                bindings = {}
                for idx in (0, 1):
                    path = Path(folder) / f'input{idx}.csv'
                    path.write_text(f'Place{idx},Value\nA,-6\nA,-2\nB,0\nB,4\n', encoding='utf-8')
                    bindings[f'Unseen source {idx}'] = str(path)
                with preview_context(ir, bindings):
                    for sheet in ir['dashboards'][0]['sheets']:
                        self.assertEqual(sheet['kind'], 'mbar')
                        self.assertEqual(sheet['worksheet_semantics']['projection']['status'], 'MAPPED')
                        ui = PanelUI()
                        with patch.object(engine, 'st', ui): engine.render_sheet(sheet, '')
                        self.assertEqual(ui.errors, [])
                        self.assertEqual(len(ui.charts), 2)
                        expected = [{'A': -8, 'B': 4}, {'A': -4, 'B': 2}]
                        if reverse: expected.reverse()
                        for chart, values in zip(ui.charts, expected):
                            self.assertEqual(dict(zip(chart.data['DIM'], chart.data['VAL'])), values)
                            domain = chart.to_dict()['layer'][0]['encoding']['x']['scale']['domain']
                            self.assertLessEqual(domain[0], min(values.values()))
                            self.assertGreater(domain[1], max(values.values()))

    def test_unimplemented_multi_measure_shapes_still_refuse(self):
        for defect in ('vertical', 'detail', 'discrete', 'extra-dimension', 'mark'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as folder:
                root = multi_measure(vertical=defect == 'vertical')
                ws = root.find('./worksheets/worksheet')
                if defect == 'detail':
                    enc = ET.SubElement(ws.find('.//pane'), 'encodings')
                    ET.SubElement(enc, 'detail', column='[federated.source_0].[none:Place0:nk]')
                if defect == 'discrete':
                    ws.find('./table/cols').text = ws.find('./table/cols').text.replace('avg:Value:qk', 'avg:Value:ok')
                if defect == 'extra-dimension':
                    ws.find('./table/rows').text += ' + [federated.source_0].[none:Calculation_Filter:nk]'
                if defect == 'mark': ws.find('.//mark').set('class', 'Line')
                sheet = parse(root, folder)['dashboards'][0]['sheets'][0]
                self.assertEqual(sheet['worksheet_semantics']['projection']['status'], 'REQUIRES_PLAN')
                with patch.object(engine, '_render_sheet_impl') as render, patch.object(engine, 'st', PanelUI()):
                    engine.render_sheet(sheet, '')
                    render.assert_not_called()

    def test_formula_like_caption_cannot_redirect_scoped_bars_to_member_list(self):
        with tempfile.TemporaryDirectory() as folder:
            root = multi_measure()
            for column in root.findall('.//column[@name="[Value]"]'):
                column.set('caption', 'AVG(0)')
            ir = parse(root, folder)
            bindings = {}
            for idx in (0, 1):
                path = Path(folder) / f'input{idx}.csv'
                path.write_text(f'Place{idx},Value\nA,-6\nA,-2\n', encoding='utf-8')
                bindings[f'Unseen source {idx}'] = str(path)
            with preview_context(ir, bindings):
                for sheet in ir['dashboards'][0]['sheets']:
                    ui = PanelUI()
                    with patch.object(engine, 'st', ui), patch.object(engine, '_placeholder_list') as fallback:
                        engine.render_sheet(sheet, '')
                        fallback.assert_not_called()
                    self.assertEqual(ui.errors, [])
                    self.assertEqual([c.data['VAL'].iloc[0] for c in ui.charts], [-8, -4])

    def test_three_axis_shape_with_calculated_dimension_and_measure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = multi_measure(rename=True)
            for idx, ws in enumerate(root.findall('./worksheets/worksheet')):
                ws.find('./table/cols').text += f' + [federated.source_{idx}].[usr:Calculation_2:qk]'
                ws.find('./table/rows').text = f'[federated.source_{idx}].[none:Calculation_Filter:nk]'
            ir = parse(root, folder)
            self.assertEqual(ir_contract.validate_ir(ir), [])
            bindings = {}
            for idx in (0, 1):
                path = Path(folder) / f'input{idx}.csv'
                path.write_text(f'Place{idx},Value\nA,2\nA,4\nB,0\n', encoding='utf-8')
                bindings[f'Unseen source {idx}'] = str(path)
            with preview_context(ir, bindings):
                for idx, sheet in enumerate(ir['dashboards'][0]['sheets']):
                    ui = PanelUI()
                    with patch.object(engine, 'st', ui): engine.render_sheet(sheet, '')
                    self.assertEqual(ui.errors, [])
                    self.assertEqual(len(ui.charts), 3)
                    expected = [{'A': 6, 'B': 0}, {'A': 3, 'B': 0}, {'A': 13 if idx == 0 else 61, 'B': 1}]
                    for chart, values in zip(ui.charts, expected):
                        self.assertEqual(dict(zip(chart.data['DIM'], chart.data['VAL'])), values)

    def test_shared_card_inheritance_override_and_axis_grain(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse), tempfile.TemporaryDirectory() as folder:
                root = multi_measure(rename=reverse)
                ws = root.find('./worksheets/worksheet')
                panes = ws.find('.//panes')
                shared = panes[0]
                shared.set('id', 'all')
                enc = ET.SubElement(shared, 'encodings')
                ET.SubElement(enc, 'color', column='[federated.source_0].[none:Place0:nk]')
                for index, token in enumerate(ws.find('./table/cols').text.split(' + ')):
                    child = ET.SubElement(panes, 'pane', id=str(index), attrib={'x-axis-name': token})
                    if index == 1:
                        ET.SubElement(child, 'mark', attrib={'class': 'Line'})
                        enc = ET.SubElement(child, 'encodings')
                        ET.SubElement(enc, 'color', column='[federated.source_0].[none:Calculation_Filter:nk]')
                if reverse: panes[:] = list(reversed(list(panes)))
                ir = parse(root, folder)
                self.assertEqual(ir_contract.validate_ir(ir), [])
                sheet = ir['dashboards'][0]['sheets'][0]
                model = sheet['worksheet_semantics']
                layers = {p['pane_id']: p for p in model['layers']['panes']}
                self.assertEqual(set(layers), {'pane:0', 'pane:1'})
                self.assertEqual(layers['pane:0']['marks'], ['Bar'])
                self.assertEqual(layers['pane:1']['marks'], ['Line'])
                self.assertEqual(len(layers['pane:0']['inherited_occurrence_ids']), 1)
                self.assertEqual(layers['pane:0']['inherited_occurrence_ids'], layers['pane:1']['shadowed_occurrence_ids'])
                for grain in model['grain']['panes']:
                    self.assertEqual(len(grain['aggregate_occurrence_ids']), 1)
                self.assertEqual(sheet['support']['status'], 'UNSUPPORTED')
                layers['pane:0']['inherited_occurrence_ids'].clear()
                self.assertTrue(any(i.code == 'pane-inheritance' for i in ir_contract.validate_ir(ir)))

    def test_ambiguous_shared_cards_and_unknown_channels_are_explicit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = workbook()
            ws = root.find('./worksheets/worksheet')
            panes = ws.find('.//panes')
            panes[0].set('id', 'shared')
            enc = ET.SubElement(panes[0], 'encodings')
            ET.SubElement(enc, 'path', column='[federated.source_0].[none:Place0:nk]')
            ET.SubElement(panes, 'pane', id='child', attrib={'x-axis-name': ws.find('./table/cols').text})
            model = parse(root, folder)['dashboards'][0]['sheets'][0]['worksheet_semantics']
            self.assertEqual(model['layers']['status'], 'REQUIRES_PLAN')
            self.assertIn('channel policy', ' '.join(model['layers']['reasons']))
            extra = deepcopy(panes[0]); extra.set('id', 'other'); panes.append(extra)
            model = parse(root, folder)['dashboards'][0]['sheets'][0]['worksheet_semantics']
            self.assertIn('ambiguous inheritance', ' '.join(model['layers']['reasons']))


if __name__ == '__main__': unittest.main()
