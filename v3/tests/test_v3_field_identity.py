"""Numeric proofs for datasource collisions on two unrelated worksheet shapes."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
import engine
import findings
import ir_contract
import tableau_parser as TP
from v3_local import preview_context


def workbook(shape='bar', reverse=False, rename=False):
    root = ET.Element('workbook')
    sources = ET.SubElement(root, 'datasources')
    worksheets = ET.SubElement(root, 'worksheets')
    sheets = []
    for idx, multiplier in [(0, 2), (1, 10)]:
        did = 'federated.source_' + str(idx)
        label = f'Unseen source {idx}'
        ds = ET.SubElement(sources, 'datasource', name=did, caption=label)
        ET.SubElement(ds, 'column', name='[Value]', caption='Input renamed' if rename else 'Input',
                      role='measure', datatype='real')
        ET.SubElement(ds, 'column', name=f'[Place{idx}]', caption='Category renamed' if rename else 'Category',
                      role='dimension', datatype='string')
        defs = [('Calculation_1', 'Score', f'SUM([Value])*{multiplier}', 'measure', 'real'),
                ('Calculation_2', 'Result', '[Calculation_1]+1', 'measure', 'real'),
                ('Calculation_Filter', 'Choice', f'[Place{idx}]', 'dimension', 'string')]
        for internal, caption, formula, role, datatype in defs:
            col = ET.SubElement(ds, 'column', name=f'[{internal}]',
                                caption=caption + (' renamed' if rename else ''), role=role, datatype=datatype)
            ET.SubElement(col, 'calculation', attrib={'class': 'tableau', 'formula': formula})
        name = f'Sheet {idx}' if not rename else f'Unfamiliar renamed sheet {idx}'
        sheets.append(name)
        ws = ET.SubElement(worksheets, 'worksheet', name=name)
        table = ET.SubElement(ws, 'table')
        dep = ET.SubElement(table, 'datasource-dependencies', datasource=did)
        for col in ds:
            dep.append(deepcopy(col))
        ET.SubElement(dep, 'column-instance', name='[usr:Calculation_2:qk]',
                      column='[Calculation_2]', derivation='User', type='quantitative')
        pane = ET.SubElement(ET.SubElement(table, 'panes'), 'pane')
        ET.SubElement(pane, 'mark', attrib={'class': 'Bar' if shape == 'bar' else 'Text'})
        if shape == 'bar':
            ET.SubElement(table, 'cols').text = f'[{did}].[usr:Calculation_2:qk]'
            ET.SubElement(table, 'rows').text = f'[{did}].[none:Place{idx}:nk]'
        else:
            ET.SubElement(ET.SubElement(pane, 'encodings'), 'text', column=f'[{did}].[usr:Calculation_2:qk]')
        ET.SubElement(table, 'filter', column=f'[{did}].[none:Calculation_Filter:nk]', attrib={'class': 'categorical'})
    if reverse:
        sources[:] = list(reversed(list(sources)))
        worksheets[:] = list(reversed(list(worksheets)))
    dash = ET.SubElement(ET.SubElement(root, 'dashboards'), 'dashboard', name='Mixed sources')
    zones = ET.SubElement(dash, 'zones')
    for idx, name in enumerate(sheets):
        ET.SubElement(zones, 'zone', name=name, x=str(idx * 50000), y='0', w='50000', h='100000')
        ET.SubElement(zones, 'zone', name=name, type='filter',
                      param=f'[federated.source_{idx}].[none:Calculation_Filter:nk]')
    return root


def parse(root, folder):
    path = Path(folder) / 'unfamiliar.twb'
    ET.ElementTree(root).write(path, encoding='utf-8')
    return TP.build_ir(str(path), scoped=True)


class UI:
    def __init__(self):
        self.charts, self.metrics, self.choices, self.errors = [], [], [], []
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def columns(self, weights): return [self] * (weights if isinstance(weights, int) else len(weights))
    def altair_chart(self, chart, **kwargs): self.charts.append(chart)
    def metric(self, *args, **kwargs): self.metrics.append(args)
    def selectbox(self, caption, values, **kwargs):
        self.choices.append((caption, values))
        return values[1]
    def warning(self, message): self.errors.append(message)
    def __getattr__(self, name): return lambda *args, **kwargs: None


class FieldIdentityTests(unittest.TestCase):
    def test_scoped_metadata_and_nested_calculations_on_two_shapes(self):
        for shape in ('bar', 'kpi'):
            for reverse in (False, True):
                with self.subTest(shape=shape, reverse=reverse), tempfile.TemporaryDirectory() as folder:
                    ir = parse(workbook(shape, reverse), folder)
                    self.assertEqual(ir_contract.validate_ir(ir), [])
                    by_name = {s['name']: s for d in ir['dashboards'] for s in d['sheets']}
                    a, b = by_name['Sheet 0'], by_name['Sheet 1']
                    self.assertNotEqual(a['field_bindings']['Result'], b['field_bindings']['Result'])
                    bindings = {}
                    for idx, rows in [(0, 'A,3\nB,7'), (1, 'C,11\nD,13')]:
                        data = Path(folder) / f'data{idx}.csv'
                        data.write_text(f'Place{idx},Value\n{rows}\n', encoding='utf-8')
                        bindings[f'Unseen source {idx}'] = str(data)
                    with preview_context(ir, bindings), patch.object(engine, 'st', UI()):
                        for s, expected in [(a, 21), (b, 241), (a, 21)]:
                            with engine.field_scope(s):
                                value = engine.resolve_measure(engine.tbl(s), s['name'],
                                                               {'caption': 'Result', 'agg': 'usr'})
                                result = engine.q(f"SELECT {value['sql']} AS V FROM {engine.tbl(s)}")
                                self.assertEqual(result.iloc[0, 0], expected)
                            engine.render_sheet(s, '')
                        self.assertFalse([f for f in findings.all_findings() if f['severity'] == 'BLOCKER'])

    def test_rename_preserves_field_ids_and_compiled_semantics(self):
        with tempfile.TemporaryDirectory() as folder:
            original = parse(workbook(), folder)
            renamed = parse(workbook(rename=True, reverse=True), folder)
        self.assertEqual(set(original['fields']), set(renamed['fields']))
        for fid, field in original['fields'].items():
            self.assertEqual(field.get('calculation'), renamed['fields'][fid].get('calculation'))

    def test_same_caption_filters_get_own_values_and_do_not_cross_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            ir = parse(workbook(), folder)
            bindings = {}
            for idx, group in [(0, 'A'), (1, 'B')]:
                path = Path(folder) / f'data{idx}.csv'
                path.write_text(f'Place{idx},Value\n{group},3\n', encoding='utf-8')
                bindings[f'Unseen source {idx}'] = str(path)
            dash = ir['dashboards'][0]
            self.assertEqual(len(dash['filters']), 2)
            ui = UI()
            with preview_context(ir, bindings), patch.object(engine, 'st', ui):
                parts = engine.build_where(dash)
                self.assertEqual([values for _, values in ui.choices], [['All', 'A'], ['All', 'B']])
                for s in dash['sheets']:
                    with engine.field_scope(s):
                        own = engine._parts_for_sheet(parts, s)
                        self.assertEqual(len(own), 1)
                        self.assertEqual(own[0]['datasource_id'], s['datasource_id'])

    def test_invalid_field_reference_rejected_before_codegen(self):
        with tempfile.TemporaryDirectory() as folder:
            ir = parse(workbook(), folder)
        ir['dashboards'][0]['sheets'][0]['field_bindings']['Result'] = 'missing-id'
        self.assertTrue(any(i.code == 'field-reference' for i in ir_contract.validate_ir(ir)))

    def test_duplicate_datasource_captions_are_explicitly_blocked(self):
        root = workbook()
        for ds in root.findall('./datasources/datasource'):
            ds.set('caption', 'Same display name')
        with tempfile.TemporaryDirectory() as folder:
            ir = parse(root, folder)
        self.assertTrue(all(s['support']['status'] == 'INVALID' for d in ir['dashboards'] for s in d['sheets']))

    def test_inline_equal_expressions_and_conflicting_captions(self):
        for shape in ('bar', 'kpi'):
            for equivalent in (True, False):
                with self.subTest(shape=shape, equivalent=equivalent), tempfile.TemporaryDirectory() as folder:
                    root = workbook(shape)
                    ds = root.find('./datasources/datasource')
                    col = ET.SubElement(ds, 'column', name='[SecondResult]', caption='Result',
                                        datatype='real', role='measure')
                    ET.SubElement(col, 'calculation', attrib={'class': 'tableau',
                                  'formula': '[Calculation_1]+1' if equivalent else '[Calculation_1]+100'})
                    pane = root.find('./worksheets/worksheet/table/panes/pane')
                    enc = pane.find('encodings')
                    if enc is None:
                        enc = ET.SubElement(pane, 'encodings')
                    ET.SubElement(enc, 'color', column='[federated.source_0].[usr:SecondResult:qk]')
                    ir = parse(root, folder)
                    sheet = ir['dashboards'][0]['sheets'][0]
                    self.assertEqual(sheet.get('support', {}).get('status') == 'INVALID', not equivalent)
                    result_fields = [f for f in ir['fields'].values()
                                     if f['caption'] == 'Result' and f['datasource_id'] == 'federated.source_0']
                    self.assertEqual(len(result_fields), 2)  # identity never collapsed

    def test_scope_restores_maps_after_failure_and_rejects_foreign_measure(self):
        with tempfile.TemporaryDirectory() as folder:
            ir = parse(workbook(), folder)
        engine.configure(ir)
        a, b = ir['dashboards'][0]['sheets']
        originals = engine.CALCS, engine.COLMAP, engine.ALIASES
        with self.assertRaisesRegex(RuntimeError, 'injected'):
            with engine.field_scope(a):
                with engine.field_scope(b):
                    self.assertIn('*10', engine.CALCS['Result']['sql'])
                self.assertIn('*2', engine.CALCS['Result']['sql'])
                with self.assertRaisesRegex(ValueError, 'Cross-datasource'):
                    engine.resolve_measure('unused', a['name'],
                                           {'caption': 'Result', 'field_id': b['field_bindings']['Result']})
                raise RuntimeError('injected')
        self.assertIs(engine.CALCS, originals[0])
        self.assertIs(engine.COLMAP, originals[1])
        self.assertIs(engine.ALIASES, originals[2])

    def test_untranslated_dependency_blocks_one_sheet_and_preserves_sibling(self):
        with tempfile.TemporaryDirectory() as folder:
            root = workbook('kpi')
            calc = root.find('./datasources/datasource/column[@name="[Calculation_1]"]/calculation')
            calc.set('formula', 'SUM([Foreign source].[Value])')
            ir = parse(root, folder)
            a, b = ir['dashboards'][0]['sheets']
            self.assertEqual(a['support']['status'], 'INVALID')
            self.assertNotEqual(b.get('support', {}).get('status'), 'INVALID')
            self.assertEqual(ir_contract.assess_ir(ir)['state'], 'BLOCKED')
            bindings = {}
            for idx in (0, 1):
                path = Path(folder) / f'data{idx}.csv'
                path.write_text(f'Place{idx},Value\nA,3\n', encoding='utf-8')
                bindings[f'Unseen source {idx}'] = str(path)
            ui = UI()
            with preview_context(ir, bindings), patch.object(engine, 'st', ui):
                engine.render_sheet(a, '')
                engine.render_sheet(b, '')
            self.assertEqual(len(ui.errors), 1)
            self.assertEqual(len(ui.metrics), 1)

    def test_undeclared_datasource_does_not_inherit_local_metadata(self):
        meta, _ = TP.column_meta(workbook(), _scoped=True)
        local = meta.for_datasource('federated.source_0')
        self.assertNotIn('field_id', TP.parse_field('[unknown].[usr:Calculation_2:qk]', local))

    def test_ambiguous_unscoped_runtime_lookup_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            ir = parse(workbook(), folder)
        engine.configure(ir)
        self.assertNotIn('Result', ir['calcs'])
        with self.assertRaisesRegex(ValueError, 'datasource-scoped'):
            engine.resolve_measure('unused', 'sheet', {'caption': 'Result'})
        with self.assertRaisesRegex(ValueError, 'datasource-scoped'):
            engine.rdim('Category')

    def test_failed_filter_query_keeps_other_controls(self):
        with tempfile.TemporaryDirectory() as folder:
            ir = parse(workbook(), folder)
            path = Path(folder) / 'data.csv'
            path.write_text('Place1,Value\nB,7\n', encoding='utf-8')
            bindings = {'Unseen source 0': str(path), 'Unseen source 1': str(path)}
            ui = UI()
            with preview_context(ir, bindings), patch.object(engine, 'st', ui):
                parts = engine.build_where(ir['dashboards'][0])
            self.assertEqual(len(ui.errors), 1)
            self.assertEqual(len(parts), 1)
            self.assertEqual(parts[0]['datasource_id'], 'federated.source_1')

    def test_qualified_reference_guard_ignores_strings_and_parameters(self):
        from field_identity import has_unplanned_qualified_reference as check
        self.assertTrue(check('SUM([Unfamiliar datasource].[Value])'))
        self.assertFalse(check('"[Unfamiliar datasource].[Value]"'))
        self.assertFalse(check('[Parameters].[Parameter 1] + 1 // [Foreign].[Value]'))


if __name__ == '__main__':
    unittest.main()
