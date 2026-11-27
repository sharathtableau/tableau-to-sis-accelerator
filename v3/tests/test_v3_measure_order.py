"""Measure-order numeric proofs independent of workbook names and profiles."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_v3_tables import fixture, parse, bindings, TableUI, engine, preview_context
import ir_contract


def ordered(form='computed-sort', direction='DESC', reverse=False, aggregate='sum'):
    root = fixture(reverse=reverse)
    for table in root.findall('./worksheets/worksheet/table'):
        table.remove(table.find('manual-sort'))
        dim = table.find('rows').text
        measure = table.find('.//encodings/text').get('column').replace('sum:', aggregate + ':')
        if form == 'computed-sort':
            ET.SubElement(table, form, column=dim, using=measure, direction=direction)
        else:
            ET.SubElement(ET.SubElement(table, 'shelf-sorts'), form, attrib={
                'dimension-to-sort': dim, 'measure-to-sort-by': measure,
                'direction': direction, 'shelf': 'rows', 'is-on-innermost-dimension': 'true'})
    return root


class MeasureOrderTests(unittest.TestCase):
    def test_both_sort_forms_aggregates_and_directions_across_sources(self):
        for form, agg in [('computed-sort','sum'), ('shelf-sort-v2','avg')]:
            for direction in ('ASC','DESC'):
                with self.subTest(form=form,direction=direction), tempfile.TemporaryDirectory() as folder:
                    ir = parse(ordered(form,direction,reverse=True,aggregate=agg),folder)
                    self.assertEqual(ir_contract.validate_ir(ir),[])
                    rows=[('A',1,2),('B',1,-3),('A',1,4),('C',1,10)]
                    with preview_context(ir,bindings(folder,rows)):
                        for sheet in ir['dashboards'][0]['sheets']:
                            self.assertEqual(sheet['worksheet_semantics']['table_execution']['status'],'READY')
                            ui=TableUI()
                            with patch.object(engine,'st',ui): engine.render_sheet(sheet,'')
                            self.assertEqual(ui.errors,[])
                            expected=['B','A','C'] if direction=='ASC' else ['C','A','B']
                            self.assertEqual(ui.tables[0].iloc[:,0].tolist(),expected)
                            n=int(sheet['datasource_id'][-1])+1
                            self.assertEqual(dict(zip(ui.tables[0].iloc[:,0],ui.tables[0].iloc[:,1])),{'A':6*n,'B':-3*n,'C':10*n})

    def test_sort_precedes_limit_without_manual_member_list(self):
        with tempfile.TemporaryDirectory() as folder:
            ir=parse(ordered(),folder)
            with preview_context(ir,bindings(folder,[(f'row{i}',1,i-100) for i in range(205)])):
                ui=TableUI()
                with patch.object(engine,'st',ui): engine.render_sheet(ir['dashboards'][0]['sheets'][0],'')
                self.assertEqual(ui.tables[0].iloc[:,1].tolist(),list(range(104,-96,-1)))
                self.assertTrue(any('200 of 205' in e for e in ui.errors))

    def test_ties_and_nulls_anywhere_refuse_and_sibling_survives(self):
        for defect in ('ties','null-key','null-header'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as folder:
                ir=parse(ordered(),folder)
                paths=bindings(folder,[(f'row{i}',1,i) for i in range(205)])
                extra={'ties':'extra,1,0\n','null-key':'extra,1,\n','null-header':',1,-1\n'}[defect]
                with open(paths['Unseen source 0'],'a') as file: file.write(extra)
                with preview_context(ir,paths):
                    ui=TableUI()
                    with patch.object(engine,'st',ui): engine.render_sheet(ir['dashboards'][0]['sheets'][0],'')
                    self.assertEqual(ui.tables,[])
                    self.assertTrue(any('tied aggregates or null' in e for e in ui.errors))
                    ui=TableUI()
                    with patch.object(engine,'st',ui): engine.render_sheet(ir['dashboards'][0]['sheets'][1],'')
                    self.assertEqual(len(ui.tables),1)

    def test_ambiguous_static_sort_contracts_never_query(self):
        for defect in ('foreign','hidden','direction','mixed','nested','option','duplicate'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as folder:
                root=ordered()
                table=root.find('./worksheets/worksheet/table'); node=table.find('computed-sort')
                if defect=='foreign': node.set('using',node.get('using').replace('source_0','source_1'))
                if defect=='hidden': node.set('using',node.get('using').replace('sum:','max:'))
                if defect=='direction': node.attrib.pop('direction')
                if defect=='mixed': ET.SubElement(table,'manual-sort',column=table.find('rows').text)
                if defect=='nested': table.find('rows').text+=' / '+table.find('rows').text
                if defect=='option': node.set('unknown','true')
                if defect=='duplicate': ET.SubElement(table,'computed-sort',attrib=dict(node.attrib))
                sheet=parse(root,folder)['dashboards'][0]['sheets'][0]
                self.assertIn(sheet['support']['status'],('UNSUPPORTED','INVALID'))
                with patch.object(engine,'q') as q,patch.object(engine,'st',TableUI()):
                    engine.render_sheet(sheet,''); q.assert_not_called()

    def test_old_or_changed_order_plan_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            ir=parse(ordered(),folder); sheet=ir['dashboards'][0]['sheets'][0]
            sheet['worksheet_semantics']['table_execution']['ordering'][0]['direction']='ASC'
            self.assertTrue(any(i.code=='table-plan' for i in ir_contract.validate_ir(ir)))
            with patch.object(engine,'q') as q,patch.object(engine,'st',TableUI()):
                engine.render_sheet(sheet,''); q.assert_not_called()


if __name__=='__main__': unittest.main()
