"""Rank-card source proof and independent numeric checks, without name routing."""
from pathlib import Path
import sys,json,unittest,xml.etree.ElementTree as ET
from copy import deepcopy
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import rank_card_plan

class RankCardTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.ir=json.loads(Path('reports/demo_ecommerce_20261002/ir_after_labels.json').read_text(encoding='utf-8'))
  cls.sheet=next(s for d in cls.ir['dashboards'] for s in d['sheets'] if s['name']=='Revenue by Channels')
 def test_renamed_card_has_identical_source_plan(self):
  s=deepcopy(self.sheet);plan=rank_card_plan.build(s,s['worksheet_semantics']);self.assertEqual(plan['status'],'MAPPED')
  s['name']='Unseen arbitrary caption';s['title']='Renamed';self.assertEqual(rank_card_plan.build(s,s['worksheet_semantics']),plan)
 def test_all_authored_occurrences_are_consumed(self):
  s=self.sheet;plan=rank_card_plan.build(s,s['worksheet_semantics'])
  self.assertEqual(set(plan['bindings']),{o['id'] for o in s['worksheet_semantics']['occurrences']})
 def test_wrong_sign_formula_refused(self):
  s=deepcopy(self.sheet);m=s['worksheet_semantics'];root=ET.fromstring(m['source_xml'])
  shape=next(o for o in m['occurrences'] if o['channel']=='shape')
  root.find(".//datasource-dependencies/column[@name='["+shape['field_definition']['physical']+"]']/calculation").set('formula','ABS([net_revenue_usd])')
  m['source_xml']=ET.tostring(root,encoding='unicode');self.assertEqual(rank_card_plan.build(s,m)['status'],'REQUIRES_PLAN')
 def test_wrong_rank_addressing_refused(self):
  s=deepcopy(self.sheet);m=s['worksheet_semantics'];o=next(o for o in m['occurrences'] if o['channel']=='rows' and o['level']=='table');o['table_calculation_xml']=['<table-calc ordering-type="Rows"/>']
  self.assertEqual(rank_card_plan.build(s,m)['status'],'REQUIRES_PLAN')
 def test_context_phase_refused(self):
  s=deepcopy(self.sheet);s['applied_filters'][0]['context']=True
  self.assertEqual(rank_card_plan.build(s,s['worksheet_semantics'])['status'],'REQUIRES_PLAN')
 def test_live_revenue_card_matches_independent_extract_groups(self):
  import pandas as pd,numpy as np
  folder=Path('reports/demo_ecommerce_20261002');df=pd.read_csv(folder/'packaged/TEMP_0xfnzak033ztnc13wohgv0xghqq0.csv',low_memory=False)
  df.columns=df.columns.str.upper();dates=pd.to_datetime(df.EVENT_DATE);age=(dates.max()-dates).dt.days
  valid=~df.IS_REFUNDED.astype(str).str.lower().isin(['true','1']);current=valid & age.between(0,20);prior=valid & age.between(21,41)
  current_values=df.loc[current].groupby('CHANNEL').NET_REVENUE_USD.sum();prior_values=df.loc[prior].groupby('CHANNEL').NET_REVENUE_USD.sum()
  chosen=set(df.groupby('CHANNEL').ACQUISITION_CHANNEL.count().nlargest(3).index)
  record=next(r for r in json.loads((folder/'live_all_card_execution.json').read_text(encoding='utf-8')) if r['name']=='Revenue by Channels')
  actual=pd.DataFrame(record['data'][-1]['values']).set_index('CATEGORY');self.assertEqual(set(actual.index),chosen)
  for category,row in actual.iterrows():
   self.assertTrue(np.isclose(row.M0,current_values[category]))
   self.assertTrue(np.isclose(row.M1,(current_values[category]-prior_values[category])/prior_values[category]))

class ComposedDualTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  import dual_plan
  cls.module=dual_plan;ir=json.loads(Path('reports/demo_ecommerce_20261002/ir_after_labels.json').read_text(encoding='utf-8'))
  cls.sheet=next(s for d in ir['dashboards'] for s in d['sheets'] if s['name']=='Gross vs. Net')
 def test_source_owned_measure_names_overlay_maps_after_rename(self):
  s=deepcopy(self.sheet);s['name']='Unseen overlay';plan=self.module.build(s,s['worksheet_semantics'])
  self.assertEqual(plan['status'],'READY');self.assertEqual(set(plan['roles']),{o['id'] for o in s['worksheet_semantics']['occurrences']})
 def test_foreign_measure_names_refused(self):
  s=deepcopy(self.sheet);o=next(o for o in s['worksheet_semantics']['occurrences'] if o['channel']=='color');o['token']='[other].[:Measure Names]'
  self.assertEqual(self.module.build(s,s['worksheet_semantics'])['status'],'BLOCKED')
 def test_extra_occurrence_refused(self):
  s=deepcopy(self.sheet);o=deepcopy(s['worksheet_semantics']['occurrences'][0]);o['id']='unseen';o['channel']='size';s['worksheet_semantics']['occurrences'].append(o)
  self.assertEqual(self.module.build(s,s['worksheet_semantics'])['status'],'BLOCKED')
 def test_source_filter_phase_refused(self):
  s=deepcopy(self.sheet);m=s['worksheet_semantics'];root=ET.fromstring(m['datasource_xml']);ET.SubElement(root,'filter');m['datasource_xml']=ET.tostring(root,encoding='unicode')
  self.assertEqual(self.module.build(s,m)['status'],'BLOCKED')

if __name__=='__main__':unittest.main()
