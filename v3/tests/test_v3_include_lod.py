"""AI03.1 independent numeric, privacy, budget and refusal evidence."""
import json
from copy import deepcopy
from pathlib import Path
import sys,tempfile,unittest
import xml.etree.ElementTree as ET
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import duckdb
import include_lod_plan as L
import cortex_assist as CA
from test_v3_cortex_contract import RawSession
from test_v3_field_identity import parse
from ir_contract import assess_ir


def catalog(formula='{INCLUDE [Customer]: SUM([Amount])}',renamed=False):
    names=['Customer','Region','Amount','When','Metric','UNRELATED_PRIVATE_FIELD']
    fields={}
    for name in names:
        fields['ds::'+name]={'id':'ds::'+name,'datasource_id':'ds','internal':name,
                            'physical':name,'caption':'Renamed '+name if renamed else name,
                            'datatype':'real' if name in ('Amount','Metric') else 'date' if name=='When' else 'string',
                            'is_calculation':name=='Metric','formula':formula if name=='Metric' else None}
    return fields


def workbook(renamed=False,independent=False):
    root=ET.fromstring('<workbook><datasources/><worksheets/><dashboards><dashboard name="Plan review"><zones/></dashboard></dashboards></workbook>')
    for i in range(2):
        ds='federated.renamed'+str(i) if renamed else 'federated.orders'+str(i)
        source=ET.SubElement(root.find('datasources'),'datasource',name=ds,caption='Source '+str(i))
        ET.SubElement(ET.SubElement(source,'connection'),'relation',type='table',table='[facts]')
        for name,datatype,role in [('Customer','string','dimension'),('Amount','real','measure'),('When','date','dimension'),('UNRELATED_PRIVATE_FIELD','string','dimension')]:
            ET.SubElement(source,'column',name='['+name+']',caption=('Alias '+name if renamed else name),datatype=datatype,role=role)
        f=ET.SubElement(source,'column',name='[Metric]',caption='New metric' if renamed else 'Metric',datatype='real',role='measure')
        formula='{INCLUDE YEAR([When]), MONTH([When]), DAY([When]): SUM(ZN([Amount])*2)}' if independent else '{INCLUDE [Customer]: SUM([Amount])}'
        ET.SubElement(f,'calculation',attrib={'class':'tableau','formula':formula})
        name=('Renamed ' if renamed else 'Sheet ')+str(i);ws=ET.SubElement(root.find('worksheets'),'worksheet',name=name);table=ET.SubElement(ws,'table')
        ET.SubElement(table,'datasource-dependencies',datasource=ds)
        pane=ET.SubElement(ET.SubElement(table,'panes'),'pane');ET.SubElement(pane,'mark',attrib={'class':'Text'})
        ET.SubElement(ET.SubElement(pane,'encodings'),'text',column='['+ds+'].[avg:Metric:qk]')
        ET.SubElement(table,'rows');ET.SubElement(table,'cols')
        ET.SubElement(root.find('.//zones'),'zone',name=name,x=str(i*50000),y='0',w='50000',h='100000')
    return root


