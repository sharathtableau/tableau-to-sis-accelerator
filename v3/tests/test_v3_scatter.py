from pathlib import Path
import sys,tempfile,unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_v3_field_identity import workbook,parse,UI
from v3_local import preview_context
import engine,ir_contract,scatter_plan


def fixture(rename=False,detail=False):
    root=workbook(rename=rename)
    for i,ws in enumerate(root.findall('./worksheets/worksheet')):
        ds=f'federated.source_{i}';table=ws.find('table')
        table.find('cols').text=f'[{ds}].[sum:Value:qk]'
        table.find('rows').text=f'[{ds}].[avg:Value:qk]'
        pane=table.find('./panes/pane');pane.find('mark').set('class','Circle')
        enc=ET.SubElement(pane,'encodings')
        ET.SubElement(enc,'color',column=f'[{ds}].[none:Place{i}:nk]')
        ET.SubElement(enc,'text',column=f'[{ds}].[none:Place{i}:nk]')
        ET.SubElement(enc,'size',column=f'[{ds}].[sum:Value:qk]')
        if detail:ET.SubElement(enc,'lod',column=f'[{ds}].[none:Calculation_Filter:nk]')
    return root


class ScatterTests(unittest.TestCase):
    def test_numeric_marks_grouping_and_rename_variations(self):
        for renamed in (False,True):
            for detail in (False,True):
                with self.subTest(renamed=renamed,detail=detail),tempfile.TemporaryDirectory() as folder:
                    ir=parse(fixture(renamed,detail),folder)
                    self.assertEqual(ir_contract.validate_ir(ir),[])
                    bindings={}
                    for i in range(2):
                        file=Path(folder)/f'{i}.csv';file.write_text(f'Place{i},Value\nA,3\nB,7\nA,5\n')
                        bindings[f'Unseen source {i}']=str(file)
                    with preview_context(ir,bindings),patch.object(engine,'st',UI()):
                        for sheet in ir['dashboards'][0]['sheets']:
                            self.assertEqual(sheet['worksheet_semantics']['projection']['status'],'MAPPED',sheet.get('support'))
                            plan=scatter_plan.build(sheet,sheet['worksheet_semantics'])
                            frames=[];real=engine.q
                            def query(sql):
                                frame=real(sql);frames.append(frame);return frame
                            with patch.object(engine,'q',side_effect=query),engine.field_scope(sheet):
                                engine.r_scatter(sheet,'')
                            frame=frames[-1]
                            self.assertEqual(len(frame),2)
                            metrics=[c for c in frame if c.startswith('M')]
                            self.assertEqual(list(frame[metrics[0]]),[8,7])
                            self.assertEqual(list(frame[metrics[1]]),[4,7])
                            self.assertEqual(list(frame['LABEL']),['A','B'])
                            tips=engine.st.charts[-1].to_dict()['layer'][0]['encoding']['tooltip']
                            self.assertTrue(any('AVG' in t.get('title','') for t in tips))
                            self.assertTrue(any('SUM' in t.get('title','') for t in tips))
    def test_field_default_aggregation_is_not_disaggregated_view(self):
        root=fixture();source=root.find('./datasources/datasource')
        record=ET.SubElement(ET.SubElement(source,'metadata-records'),'metadata-record',attrib={'class':'column'})
        ET.SubElement(record,'aggregation').text='Sum'
        with tempfile.TemporaryDirectory() as folder:
            ir=parse(root,folder);sheet=ir['dashboards'][0]['sheets'][0]
            self.assertEqual(sheet['worksheet_semantics']['projection']['status'],'MAPPED')
            ET.SubElement(root.find('.//worksheet/table'),'aggregation',value='false')
            ir=parse(root,folder);sheet=ir['dashboards'][0]['sheets'][0]
            self.assertIn('Disaggregated',sheet['support']['reason'])
    def test_physical_normalization_collision_refuses(self):
        root=fixture();source=root.find('./datasources/datasource')
        ET.SubElement(source,'column',name='[value]',caption='Different physical field',role='measure',datatype='real')
        with tempfile.TemporaryDirectory() as folder:
            ir=parse(root,folder);sheet=ir['dashboards'][0]['sheets'][0]
            self.assertIn('collide',sheet['support']['reason'])
            with patch.object(engine,'q',side_effect=AssertionError('query must not run')),patch.object(engine,'st',UI()):engine.render_sheet(sheet,[])
    def test_unknown_channel_refuses_before_query(self):
        root=fixture();ET.SubElement(root.find('.//pane/encodings'),'path',column='[federated.source_0].[none:Place0:nk]')
        with tempfile.TemporaryDirectory() as folder:
            ir=parse(root,folder);sheet=ir['dashboards'][0]['sheets'][0]
            self.assertEqual(sheet['support']['status'],'UNSUPPORTED')
            with patch.object(engine,'q',side_effect=AssertionError('SQL must not execute')),patch.object(engine,'st',UI()):
                engine.render_sheet(sheet,[])


if __name__=='__main__':unittest.main()
