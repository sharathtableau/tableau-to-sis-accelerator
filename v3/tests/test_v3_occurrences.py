"""Occurrence/grain proofs using independently varied source IDs and mark shapes."""
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
import codegen
from test_v3_field_identity import workbook, parse, UI
from v3_local import preview_context


def sheets(ir):
    return ir['dashboards'][0]['sheets']


class OccurrenceTests(unittest.TestCase):
    def test_identity_survives_rename_and_declaration_order_on_two_shapes(self):
        for shape in ('bar', 'kpi'):
            with self.subTest(shape=shape), tempfile.TemporaryDirectory() as folder:
                a = parse(workbook(shape), folder)
                b = parse(workbook(shape, reverse=True, rename=True), folder)
                for x, y in zip(sheets(a), sheets(b)):
                    mx, my = x['worksheet_semantics'], y['worksheet_semantics']
                    self.assertEqual(mx['worksheet_id'], my['worksheet_id'])
                    self.assertEqual([o['id'] for o in mx['occurrences']], [o['id'] for o in my['occurrences']])
                    self.assertEqual(mx['grain'], my['grain'])
                    self.assertEqual(mx['projection']['status'], 'MAPPED')
                self.assertEqual(ir_contract.validate_ir(a), [])

    def test_repeated_aggregations_remain_distinct_and_discrete_aggregate_not_grouped(self):
        for shape in ('bar', 'kpi'):
            with self.subTest(shape=shape), tempfile.TemporaryDirectory() as folder:
                root = workbook(shape)
                for idx, ws in enumerate(root.findall('./worksheets/worksheet')):
                    tokens = [f'[federated.source_{idx}].[sum:Value:qk]', f'[federated.source_{idx}].[avg:Value:ok]']
                    if shape == 'bar':
                        ws.find('./table/cols').text = ' + '.join(tokens)
                    else:
                        enc = ws.find('.//encodings')
                        enc.clear()
                        for token in tokens:
                            ET.SubElement(enc, 'text', column=token)
                ir = parse(root, folder)
                for sheet in sheets(ir):
                    model = sheet['worksheet_semantics']
                    values = [o for o in model['occurrences'] if o['aggregation'] in ('sum', 'avg')]
                    self.assertEqual(len(values), 2)
                    self.assertNotEqual(values[0]['id'], values[1]['id'])
                    self.assertTrue(all(o['level'] == 'aggregate' for o in values))
                    grouped = [g['field_id'] for p in model['grain']['panes'] for g in p['group_by']]
                    self.assertNotIn(values[0]['field_id'], grouped)
                    self.assertEqual(model['projection']['status'], 'REQUIRES_PLAN')
                    self.assertIn(sheet['support']['status'], ('UNSUPPORTED', 'INVALID'))

    def test_panes_do_not_collapse_and_axis_selectors_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = workbook()
            ws = root.find('./worksheets/worksheet')
            panes = ws.find('.//panes')
            first = panes[0]
            first.set('id', '31')
            first.set('x-axis-name', ws.find('./table/cols').text)
            second = deepcopy(first)
            second.set('id', '72')
            second.find('mark').set('class', 'Line')
            panes.append(second)
            a = sheets(parse(root, folder))[0]['worksheet_semantics']
            panes[:] = list(reversed(list(panes)))
            b = sheets(parse(root, folder))[0]['worksheet_semantics']
            self.assertEqual({p['id'] for p in a['panes']}, {'pane:31', 'pane:72'})
            self.assertEqual({o['id'] for o in a['occurrences']}, {o['id'] for o in b['occurrences']})
            self.assertEqual(len([o for o in a['occurrences'] if o['selector']]), 2)
            self.assertEqual(a['projection']['status'], 'REQUIRES_PLAN')

    def test_unconsumed_detail_blocks_only_affected_sheet(self):
        with tempfile.TemporaryDirectory() as folder:
            root = workbook()
            pane = root.find('./worksheets/worksheet/table/panes/pane')
            enc = ET.SubElement(pane, 'encodings')
            ET.SubElement(enc, 'detail', column='[federated.source_0].[none:Place0:nk]')
            ir = parse(root, folder)
            a, b = sheets(ir)
            self.assertEqual(a['support']['status'], 'UNSUPPORTED')
            self.assertEqual(b['worksheet_semantics']['projection']['status'], 'MAPPED')
            bindings = {}
            for idx in (0, 1):
                path = Path(folder) / f'data{idx}.csv'
                path.write_text(f'Place{idx},Value\nA,3\nB,7\n', encoding='utf-8')
                bindings[f'Unseen source {idx}'] = str(path)
            ui = UI()
            with preview_context(ir, bindings), patch.object(engine, 'st', ui):
                engine.render_sheet(a, '')
                engine.render_sheet(b, '')
            self.assertEqual(len(ui.errors), 1)
            self.assertEqual(len(ui.charts), 1)

    def test_corrupt_grain_and_projection_rejected_before_runtime(self):
        for defect in ('grain', 'binding', 'pane', 'owner', 'missing'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as folder:
                ir = parse(workbook(), folder)
                model = sheets(ir)[0]['worksheet_semantics']
                if defect == 'grain': model['grain']['panes'][0]['group_by'] = []
                if defect == 'binding': model['projection']['bindings'].clear()
                if defect == 'pane': model['occurrences'][0]['pane_id'] = 'missing'
                if defect == 'owner': model['occurrences'][0]['worksheet_id'] = 'wrong'
                if defect == 'missing': del model['occurrences'][0]['level']
                with self.assertRaises(ir_contract.IRValidationError):
                    ir_contract.require_valid_ir(ir)
                engine.configure(ir)
                ui = UI()
                with patch.object(engine, 'st', ui), patch.object(engine, '_render_sheet_impl') as render:
                    engine.render_sheet(sheets(ir)[0], '')
                    render.assert_not_called()
                self.assertEqual(len(ui.errors), 1)

    def test_table_calc_evidence_is_preserved_and_requires_plan(self):
        with tempfile.TemporaryDirectory() as folder:
            root = workbook()
            ci = root.find('.//worksheet/table/datasource-dependencies/column-instance')
            tc = ET.SubElement(ci, 'table-calc', type='Rank', attrib={'ordering-type': 'Rows'})
            ET.SubElement(tc, 'partition', column='[Place0]')
            ir = parse(root, folder)
            sheet = sheets(ir)[0]
            model = sheet['worksheet_semantics']
            calc = next(o for o in model['occurrences'] if o['level'] == 'table')
            self.assertIn('partition', calc['table_calculation_xml'][0])
            self.assertEqual(sheet['support']['status'], 'UNSUPPORTED')
            self.assertIn(calc['id'], model['grain']['panes'][0]['deferred_occurrence_ids'])

    def test_new_mark_cannot_use_old_bar_mapping(self):
        with tempfile.TemporaryDirectory() as folder:
            root = workbook()
            root.find('.//worksheet/table/panes/pane/mark').set('class', 'UnseenMark')
            sheet = sheets(parse(root, folder))[0]
            self.assertEqual(sheet['support']['status'], 'UNSUPPORTED')
            self.assertEqual(sheet['worksheet_semantics']['panes'][0]['marks'], ['UnseenMark'])

    def test_late_drill_metadata_is_checked_in_final_projection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = workbook()
            import tableau_parser as parser
            with patch.object(parser, 'drill_hierarchies', return_value={'Place': ['Category', 'Next level']}):
                ir = parse(root, folder)
            self.assertEqual(ir_contract.validate_ir(ir), [])
            for sheet in sheets(ir):
                self.assertIn('drill', sheet)
                self.assertEqual(sheet['worksheet_semantics']['projection']['status'], 'REQUIRES_PLAN')

    def test_source_filter_and_sort_evidence_survives(self):
        with tempfile.TemporaryDirectory() as folder:
            root = workbook()
            table = root.find('./worksheets/worksheet/table')
            ET.SubElement(table, 'sort', column='[federated.source_0].[sum:Value:qk]', direction='DESC')
            model = sheets(parse(root, folder))[0]['worksheet_semantics']
            evidence = ' '.join(e['xml'] for e in model['query_metadata'])
            self.assertIn('<filter', evidence)
            self.assertIn('<sort', evidence)
            self.assertIn('DESC', evidence)
            self.assertEqual(model['grain']['filter_order'], 'NOT_COMPILED')

    def test_measure_values_expand_from_authored_members_on_rows_and_columns(self):
        for shelf in ('rows', 'cols'):
            with self.subTest(shelf=shelf), tempfile.TemporaryDirectory() as folder:
                root = workbook('kpi')
                for idx, ws in enumerate(root.findall('./worksheets/worksheet')):
                    did = f'federated.source_{idx}'
                    ws.find('.//encodings/text').set('column', f'[{did}].[Multiple Values]')
                    ET.SubElement(ws.find('table'), shelf).text = f'[{did}].[:Measure Names]'
                    filt = ET.SubElement(ws.find('table'), 'filter', column=f'[{did}].[:Measure Names]')
                    union = ET.SubElement(filt, 'groupfilter', function='union')
                    for token in ('usr:Calculation_2:qk', 'sum:Value:qk'):
                        ET.SubElement(union, 'groupfilter', function='member', member=f'"[{did}].[{token}]"')
                ir = parse(root, folder)
                for sheet in sheets(ir):
                    model = sheet['worksheet_semantics']
                    self.assertEqual(model['projection']['status'], 'MAPPED')
                    self.assertEqual(len([o for o in model['occurrences'] if o.get('container_id')]), 2)
                    self.assertEqual(model['grain']['panes'][0]['group_by'], [])
                self.assertEqual(ir_contract.validate_ir(ir), [])

    def test_lod_sql_requires_grain_planning(self):
        with tempfile.TemporaryDirectory() as folder:
            root = workbook('kpi')
            root.find('./datasources/datasource/column[@name="[Calculation_1]"]/calculation').set(
                'formula', '{FIXED : SUM([Value])}')
            sheet = sheets(parse(root, folder))[0]
            self.assertEqual(sheet['worksheet_semantics']['projection']['status'], 'REQUIRES_PLAN')


def smoke_corpus():
    """Optional actual scoped renderer check, independent of the legacy suite."""
    import tableau_parser as parser
    import findings
    for filename in ('Superstore.twb', '_ecom.twb'):
        ir = parser.build_ir(filename, scoped=True)
        ir_contract.require_valid_ir(ir)
        engine.configure(ir)
        mapped = refused = 0
        for dash in ir['dashboards']:
            for sheet in dash['sheets']:
                ui = UI()
                findings.clear()
                with patch.object(engine, 'st', ui):
                    engine.render_sheet(sheet, '')
                blockers = [f for f in findings.all_findings() if f['severity'] == 'BLOCKER']
                if sheet.get('support', {}).get('status') in ('INVALID', 'UNSUPPORTED'):
                    assert blockers and not ui.charts and not ui.metrics, sheet['name']
                    refused += 1
                else:
                    assert not blockers and not ui.errors, (sheet['name'], blockers, ui.errors)
                    mapped += 1
        print(f'{filename}: mapped={mapped}, explicit refusals={refused}; no parity claim', flush=True)


if __name__ == '__main__':
    if '--smoke-corpus' in sys.argv:
        smoke_corpus()
    else:
        unittest.main()
