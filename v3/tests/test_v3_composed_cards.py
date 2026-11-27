from pathlib import Path
import json,xml.etree.ElementTree as ET,unittest,sys
from copy import deepcopy
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import logical_scatter,decoration_plan,composed_pie

class ComposedCardTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.ir=json.loads(Path('reports/demo_ecommerce_20261002/ir_after_labels.json').read_text(encoding='utf-8'))
  cls.sheets={s['name']:s for d in cls.ir['dashboards'] for s in d['sheets']}
 def test_scatter_owns_dimension_key_after_rename(self):
  s=deepcopy(self.sheets['Revenue vs. Customers']);s['name']='Unknown scatter'
  p=logical_scatter.build(s,s['worksheet_semantics']);self.assertEqual(p['status'],'MAPPED');self.assertEqual(p['count_key'],'customer_id (Customers)')
  self.assertEqual(set(p['bindings']),{o['id'] for o in s['worksheet_semantics']['occurrences']})
 def test_scatter_missing_key_metadata_refused(self):
  s=deepcopy(self.sheets['Revenue vs. Customers']);m=s['worksheet_semantics'];root=ET.fromstring(m['datasource_xml'])
  for n in root.findall('.//metadata-record/object-id'):
   if 'Customers_' in (n.text or ''):n.text='[Foreign owner]'
  m['datasource_xml']=ET.tostring(root,encoding='unicode');self.assertEqual(logical_scatter.build(s,m)['status'],'REQUIRES_PLAN')
 def test_scatter_additional_encoding_refused(self):
  s=deepcopy(self.sheets['Revenue vs. Customers']);o=deepcopy(s['worksheet_semantics']['occurrences'][0]);o['id']='unknown';o['channel']='color';s['worksheet_semantics']['occurrences'].append(o)
  self.assertEqual(logical_scatter.build(s,s['worksheet_semantics'])['status'],'REQUIRES_PLAN')
 def test_scatter_numeric_oracle_passes_every_mark(self):
  p=json.loads(Path('reports/demo_ecommerce_20261002/logical_grain/scatter_numeric_oracle.json').read_text());self.assertGreater(p['rows'],50);self.assertTrue(all(p['checks'].values()))
 def test_static_tooltip_has_no_numeric_roles(self):
  s=deepcopy(self.sheets['info']);s['name']='Unknown tooltip';p=decoration_plan.build(s,s['worksheet_semantics']);self.assertEqual(p['status'],'MAPPED');self.assertEqual(set(p['bindings'].values()),{'constant-decoration'})
 def test_dynamic_tooltip_refused(self):
  s=deepcopy(self.sheets['info']);m=s['worksheet_semantics'];root=ET.fromstring(m['source_xml']);root.find('.//customized-tooltip/formatted-text/run').text='<[source].[Value]>';m['source_xml']=ET.tostring(root,encoding='unicode');self.assertEqual(decoration_plan.build(s,m)['status'],'REQUIRES_PLAN')
 def test_donut_reuses_exact_segment_and_total(self):
  s=deepcopy(self.sheets['Order vs. Invoice']);s['name']='Unknown composed donut';p=composed_pie.build(s,s['worksheet_semantics']);self.assertEqual(p['status'],'MAPPED');self.assertEqual(p['order'],['order','invoice']);self.assertEqual(set(p['bindings']),{o['id'] for o in s['worksheet_semantics']['occurrences']})
 def test_wrong_center_measure_refused(self):
  s=deepcopy(self.sheets['Order vs. Invoice']);o=next(o for o in s['worksheet_semantics']['occurrences'] if o['channel']=='text' and o['caption']=='Revenue');o['field_id']='foreign::value';self.assertEqual(composed_pie.build(s,s['worksheet_semantics'])['status'],'REQUIRES_PLAN')
 def test_responsive_rank_rows_preserve_formats_and_changes(self):
  data=json.loads(Path('reports/demo_ecommerce_20261002/logical_grain/render_execution.json').read_text(encoding='utf-8'));s=next(s for s in data if s['name']=='Revenue by Channels');html=s['html'][-1];self.assertIn('$572,134',html);self.assertIn('4.1%',html);self.assertIn('v3-rank-card',html);self.assertIn('rank-change',html)
 def test_parameter_alias_does_not_change_raw_value(self):
  import engine
  from unittest.mock import patch
  class Host:
   def selectbox(self,caption,options,**kwargs):
    self.label=kwargs['format_func']('7');return '7'
  host=Host()
  with patch.object(engine,'IR',self.ir),patch.object(engine,'PARAM_DOMAIN',{'Select Date':['7','21']}),patch.object(engine,'PARAMS',{}):
   result=engine._param_widget('Select Date','21','unseen',host);self.assertEqual(result,'7');self.assertEqual(engine.PARAMS['Select Date'],'7');self.assertEqual(host.label,'Last 7 Days')

