"""Execute independent source panes, never legacy inferred chart slots."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine
import ir_contract
from test_v3_field_identity import workbook, parse, UI
from v3_local import preview_context


def fixture(three=False, reverse=False):
    root = workbook(rename=reverse)
    for ds in root.findall('./datasources/datasource'):
        ET.SubElement(ET.SubElement(ds, 'connection'), 'relation', type='table', table='[source]')
    for idx, ws in enumerate(root.findall('./worksheets/worksheet')):
        table = ws.find('table')
        for f in table.findall('filter'): table.remove(f)
        tokens = [f'[federated.source_{idx}].[{agg}:Value:qk]' for agg in
                  (('sum', 'avg', 'max') if three else ('sum', 'avg'))]
        table.find('cols').text = '(' + ' / '.join(tokens) + ')'
        panes = table.find('panes')
        shared = panes[0]; shared.set('id', 'all')
        color = ET.SubElement(ET.SubElement(shared, 'encodings'), 'color',
                              column=f'[federated.source_{idx}].[none:Place{idx}:nk]')
        for j, token in enumerate(tokens):
            child = ET.SubElement(panes, 'pane', id=str(j), attrib={'x-axis-name': token})
            if j == 1:
                ET.SubElement(ET.SubElement(child, 'encodings'), 'color', column=color.get('column'))
        if reverse: panes[:] = list(reversed(list(panes)))
    zones = root.find('.//dashboard/zones')
    for z in list(zones):
        if z.get('type') == 'filter': zones.remove(z)
    return root


def binding(folder):
    result = {}
    for idx in (0, 1):
        path = Path(folder) / f'data{idx}.csv'
        n = idx + 1
        path.write_text(f'Place{idx},Value\nA,{2*n}\nA,{4*n}\nB,{-3*n}\n', encoding='utf-8')
        result[f'Unseen source {idx}'] = str(path)
    return result


class PaneExecutionTests(unittest.TestCase):
    def test_two_and_three_panes_numeric_identity_order_and_inheritance(self):
        for three, reverse in ((False, False), (True, True)):
            with self.subTest(three=three), tempfile.TemporaryDirectory() as folder:
                ir = parse(fixture(three, reverse), folder)
                self.assertEqual(ir_contract.validate_ir(ir), [])
                with preview_context(ir, binding(folder)):
                    for idx, sheet in enumerate(ir['dashboards'][0]['sheets']):
                        plans = sheet['worksheet_semantics']['pane_execution']['panes']
                        self.assertTrue(all(p['status'] == 'READY' for p in plans), plans)
                        self.assertEqual(len(plans), 3 if three else 2)
                        # Mutate legacy renderer slots: executable plans must not use them.
                        sheet['measures'] = []
                        sheet['dim'] = 'wrong legacy dimension'
                        ui = UI()
                        with patch.object(engine, 'st', ui), patch.object(engine, '_render_sheet_impl') as legacy:
                            engine.render_sheet(sheet, '')
                            legacy.assert_not_called()
                        self.assertEqual(ui.errors, [])
                        self.assertEqual(len(ui.charts), len(plans))
                        n = idx + 1
                        expected = [{'A': 6*n, 'B': -3*n}, {'A': 3*n, 'B': -3*n}]
                        if three: expected.append({'A': 4*n, 'B': -3*n})
                        for chart, values in zip(ui.charts, expected):
                            self.assertEqual(dict(zip(chart.data.DIM, chart.data.VAL)), values)
                            self.assertEqual(chart.to_dict()['encoding']['color']['field'], 'DIM')

    def test_unsupported_pane_and_sql_failure_leave_independent_sibling(self):
        for failure in ('mark', 'sql'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as folder:
                root = fixture()
                if failure == 'mark':
                    ET.SubElement(root.find('.//worksheet/table/panes/pane[@id="0"]'), 'mark', attrib={'class': 'Line'})
                ir = parse(root, folder)
                sheet = ir['dashboards'][0]['sheets'][0]
                if failure == 'mark': self.assertEqual(sheet['support']['status'], 'PARTIAL')
                with preview_context(ir, binding(folder)):
                    query = engine.q
                    def run(sql):
                        if failure == 'sql' and 'SUM(' in sql: raise RuntimeError('injected query failure')
                        return query(sql)
                    ui = UI()
                    with patch.object(engine, 'st', ui), patch.object(engine, 'q', side_effect=run):
                        engine.render_sheet(sheet, '')
                    self.assertEqual(len(ui.charts), 1)
                    self.assertEqual(dict(zip(ui.charts[0].data.DIM, ui.charts[0].data.VAL)), {'A': 3, 'B': -3})
                    self.assertEqual(len(ui.errors), 1)

    def test_stale_plan_refuses_before_query(self):
        with tempfile.TemporaryDirectory() as folder:
            ir = parse(fixture(), folder)
            sheet = ir['dashboards'][0]['sheets'][0]
            sheet['worksheet_semantics']['pane_execution']['panes'][0]['aggregates'] = []
            self.assertTrue(any(i.code == 'pane-plan' for i in ir_contract.validate_ir(ir)))
            with patch.object(engine, 'q') as query, patch.object(engine, 'st', UI()):
                engine.render_sheet(sheet, '')
                query.assert_not_called()

    def test_shared_unknown_semantics_block_all_queries(self):
        for defect in ('join', 'filter', 'union-shelf', 'indexed', 'calculation', 'color-grain', 'aggregation'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as folder:
                root = fixture()
                ws = root.find('./worksheets/worksheet')
                if defect == 'join': root.find('.//datasource/connection/relation').set('type', 'join')
                if defect == 'filter': ET.SubElement(ws.find('table'), 'filter', column='unknown')
                if defect == 'aggregation': ET.SubElement(ws.find('table'), 'aggregation', value='false')
                if defect == 'union-shelf': ws.find('./table/cols').text = ws.find('./table/cols').text.replace(' / ', ' + ')
                if defect == 'indexed': ws.find('.//pane[@id="0"]').set('x-index', '1')
                if defect == 'calculation':
                    for col in root.findall('.//column[@name="[Value]"]'):
                        ET.SubElement(col, 'calculation', attrib={'class': 'tableau', 'formula': '1'})
                if defect == 'color-grain':
                    for color in ws.findall('.//encodings/color'):
                        color.set('column', '[federated.source_0].[none:Calculation_Filter:nk]')
                sheet = parse(root, folder)['dashboards'][0]['sheets'][0]
                self.assertEqual(sheet['support']['status'], 'UNSUPPORTED')
                with patch.object(engine, 'q') as query, patch.object(engine, 'st', UI()):
                    engine.render_sheet(sheet, '')
                    query.assert_not_called()

    def test_runtime_filters_refuse_without_sql(self):
        with tempfile.TemporaryDirectory() as folder:
            ir = parse(fixture(), folder)
            engine.configure(ir)
            with patch.object(engine, 'q') as query, patch.object(engine, 'st', UI()):
                engine.render_sheet(ir['dashboards'][0]['sheets'][0], 'WHERE 1=1')
                query.assert_not_called()

    def test_corrupt_evidence_is_a_validation_issue(self):
        with tempfile.TemporaryDirectory() as folder:
            ir = parse(fixture(), folder)
            ir['dashboards'][0]['sheets'][0]['worksheet_semantics']['source_xml'] = '<broken'
            self.assertTrue(any(i.code == 'occurrence-schema' for i in ir_contract.validate_ir(ir)))


if __name__ == '__main__': unittest.main()
