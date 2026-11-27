"""Calendar grain proofs across line/color and scalar-area workbook shapes."""
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


def fixture(mark='Line', part='tmn', reverse=False):
    root = workbook(reverse=reverse, rename=reverse)
    for parent in root.findall('./datasources/datasource') + root.findall('.//datasource-dependencies'):
        ET.SubElement(parent, 'column', name='[Event]', caption='Calendar renamed' if reverse else 'Calendar',
                      datatype='date', role='dimension')
    for ws in root.findall('./worksheets/worksheet'):
        table = ws.find('table')
        did = table.find('datasource-dependencies').get('datasource')
        index = did[-1]
        table.find('cols').text = f'[{did}].[{part}:Event:qk]'
        table.find('rows').text = f'[{did}].[{"sum" if mark == "Line" else "avg"}:Value:qk]'
        pane = table.find('./panes/pane')
        pane.find('mark').set('class', mark)
        ET.SubElement(table, 'aggregation', value='true')
        if mark == 'Line':
            ET.SubElement(ET.SubElement(pane, 'encodings'), 'color', column=f'[{did}].[none:Place{index}:nk]')
        for f in table.findall('filter'): table.remove(f)
    zones = root.find('.//dashboard/zones')
    for z in list(zones):
        if z.get('type') == 'filter': zones.remove(z)
    return root


def bindings(folder):
    result = {}
    for i in (0, 1):
        n = i + 1
        path = Path(folder) / f'temporal{i}.csv'
        rows = [('2024-01-02', 'A', 2), ('2024-01-18', 'A', 4), ('2024-12-31', 'B', 8),
                ('2025-01-04', 'A', 10), ('2025-01-04', 'A', 6), ('2025-04-01', 'B', 20)]
        path.write_text(f'Event,Place{i},Value\n' + ''.join(f'{d},{g},{v*n}\n' for d,g,v in rows), encoding='utf-8')
        result[f'Unseen source {i}'] = str(path)
    return result


class TemporalTests(unittest.TestCase):
    def test_calendar_grain_numeric_line_and_area_across_year_boundaries(self):
        expected = {
            'tmn': ['2024-01-01', '2024-12-01', '2025-01-01', '2025-04-01'],
            'tqr': ['2024-01-01', '2024-10-01', '2025-01-01', '2025-04-01'],
            'tyr': ['2024-01-01', '2025-01-01'],
        }
        for mark in ('Line', 'Area'):
            for part in expected:
                for reverse in (False, True):
                    with self.subTest(mark=mark, part=part, reverse=reverse), tempfile.TemporaryDirectory() as folder:
                        ir = parse(fixture(mark, part, reverse), folder)
                        self.assertEqual(ir_contract.validate_ir(ir), [])
                        with preview_context(ir, bindings(folder)):
                            for sheet in ir['dashboards'][0]['sheets']:
                                self.assertEqual(sheet['worksheet_semantics']['projection']['status'], 'MAPPED', sheet.get('support'))
                                ui = UI()
                                with patch.object(engine, 'st', ui): engine.render_sheet(sheet, '')
                                self.assertEqual(ui.errors, [])
                                self.assertEqual(len(ui.charts), 1)
                                data = ui.charts[0].data.sort_values(['T'] + (['FLAG'] if mark == 'Line' else []))
                                n = int(sheet['datasource_id'][-1]) + 1
                                if mark == 'Line':
                                    self.assertEqual(list(data.VAL), [6*n, 8*n, 16*n, 20*n])
                                    self.assertEqual(list(data.FLAG), ['A', 'B', 'A', 'B'])
                                    dates = [expected[part][0]] * 2 + [expected[part][1]] * 2 if part == 'tyr' else expected[part]
                                    self.assertIsNone(ui.charts[0].to_dict()['encoding']['y']['stack'])
                                else:
                                    values = [14/3*n, 12*n] if part == 'tyr' else [3*n, 8*n, 8*n, 20*n]
                                    for actual, value in zip(data.VAL, values): self.assertAlmostEqual(actual, value)
                                    self.assertEqual(len(data), len(values))
                                    dates = expected[part]
                                self.assertEqual(list(data['T'].dt.strftime('%Y-%m-%d')), dates)

    def test_day_grain_preserves_distinct_days(self):
        with tempfile.TemporaryDirectory() as folder:
            ir = parse(fixture('Area', 'tdy'), folder)
            with preview_context(ir, bindings(folder)):
                ui = UI()
                with patch.object(engine, 'st', ui): engine.render_sheet(ir['dashboards'][0]['sheets'][0], '')
                self.assertEqual(ui.errors, [])
                data = ui.charts[0].data.sort_values('T')
                self.assertEqual(list(data.VAL), [2, 4, 8, 8, 20])
                self.assertEqual(len(data), 5)

    def test_unsupported_date_semantics_refuse_before_query_and_leave_sibling(self):
        for defect in ('exact-date', 'week', 'part-only', 'fiscal', 'fiscal-node', 'detail', 'area-color', 'date-calc', 'disaggregated'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as folder:
                root = fixture('Area' if defect == 'area-color' else 'Line')
                ws = root.find('./worksheets/worksheet')
                if defect in ('exact-date', 'week', 'part-only'):
                    ws.find('./table/cols').text = '[federated.source_0].[' + {
                        'exact-date': 'mdy:Event:ok', 'week': 'twk:Event:qk', 'part-only': 'mn:Event:qk'}[defect] + ']'
                if defect == 'fiscal': root.find('./datasources/datasource/column[@name="[Event]"]').set('fiscal-year-start', '4')
                if defect == 'fiscal-node': ET.SubElement(ws.find('table'), 'fiscal-calendar', start='4')
                if defect == 'disaggregated': ws.find('./table/aggregation').set('value', 'false')
                if defect in ('detail', 'area-color'):
                    pane = ws.find('.//pane')
                    enc = pane.find('encodings')
                    if enc is None: enc = ET.SubElement(pane, 'encodings')
                    ET.SubElement(enc, 'color' if defect == 'area-color' else 'detail', column='[federated.source_0].[none:Place0:nk]')
                if defect == 'date-calc':
                    ET.SubElement(root.find('./datasources/datasource/column[@name="[Event]"]'), 'calculation',
                                  attrib={'class': 'tableau', 'formula': 'TODAY()'})
                ir = parse(root, folder)
                bad, good = ir['dashboards'][0]['sheets']
                self.assertEqual(bad['support']['status'], 'UNSUPPORTED')
                with patch.object(engine, 'q') as query, patch.object(engine, 'st', UI()):
                    engine.render_sheet(bad, '')
                    query.assert_not_called()
                with preview_context(ir, bindings(folder)):
                    ui = UI()
                    with patch.object(engine, 'st', ui): engine.render_sheet(good, '')
                    self.assertEqual(ui.errors, [])
                    self.assertEqual(len(ui.charts), 1)

    def test_changed_date_grain_invalidates_projection(self):
        with tempfile.TemporaryDirectory() as folder:
            ir = parse(fixture(), folder)
            sheet = ir['dashboards'][0]['sheets'][0]
            sheet['x']['datepart'] = 'tyr'
            self.assertTrue(any(i.code == 'occurrence-projection' for i in ir_contract.validate_ir(ir)))


if __name__ == '__main__': unittest.main()
