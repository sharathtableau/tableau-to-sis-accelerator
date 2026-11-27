"""Real SQL proofs for the shared authored row-filter contract."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import engine
import ir_contract
import cortex_assist as CA
from v3_local import preview_context
from test_v3_field_identity import parse
from test_v3_tables import fixture, bindings, TableUI
from test_v3_cortex_assist import fixture as visual_fixture, Session


def selection(root, values, field=None):
    nodes=[]
    for ws in root.findall('./worksheets/worksheet'):
        table=ws.find('table');did=table.find('datasource-dependencies').get('datasource')
        internal=field or 'Place'+did[-1]
        node=ET.SubElement(table,'filter',{'class':'categorical','column':f'[{did}].[none:{internal}:nk]'})
        union=ET.SubElement(node,'groupfilter',function='union')
        for value in values:
            ET.SubElement(union,'groupfilter',function='member',level=f'[none:{internal}:nk]',
                          member='%null%' if value is None else json.dumps(value))
        nodes.append(node)
    return nodes


class FilterTests(unittest.TestCase):
    def test_filtered_aggregate_order_precedes_display_limit(self):
        from test_v3_measure_order import ordered
        with tempfile.TemporaryDirectory() as folder:
            root=ordered();selection(root,[1],field='Bucket')
            rows=[(f'K{i:03}',1,i+1) for i in range(205)]+[('Excluded',2,9999)]
            ir=parse(root,folder);ui=TableUI()
            with preview_context(ir,bindings(folder,rows)),patch.object(engine,'st',ui):
                engine.render_sheet(ir['dashboards'][0]['sheets'][0],[])
            self.assertEqual(len(ui.tables[0]),200)
            self.assertEqual(ui.tables[0].iloc[:,0].tolist(),[f'K{i:03}' for i in range(204,4,-1)])
            self.assertEqual(ui.tables[0].iloc[:,1].tolist(),list(range(205,5,-1)))
            self.assertTrue(any('200 of 205' in e for e in ui.errors))

    def test_real_streamlit_filtered_table(self):
        from streamlit.testing.v1 import AppTest
        source='''
import sys,tempfile
sys.path.insert(0,ROOT)
sys.path.insert(0,ROOT+'/tests')
import engine
from test_v3_filters import selection
from test_v3_tables import fixture,parse,bindings
from v3_local import preview_context
with tempfile.TemporaryDirectory() as folder:
    root=fixture();selection(root,['A']);selection(root,[1],field='Bucket')
    ir=parse(root,folder)
    with preview_context(ir,bindings(folder)):
        engine.render_sheet(ir['dashboards'][0]['sheets'][0],[])
'''.replace('ROOT',repr(str(Path(__file__).resolve().parents[1]).replace('\\','/')))
        app=AppTest.from_string(source).run(timeout=30)
        self.assertEqual(len(app.exception),0)
        self.assertEqual(len(app.warning),0)
        self.assertEqual(app.dataframe[0].value.iloc[:,1].tolist(),[8])

    def test_composed_filters_numeric_results_and_authored_order_across_sources(self):
        for reverse in (False,True):
            with self.subTest(reverse=reverse),tempfile.TemporaryDirectory() as folder:
                root=fixture(reverse=reverse,duplicate=True)
                selection(root,['A','B',None]);selection(root,[1],field='Bucket')
                ir=parse(root,folder);self.assertEqual(ir_contract.validate_ir(ir),[])
                ui=TableUI()
                with preview_context(ir,bindings(folder)),patch.object(engine,'st',ui):
                    for sheet in ir['dashboards'][0]['sheets']:
                        engine.render_sheet(sheet,[])
                        out=ui.tables[-1]
                        multiplier=int(sheet['datasource_id'][-1])+1
                        self.assertEqual(out.iloc[:,0].tolist(),[None,'A'])
                        self.assertEqual(out.iloc[:,2].tolist(),[7*multiplier,8*multiplier])
                self.assertFalse(ui.errors,ui.errors)

    def test_empty_null_and_intersection_selections(self):
        for values,second,expected in [([],None,[]),([None],None,[None]),(['A','B'],['B'],['B']),(['A'],['B'],[])]:
            with self.subTest(values=values,second=second),tempfile.TemporaryDirectory() as folder:
                root=fixture();selection(root,values)
                if second is not None:selection(root,second)
                ir=parse(root,folder);ui=TableUI()
                with preview_context(ir,bindings(folder)),patch.object(engine,'st',ui):
                    engine.render_sheet(ir['dashboards'][0]['sheets'][0],[])
                self.assertEqual(len(ui.tables),1,ui.errors)
                self.assertEqual(ui.tables[0].iloc[:,0].tolist(),expected)

    def test_cortex_reuses_filter_contract_for_each_replacement_shape(self):
        for mark,target in [('Pie','table'),('Circle','bar'),('Square','dot')]:
            with self.subTest(mark=mark),tempfile.TemporaryDirectory() as folder:
                root=visual_fixture(mark,reverse=True);selection(root,['A'])
                ir=CA.assist(parse(root,folder),Session(target));ui=TableUI()
                self.assertEqual(ir_contract.validate_ir(ir),[])
                with preview_context(ir,bindings(folder)),patch.object(engine,'st',ui):
                    for sheet in ir['dashboards'][0]['sheets']:
                        self.assertIn('ai_adaptation',sheet,ir['ai_assistance'])
                        with patch.object(engine,'q',wraps=engine.q) as query:
                            engine.render_sheet(sheet,[])
                            sql=query.call_args[0][0]
                            data=engine.q(sql)
                            self.assertEqual(data.CATEGORY.tolist(),['A'])
                            self.assertEqual(data.VALUE.tolist(),[18*(int(sheet['datasource_id'][-1])+1)])
                            self.assertLess(sql.index(' WHERE '),sql.index(' GROUP BY'))

    def test_invalid_filter_semantics_refuse_before_query_and_ai(self):
        changes=[lambda n:n.set('context','true'),
                 lambda n:n[0].set('function','except'),
                 lambda n:n[0][0].set('level','[none:Different:nk]'),
                 lambda n:n.set('column','[other].[none:Place0:nk]'),
                 lambda n:n[0][0].set('member','42'),
                 lambda n:n[0][0].set('member',json.dumps('a\\b')),
                 lambda n:ET.SubElement(n,'condition')]
        for shape in ('table','visual'):
            for change in changes:
                with self.subTest(shape=shape,change=change),tempfile.TemporaryDirectory() as folder:
                    root=fixture() if shape=='table' else visual_fixture()
                    nodes=selection(root,['A']);change(nodes[0])
                    ir=parse(root,folder);session=Session('table')
                    if shape=='visual':
                        ir=CA.assist(ir,session)
                        self.assertEqual(len(session.calls),1) # healthy sibling only
                    ui=TableUI()
                    with preview_context(ir,bindings(folder)),patch.object(engine,'st',ui):
                        with patch.object(engine,'q') as query:
                            engine.render_sheet(ir['dashboards'][0]['sheets'][0],[])
                            query.assert_not_called()
                        engine.render_sheet(ir['dashboards'][0]['sheets'][1],[])
                    self.assertEqual(len(ui.tables),1,ui.errors)

    def test_stale_filter_plan_and_catalog_refuse_before_query(self):
        for mutation in ('plan','catalog','source'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as folder:
                root=fixture();selection(root,['A']);ir=parse(root,folder)
                sheet=ir['dashboards'][0]['sheets'][0];model=sheet['worksheet_semantics']
                if mutation=='plan':model['table_execution']['filter_plan']['filters'][0]['values']=['B']
                elif mutation=='source':model['source_xml']=model['source_xml'].replace('&quot;A&quot;','&quot;B&quot;')
                else:
                    fid=model['table_execution']['filter_plan']['filters'][0]['field_id']
                    ir['fields'][fid]['physical']='Changed'
                self.assertTrue(ir_contract.validate_ir(ir))
                with preview_context(ir,bindings(folder)),patch.object(engine,'st',TableUI()),patch.object(engine,'q') as query:
                    engine.render_sheet(sheet,[]);query.assert_not_called()

    def test_literal_quotes_remain_data_and_runtime_sql_is_refused(self):
        text="O'Brien' OR 1=1 --"
        with tempfile.TemporaryDirectory() as folder:
            root=fixture(values=[text,'A']);selection(root,[text]);ir=parse(root,folder);ui=TableUI()
            with preview_context(ir,bindings(folder,[(text,1,5),('A',1,99)])),patch.object(engine,'st',ui):
                sheet=ir['dashboards'][0]['sheets'][0];engine.render_sheet(sheet,[])
                self.assertEqual(ui.tables[0].iloc[:,1].tolist(),[5])
                with patch.object(engine,'q') as query:
                    engine.render_sheet(sheet,['1=1']);query.assert_not_called()


if __name__=='__main__':unittest.main()

