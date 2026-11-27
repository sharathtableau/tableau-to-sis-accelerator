"""Exercise real renderer output paths missed by injected-state checks."""
import unittest,json,contextlib
from pathlib import Path
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
import pandas as pd
import engine,logical_scatter,decoration_plan

class HostedCorrectionsTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  import tableau_parser
  cls.ir=tableau_parser.build_ir('Workbooks/ECommerce_Live_Relationship_Events_Customers_Products.twbx',scoped=True)
  cls.sheets={s['name']:s for d in cls.ir['dashboards'] for s in d['sheets']}
 def test_static_info_renders_even_when_non_data(self):
  s=deepcopy(self.sheets['info']);s['name']='Unseen static information';s['non_data']=True
  s['worksheet_semantics']['projection']=decoration_plan.build(s,s['worksheet_semantics'])
  html=[];popovers=[]
  def popover(label,**kw):popovers.append(label);return contextlib.nullcontext()
  with patch.object(engine.st,'popover',popover),patch.object(engine.st,'markdown',lambda m,**kw:html.append(m)),patch.object(engine,'q',side_effect=AssertionError('Decoration must not query data')):
   engine._render_sheet_impl(s,[])
  self.assertEqual(len(popovers),1);self.assertTrue(html);self.assertIn('<strong>',html[0])
 def test_unplanned_non_data_remains_query_free(self):
  with patch.object(engine,'q',side_effect=AssertionError('Blank must not query')),patch.object(engine.st,'popover',side_effect=AssertionError('Blank must not acquire tooltip')):
   engine._render_sheet_impl({'name':'Unseen blank','non_data':True},[])
 def test_every_calendar_source_wires_real_control(self):
  names=['Revenue','Quantity','AOV','Avg. Days to 2nd Purchase']
  self.assertEqual(len(names),4)
  for name in names:
   s=deepcopy(self.sheets[name]);actions=[a for a in s.get('dashboard_actions',[]) if a['status']=='READY' and a['source']==s['name']]
   self.assertEqual(len(actions),1,name)
   controls=[];charts=[];state={}
   def multi(label,options,**kw):controls.append((options,kw))
   data=pd.DataFrame({'T':pd.to_datetime(['2025-01-01','2025-01-02']),'VAL':[10.,20.]})
   with patch.object(engine,'IR',self.ir),patch.object(engine,'tbl',return_value='bound'),patch.object(engine,'px',return_value='"DATE"'),patch.object(engine,'resolve_measure',return_value={'sql':(s['worksheet_semantics']['projection'].get('fixed_measure') or {'sql':'SUM(V)'})['sql'],'fmt':'cur0'}),patch.object(engine,'q',return_value=data),patch.object(engine,'value_labels',return_value=None),patch.object(engine,'cat_colors',return_value=([],[])),patch.object(engine.st,'session_state',state),patch.object(engine.st,'altair_chart',lambda c,**kw:charts.append(c)),patch.object(engine.st,'multiselect',multi),patch('kpi_fixed_plan.compile_sql',return_value='validated query'):
    engine._timeseries_one(s,'',False,bar=True)
   self.assertEqual(len(controls),1,name);self.assertEqual(len(charts),1,name)
   options,kw=controls[0];self.assertEqual(options,['2025-01-01','2025-01-02'])
   import dashboard_actions as actions_module
   for values in [options[:1],options,[]]:
    with patch.object(engine.st,'session_state',state):
     state[kw['key']]=values;kw['on_change']();self.assertEqual(state[actions_module.state_key(self.ir,actions[0])],values)
 def test_scatter_reference_aggregates_have_distinct_axis_ownership(self):
  s=deepcopy(self.sheets['Revenue vs. Customers']);s['name']='Unseen scatter';s['dashboard_actions']=[]
  m=s['worksheet_semantics'];m['projection']=logical_scatter.build(s,m)
  group=next(o for o in m['occurrences'] if o['id']==m['projection']['group'])['field_definition']['physical']
  data=pd.DataFrame({'CATEGORY':['a','b'],'X':[1000.,3000.],'Y':[10.,30.]});charts=[]
  eng=SimpleNamespace(tbl=lambda s:'bound',table_columns=lambda t:[group,m['projection']['count_key']],to_phys=lambda s:s.upper(),resolve_measure=lambda *a:{'sql':'SUM(V)','fmt':'cur0'},q=lambda sql:data,st=SimpleNamespace(altair_chart=lambda c,**kw:charts.append(c)),findings=SimpleNamespace(record=lambda *a:None))
  with patch('kpi_fixed_plan.compile_sql',return_value='validated query'):logical_scatter.render(s,'',eng)
  spec=charts[0].to_dict();aliases=[v['as'] for layer in spec['layer'] for transform in layer.get('transform',[]) for v in transform.get('aggregate',[])]
  self.assertEqual(set(aliases),{'REF_X','REF_Y'})
  import vl_convert as vl
  vega=vl.vegalite_to_vega(spec);aggregates=[t for d in vega['data'] for t in d.get('transform',[]) if t['type']=='aggregate']
  ownership={alias:field for a in aggregates for alias,field in zip(a.get('as',[]),a.get('fields',[]))}
  self.assertEqual(ownership['REF_X'],'X');self.assertEqual(ownership['REF_Y'],'Y')
  self.assertTrue(vl.vegalite_to_png(spec))

if __name__=='__main__':unittest.main()
