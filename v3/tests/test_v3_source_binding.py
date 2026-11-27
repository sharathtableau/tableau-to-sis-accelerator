from copy import deepcopy
from pathlib import Path
import sys, unittest
import xml.etree.ElementTree as ET
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from v3_source_binding import resolve_sources, validate_table


def fixture(alias='Anything', table='DB.SCHEMA.RECORDS'):
    parts='.'.join('['+p+']' for p in table.split('.'))
    root=ET.fromstring(f'''<workbook><datasources><datasource name="ds" caption="{alias}"><connection class="federated"><named-connections><named-connection name="connection-id"><connection class="snowflake" server="org-account.snowflakecomputing.com" dbname="DB" schema="SCHEMA"/></named-connection></named-connections><relation type="table" connection="connection-id" table="{parts}"/></connection></datasource></datasources></workbook>''')
    ir={'field_scopes':{'ds':{'caption':alias}},'fields':{'id':{'datasource_id':'ds','physical':'Amount','datatype':'real'}}}
    return root,ir


class Session:
    def __init__(self): self.calls=[];self.column='Amount';self.type='NUMBER(38,2)'
    def sql(self,sql): self.calls.append(sql);self.current=sql;return self
    def collect(self):
        if self.current.startswith('SELECT CURRENT'): return [{'LOCATOR':'locator','ACCOUNT_NAME':'account','ORG':'org','REGION':'region'}]
        if self.current.startswith('DESCRIBE'): return [{'name':self.column,'type':self.type}]
        return []


class BindingTests(unittest.TestCase):
    def test_alias_and_table_names_do_not_select_behavior(self):
        for alias,table in [('Unseen','DB.SCHEMA.RECORDS'),('Sales','OTHER.PUBLIC.TRANSACTIONS')]:
            root,ir=fixture(alias,table);session=Session()
            bound,reports=resolve_sources(root,ir,session)
            self.assertEqual(bound,{alias:table});self.assertEqual(reports[0]['state'],'BOUND')
            self.assertEqual(session.calls[-1],'SELECT * FROM '+table+' LIMIT 0')
    def test_wrong_account_refuses_before_object_query(self):
        root,ir=fixture();root.find('.//named-connection/connection').set('server','other-account.snowflakecomputing.com')
        session=Session();bound,reports=resolve_sources(root,ir,session)
        self.assertEqual(bound,{});self.assertEqual(len(session.calls),1)
    def test_schema_case_and_type_mismatch_do_not_query_rows(self):
        for column,datatype in [('AMOUNT','NUMBER(38,2)'),('Amount','VARCHAR(10)')]:
            root,ir=fixture();session=Session();session.column=column;session.type=datatype
            bound,_=resolve_sources(root,ir,session);self.assertEqual(bound,{})
            self.assertFalse(any('LIMIT' in sql for sql in session.calls))
    def test_multi_table_join_and_custom_sql_refuse_before_sql(self):
        for kind in ['join','text','table']:
            root,ir=fixture();ET.SubElement(root.find('.//datasource/connection'),'relation',type=kind,connection='connection-id',table='[DB].[SCHEMA].[SECOND]')
            session=Session();self.assertEqual(resolve_sources(root,ir,session)[0],{});self.assertEqual(session.calls,[])
    def test_extract_records_live_upstream_limitation(self):
        root,ir=fixture();ET.SubElement(root.find('.//datasource'),'extract')
        _,reports=resolve_sources(root,ir,Session());self.assertIn('snapshot parity',reports[0]['reason'])
    def test_manual_injection_and_missing_schema_refuse(self):
        session=Session()
        with self.assertRaises(ValueError):validate_table(session,'DB.S.T;DROP TABLE T',[])
        self.assertEqual(session.calls,[])
    def test_duplicate_caption_binding_error_is_isolated(self):
        root,ir=fixture();ir['field_scopes']['ds']['binding_error']='Duplicate caption'
        session=Session();self.assertEqual(resolve_sources(root,ir,session)[0],{});self.assertEqual(session.calls,[])


if __name__=='__main__':unittest.main()