class IncludeTests(unittest.TestCase):
    def test_two_stage_average_preserves_view_grain_and_duplicate_rows(self):
        for renamed in (False,True):
            fields=catalog(renamed=renamed);p=L.build(fields['ds::Metric'],fields,['ds::Region'],'avg')
            db=duckdb.connect()
            db.execute('CREATE TABLE facts(Customer VARCHAR, Region VARCHAR, Amount DOUBLE)')
            db.executemany('INSERT INTO facts VALUES(?,?,?)',[('A','East',1),('A','East',3),('B','East',10),('A','West',5),('C','West',None),(None,'North',2),(None,'North',4)])
            rows=db.execute(L.compile_unfiltered_sql(p,fields,'facts')).fetchall();db.close()
            self.assertEqual(dict(rows),{'East':7.0,'West':5.0,'North':6.0})
            self.assertNotEqual(dict(rows)['East'],14/3)

    def test_date_keys_and_arithmetic_uneven_groups(self):
        fields=catalog('{INCLUDE YEAR([When]), MONTH([When]), DAY([When]): SUM(ZN([Amount])*2)}')
        plan=L.build(fields['ds::Metric'],fields,[],'avg');db=duckdb.connect()
        db.execute('CREATE TABLE facts("When" DATE, Amount DOUBLE)')
        db.execute("INSERT INTO facts VALUES ('2024-01-01',1),('2024-01-01',3),('2025-01-01',10)")
        self.assertEqual(db.execute(L.compile_unfiltered_sql(plan,fields,'facts')).fetchone()[0],14)
        db.close()

    def test_overlap_empty_and_all_null(self):
        fields=catalog();db=duckdb.connect();db.execute('CREATE TABLE facts(Customer VARCHAR, Amount DOUBLE)')
        p=L.build(fields['ds::Metric'],fields,[],'avg')
        self.assertEqual(db.execute(L.compile_unfiltered_sql(p,fields,'facts')).fetchall(),[(None,)])
        db.execute("INSERT INTO facts VALUES ('A',NULL),('A',NULL),('B',2),('B',4)")
        self.assertEqual(db.execute(L.compile_unfiltered_sql(p,fields,'facts')).fetchone()[0],6)
        p=L.build(fields['ds::Metric'],fields,['ds::Customer'],'sum')
        self.assertEqual(dict(db.execute(L.compile_unfiltered_sql(p,fields,'facts')).fetchall()),{'A':None,'B':6})
        db.close()

    def test_unsupported_dependencies_and_syntax_refuse(self):
        for formula in ['{FIXED [Customer]:SUM([Amount])}','{INCLUDE [Customer]:[Other]}','{INCLUDE [Customer]:SUM([Metric])}',
                        '{INCLUDE [Customer]:SUM([Amount])/2}','{INCLUDE [Customer],:SUM([Amount])}',
                        '{INCLUDE [Customer],[Customer]:SUM([Amount])}','{INCLUDE DAY([Customer]):SUM([Amount])}',
                        '{INCLUDE [Customer]:SUM([other].[Amount])}','{INCLUDE [Customer]:SUM(LOOKUP([Amount],-1))}']:
            with self.subTest(formula=formula):
                f=catalog(formula)
                with self.assertRaises((ValueError,SyntaxError)):L.build(f['ds::Metric'],f,[],'avg')

    def test_stale_plan_and_injection_never_compile(self):
        f=catalog();p=L.build(f['ds::Metric'],f,[],'avg')
        for field,value in [('outer_aggregation','sum'),('dependency_hash','forged'),('execution','ENABLED')]:
            changed=deepcopy(p);changed[field]=value
            with self.assertRaises(ValueError):L.compile_unfiltered_sql(changed,f,'facts')
        with self.assertRaises(ValueError):L.compile_unfiltered_sql(p,f,'facts; DROP TABLE facts')
        f['ds::Amount']['physical']='Changed'
        with self.assertRaises(ValueError):L.validate(p,f)

    def test_cortex_validated_plan_privacy_and_mismatch(self):
        f=catalog();p=L.build(f['ds::Metric'],f,['ds::Region'],'avg')
        payload={'choice':'use_validated_include_plan','reason':'proposal','expression_json':json.dumps(p['expression'])}
        session=RawSession(response=payload);context=CA.ProposalContext(1)
        self.assertEqual(L.propose(p,f,session,CA.DEFAULT_MODEL,context),p)
        self.assertNotIn('UNRELATED_PRIVATE_FIELD',session.calls[0][1][1])
        self.assertNotIn('physical',session.calls[0][1][1]);self.assertEqual(context.attempts,1)
        wrong=deepcopy(payload);wrong['expression_json']=json.dumps(['include',[], 'sum',['number',100],[],'avg'])
        with self.assertRaisesRegex(ValueError,'independent'):L.propose(p,f,RawSession(response=wrong),CA.DEFAULT_MODEL,CA.ProposalContext(1))

    def test_intake_uses_shared_budget_and_never_changes_acceptance(self):
        for renamed in (False,True):
            for independent in (False,True):
                with self.subTest(renamed=renamed,independent=independent),tempfile.TemporaryDirectory() as folder:
                    ir=parse(workbook(renamed,independent),folder);before=deepcopy(ir);first=ir['dashboards'][0]['sheets'][0]
                    fid=first['worksheet_semantics']['occurrences'][0]['field_id'];p=L.build(ir['fields'][fid],ir['fields'],[],'avg')
                    session=RawSession(response={'choice':'use_validated_include_plan','reason':'proposal','expression_json':json.dumps(p['expression'])})
                    result=CA.assist(ir,session,max_calls=1)
                    self.assertEqual(ir,before);self.assertEqual(result['dashboards'],ir['dashboards'])
                    self.assertEqual(assess_ir(result),assess_ir(ir))
                    self.assertEqual([x['status'] for x in result['ai_include_plans']],['PLAN_VALIDATED','REFUSED'])
                    self.assertEqual(result['ai_include_plans'][1]['error_code'],'CALL_LIMIT')
                    self.assertEqual(result['ai_usage']['attempted_calls'],1)
                    self.assertNotIn('UNRELATED_PRIVATE_FIELD',session.calls[0][1][1])
                    unavailable=CA.assist(ir,None,max_calls=1)
                    self.assertTrue(all(x['status']=='REFUSED' for x in unavailable['ai_include_plans']))


if __name__=='__main__':unittest.main()
