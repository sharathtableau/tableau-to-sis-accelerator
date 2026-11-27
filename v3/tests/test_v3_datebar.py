"""Date bars consume authored date grain, not legacy month approximations."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine
import ir_contract
from test_v3_temporal import fixture, bindings
from test_v3_field_identity import parse, UI
from v3_local import preview_context


def bar_fixture(token, discrete, reverse=False):
    root = fixture('Bar', token, reverse)
    if discrete:
        for cols in root.findall('.//worksheet/table/cols'):
            cols.text = cols.text.replace(':qk]', ':ok]')
    return root


class DateBarTests(unittest.TestCase):
    def test_numeric_grain_and_bar_axes_across_datasource_shapes(self):
        cases = [
            ('mn', True, [1, 4, 12], [5.5, 20, 8]),
            ('qr', True, [1, 2, 4], [5.5, 20, 8]),
            ('yr', True, [2024, 2025], [14/3, 12]),
            ('dy', True, [1, 2, 4, 18, 31], [20, 2, 8, 4, 8]),
        ]
        for discrete in (False, True):
            for token, keys, values in (
                ('tmn', ['2024-01-01', '2024-12-01', '2025-01-01', '2025-04-01'], [3, 8, 8, 20]),
                ('tqr', ['2024-01-01', '2024-10-01', '2025-01-01', '2025-04-01'], [3, 8, 8, 20]),
                ('tyr', ['2024-01-01', '2025-01-01'], [14/3, 12]),
                ('tdy', ['2024-01-02', '2024-01-18', '2024-12-31', '2025-01-04', '2025-04-01'], [2, 4, 8, 8, 20]),
            ): cases.append((token, discrete, keys, values))
        for reverse in (False, True):
            for token, discrete, keys, values in cases:
                with self.subTest(reverse=reverse, token=token, discrete=discrete), tempfile.TemporaryDirectory() as folder:
                    ir = parse(bar_fixture(token, discrete, reverse), folder)
                    self.assertEqual(ir_contract.validate_ir(ir), [])
                    with preview_context(ir, bindings(folder)):
                        for sheet in ir['dashboards'][0]['sheets']:
                            self.assertEqual(sheet['kind'], 'dtbar')
                            self.assertEqual(sheet['worksheet_semantics']['projection']['status'], 'MAPPED')
                            ui = UI()
                            with patch.object(engine, 'st', ui): engine.render_sheet(sheet, '')
                            self.assertEqual(ui.errors, [])
                            self.assertEqual(len(ui.charts), 1)
                            chart = ui.charts[0]
                            data = chart.data.sort_values('T')
                            actual_keys = list(data['T']) if discrete else list(data['T'].dt.strftime('%Y-%m-%d'))
                            self.assertEqual(actual_keys, keys)
                            factor = int(sheet['datasource_id'][-1]) + 1
                            for actual, expected in zip(data.VAL, values): self.assertAlmostEqual(actual, expected * factor)
                            spec = chart.to_dict()
                            self.assertEqual(spec['mark']['type'], 'bar')
                            self.assertEqual(spec['encoding']['x']['type'], 'ordinal' if discrete else 'temporal')
                            self.assertIsNone(spec['encoding']['y']['stack'])

    def test_unsupported_bar_variants_refuse_before_query(self):
        for defect in ('color', 'text', 'detail', 'week', 'fiscal', 'sort', 'tampered-plan'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as folder:
                root = bar_fixture('mn', True)
                ws = root.find('.//worksheet')
                if defect in ('color', 'text', 'detail'):
                    enc = ET.SubElement(ws.find('.//pane'), 'encodings')
                    ET.SubElement(enc, defect, column='[federated.source_0].[' + ('avg:Value:qk' if defect == 'text' else 'none:Place0:nk') + ']')
                if defect == 'week': ws.find('./table/cols').text = '[federated.source_0].[wk:Event:ok]'
                if defect == 'fiscal': ET.SubElement(ws.find('table'), 'fiscal-calendar', start='4')
                if defect == 'sort': ET.SubElement(ws.find('table'), 'sort', direction='DESC')
                ir = parse(root, folder)
                bad, good = ir['dashboards'][0]['sheets']
                if defect == 'tampered-plan':
                    bad['worksheet_semantics']['projection']['date_axis']['mode'] = 'date-value'
                    self.assertTrue(ir_contract.validate_ir(ir))
                else: self.assertEqual(bad['support']['status'], 'UNSUPPORTED')
                with patch.object(engine, 'st', UI()), patch.object(engine, 'q') as query:
                    engine.render_sheet(bad, '')
                    query.assert_not_called()
                with preview_context(ir, bindings(folder)):
                    ui = UI()
                    with patch.object(engine, 'st', ui): engine.render_sheet(good, '')
                    self.assertEqual(ui.errors, [])
                    self.assertEqual(len(ui.charts), 1)

    def test_null_date_groups_fail_visibly_and_leave_sibling(self):
        for discrete in (False, True):
            with self.subTest(discrete=discrete), tempfile.TemporaryDirectory() as folder:
                ir = parse(bar_fixture('tmn', discrete), folder)
                mapping = bindings(folder)
                with Path(mapping['Unseen source 0']).open('a') as stream: stream.write(',A,100\n')
                with preview_context(ir, mapping):
                    ui = UI()
                    with patch.object(engine, 'st', ui):
                        for sheet in ir['dashboards'][0]['sheets']: engine.render_sheet(sheet, '')
                    self.assertEqual(len(ui.errors), 1)
                    self.assertIn('Null date-bar groups', ui.errors[0])
                    self.assertEqual(len(ui.charts), 1)


if __name__ == '__main__': unittest.main()
