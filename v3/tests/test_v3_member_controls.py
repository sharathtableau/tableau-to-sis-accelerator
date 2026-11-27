import unittest,contextlib
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
import tableau_parser,member_card_plan,detail_record_plan,parameter_actions,worksheet_semantics,engine

class MemberTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.ir=tableau_parser.build_ir('Workbooks/ECommerce_Live_Relationship_Events_Customers_Products.twbx',scoped=True)
  cls.sheets={s['name']:s for d in cls.ir['dashboards'] for s in d['sheets']}
 def test_all_seven_contracts_consume_every_occurrence(self):
  for name in ['Detail','Select Products','Metric Selector','Show','Hide','Product img','Customer Details']:
   s=self.sheets[name];m=s['worksheet_semantics'];p=m['projection']
   self.assertEqual(p['status'],'MAPPED',name);self.assertEqual(set(p['bindings']),{o['id'] for o in m['occurrences']},name)
   self.assertFalse(worksheet_semantics.validate_sheet(s),name)
 def test_renamed_sheets_are_structural(self):
  for name in ['Detail','Product img','Customer Details','Select Products','Metric Selector','Show','Hide']:
   s=deepcopy(self.sheets[name]);s['name']='Unseen worksheet';m=s['worksheet_semantics'];build=detail_record_plan.build if name=='Detail' else member_card_plan.build
   self.assertEqual(build(s,m),m['projection'])
 def test_source_parameter_buttons_are_query_free_and_callable(self):
  for name in ['Metric Selector','Show','Hide']:
   s=self.sheets[name];buttons=[];state={};eng=SimpleNamespace(st=SimpleNamespace(columns=lambda n:[contextlib.nullcontext() for _ in range(n)],container=contextlib.nullcontext,button=lambda label,**kw:buttons.append((label,kw)),session_state=state),q=lambda sql:self.fail('Parameter button queried data'),IR=self.ir,PARAMS=dict(self.ir['params']),PARAM_DOMAIN=self.ir['param_domains'],_param_display_value=engine._param_display_value)
   member_card_plan.render(s,'',eng)
   self.assertEqual(len(buttons),3 if name=='Metric Selector' else 1)
   for label,kw in buttons:
    kw['on_click'](*kw['args']);action=kw['args'][1];self.assertIn(parameter_actions.key(self.ir,action['parameter']['caption']),state)
 def test_invalid_parameter_type_and_domain_are_refused(self):
  action=self.sheets['Metric Selector']['parameter_actions'][0];eng=SimpleNamespace(st=SimpleNamespace(session_state={}),IR=self.ir,PARAM_DOMAIN=self.ir['param_domains'],PARAMS={},_param_display_value=engine._param_display_value)
  for value in ['1',99,True]:
   with self.assertRaises(ValueError):parameter_actions.assign(eng,action,value)
 def test_reset_clears_current_controls_and_retains_intake(self):
  s=self.sheets['Select Products'];action=s['parameter_actions'][0];state={'v3_result':'keep','param::Product::Category Parameter':'changed','Product::filter':'changed'}
  eng=SimpleNamespace(st=SimpleNamespace(session_state=state),IR=self.ir,PARAM_DEFS=self.ir['params'],PARAM_DOMAIN=self.ir['param_domains'],PARAMS=dict(self.ir['params']),_param_display_value=engine._param_display_value)
  parameter_actions.assign(eng,action,'Microsoft Copilot for Office Monthly');parameter_actions.reset(eng)
  self.assertEqual(state,{'v3_result':'keep'});self.assertEqual(eng.PARAMS,self.ir['params'])
 def test_reset_assigns_registered_widgets_and_canonical_filters(self):
  from dashboard_actions import state_key
  d=next(d for d in self.ir['dashboards'] if d['actions']);action=d['actions'][0];base=state_key(self.ir,action)
  state={'v3_result':'keep',base:['stale'],base+'_chart_values':['stale'],'param::Product::Metric':'2','unrelated':'keep'}
  eng=SimpleNamespace(st=SimpleNamespace(session_state=state),IR=self.ir,PARAM_DEFS=self.ir['params'],PARAM_DOMAIN=self.ir['param_domains'],PARAMS=dict(self.ir['params']),_param_display_value=engine._param_display_value)
  parameter_actions.register_default(self.ir,eng.st,base+'_chart_values',[])
  parameter_actions.register_default(self.ir,eng.st,'param::Product::Metric','1')
  parameter_actions.reset(eng)
  self.assertEqual(state[base],[]);self.assertEqual(state[base+'_chart_values'],[])
  self.assertEqual(state['param::Product::Metric'],'1');self.assertEqual(state['v3_result'],'keep');self.assertEqual(state['unrelated'],'keep')
 def test_unmounted_parameter_widget_uses_current_assignment_and_registers_source_default(self):
  state={};ir={'field_scopes':{},'source_file':'renamed-widget-case','dashboards':[]}
  host=SimpleNamespace(selectbox=lambda caption,opts,**kw:opts[kw['index']])
  with patch.object(engine,'IR',ir),patch.object(engine,'PARAMS',{'Switch':2}),patch.object(engine,'PARAM_DEFS',{'Switch':1}),patch.object(engine,'PARAM_DOMAIN',{'Switch':['1','2']}),patch.object(engine.st,'session_state',state):
   self.assertEqual(engine._param_widget('Switch',1,'Renamed',host=host),'2')
   self.assertEqual(state[parameter_actions.defaults_key(ir)]['param::Renamed::Switch'],'1')
 def test_regular_parameter_change_is_shared_across_dashboard_controls(self):
  state={'param::One::Metric':'2','param::Two::Metric':'1','unrelated':'keep'}
  eng=SimpleNamespace(st=SimpleNamespace(session_state=state),IR=self.ir,PARAM_DEFS=self.ir['params'],PARAMS=dict(self.ir['params']))
  for w in ['param::One::Metric','param::Two::Metric']:parameter_actions.register_default(self.ir,eng.st,w,'1')
  parameter_actions.widget_changed(eng,'Metric','param::One::Metric')
  self.assertEqual(state['param::Two::Metric'],'2');self.assertEqual(state['unrelated'],'keep')
  eng.PARAMS=dict(self.ir['params']);parameter_actions.apply(eng);self.assertEqual(eng.PARAMS['Metric'],'2')
 def test_temporal_parameter_callback_keeps_typed_marker(self):
  ir={'field_scopes':{},'source_file':'renamed-date','dashboards':[]};state={'param::Renamed::Date':'2025-10-30'}
  eng=SimpleNamespace(st=SimpleNamespace(session_state=state),IR=ir,PARAM_DEFS={'Date':'#2025-10-31#'},PARAMS={'Date':'#2025-10-31#'})
  parameter_actions.widget_changed(eng,'Date','param::Renamed::Date',True)
  self.assertEqual(state[parameter_actions.key(ir,'Date')],'#2025-10-30#')
 def test_member_extra_encoding_blocks_plan(self):
  s=deepcopy(self.sheets['Product img']);m=s['worksheet_semantics'];o=deepcopy(m['occurrences'][-1]);o['id']='unseen';o['channel']='color';m['occurrences'].append(o)
  self.assertEqual(member_card_plan.build(s,m)['status'],'REQUIRES_PLAN')
 def test_detail_index_requires_complete_addressing(self):
  s=deepcopy(self.sheets['Detail']);m=s['worksheet_semantics'];o=next(o for o in m['occurrences'] if o['level']=='table');o['table_calculation_xml']=['<table-calc ordering-type="Columns"/>']
  self.assertEqual(detail_record_plan.build(s,m)['status'],'REQUIRES_PLAN')

if __name__=='__main__':unittest.main()
