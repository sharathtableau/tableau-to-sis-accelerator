from pathlib import Path
import sys,tempfile,unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_v3_field_identity import workbook,parse,UI
from v3_local import preview_context
import engine,ir_contract


def labelled(rename=False, vertical=False, wrong_tooltip=False):
    root=workbook(rename=rename)
    for idx,ws in enumerate(root.findall('./worksheets/worksheet')):
        ds=f'federated.source_{idx}'
        dim=f'[{ds}].[none:Calculation_Filter:nk]'
        metric=f'[{ds}].[usr:Calculation_2:qk]'
        ws.find('./table/rows').text=metric if vertical else dim
        ws.find('./table/cols').text=dim if vertical else metric
        pane=ws.find('.//pane');enc=ET.SubElement(pane,'encodings')
        ET.SubElement(enc,'text',column=dim)
        ET.SubElement(enc,'text',column=metric)
        tooltip=f'[{ds}].[attr:{"Value" if wrong_tooltip else "Calculation_Filter"}:nk]'
        ET.SubElement(enc,'tooltip',column=tooltip)
        formatted=ET.SubElement(ET.SubElement(pane,'customized-label'),'formatted-text')
        ET.SubElement(formatted,'run').text=f'<{dim}> / <{metric}>'
    return root


class LabelTests(unittest.TestCase):
    def test_same_group_attr_and_composed_label_numeric_rename_invariance(self):
        for rename in (False,True):
            for vertical in (False,True):
                with self.subTest(rename=rename,vertical=vertical),tempfile.TemporaryDirectory() as folder:
                    ir=parse(labelled(rename,vertical),folder)
                    self.assertEqual(ir_contract.validate_ir(ir),[])
                    bindings={}
                    for i in range(2):
                        file=Path(folder)/f'{i}.csv';file.write_text(f'Place{i},Value\nA,3\nB,7\nA,5\n')
                        bindings[f'Unseen source {i}']=str(file)
                    with preview_context(ir,bindings),patch.object(engine,'st',UI()):
                        for i,sheet in enumerate(ir['dashboards'][0]['sheets']):
                            self.assertEqual(sheet['worksheet_semantics']['projection']['status'],'MAPPED')
                            calls=[];real=engine.q
                            def query(sql):
                                frame=real(sql);calls.append(frame);return frame
                            with patch.object(engine,'q',side_effect=query),engine.field_scope(sheet):
                                engine.r_bar(sheet,'')
                            rows=calls[0]
                            self.assertEqual(list(rows['DIM']),['A','B'])
                            self.assertEqual(list(rows['VAL']),[17,15] if i==0 else [81,71])
                            self.assertEqual(list(rows['LBLTXT']),['A / 17.00','B / 15.00'] if i==0 else ['A / 81.00','B / 71.00'])
    def test_attr_outside_group_stays_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            ir=parse(labelled(wrong_tooltip=True),folder)
            for sheet in ir['dashboards'][0]['sheets']:
                self.assertEqual(sheet['support']['status'],'UNSUPPORTED')
                self.assertIn('Unconsumed tooltip',sheet['support']['reason'])


if __name__=='__main__':unittest.main()
