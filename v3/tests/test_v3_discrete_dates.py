"""Discrete date components and full date values must not share query grain."""
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


def discrete_fixture(mark, token, reverse=False):
    root = fixture(mark, token, reverse)
    for ws in root.findall('./worksheets/worksheet'):
        cols = ws.find('./table/cols')
        cols.text = cols.text.replace(':qk]', ':ok]')
    return root


class DiscreteDateTests(unittest.TestCase):
    def test_date_parts_and_values_numeric_grain_across_two_shapes(self):
        axes = {'mn': [1, 4, 12], 'qr': [1, 2, 4], 'yr': [2024, 2025],
                'dy': [1, 2, 4, 18, 31],
                'tmn': ['2024-01-01', '2024-12-01', '2025-01-01', '2025-04-01']}
        for mark in ('Line', 'Area'):
            for token in axes:
                with self.subTest(mark=mark, token=token), tempfile.TemporaryDirectory() as folder:
                    ir = parse(discrete_fixture(mark, token, reverse=mark == 'Area'), folder)
                    self.assertEqual(ir_contract.validate_ir(ir), [])
                    with preview_context(ir, bindings(folder)):
                        for sheet in ir['dashboards'][0]['sheets']:
                            projection = sheet['worksheet_semantics']['projection']
                            self.assertEqual(projection['status'], 'MAPPED', sheet.get('support'))
                            self.assertTrue(projection['date_axis']['discrete'])
                            self.assertEqual(projection['date_axis']['mode'], 'date-value' if token == 'tmn' else 'date-part')
                            ui = UI()
                            with patch.object(engine, 'st', ui): engine.render_sheet(sheet, '')
                            self.assertEqual(ui.errors, [])
                            self.assertEqual(len(ui.charts), 1)
                            data = ui.charts[0].data.sort_values(['T'] + (['FLAG'] if mark == 'Line' else []))
                            factor = int(sheet['datasource_id'][-1]) + 1
                            if token in ('mn', 'qr'):
                                values = [22, 20, 8] if mark == 'Line' else [5.5, 20, 8]
                                keys = axes[token]
                            elif token == 'yr':
                                values = [6, 8, 16, 20] if mark == 'Line' else [14/3, 12]
                                keys = [2024, 2024, 2025, 2025] if mark == 'Line' else axes[token]
                            elif token == 'dy':
                                values = [20, 2, 16, 4, 8] if mark == 'Line' else [20, 2, 8, 4, 8]
                                keys = axes[token]
                            else:
                                values = [6, 8, 16, 20] if mark == 'Line' else [3, 8, 8, 20]
                                keys = axes[token]
                            self.assertEqual(list(data['T']), keys)
                            self.assertEqual(len(data), len(values))
                            for actual, expected in zip(data.VAL, values): self.assertAlmostEqual(actual, expected*factor)
                            chart = ui.charts[0].to_dict()
                            self.assertEqual(chart['encoding']['x']['type'], 'ordinal')
                            self.assertEqual(chart['encoding']['x']['sort'], 'ascending')
                            self.assertEqual(chart['encoding']['tooltip'][0]['type'], 'ordinal')

    def test_discrete_year_quarter_day_values_keep_full_date_identity(self):
        for token, expected in (
                ('tyr', ['2024-01-01', '2025-01-01']),
                ('tqr', ['2024-01-01', '2024-10-01', '2025-01-01', '2025-04-01']),
                ('tdy', ['2024-01-02', '2024-01-18', '2024-12-31', '2025-01-04', '2025-04-01'])):
            with self.subTest(token=token), tempfile.TemporaryDirectory() as folder:
                ir = parse(discrete_fixture('Area', token), folder)
                with preview_context(ir, bindings(folder)):
                    ui = UI()
                    with patch.object(engine, 'st', ui): engine.render_sheet(ir['dashboards'][0]['sheets'][0], '')
                    self.assertEqual(ui.errors, [])
                    self.assertEqual(sorted(ui.charts[0].data['T']), expected)

    def test_unknown_ordering_and_tampered_date_plan_do_not_query(self):
        for defect in ('sort', 'plan'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as folder:
                root = discrete_fixture('Line', 'mn')
                if defect == 'sort': ET.SubElement(root.find('.//worksheet/table'), 'sort', direction='DESC')
                ir = parse(root, folder)
                sheet = ir['dashboards'][0]['sheets'][0]
                if defect == 'plan':
                    sheet['worksheet_semantics']['projection']['date_axis']['mode'] = 'date-value'
                    self.assertTrue(ir_contract.validate_ir(ir))
                else: self.assertEqual(sheet['support']['status'], 'UNSUPPORTED')
                with patch.object(engine, 'q') as query, patch.object(engine, 'st', UI()):
                    engine.render_sheet(sheet, '')
                    query.assert_not_called()

    def test_null_discrete_date_refuses_without_hiding_the_group(self):
        with tempfile.TemporaryDirectory() as folder:
            ir = parse(discrete_fixture('Line', 'mn'), folder)
            mapping = bindings(folder)
            path = Path(mapping['Unseen source 0'])
            with path.open('a') as stream: stream.write(',A,100\n')
            with preview_context(ir, mapping):
                ui = UI()
                with patch.object(engine, 'st', ui):
                    engine.render_sheet(ir['dashboards'][0]['sheets'][0], '')
                    engine.render_sheet(ir['dashboards'][0]['sheets'][1], '')
                self.assertEqual(len(ui.errors), 1)
                self.assertIn('Null discrete-date', ui.errors[0])
                self.assertEqual(len(ui.charts), 1)


if __name__ == '__main__': unittest.main()
