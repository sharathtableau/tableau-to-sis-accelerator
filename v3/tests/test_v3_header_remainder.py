"""E18.1: singleton remainder, independent shape and rejection proofs."""
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
import xml.etree.ElementTree as ET
from test_v3_more_visuals import fixture, parse, engine, ir_contract, preview_context
from test_v3_tables import TableUI


def partial(renamed=False, independent=False):
    root=fixture('cross',renamed)
    for i,table in enumerate(root.findall('./worksheets/worksheet/table')):
        dictionary=table.find('manual-sort/dictionary')
        if independent:
            ds=f'federated.source_{i}'
            for parent in [root.find(f'./datasources/datasource[@name="{ds}"]'),table.find('datasource-dependencies')]:
                ET.SubElement(parent,'column',name='[Extra]',caption='Additional renamed' if renamed else 'Additional',role='measure',datatype='real')
            group=table.find('filter/groupfilter[@function="union"]')
            ET.SubElement(group,'groupfilter',function='member',level='[:Measure Names]',member=json.dumps(f'[{ds}].[min:Extra:qk]'))
            dictionary[:]=list(reversed(list(dictionary)))
        else:
            dictionary.remove(dictionary[-1])
    return root


class HeaderRemainderTests(unittest.TestCase):
    def test_singleton_numeric_order_for_renamed_and_independent_shapes(self):
        for renamed in (False,True):
            for independent in (False,True):
                with self.subTest(renamed=renamed,independent=independent),tempfile.TemporaryDirectory() as folder:
                    root=partial(renamed,independent)
                    # Reverse membership XML: union order must not decide the prefix.
                    for g in root.findall('.//filter/groupfilter[@function="union"]'):g[:]=list(reversed(list(g)))
                    ir=parse(root,folder);self.assertEqual(ir_contract.validate_ir(ir),[])
                    bindings={}
                    for i in range(2):
                        p=Path(folder)/f'{i}.csv'
                        p.write_text(f'Place{i},Value,Extra\nA,3,9\nA,5,-4\nB,7,2\n',encoding='utf-8')
                        bindings[f'Unseen source {i}']=str(p)
                    with preview_context(ir,bindings):
                        for sheet in ir['dashboards'][0]['sheets']:
                            self.assertNotIn(sheet.get('support',{}).get('status'),('INVALID','UNSUPPORTED'))
                            ui=TableUI()
                            with patch.object(engine,'st',ui):engine.render_sheet(sheet,'')
                            self.assertEqual(ui.errors,[]);self.assertEqual(len(ui.tables),1)
                            self.assertEqual(ui.tables[0].values.tolist(),
                                             [['A',4,8,-4],['B',7,7,2]] if independent else [['A',8,4],['B',7,7]])

    def test_multiple_remainders_remain_blocked(self):
        root=partial(independent=True)
        d=root.find('.//manual-sort/dictionary');d.remove(d[-1])
        with tempfile.TemporaryDirectory() as folder:
            sheet=parse(root,folder)['dashboards'][0]['sheets'][0]
            self.assertIn('remainder-order contract',sheet['support']['reason'])
            with patch.object(engine,'q') as query,patch.object(engine,'st',TableUI()):
                engine.render_sheet(sheet,'');query.assert_not_called()

    def test_malformed_manual_contracts_never_query(self):
        for defect in ('empty','array','object','null','duplicate','foreign','option','bucket-option','child','two-dictionaries','missing-dictionary'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as folder:
                root=partial();manual=root.find('.//manual-sort');d=manual.find('dictionary')
                if defect=='empty':d.clear()
                elif defect in ('array','object','null'):d[0].text={'array':'[]','object':'{}','null':'null'}[defect]
                elif defect=='duplicate':d.append(deepcopy(d[0]))
                elif defect=='foreign':d[0].text=json.dumps('[foreign].[sum:Value:qk]')
                elif defect=='option':manual.set('unknown','true')
                elif defect=='bucket-option':d[0].set('unknown','true')
                elif defect=='child':ET.SubElement(d,'unknown')
                elif defect=='two-dictionaries':manual.append(deepcopy(d))
                else:manual.remove(d)
                sheet=parse(root,folder)['dashboards'][0]['sheets'][0]
                self.assertIn('Invalid measure header ordering',sheet['support']['reason'])
                with patch.object(engine,'q') as query,patch.object(engine,'st',TableUI()):
                    engine.render_sheet(sheet,'');query.assert_not_called()

    def test_tampered_cached_plan_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            ir=parse(partial(),folder);sheet=ir['dashboards'][0]['sheets'][0]
            plan=sheet['worksheet_semantics']['projection']
            plan['bindings'].clear()
            self.assertTrue(ir_contract.validate_ir(ir))
            with patch.object(engine,'q') as query,patch.object(engine,'st',TableUI()):
                engine.render_sheet(sheet,'');query.assert_not_called()


if __name__=='__main__':unittest.main()
