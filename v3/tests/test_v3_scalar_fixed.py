"""Source-binding placeholders must not imply table-calculation grain."""
from pathlib import Path
import sys, tempfile, unittest
import xml.etree.ElementTree as ET
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_v3_field_identity import workbook,parse

class ScalarFixedTests(unittest.TestCase):
    def calc(self,formula,shape='kpi',rename=False):
        with tempfile.TemporaryDirectory() as folder:
            root=workbook(shape,rename=rename)
            # Replace nested aggregate with independently authored LOD shapes.
            for node in root.findall('.//column[@name="[Calculation_2]"]/calculation'):
                node.set('formula',formula)
            ir=parse(root,folder)
            return ir['dashboards'][0]['sheets']

    def test_scalar_fixed_inside_aggregate_is_not_table_calc(self):
        for rename in (False,True):
            for shape in ('kpi','bar'):
                for sheet in self.calc('SUM(IF [Value] <= {FIXED : MAX([Value])} THEN [Value] END)',shape,rename):
                    occ=[o for o in sheet['worksheet_semantics']['occurrences'] if o.get('caption') in ('Result','Result renamed')]
                    self.assertTrue(occ)
                    self.assertTrue(all(o['level']=='aggregate' for o in occ))
                    self.assertEqual(sheet['worksheet_semantics']['projection']['status'],'MAPPED')

    def test_scalar_fixed_row_expression_is_row_level(self):
        for sheet in self.calc('[Value] <= {FIXED : MAX([Value])}'):
            occ=sheet['worksheet_semantics']['occurrences']
            self.assertTrue(all(o['level']=='row' for o in occ))

    def test_true_window_and_partitioned_fixed_still_require_plan(self):
        for formula in ('WINDOW_MAX(SUM([Value]))','{FIXED [Place0] : MAX([Value])}'):
            for sheet in self.calc(formula):
                self.assertEqual(sheet['worksheet_semantics']['projection']['status'],'REQUIRES_PLAN')
                self.assertTrue(any(o['level']=='table' for o in sheet['worksheet_semantics']['occurrences']))



from unittest.mock import patch
import engine, ir_contract
from v3_local import preview_context

class ParameterLabelTests(unittest.TestCase):
    def fixture(self, shape='Shape', encoding='text'):
        root=workbook('kpi')
        ds=ET.SubElement(root.find('./datasources'),'datasource',name='Parameters')
        col=ET.SubElement(ds,'column',name='[Unseen period]',caption='Chosen period',datatype='integer',value='7',attrib={'param-domain-type':'list'})
        members=ET.SubElement(col,'members')
        for v in ('7','30'): ET.SubElement(members,'member',value=v)
        ET.SubElement(ET.SubElement(col,'aliases'),'alias',key='7',value='Last week')
        for ws in root.findall('./worksheets/worksheet'):
            pane=ws.find('.//pane');pane.find('mark').set('class',shape)
            ET.SubElement(pane.find('encodings'),encoding,column='[Parameters].[Unseen period]')
            label=ET.SubElement(ET.SubElement(pane,'customized-label'),'formatted-text')
            ET.SubElement(label,'run').text='Score: '
            ET.SubElement(label,'run').text='<[federated.source_'+str(list(root.findall('./worksheets/worksheet')).index(ws))+'].[usr:Calculation_2:qk]>'
            ET.SubElement(label,'run').text=' compared with '
            ET.SubElement(label,'run').text='<[Parameters].[Unseen period]>'
        return root

    def test_bound_parameter_is_display_only_and_updates(self):
        with tempfile.TemporaryDirectory() as folder:
            ir=parse(self.fixture(),folder)
            self.assertEqual(ir_contract.validate_ir(ir),[])
            bindings={}
            for idx in (0,1):
                p=Path(folder)/f'd{idx}.csv';p.write_text(f'Place{idx},Value\nA,3\nB,7\n');bindings[f'Unseen source {idx}']=str(p)
            for sheet in ir['dashboards'][0]['sheets']:
                model=sheet['worksheet_semantics']
                self.assertEqual(model['projection']['status'],'MAPPED')
                self.assertEqual(model['grain']['panes'][0]['group_by'],[])
                with preview_context(ir,bindings):
                    for value,expected in [('7','Last week'),('30','30'),('<script>','&lt;script&gt;')]:
                        engine.PARAMS['Chosen period']=value
                        output=[]
                        with patch.object(engine.st,'markdown',lambda text,**kwargs:output.append(text)):
                            engine.render_sheet(sheet, [])
                        self.assertTrue(any(expected in v for v in output),output)

    def test_shape_encoding_is_not_silently_dropped(self):
        with tempfile.TemporaryDirectory() as folder:
            ir=parse(self.fixture(encoding='shape'),folder)
            self.assertTrue(all(s['worksheet_semantics']['projection']['status']=='REQUIRES_PLAN' for s in ir['dashboards'][0]['sheets']))

    def test_parameter_identity_tampering_is_invalid(self):
        with tempfile.TemporaryDirectory() as folder:
            ir=parse(self.fixture(),folder)
            for o in ir['dashboards'][0]['sheets'][0]['worksheet_semantics']['occurrences']:
                if o.get('parameter'): o['parameter']['internal']='Different'
            self.assertTrue(ir_contract.validate_ir(ir))



