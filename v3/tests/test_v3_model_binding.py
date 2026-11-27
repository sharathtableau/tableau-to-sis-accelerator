"""Independent SQL/cardinality proofs for read-only relationship binding."""
import sys,unittest,json
from pathlib import Path
from copy import deepcopy
import xml.etree.ElementTree as ET
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import duckdb
from v3_model_binding import build,validate,compile_query,replace_bindings,table_name
from v3_source_binding import resolve_sources


def fixture():
    ds=ET.fromstring('''<datasource name="unseen" caption="Unseen model"><connection class="federated"><named-connections><named-connection name="c"><connection class="snowflake" server="org-account.snowflakecomputing.com"/></named-connection></named-connections></connection><object-graph><objects><object id="Fact" caption="Fact"><relation type="table" connection="c" table="[DB].[PUBLIC].[FACT]"/></object><object id="Dim" caption="Dim"><relation type="table" connection="c" table="[DB].[PUBLIC].[DIM]"/></object></objects><relationships><relationship><expression op="="><expression op="[fk]"/><expression op="[key]"/></expression><first-end-point object-id="Fact"/><second-end-point object-id="Dim"/></relationship></relationships></object-graph></datasource>''')
    for local,owner,remote,dtype in [('fk','Fact','FK','string'),('amount','Fact','AMOUNT','real'),('key','Dim','KEY','string'),('label','Dim','LABEL','string')]:
        ET.SubElement(ds,'column',name='['+local+']',datatype=dtype)
        mr=ET.SubElement(ds,'metadata-record',attrib={'class':'column'})
        for tag,value in [('local-name','['+local+']'),('object-id','['+owner+']'),('remote-name',remote),('local-type',dtype)]:ET.SubElement(mr,tag).text=value
    return ds

class Session:
    def __init__(self,dim="('A','Alpha'),('B','Beta')"):
        self.calls=[];self.con=duckdb.connect()
        self.con.execute('CREATE TABLE FACT(FK VARCHAR, AMOUNT DOUBLE)')
        self.con.execute("INSERT INTO FACT VALUES ('A',3),('A',7),('B',11)")
        self.con.execute('CREATE TABLE DIM(KEY VARCHAR, LABEL VARCHAR)')
        self.con.execute('INSERT INTO DIM VALUES '+dim)
    def sql(self,sql):self.calls.append(sql);self.query=sql;return self
    def collect(self):
        if self.query.startswith('SELECT CURRENT'):return [{'ORG':'org','ACCOUNT_NAME':'account'}]
        if self.query.startswith('DESCRIBE'):return [{'NAME':n,'TYPE':t} for n,t in ([('FK','VARCHAR'),('AMOUNT','DOUBLE')] if self.query.endswith('FACT') else [('KEY','VARCHAR'),('LABEL','VARCHAR')])]
        sql=self.query.replace('DB.PUBLIC.FACT','FACT').replace('DB.PUBLIC.DIM','DIM')
        result=self.con.execute(sql);return [dict(zip([c[0] for c in result.description],row)) for row in result.fetchall()]

class ModelTests(unittest.TestCase):
    def test_fact_grain_and_grouped_sum_preserved(self):
        plan=build(fixture());session=Session();report=validate(session,plan)
        self.assertEqual(report['fact_rows'],3);self.assertEqual(report['output_rows'],3)
        values=session.sql('SELECT LABEL,SUM(AMOUNT) AS V FROM ('+compile_query(plan)+') GROUP BY LABEL ORDER BY LABEL').collect()
        self.assertEqual(values,[{'LABEL':'Alpha','V':10},{'LABEL':'Beta','V':11}])
        self.assertTrue(all(not q.upper().startswith(('CREATE','ALTER','DROP','INSERT')) for q in session.calls))
    def test_duplicate_null_and_unmatched_dimension_keys(self):
        # DM8 (Tableau's relationship semantics): a key whose rows differ refuses; a null related key and an
        # unmatched fact key are kept (Null related fields, as Tableau) and counted
        with self.assertRaises(ValueError) as cm:validate(Session("('A','Alpha'),('A','Again'),('B','Beta')"),build(fixture()))
        self.assertIn('KEY_REPEATS_VARYING',str(cm.exception))
        for data,nulls,unmatched in [("(NULL,'Unknown'),('A','Alpha'),('B','Beta')",1,0),("('A','Alpha')",0,1)]:
            with self.subTest(data=data):
                edge=validate(Session(data),build(fixture()))['edges'][0]
                self.assertEqual((edge['null_keys'],edge['unmatched_fact_keys']),(nulls,unmatched))
    def test_key_ownership_and_complex_relationship_refuse(self):
        for mutate in (lambda ds:ds.find('.//expression').set('op','<'),lambda ds:ds.find('.//second-end-point').set('object-id','Missing')):
            ds=fixture();mutate(ds)
            with self.assertRaises(ValueError):build(ds)
    def test_wrong_account_and_injection_refuse(self):
        plan=build(fixture());plan['server']='other-account.snowflakecomputing.com'
        session=Session()
        with self.assertRaises(ValueError):validate(session,plan)
        self.assertEqual(len(session.calls),1)
        plan=build(fixture());plan['tables']['Fact']['table']='DB.PUBLIC.FACT;DROP TABLE X'
        with self.assertRaises(ValueError):compile_query(plan)
    def test_replacement_ignores_literals_and_prefixes(self):
        plan=build(fixture());name=table_name(plan);mapping={'Unseen':{'table':name,'model_plan':plan}}
        query="SELECT 'FROM "+name+"' AS L FROM "+name+' WHERE AMOUNT>0'
        rewritten=replace_bindings(query,mapping)
        self.assertIn("'FROM "+name+"'",rewritten);self.assertIn('FROM (SELECT',rewritten)
        self.assertEqual(replace_bindings('SELECT * FROM '+name+'_OTHER',mapping),'SELECT * FROM '+name+'_OTHER')
    def test_source_binding_uses_exact_structural_model(self):
        root=ET.Element('workbook');ET.SubElement(root,'datasources').append(fixture())
        ir={'field_scopes':{'unseen':{'caption':'Unseen model'}},'fields':{}}
        bound,reports=resolve_sources(root,ir,Session());self.assertIn('Unseen model',bound);self.assertEqual(reports[0]['state'],'BOUND')

if __name__=='__main__':unittest.main()
