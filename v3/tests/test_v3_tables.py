"""Numeric/order proofs for the bounded occurrence-driven text-table contract."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine
import findings
import ir_contract
from test_v3_field_identity import workbook, parse, UI
from v3_local import preview_context


class TableUI(UI):
    def __init__(self):
        super().__init__()
        self.tables, self.labels, self.captions = [], [], []
    def dataframe(self, data, **kwargs):
        self.tables.append(data)
        self.labels.append(kwargs.get('column_config'))
    def caption(self, message): self.captions.append(message)


def manual(table, token, values):
    node = ET.SubElement(table, 'manual-sort', column=token, direction='ASC')
    dictionary = ET.SubElement(node, 'dictionary')
    for v in values:
        ET.SubElement(dictionary, 'bucket').text = '%null%' if v is None else json.dumps(v)


def fixture(multi=False, reverse=False, values=None, duplicate=False):
    root = workbook(reverse=reverse, rename=reverse)
    sources = {d.get('name'): d for d in root.findall('./datasources/datasource')}
    for ds in sources.values():
        ET.SubElement(ET.SubElement(ds, 'connection'), 'relation', type='table', table='[source]')
        ET.SubElement(ds, 'column', name='[Bucket]', caption='Shared caption', datatype='integer', role='dimension')
        for col in ds.findall('column'):
            if col.get('name') in ('[Value]', '[Place0]', '[Place1]'):
                col.set('caption', 'Shared caption')
    for ws in root.findall('./worksheets/worksheet'):
        table = ws.find('table')
        did = table.find('datasource-dependencies').get('datasource')
        idx = did[-1]
        for f in table.findall('filter'): table.remove(f)
        row = f'[{did}].[none:Place{idx}:nk]'
        extra = f'[{did}].[none:Bucket:nk]'
        table.find('rows').text = row + (' / ' + extra if multi else '') + (' / ' + row if duplicate else '')
        table.find('cols').text = ''
        pane = table.find('./panes/pane')
        pane.find('mark').set('class', 'Text')
        enc = ET.SubElement(pane, 'encodings')
        for agg in ('sum', 'avg', 'cnt', 'ctd', 'sum'):
            ET.SubElement(enc, 'text', column=f'[{did}].[{agg}:Value:qk]')
        if multi:
            # Same physical field is both a dimension and a measure.
            ET.SubElement(enc, 'text', column=f'[{did}].[sum:Bucket:qk]')
            manual(table, extra, [2, 1])
        manual(table, row, values if values is not None else ['B', None, 'A'])
        ET.SubElement(table, 'aggregation', value='true')
    zones = root.find('.//dashboard/zones')
    for z in list(zones):
        if z.get('type') == 'filter': zones.remove(z)
    return root


def bindings(folder, rows=None):
    result = {}
    rows = rows or [('A', 1, 2), ('B', 2, -3), ('A', 1, 4), (None, 1, 7),
                    ('B', 2, None), ('A', 2, 10), ('A', 1, 2)]
    for idx in (0, 1):
        file = Path(folder) / f'source{idx}.csv'
        file.write_text(f'Place{idx},Bucket,Value\n' + ''.join(
            f'{g or ""},{b},{v*(idx+1) if v is not None else ""}\n' for g,b,v in rows), encoding='utf-8')
        result[f'Unseen source {idx}'] = str(file)
    return result


class TableTests(unittest.TestCase):
    def test_numeric_roles_duplicates_nulls_and_nested_order_across_sources(self):
        for multi, reverse, duplicate in ((False, False, False), (True, True, True)):
            with self.subTest(multi=multi), tempfile.TemporaryDirectory() as folder:
                ir = parse(fixture(multi, reverse, duplicate=duplicate), folder)
                self.assertEqual(ir_contract.validate_ir(ir), [])
                with preview_context(ir, bindings(folder)):
                    for sheet in ir['dashboards'][0]['sheets']:
                        plan = sheet['worksheet_semantics']['table_execution']
                        self.assertEqual(plan['status'], 'READY', plan['reasons'])
                        self.assertNotIn(sheet.get('support', {}).get('status'), ('INVALID','UNSUPPORTED'))
                        n = int(sheet['datasource_id'][-1]) + 1
                        ui = TableUI()
                        with patch.object(engine, 'st', ui), patch.object(engine, 'r_table') as legacy:
                            engine.render_sheet(sheet, '')
                            legacy.assert_not_called()
                        self.assertEqual(ui.errors, [])
                        data = ui.tables[0]
                        self.assertEqual(len(set(data.columns)), len(data.columns))
                        self.assertEqual(list(data.columns), [c['alias'] for c in plan['columns']])
                        groups = 3 if multi else 1
                        actual = data.iloc[:, groups:].values.tolist()
                        expected = ([[-3*n,-3*n,1,1,-3*n,4], [7*n,7*n,1,1,7*n,1],
                                     [10*n,10*n,1,1,10*n,2], [8*n,8*n/3,3,2,8*n,3]] if multi else
                                    [[-3*n,-3*n,1,1,-3*n], [7*n,7*n,1,1,7*n], [18*n,4.5*n,4,3,18*n]])
                        for row, exp in zip(actual, expected):
                            for value, want in zip(row, exp): self.assertAlmostEqual(value, want)
                        self.assertEqual(len(actual), len(expected))
                        self.assertEqual(data.iloc[0,0], 'B')
                        self.assertIsNone(data.iloc[1,0])
                        self.assertEqual(list(data.iloc[2:,0]), ['A'] * (2 if multi else 1))
                        if multi:
                            self.assertEqual(list(data.iloc[:,1]), [2,1,2,1])
                            self.assertEqual(list(data.iloc[:,0]), list(data.iloc[:,2]))
                        self.assertEqual(len(set(ui.labels[0].values())), len(data.columns))

    def test_sort_precedes_limit_and_truncation_reports_total(self):
        values = [f'G{i:03d}' for i in reversed(range(205))]
        with tempfile.TemporaryDirectory() as folder:
            ir = parse(fixture(values=values), folder)
            rows = [(f'G{i:03d}',1,i) for i in range(205)]
            with preview_context(ir, bindings(folder, rows)):
                ui = TableUI()
                with patch.object(engine, 'st', ui), patch.object(engine, 'q', wraps=engine.q) as query:
                    engine.render_sheet(ir['dashboards'][0]['sheets'][0], '')
                self.assertEqual(list(ui.tables[0].iloc[:,0]), values[:200])
                self.assertEqual(list(ui.tables[0].iloc[:,1]), list(reversed(range(5,205))))
                self.assertTrue(any('200 of 205' in message for message in ui.errors))
                self.assertIn('ORDER BY', query.call_args.args[0].split(' LIMIT ')[0])

    def test_unknown_member_beyond_limit_refuses_and_sibling_renders(self):
        values = [f'G{i:03d}' for i in range(205)]
        with tempfile.TemporaryDirectory() as folder:
            ir = parse(fixture(values=values), folder)
            paths = bindings(folder, [(g,1,2) for g in values])
            with open(paths['Unseen source 0'], 'a') as file: file.write('outside,1,9\n')
            with preview_context(ir, paths):
                bad, good = ir['dashboards'][0]['sheets']
                ui = TableUI()
                with patch.object(engine, 'st', ui): engine.render_sheet(bad, '')
                self.assertEqual(ui.tables, [])
                self.assertTrue(any('outside the explicit ordering' in e for e in ui.errors))
                ui = TableUI()
                with patch.object(engine, 'st', ui): engine.render_sheet(good, '')
                self.assertEqual(len(ui.tables), 1)

    def test_unplanned_constructs_refuse_before_sql(self):
        for defect in ('pivot','total','table-calc','relationship','join','sort','missing-order',
                       'duplicate-order','filter','disaggregated','detail','calc','format','source-alias'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as folder:
                root = fixture()
                ds = root.find('./datasources/datasource')
                table = root.find('./worksheets/worksheet/table')
                if defect == 'pivot': table.find('cols').text = table.find('rows').text
                if defect == 'total': ET.SubElement(table, 'grandtotals', enabled='true')
                if defect == 'table-calc': ET.SubElement(table, 'table-calc', type='Rank')
                if defect == 'relationship': ET.SubElement(ds, 'relationship')
                if defect == 'join': ds.find('.//relation').set('type', 'join')
                if defect == 'sort': ET.SubElement(table, 'computed-sort', using='[sum:Value:qk]', column='[none:Place0:nk]')
                if defect == 'missing-order': table.remove(table.find('manual-sort'))
                if defect == 'duplicate-order': table.append(deepcopy(table.find('manual-sort')))
                if defect == 'filter': ET.SubElement(table, 'filter', column='[federated.source_0].[none:Place0:nk]')
                if defect == 'disaggregated': table.find('aggregation').set('value', 'false')
                if defect == 'detail': ET.SubElement(table.find('.//encodings'), 'detail', column=table.find('rows').text)
                if defect == 'calc': ET.SubElement(ds.find('column[@name="[Value]"]'), 'calculation', formula='1+1')
                if defect == 'format': ET.SubElement(table.find('.//pane'), 'customized-label')
                if defect == 'source-alias': ET.SubElement(ds.find('column'), 'aliases')
                ir = parse(root, folder)
                bad, good = ir['dashboards'][0]['sheets']
                self.assertIn(bad['support']['status'], ('INVALID','UNSUPPORTED'))
                with patch.object(engine, 'q') as q, patch.object(engine, 'st', TableUI()):
                    engine.render_sheet(bad, '')
                    q.assert_not_called()
                with preview_context(ir, bindings(folder)):
                    ui = TableUI()
                    with patch.object(engine, 'st', ui): engine.render_sheet(good, '')
                    self.assertEqual(ui.errors, [])
                    self.assertEqual(len(ui.tables), 1)

    def test_stale_plan_catalog_and_runtime_filter_refuse_before_sql(self):
        for defect in ('plan','catalog','missing-plan','filter'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as folder:
                ir = parse(fixture(), folder)
                sheet = ir['dashboards'][0]['sheets'][0]
                if defect == 'plan': sheet['worksheet_semantics']['table_execution']['display_limit'] = 1
                if defect == 'missing-plan': del sheet['worksheet_semantics']['table_execution']
                if defect == 'catalog':
                    fid = sheet['worksheet_semantics']['table_execution']['columns'][0]['field_id']
                    ir['fields'][fid]['physical'] = 'Changed'
                with preview_context(ir, bindings(folder)), patch.object(engine, 'q') as q, patch.object(engine, 'st', TableUI()) as ui:
                    engine.render_sheet(sheet, ['unplanned'] if defect == 'filter' else '')
                    q.assert_not_called()
                    self.assertTrue(ui.errors)
                    if defect == 'catalog': self.assertIn('Field catalog changed', ui.errors[0])
                    if defect == 'filter': self.assertIn('typed scoped controls', ui.errors[0])

    def test_scoped_dashboard_filter_executes_and_preserves_numeric_results(self):
        for multi, reverse in ((False, False), (True, True)):
            with self.subTest(multi=multi, reverse=reverse), tempfile.TemporaryDirectory() as folder:
                ir = parse(fixture(multi=multi, reverse=reverse), folder)
                sheet = ir['dashboards'][0]['sheets'][0]
                datasource_id = sheet['datasource_id']
                field_id = f'{datasource_id}::Place{datasource_id[-1]}'
                paths = bindings(folder)
                with preview_context(ir, paths):
                    part = {'col': f'"Place{datasource_id[-1]}"',
                            'clause': f'"Place{datasource_id[-1]}" = \'A\'',
                            'caption': 'Shared caption', 'scope': sheet['name'],
                            'table': engine.tbl(sheet), 'governs': True,
                            'datasource_id': datasource_id, 'field_id': field_id,
                            'runtime_filter': {'kind': 'categorical', 'value': 'A'}}
                    ui = TableUI()
                    with patch.object(engine, 'st', ui), patch.object(engine, 'q', wraps=engine.q) as query:
                        engine.render_sheet(sheet, [part])
                    self.assertEqual(ui.errors, [])
                    data = ui.tables[0]
                    self.assertTrue(all(str(v) == 'A' for v in data.iloc[:, 0]))
                    if not multi:
                        self.assertEqual(data.iloc[0, 1], 18)
                    self.assertIn('"PLACE', query.call_args.args[0].upper())
                    self.assertIn("= 'A'", query.call_args.args[0])

    def test_runtime_table_filter_with_foreign_identity_refuses_before_query(self):
        with tempfile.TemporaryDirectory() as folder:
            ir = parse(fixture(), folder)
            sheet = ir['dashboards'][0]['sheets'][0]
            with preview_context(ir, bindings(folder)), patch.object(engine, 'q') as query:
                item = {'col': '"Place0"', 'clause': '"Place0" = \'A\'',
                        'table': engine.tbl(sheet), 'governs': True,
                        'datasource_id': sheet['datasource_id'], 'field_id': 'other.source::Place0',
                        'runtime_filter': {'kind': 'categorical', 'value': 'A'}}
                ui = TableUI()
                with patch.object(engine, 'st', ui): engine.render_sheet(sheet, [item])
                query.assert_not_called()
                self.assertTrue(any('same-source field identity' in e for e in ui.errors))

    def test_caption_renaming_preserves_occurrence_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            root = fixture(multi=True, duplicate=True)
            a = parse(root, folder)
            for col in root.findall('.//column'): col.set('caption', 'Changed display')
            for ws in root.findall('./worksheets/worksheet'):
                old = ws.get('name'); ws.set('name', 'Renamed ' + old)
                root.find(f'.//zone[@name="{old}"]').set('name', 'Renamed ' + old)
            root.find('datasources')[:] = list(reversed(root.find('datasources')[:]))
            b = parse(root, folder)
            for x,y in zip(a['dashboards'][0]['sheets'], b['dashboards'][0]['sheets']):
                xp,yp = (s['worksheet_semantics']['table_execution'] for s in (x,y))
                self.assertEqual([c['alias'] for c in xp['columns']], [c['alias'] for c in yp['columns']])
                self.assertEqual(xp['ordering'], yp['ordering'])

    def test_empty_and_all_null_aggregates(self):
        for rows in ([], [('B',1,None), ('B',1,None)]):
            with self.subTest(rows=rows), tempfile.TemporaryDirectory() as folder:
                ir = parse(fixture(), folder)
                paths = bindings(folder)
                for idx, path in enumerate(paths.values()):
                    Path(path).write_text(f'Place{idx},Bucket,Value\n' + ''.join('B,1,\n' for _ in rows))
                with preview_context(ir, paths):
                    ui = TableUI()
                    with patch.object(engine, 'st', ui): engine.render_sheet(ir['dashboards'][0]['sheets'][0], '')
                    self.assertEqual(ui.errors, [])
                    self.assertEqual(len(ui.tables[0]), 1 if rows else 0)
                    if rows:
                        self.assertTrue(ui.tables[0].iloc[:,[1,2,5]].isna().all().all())
                        self.assertEqual(ui.tables[0].iloc[0,3:5].tolist(), [0,0])

    def test_numeric_manual_dimensions_and_quoted_members(self):
        with tempfile.TemporaryDirectory() as folder:
            root = fixture(multi=True, values=["O'Brien", 'A'])
            ir = parse(root, folder)
            with preview_context(ir, bindings(folder, [('A',1,3), ("O'Brien",2,5), ("O'Brien",1,7)])):
                ui = TableUI()
                with patch.object(engine, 'st', ui): engine.render_sheet(ir['dashboards'][0]['sheets'][0], '')
                self.assertEqual(ui.errors, [])
                self.assertEqual(ui.tables[0].iloc[:,0].tolist(), ["O'Brien", "O'Brien", 'A'])
                self.assertEqual(ui.tables[0].iloc[:,1].tolist(), [2,1,1])
                self.assertEqual(ui.tables[0].iloc[:,2].tolist(), [5,7,3])
        # Exercise the remaining supported aggregate operators on actual rows.
        with tempfile.TemporaryDirectory() as folder:
            root = fixture()
            for enc in root.findall('.//worksheet/table/panes/pane/encodings'):
                for element, agg in zip(enc, ('min','max','cnt','ctd','sum')):
                    token = element.get('column')
                    element.set('column', token.replace(token.split('].[')[1].split(':')[0] + ':Value', agg + ':Value'))
            ir = parse(root, folder)
            with preview_context(ir, bindings(folder)):
                ui = TableUI()
                with patch.object(engine, 'st', ui): engine.render_sheet(ir['dashboards'][0]['sheets'][0], '')
                self.assertEqual(ui.errors, [])
                self.assertEqual(ui.tables[0].iloc[:,1].tolist(), [-3,7,2])
                self.assertEqual(ui.tables[0].iloc[:,2].tolist(), [-3,7,10])

    def test_malformed_manual_orders_and_unknown_clauses_refuse(self):
        for defect in ('duplicate','wrong-type','descending','unknown','total-attribute','missing-null','literal-escape'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as folder:
                root = fixture()
                table = root.find('./worksheets/worksheet/table')
                order = table.find('manual-sort')
                if defect == 'duplicate': order[0].append(deepcopy(order[0][0]))
                if defect == 'wrong-type': order[0][0].text = '123'
                if defect == 'literal-escape': order[0][0].text = json.dumps("back\\slash")
                if defect == 'descending': order.set('direction','DESC')
                if defect == 'unknown': ET.SubElement(table,'unfamiliar-clause')
                if defect == 'total-attribute': table.set('grand-total','true')
                if defect == 'missing-null': order[0].remove(order[0][1])
                ir = parse(root, folder)
                sheet = ir['dashboards'][0]['sheets'][0]
                with preview_context(ir, bindings(folder)):
                    ui = TableUI()
                    with patch.object(engine,'st',ui), patch.object(engine,'q',wraps=engine.q) as query:
                        engine.render_sheet(sheet,'')
                    self.assertEqual(ui.tables, [])
                    self.assertTrue(ui.errors)
                    if defect != 'missing-null': query.assert_not_called()

    def test_real_streamlit_dataframe_serializes_duplicate_captions(self):
        from streamlit.testing.v1 import AppTest
        source = '''
import sys, tempfile
sys.path.insert(0, ROOT)
sys.path.insert(0, ROOT + '/tests')
import engine
from test_v3_tables import fixture, parse, bindings
from v3_local import preview_context
with tempfile.TemporaryDirectory() as folder:
    ir = parse(fixture(multi=True, duplicate=True), folder)
    with preview_context(ir, bindings(folder)):
        engine.render_sheet(ir['dashboards'][0]['sheets'][0], '')
'''.replace('ROOT', repr(str(Path(__file__).resolve().parents[1]).replace('\\','/')))
        app = AppTest.from_string(source).run(timeout=30)
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.warning), 0)
        self.assertEqual(len(app.dataframe), 1)
        self.assertEqual(app.dataframe[0].value.shape, (4,9))
        self.assertEqual(app.dataframe[0].value.iloc[:,3].tolist(), [-3,7,10,8])


if __name__ == '__main__': unittest.main()
