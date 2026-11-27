"""Strict reuse of original extract joining without partial-result fallbacks."""
import sys, unittest, tempfile
from pathlib import Path
from unittest.mock import patch
import xml.etree.ElementTree as ET
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from init_workbook import flatten_tables, parse_relationships
import pipeline

def edge(a,b,l='fk',r='id'):
    return {'first':a,'second':b,'lkey':l,'rkey':r}

class RelationshipReuseTests(unittest.TestCase):
    def test_renamed_star_preserves_rows_nulls_and_orphans(self):
        for fact,dim in [('Orders','People'),('UnseenMeasurements','Lookup')]:
            tables={fact:pd.DataFrame({'fk':[1,1,2,9], 'value':[3,5,7,11]}),dim:pd.DataFrame({'id':[1,2],'label':['A','B']})}
            evidence={};got,_=flatten_tables(tables,[edge(fact,dim)],strict=True,evidence=evidence)
            self.assertEqual(len(got),4);self.assertEqual(got['value'].sum(),26)
            self.assertEqual(got['label'].fillna('missing').tolist(),['A','A','B','missing'])
            self.assertEqual(evidence['edges'][0]['unmatched_rows'],1)
            self.assertEqual(evidence['output_rows'],4)
    def test_chain_uses_original_parent_column_tracking(self):
        t={'F':pd.DataFrame({'fk':[1,2],'group_id':[99,98],'amount':[4,9]}),
           'D':pd.DataFrame({'id':[1,2],'group_id':[5,6]}),
           'G':pd.DataFrame({'id':[5,6],'name':['a','b']})}
        got,_=flatten_tables(t,[edge('F','D'),edge('D','G','group_id')],strict=True)
        self.assertEqual(got['name'].tolist(),['a','b']);self.assertEqual(got['amount'].sum(),13)
    def test_duplicate_dimension_refuses_fanout(self):
        # E04: the fan-out is still refused -- the repeating table is left out (never joined), not the whole model
        t={'F':pd.DataFrame({'fk':[1]}),'D':pd.DataFrame({'id':[1,1],'name':['a','a']})}
        ev={};got,_=flatten_tables(t,[edge('F','D')],strict=True,evidence=ev)
        self.assertEqual(len(got),1);self.assertNotIn('name',got.columns)
        self.assertEqual([e['table'] for e in ev['left_out']],['D'])
    def test_null_dimension_refuses_pandas_null_matching(self):
        t={'F':pd.DataFrame({'fk':[None,1]}),'D':pd.DataFrame({'id':[None,1]})}
        with self.assertRaisesRegex(ValueError,'nonnull'):flatten_tables(t,[edge('F','D')],strict=True)
    def test_missing_keys_and_types_refuse(self):
        for dim in [pd.DataFrame({'other':[1]}),pd.DataFrame({'id':['1']})]:
            with self.assertRaises(ValueError):flatten_tables({'F':pd.DataFrame({'fk':[1]}),'D':dim},[edge('F','D')],strict=True)
    def test_disconnected_cycle_and_missing_endpoint_refuse(self):
        t={n:pd.DataFrame({'id':[1],'fk':[1]}) for n in ['F','D','G']}
        for rels in [[],[edge('F','D')],[edge('F','D'),edge('D','G'),edge('G','F')],[edge('F','Outside')]]:
            with self.assertRaises(ValueError):flatten_tables(t,rels,strict=True)
    def test_generated_column_collision_refuses(self):
        t={'F':pd.DataFrame({'fk':[1],'x':[8],'x (D)':[9]}),'D':pd.DataFrame({'id':[1],'x':[2]})}
        with self.assertRaisesRegex(ValueError,'collide'):flatten_tables(t,[edge('F','D')],strict=True)
    def test_single_table_and_empty_inventory(self):
        ev={};got,_=flatten_tables({'T':pd.DataFrame({'x':[1,2]})},[],strict=True,evidence=ev)
        self.assertEqual(ev['shape'],'single');self.assertEqual(len(got),2)
        with self.assertRaises(ValueError):flatten_tables({},[],strict=True)
    def test_strict_parser_rejects_unrepresented_predicates(self):
        xml='<relationship><expression op="="><expression op="[fk]"/><expression op="[id]"/></expression><first-end-point object-id="F"/><second-end-point object-id="D"/></relationship>'
        root=ET.fromstring(xml);self.assertEqual(parse_relationships(root,strict=True),[edge('F','D')])
        for op in ['AND','!=','OR']:
            root=ET.fromstring(xml);root[0].set('op',op)
            with self.assertRaises(ValueError):parse_relationships(root,strict=True)
        root=ET.fromstring(xml);ET.SubElement(root[0][0],'expression',op='[another]')
        with self.assertRaises(ValueError):parse_relationships(root,strict=True)
    def test_decode_failure_cannot_reuse_same_stem_csv(self):
        files={'Data.CSV':'old.csv','other.csv':'keep.csv'};reports=[]
        with patch('init_workbook.hyper_to_csv',side_effect=ValueError('duplicate key')):
            blocked=pipeline.decode_hypers_locally(['data.hyper'],files,'.',strict=True,reports=reports)
        self.assertEqual(files,{'other.csv':'keep.csv'});self.assertEqual(blocked,['data.hyper'])
        self.assertEqual(reports[0]['state'],'BLOCKED');self.assertIn('duplicate key',reports[0]['reason'])
    def test_legacy_default_retained(self):
        got,note=flatten_tables({'F':pd.DataFrame({'a':[1,2]}),'D':pd.DataFrame({'b':[4]})},[])
        self.assertEqual(len(got),2);self.assertIn('largest table',note)

if __name__=='__main__':unittest.main()