from test_v3_temporal import fixture as calendar_fixture
from test_v3_field_identity import UI

class MaximumBarTests(unittest.TestCase):
    def fixture(self,reverse=False,operator='='):
        root=calendar_fixture('Bar',reverse=reverse)
        for ds in root.findall('./datasources/datasource'):
            col=ET.SubElement(ds,'column',name='[MaximumFlag]',caption='Highlight',role='dimension',datatype='string')
            ET.SubElement(col,'calculation',attrib={'class':'tableau','formula':"IF [Calculation_2] "+operator+" WINDOW_MAX([Calculation_2]) THEN 'Peak' ELSE 'Other' END"})
        for ws in root.findall('./worksheets/worksheet'):
            t=ws.find('table');did=t.find('datasource-dependencies').get('datasource')
            t.find('rows').text=f'[{did}].[usr:Calculation_2:qk]'
            ET.SubElement(ET.SubElement(t.find('./panes/pane'),'encodings'),'color',column=f'[{did}].[usr:MaximumFlag:nk]')
        return root
    def test_maximum_ties_and_renamed_calendar(self):
        for reverse in (False,True):
            with tempfile.TemporaryDirectory() as folder:
                ir=parse(self.fixture(reverse),folder);self.assertEqual(ir_contract.validate_ir(ir),[])
                bindings={}
                for i in (0,1):
                    p=Path(folder)/f'b{i}.csv';p.write_text(f'Event,Place{i},Value\n2024-01-02,A,5\n2024-02-02,A,5\n2024-03-02,A,2\n');bindings[f'Unseen source {i}']=str(p)
                with preview_context(ir,bindings):
                    for sheet in ir['dashboards'][0]['sheets']:
                        self.assertEqual(sheet['worksheet_semantics']['projection']['status'],'MAPPED')
                        ui=UI()
                        with patch.object(engine,'st',ui):engine.render_sheet(sheet,[])
                        self.assertEqual(ui.errors,[]);self.assertEqual(len(ui.charts),1)
                        self.assertEqual(ui.charts[0].data.sort_values('T').FLAG.tolist(),['Peak','Peak','Other'])
    def test_other_window_comparisons_remain_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            ir=parse(self.fixture(operator='>'),folder)
            self.assertTrue(all(s['worksheet_semantics']['projection']['status']=='REQUIRES_PLAN' for s in ir['dashboards'][0]['sheets']))



class FixedExecutionTests(unittest.TestCase):
    def fixture(self):
        root=workbook('kpi')
        for idx,ds in enumerate(root.findall('./datasources/datasource')):
            ds.find('./column[@name="[Calculation_2]"]/calculation').set('formula',f'AVG({{FIXED [Place{idx}] : SUM([Value])}})')
        for idx,ws in enumerate(root.findall('./worksheets/worksheet')):
            ws.find('.//column[@name="[Calculation_2]"]/calculation').set('formula',f'AVG({{FIXED [Place{idx}] : SUM([Value])}})')
            formatted=ET.SubElement(ET.SubElement(ws.find('.//pane'),'customized-label'),'formatted-text')
            ET.SubElement(formatted,'run').text=f'<[federated.source_{idx}].[usr:Calculation_2:qk]>'
        return root
    def test_fixed_precedes_regular_filter_with_uneven_groups(self):
        with tempfile.TemporaryDirectory() as folder:
            ir=parse(self.fixture(),folder);self.assertEqual(ir_contract.validate_ir(ir),[])
            bindings={}
            for idx in (0,1):
                p=Path(folder)/f'l{idx}.csv';p.write_text(f'Place{idx},Value\nA,1\nA,9\nB,3\n');bindings[f'Unseen source {idx}']=str(p)
            with preview_context(ir,bindings):
                for sheet in ir['dashboards'][0]['sheets']:
                    self.assertEqual(sheet['worksheet_semantics']['projection']['renderer'],'kpi-fixed')
                    rows=[];oldq=engine.q
                    def capture(sql):
                        df=oldq(sql);rows.append(df);return df
                    with engine.field_scope(sheet),patch.object(engine,'q',capture),patch.object(engine.st,'markdown',lambda *a,**k:None):engine.r_kpi(sheet,'WHERE VALUE > 5')
                    self.assertEqual(rows[-1].iloc[0,0],10)
    def test_context_filter_remains_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            ir=parse(self.fixture(),folder);s=ir['dashboards'][0]['sheets'][0]
            s['applied_filters']=[{'context':True}]
            from kpi_fixed_plan import build
            self.assertEqual(build(s,s['worksheet_semantics'])['status'],'REQUIRES_PLAN')

if __name__=='__main__':unittest.main()