class PercentSummaryTests(unittest.TestCase):
 def fixture(self):
  import percent_summary
  ir=json.loads(Path('reports/demo_ecommerce_20261002/ir_after_labels.json').read_text());sheet=deepcopy(next(s for s in ir['dashboards'][0]['sheets'] if s['name']=='Order vs. Invoice (2)'));sheet['name']='Unseen summary';sheet['worksheet_semantics']['projection']=percent_summary.build(sheet,sheet['worksheet_semantics']);return ir,sheet
 def test_owned_measure_and_all_occurrences(self):
  ir,s=self.fixture();plan=s['worksheet_semantics']['projection'];self.assertEqual(plan['status'],'MAPPED');self.assertEqual(set(plan['bindings']),{o['id'] for o in s['worksheet_semantics']['occurrences']})
 def test_restart_and_orientation_refused(self):
  import percent_summary
  for attributes in [{'ordering-type':'Rows','type':'PctTotal'},{'ordering-type':'Columns','type':'PctTotal','restart-every':'1'}]:
   ir,s=self.fixture();m=s['worksheet_semantics'];root=ET.fromstring(m['source_xml']);root.find('.//table-calc').attrib=attributes;m['source_xml']=ET.tostring(root,encoding='unicode')
   pct=next(o for o in m['occurrences'] if o.get('table_calculation_xml'));pct['table_calculation_xml']=[ET.tostring(root.find('.//table-calc'),encoding='unicode')]
   self.assertEqual(percent_summary.build(s,m)['status'],'REQUIRES_PLAN')
 def test_foreign_measure_and_totals_refused(self):
  import percent_summary
  ir,s=self.fixture();m=s['worksheet_semantics'];next(o for o in m['occurrences'] if o.get('table_calculation_xml'))['field_id']='foreign::measure';self.assertEqual(percent_summary.build(s,m)['status'],'REQUIRES_PLAN')
  ir,s=self.fixture();m=s['worksheet_semantics'];root=ET.fromstring(m['source_xml']);ET.SubElement(root.find('table'),'grand-total');m['source_xml']=ET.tostring(root,encoding='unicode');self.assertEqual(percent_summary.build(s,m)['status'],'REQUIRES_PLAN')
 def test_uneven_and_zero_totals_render(self):
  import percent_summary,pandas as pd,engine
  from types import SimpleNamespace
  from unittest.mock import patch
  for values,expected in [([20.,80.],['20.0%','80.0%']),([0.,0.],['\u2014'])]:
   ir,s=self.fixture();html=[]
   eng=SimpleNamespace(IR=ir,tbl=lambda s:'bound',table_columns=lambda t:['event_type'],to_phys=lambda s:s.upper(),resolve_measure=lambda *a:{'sql':'SUM(V)','fmt':'cur0'},q=lambda sql:pd.DataFrame({'CATEGORY':['a','b'],'VALUE':values}),cat_colors=lambda *a:([],[]),fmt_tableau=engine.fmt_tableau,st=SimpleNamespace(markdown=lambda text,**kw:html.append(text)),findings=SimpleNamespace(record=lambda *a:None))
   with patch('kpi_fixed_plan.compile_sql',return_value='validated query'):percent_summary.render(s,'',eng)
   for expected_text in expected:self.assertIn(expected_text,html[0])
 def test_live_summary_matches_independent_segment_totals(self):
  import pandas as pd,numpy as np
  records=json.loads(Path('reports/demo_ecommerce_20261002/logical_grain/live_all_execution.json').read_text());rec=next(r for r in records if r['name']=='Order vs. Invoice (2)');actual=pd.DataFrame(rec['data'][-1]['values']).set_index('CATEGORY').VALUE.sort_index()
  df=pd.read_csv('reports/demo_ecommerce_20261002/packaged/TEMP_0xfnzak033ztnc13wohgv0xghqq0.csv',low_memory=False);dates=pd.to_datetime(df.event_date).dt.normalize();mask=~df.is_refunded.astype(str).str.lower().isin(['true','1'])&(dates.max()-dates).dt.days.between(0,20);expected=df[mask].groupby('event_type').net_revenue_usd.sum().sort_index()
  self.assertEqual(actual.index.tolist(),expected.index.tolist());self.assertTrue(np.allclose(actual.values,expected.values));self.assertIn('68.9%',rec['html'][-1]);self.assertIn('31.1%',rec['html'][-1])

if __name__=='__main__':unittest.main()
