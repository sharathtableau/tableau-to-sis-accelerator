import unittest,tempfile,json,contextlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import xml.etree.ElementTree as E
from urllib.parse import quote
from test_v3_field_identity import workbook,parse
import dashboard_actions as A

class ActionTests(unittest.TestCase):
 def fixture(self,renamed=False):
  root=workbook(rename=renamed);dash=root.find('./dashboards/dashboard');name=dash.get('name');ws=root.find('./worksheets/worksheet').get('name')
  actions=E.SubElement(root,'actions');a=E.SubElement(actions,'action',name='fresh-action',caption='Select a place')
  E.SubElement(a,'activation',attrib={'auto-clear':'true','type':'on-select'})
  E.SubElement(a,'source',dashboard=name,type='sheet',worksheet=ws)
  field='[federated.source_0].[Place0]'
  E.SubElement(a,'link',attrib={'delimiter':',','escape':'\\','include-null':'true','multi-select':'true','url-escape':'true','expression':'tsl:'+name+'?'+quote(field,safe='')+'~s0=<'+field+'~na>'})
  c=E.SubElement(a,'command',command='tsc:tsl-filter');E.SubElement(c,'param',name='target',value=name)
  return root
 def test_renamed_actions_preserve_id(self):
  for rename in [False,True]:
   with tempfile.TemporaryDirectory() as f:
    ir=parse(self.fixture(rename),f);a=ir['dashboards'][0]['actions'][0]
    self.assertEqual(a['status'],'READY');self.assertEqual(a['field_id'],'federated.source_0::Place0')
 def test_browser_clear_object(self):
  self.assertEqual(A.selected_values({'selection':{'linked_selection':{}}},'linked_selection','DIM'),[])
 def test_unimplemented_activation_visible(self):
  r=self.fixture();r.find('.//activation').set('type','on-hover')
  with tempfile.TemporaryDirectory() as f:
   a=parse(r,f)['dashboards'][0]['actions'][0]
   # E19: hover runs through the general contract (a selection control), never the chart-selection contract
   self.assertEqual((a['status'],a.get('mode'),a.get('activation')),('READY','control','on-hover'))
 def test_selected_values_and_clear(self):
  self.assertEqual(A.selected_values({'selection':{'s':[{'v':"O'Brien"},{'v':None},{'v':"O'Brien"}]}},'s','v'),["O'Brien",None])
  self.assertEqual(A.selected_values({'selection':{'s':[]}},'s','v'),[])
  for rows in [[{'v':float('inf')}],[{'v':'a\\b'}],[{'wrong':1}],[{'v':{}}]]:
   with self.assertRaises(ValueError):A.selected_values({'selection':{'s':rows}},'s','v')
 def test_predicates_scope_and_literal_escaping(self):
  with tempfile.TemporaryDirectory() as f:
   ir=parse(self.fixture(),f);a=ir['dashboards'][0]['actions'][0];sheet=dict(ir['dashboards'][0]['sheets'][0],name='another target')
   eng=SimpleNamespace(IR=ir,CALCS={},COLMAP={a['caption']:ir['fields'][a['field_id']]['physical']},st=SimpleNamespace(session_state={A.state_key(ir,a):["O'Brien",None]}),rdim=lambda c:'"PLACE"')
   self.assertEqual(A.predicates(sheet,eng),['("PLACE" IN (\'O\'\'Brien\') OR "PLACE" IS NULL)'])
   self.assertEqual(A.predicates(dict(sheet,datasource_id='another source'),eng),[])
   self.assertEqual(A.predicates(dict(sheet,name=a['source']),eng),[])
   eng.st.session_state[A.state_key(ir,a)]=[];self.assertEqual(A.predicates(sheet,eng),[])
 def test_layered_chart_uses_supported_control(self):
  import altair as alt,pandas as pd
  with tempfile.TemporaryDirectory() as f:
   ir=parse(self.fixture(),f);sheet=ir['dashboards'][0]['sheets'][0];a=sheet['dashboard_actions'][0];state={};draws=[];controls=[]
   def draw(chart,**kw):draws.append(kw)
   def multi(label,options,**kw):controls.append((options,kw))
   eng=SimpleNamespace(IR=ir,st=SimpleNamespace(session_state=state,altair_chart=draw,popover=lambda label:contextlib.nullcontext(),multiselect=multi))
   base=alt.Chart(pd.DataFrame({'DIM':['a','b'],'V':[1,2]})).encode(x='DIM:N',y='V:Q')
   A.chart(base.mark_bar()+base.mark_text(),sheet,'DIM',eng)
   self.assertNotIn('on_select',draws[0]);self.assertEqual(controls[0][0],['a','b'])
   kw=controls[0][1];state[kw['key']]=['a','b'];kw['on_change']();self.assertEqual(state[A.state_key(ir,a)],['a','b'])
   state[kw['key']]=[];kw['on_change']();self.assertEqual(state[A.state_key(ir,a)],[])
 def test_widget_callback_records_selection(self):
  import altair as alt,pandas as pd
  with tempfile.TemporaryDirectory() as f:
   ir=parse(self.fixture(),f);sheet=ir['dashboards'][0]['sheets'][0];a=sheet['dashboard_actions'][0];state={};calls=[]
   def draw(chart,**kw):calls.append(kw)
   eng=SimpleNamespace(IR=ir,st=SimpleNamespace(session_state=state,altair_chart=draw))
   A.chart(alt.Chart(pd.DataFrame({'DIM':['a'],'V':[1]})).mark_bar().encode(x='DIM:N',y='V:Q'),sheet,'DIM',eng)
   key=calls[0]['key'];state[key]={'selection':{'linked_selection':[{'DIM':'a'}]}};calls[0]['on_select']()
   self.assertEqual(state[A.state_key(ir,a)],['a'])

