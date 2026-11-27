"""Cortex transport is mocked; source validation, SQL and rendering are real."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import cortex_assist as CA
import engine
import ir_contract
import v3_local
from test_v3_field_identity import workbook, parse
from test_v3_tables import TableUI, bindings
from v3_local import preview_context


def fixture(mark='Pie',reverse=False,calculated=False):
    root=workbook(reverse=reverse,rename=reverse)
    for ds in root.findall('./datasources/datasource'):
        ET.SubElement(ET.SubElement(ds,'connection'),'relation',type='table',table='[source]')
        if calculated:
            col=ET.SubElement(ds,'column',name='[Calc]',caption='Numeric result',datatype='real',role='measure')
            ET.SubElement(col,'calculation',attrib={'class':'tableau','formula':'ABS([Value]) + ZN([Value]) * 2'})
    for ws in root.findall('./worksheets/worksheet'):
        table=ws.find('table'); did=table.find('datasource-dependencies').get('datasource');idx=did[-1]
        for f in table.findall('filter'):table.remove(f)
        table.find('rows').text='';table.find('cols').text=''
        pane=table.find('./panes/pane');pane.find('mark').set('class',mark)
        enc=ET.SubElement(pane,'encodings')
        ET.SubElement(enc,'color',column=f'[{did}].[none:Place{idx}:nk]')
        ET.SubElement(enc,'size',column=f'[{did}].[sum:{"Calc" if calculated else "Value"}:qk]')
        ET.SubElement(enc,'text',column=f'[{did}].[sum:{"Calc" if calculated else "Value"}:qk]')
        ET.SubElement(table,'aggregation',value='true')
    zones=root.find('.//dashboard/zones')
    for z in list(zones):
        if z.get('type')=='filter':zones.remove(z)
    return root


class Session:
    def __init__(self,visual='bar',wrong_calc=False,fail=False):
        self.visual,self.wrong_calc,self.fail=visual,wrong_calc,fail
        self.calls=[]
    def sql(self,sql,params=None):
        if self.fail:raise RuntimeError('Cortex service unavailable')
        self.calls.append((sql,params))
        prompt=json.loads(params[1].split('\n',1)[1]); evidence=prompt['source_evidence']
        if 'formula' in evidence:
            fid=next(f['id'] for f in evidence['fields'] if f['internal']=='Value')
            node=['field',fid]
            expression=['add',['abs',node],['multiply',['zn',node],['number',2]]]
            if self.wrong_calc:expression=['number',0]
            value={'choice':'use_validated_expression','reason':'Row expression',
                   'expression_json':json.dumps(expression)}
        else:value={'choice':self.visual,'reason':'Compare category magnitudes with a supported renderer'}
        self.value=value
        return self
    def collect(self):return [[json.dumps(self.value)]]


class CortexTests(unittest.TestCase):
    def test_replacements_render_actual_numeric_results_across_marks_and_sources(self):
        for mark,target in [('Pie','bar'),('Circle','dot'),('Square','table')]:
            with self.subTest(mark=mark),tempfile.TemporaryDirectory() as folder:
                original=parse(fixture(mark,reverse=True),folder)
                ir=CA.assist(original,Session(target))
                self.assertEqual(ir_contract.validate_ir(ir),[])
                self.assertTrue(all(r['status']=='SUBSTITUTED' for r in ir['ai_assistance']),ir['ai_assistance'])
                self.assertTrue(all(s['state']=='PARTIAL' for s in ir_contract.assess_ir(ir)['sheets']))
                self.assertFalse(any(s.get('ai_adaptation') for s in original['dashboards'][0]['sheets']))
                with preview_context(ir,bindings(folder,[('A',1,2),('B',1,-3),('A',1,4)])):
                    for s in ir['dashboards'][0]['sheets']:
                        ui=TableUI()
                        with patch.object(engine,'st',ui):engine.render_sheet(s,'')
                        self.assertEqual(ui.errors,[])
                        data=ui.tables[0] if target=='table' else ui.charts[0].data
                        n=int(s['datasource_id'][-1])+1
                        self.assertEqual(dict(zip(data.CATEGORY,data.VALUE)),{'A':6*n,'B':-3*n})
                        self.assertTrue(any('AI visual substitution' in c for c in ui.captions))

    def test_cortex_calculation_ast_is_used_only_after_independent_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            original=parse(fixture(calculated=True),folder)
            session=Session();ir=CA.assist(original,session)
            self.assertTrue(all(r['status']=='SUBSTITUTED' for r in ir['ai_assistance']),ir['ai_assistance'])
            self.assertEqual(len(session.calls),4)
            with preview_context(ir,bindings(folder,[('A',1,2),('A',1,4),('B',1,-3)])):
                ui=TableUI()
                with patch.object(engine,'st',ui),patch.object(engine,'q',wraps=engine.q) as q:
                    engine.render_sheet(ir['dashboards'][0]['sheets'][0],'')
                self.assertEqual(ui.errors,[])
                self.assertEqual(dict(zip(ui.charts[0].data.CATEGORY,ui.charts[0].data.VALUE)),{'A':18,'B':-3})
                self.assertIn('COALESCE',q.call_args.args[0])
            bad=CA.assist(original,Session(wrong_calc=True))
            self.assertTrue(all(r['status']=='REFUSED' for r in bad['ai_assistance']))
            with patch.object(engine,'q') as q,patch.object(engine,'st',TableUI()):
                engine.render_sheet(bad['dashboards'][0]['sheets'][0],'');q.assert_not_called()

    def test_unsupported_semantics_do_not_reach_cortex_or_sql(self):
        for defect in ('filter','relationship','encoding','disaggregated','axes','style','table-calc'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as folder:
                root=fixture();table=root.find('./worksheets/worksheet/table')
                if defect=='filter':ET.SubElement(table,'filter',column='[federated.source_0].[none:Place0:nk]')
                if defect=='relationship':ET.SubElement(root.find('./datasources/datasource'),'relationship')
                if defect=='encoding':ET.SubElement(table.find('.//encodings'),'detail',column='[federated.source_0].[none:Place0:nk]')
                if defect=='disaggregated':table.find('aggregation').set('value','false')
                if defect=='axes':table.find('rows').text='[federated.source_0].[none:Place0:nk]'
                if defect=='style':ET.SubElement(table,'style')
                if defect=='table-calc':ET.SubElement(table,'table-calc')
                session=Session();ir=CA.assist(parse(root,folder),session)
                self.assertEqual(ir['ai_assistance'][0]['status'],'REFUSED')
                self.assertEqual(len(session.calls),1)  # only the healthy sibling is eligible
                with patch.object(engine,'q') as q,patch.object(engine,'st',TableUI()):
                    engine.render_sheet(ir['dashboards'][0]['sheets'][0],'');q.assert_not_called()

    def test_total_request_budget_counts_calculation_and_visual_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            original=parse(fixture(reverse=True,calculated=True),folder)
            session=Session()
            ir=CA.assist(original,session,max_calls=1)
            self.assertEqual(len(session.calls),1)
            self.assertTrue(all(r['status']=='REFUSED' for r in ir['ai_assistance']))
            # The earlier keyword remains accepted, but now caps total requests.
            session=Session()
            CA.assist(original,session,max_sheets=1)
            self.assertEqual(len(session.calls),1)

    def test_invalid_choices_and_service_outage_retain_original_refusal(self):
        with tempfile.TemporaryDirectory() as folder:
            original=parse(fixture(),folder)
            for session in (None,Session('execute SQL'),Session('refuse'),Session(fail=True)):
                ir=CA.assist(original,session)
                self.assertTrue(all(r['status']=='REFUSED' for r in ir['ai_assistance']))
                self.assertFalse(any(s.get('ai_adaptation') for s in ir['dashboards'][0]['sheets']))

    def test_changed_source_catalog_or_proposal_never_queries(self):
        for defect in ('source','catalog','renderer','expression'):
            with self.subTest(defect=defect),tempfile.TemporaryDirectory() as folder:
                ir=CA.assist(parse(fixture(),folder),Session());s=ir['dashboards'][0]['sheets'][0]
                self.assertIn('ai_adaptation',s,ir['ai_assistance'])
                if defect=='source':s['worksheet_semantics']['source_xml']+=' '
                if defect=='catalog':ir['fields'][s['ai_adaptation']['contract']['measure_id']]['physical']='Other'
                if defect=='renderer':s['ai_adaptation']['renderer']='arbitrary-python'
                if defect=='expression':s['ai_adaptation']['calculations']={'invented':['number',999]}
                with preview_context(ir,bindings(folder)),patch.object(engine,'q') as q,patch.object(engine,'st',TableUI()) as ui:
                    engine.render_sheet(s,'');q.assert_not_called();self.assertTrue(ui.errors)

    def test_limit_and_null_failures_preserve_healthy_siblings(self):
        with tempfile.TemporaryDirectory() as folder:
            ir=CA.assist(parse(fixture(),folder),Session())
            paths=bindings(folder,[(f'C{i}',1,i) for i in range(205)])
            with open(paths['Unseen source 0'],'a') as file:file.write(',1,999\n')
            with preview_context(ir,paths):
                a,b=ir['dashboards'][0]['sheets'];ui=TableUI()
                with patch.object(engine,'st',ui):engine.render_sheet(a,'')
                self.assertEqual(ui.charts,[]);self.assertTrue(ui.errors)
                ui=TableUI()
                with patch.object(engine,'st',ui):engine.render_sheet(b,'')
                self.assertEqual(len(ui.charts[0].data),200)
                self.assertEqual(ui.charts[0].data.VALUE.tolist(),list(range(408,8,-2)))
                self.assertTrue(any('200 of 205' in e for e in ui.errors))

    def test_numeric_language_refuses_unsafe_or_different_evaluation_levels(self):
        fields={'v':{'id':'v','internal':'Value','datasource_id':'d','datatype':'real','is_calculation':False}}
        field={'id':'c','datasource_id':'d'}
        for formula in ('SUM([Value])','[Value]/2','[foreign].[Value]','{FIXED:SUM([Value])}',
                        'LOOKUP([Value],1)',"__import__('os')",'[Value] + f0','[Value] # ignored','[Value] ** 2'):
            with self.subTest(formula=formula),self.assertRaises((ValueError,SyntaxError)):
                CA.calculation_plan(dict(field,formula=formula),fields)

    def test_transport_uses_bound_parameters_and_rejects_extra_payload(self):
        session=Session();CA.request(session,'model',"task",{'text':"'); DROP TABLE X; --"},['bar'])
        sql,params=session.calls[0]
        self.assertNotIn('DROP TABLE',sql);self.assertIn('DROP TABLE',params[1])
        self.assertIn('response_format',sql)
        response_format=json.loads(params[2])
        self.assertEqual(response_format['type'],'json')
        self.assertFalse(response_format['schema']['additionalProperties'])
        with patch.object(session,'collect',return_value=[[{'choice':'bar','reason':'fine','sql':'DROP'}]]):
            with self.assertRaises(ValueError):CA.request(session,'model','task',{},['bar'])

    def test_shared_intake_applies_ai_automatically_when_session_available(self):
        raw=ET.tostring(fixture())
        with patch('backend._active_session',return_value=Session()),patch('pipeline.onboard') as onboard:
            result=v3_local.prepare_payload(raw,'unfamiliar.twb')
        self.assertTrue(all(r['status']=='SUBSTITUTED' for r in result['ir']['ai_assistance']))
        self.assertTrue(all(s['state']=='PARTIAL' for s in result['assessment']['sheets']))
        onboard.assert_not_called()

    def test_real_streamlit_replacement_component(self):
        from streamlit.testing.v1 import AppTest
        code='''
import sys,tempfile
sys.path.insert(0, ROOT)
sys.path.insert(0, ROOT+'/tests')
from test_v3_cortex_assist import fixture,parse,Session,bindings,CA,engine,preview_context
with tempfile.TemporaryDirectory() as folder:
    ir=CA.assist(parse(fixture(),folder),Session('dot'))
    with preview_context(ir,bindings(folder,[('A',1,2),('A',1,4),('B',1,-3)])):
        engine.render_sheet(ir['dashboards'][0]['sheets'][0],'')
'''.replace('ROOT',repr(str(Path(__file__).resolve().parents[1]).replace('\\','/')))
        app=AppTest.from_string(code).run(timeout=30)
        self.assertFalse(app.exception)
        self.assertTrue((app.get('vega_lite_chart') or app.get('arrow_vega_lite_chart')))
        self.assertTrue(any('AI visual substitution' in c.value for c in app.caption))

    def test_snowflake_preview_accepts_only_explicit_bindings_and_restores_state(self):
        from copy import deepcopy
        import config
        with tempfile.TemporaryDirectory() as folder:
            ir=CA.assist(parse(fixture(),folder),Session())
            previous=deepcopy(config.DATASOURCES)
            # DM6: object() cannot answer the runtime table re-check (tested in test_v3_dm6_drift)
            with patch('backend._active_session',return_value=object()),patch('v3_local._validate_table_once'):
                with preview_context(ir,{'Unseen source 0':'DB.SCHEMA.TABLE_A'},snowflake=True):
                    self.assertEqual(config.DATASOURCES,{'Unseen source 0':{'table':'DB.SCHEMA.TABLE_A','local_file':None}})
                    with patch.object(engine,'q') as q,patch.object(engine,'st',TableUI()):
                        engine.render_sheet(ir['dashboards'][0]['sheets'][1],'');q.assert_not_called()
                self.assertEqual(config.DATASOURCES,previous)
                for mapping in ({'Unknown':'DB.SCHEMA.TABLE_A'},{'Unseen source 0':'DB.SCHEMA.T;DROP'}):
                    with self.assertRaises(ValueError):
                        with preview_context(ir,mapping,snowflake=True):pass
                    self.assertEqual(config.DATASOURCES,previous)


if __name__=='__main__':unittest.main()