class AllFieldsActionTests(unittest.TestCase):
 def fixture(self,datatype='string',token=None):
  root=E.Element('workbook');nodes=E.SubElement(root,'actions');node=E.SubElement(nodes,'action')
  E.SubElement(node,'activation',attrib={'auto-clear':'true','type':'on-select'})
  E.SubElement(node,'source',dashboard='Unseen dashboard',type='sheet',worksheet='Unseen source')
  command=E.SubElement(node,'command',command='tsc:tsl-filter')
  E.SubElement(command,'param',name='special-fields',value='all');E.SubElement(command,'param',name='target',value='Unseen dashboard')
  field={'caption':'Grouping','datasource_id':'ds','datatype':datatype,'physical':'KEY','is_calculation':False}
  occurrence={'id':'group','channel':'rows','level':'row','field_id':'ds::key','datatype':datatype,'token':token or '[ds].[none:key:nk]','datepart':token.split('.[')[-1].split(':')[0] if token else None,'discrete':False}
  sheet={'name':'Unseen source','datasource_id':'ds','worksheet_semantics':{'projection':{'status':'MAPPED'},'occurrences':[occurrence]}}
  dash={'name':'Unseen dashboard','sheets':[sheet]};return root,dash,{'ds::key':field}
 def action(self,root,dash,fields):
  A.extract(root,[dash],fields);return dash['actions'][0]
 def test_renamed_physical_group_ready(self):
  root,dash,fields=self.fixture();action=self.action(root,dash,fields)
  self.assertEqual(action['status'],'READY');self.assertEqual(action['selection_kind'],'all-fields-single-group')
 def test_unmapped_source_and_calculation_refused(self):
  for mutation in ['projection','field']:
   root,dash,fields=self.fixture()
   if mutation=='projection':dash['sheets'][0]['worksheet_semantics']['projection']['status']='REQUIRES_PLAN'
   else:fields['ds::key']['is_calculation']=True
   a=self.action(root,dash,fields)
   if mutation=='projection':      # E19: an unmapped source gets the general contract's selection control
    self.assertEqual((a['status'],a.get('mode')),('READY','control'))
   else:                           # an untranslated calculation is never passed (or dropped)
    self.assertEqual(a['status'],'BLOCKED')
 def test_extra_group_and_unknown_parameter_refused(self):
  root,dash,fields=self.fixture();dash['sheets'][0]['worksheet_semantics']['occurrences'].append({'id':'other','channel':'color','level':'row','field_id':'ds::other'})
  a=self.action(root,dash,fields)
  if 'ds::other' in fields:            # E19: two grouping fields -> the general contract passes both
   self.assertEqual((a['status'],a.get('mode'),len(a.get('fields',[]))),('READY','control',2))
  else:
   self.assertEqual(a['status'],'BLOCKED')
  root,dash,fields=self.fixture();E.SubElement(root.find('.//command'),'param',name='unknown',value='all')
  self.assertEqual(self.action(root,dash,fields)['status'],'BLOCKED')
 def test_only_complete_calendar_day_ready(self):
  # E19: a month truncation runs through the general contract; an exact date-time value is still refused
  for token,expected in [('[ds].[tdy:key:qk]','READY'),('[ds].[tmn:key:qk]','READY'),('[ds].[none:key:nk]','BLOCKED')]:
   root,dash,fields=self.fixture('datetime',token);action=self.action(root,dash,fields)
   self.assertEqual(action['status'],expected)
   if token.startswith('[ds].[tdy'):self.assertEqual(action['datepart'],'day-value')
   if token.startswith('[ds].[tmn'):self.assertEqual(action['fields'][0]['datepart'],['trunc','MONTH'])
 def test_mixed_raw_and_calendar_group_refused(self):
  root,dash,fields=self.fixture('datetime','[ds].[tdy:key:qk]')
  occurrence=dict(dash['sheets'][0]['worksheet_semantics']['occurrences'][0],id='raw-detail',channel='lod',datepart=None)
  dash['sheets'][0]['worksheet_semantics']['occurrences'].append(occurrence)
  self.assertEqual(self.action(root,dash,fields)['status'],'BLOCKED')
 def test_day_literals_single_multi_clear_and_invalid(self):
  root,dash,fields=self.fixture('datetime','[ds].[tdy:key:qk]');action=self.action(root,dash,fields)
  sheet=dict(dash['sheets'][0],name='Unseen target');sheet['dashboard_actions']=dash['actions']
  state={};eng=SimpleNamespace(IR={'source_file':'random.twb','fields':fields},CALCS={},COLMAP={'Grouping':'KEY'},st=SimpleNamespace(session_state=state),rdim=lambda c:'"KEY"')
  key=A.state_key(eng.IR,action)
  for values in [['2025-01-01'],['2025-01-01','2025-01-02']]:
   state[key]=values;clause=A.predicates(sheet,eng)[0];self.assertIn("DATE_TRUNC('day', ",clause)
   for value in values:self.assertIn("DATE '"+value+"'",clause)
  state[key]=[];self.assertEqual(A.predicates(sheet,eng),[])
  for values in [['2025-02-30'],['2025-1-1'],['injected'],[42]]:
   state[key]=values
   with self.assertRaises(ValueError):A.predicates(sheet,eng)
 def test_plain_chart_calendar_control_single_multi_clear(self):
  import altair as alt,pandas as pd
  root,dash,fields=self.fixture('datetime','[ds].[tdy:key:qk]');action=self.action(root,dash,fields);state={};controls=[]
  eng=SimpleNamespace(IR={'source_file':'unseen.twb'},st=SimpleNamespace(session_state=state,altair_chart=lambda *a,**k:None,popover=lambda label:contextlib.nullcontext(),multiselect=lambda label,options,**kw:controls.append((options,kw))))
  chart=alt.Chart(pd.DataFrame({'T':pd.to_datetime(['2025-01-01','2025-01-02']),'V':[1,2]})).mark_bar().encode(x='T:T',y='V:Q')
  A.chart(chart,dash['sheets'][0],'T',eng);options,kw=controls[0];self.assertEqual(options,['2025-01-01','2025-01-02'])
  for values in [options[:1],options,[]]:
   state[kw['key']]=values;kw['on_change']();self.assertEqual(state[A.state_key(eng.IR,action)],values)

if __name__=='__main__':unittest.main()
