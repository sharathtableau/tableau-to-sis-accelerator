"""Typed authored Tableau parameter assignments; no executable action strings."""
import re,json,hashlib
import xml.etree.ElementTree as ET

def context(engine):
 """Freeze callback identity before preview_context restores process globals."""
 from types import SimpleNamespace
 return SimpleNamespace(st=engine.st,IR=engine.IR,PARAMS=dict(engine.PARAMS),
                        PARAM_DEFS=dict(getattr(engine,'PARAM_DEFS',engine.IR.get('params',{}))),
                        PARAM_DOMAIN=dict(engine.PARAM_DOMAIN),_param_display_value=engine._param_display_value)

def extract(root, dashboard, sheet, fields, parameters):
 result=[]
 for node in root.findall('./actions/edit-parameter-action'):
  source=node.find('source')
  if source is None or source.get('dashboard')!=dashboard or source.get('worksheet')!=sheet['name']:continue
  record={'id':hashlib.sha256(ET.tostring(node)).hexdigest()[:24],'status':'BLOCKED','caption':node.get('caption')}
  try:
   if source.get('type')!='sheet' or node.find('activation') is None or node.find('activation').attrib!={'type':'on-select'} or node.find('agg-type') is None or node.find('agg-type').attrib!={'type':'attr'} or node.find('clear-option') is None or node.find('clear-option').get('type')!='do-nothing':raise ValueError('Parameter action activation/clear requires another contract')
   params={p.get('name'):p.get('value') for p in node.findall('./params/param')}
   if set(params)!={'source-field','target-parameter'}:raise ValueError('Parameter action arguments are incomplete')
   target=re.fullmatch(r'\[Parameters\]\.\[([^\]]+)\]',params['target-parameter'])
   if not target or target[1] not in parameters:raise ValueError('Parameter target is undeclared')
   occurrences=[o for o in sheet['worksheet_semantics']['occurrences'] if o['token']==params['source-field'] and o.get('field_id') and o['level']=='row' and o['aggregation']=='none']
   if not occurrences:raise ValueError('Parameter source must be a declared row occurrence')
   sourcefield=fields.get(occurrences[0]['field_id']);definition=parameters[target[1]]
   if not sourcefield or sourcefield.get('datasource_id')!=sheet['datasource_id'] or sourcefield.get('datatype')!=definition['datatype']:raise ValueError('Parameter action type or source ownership differs')
   record.update(status='READY',field_id=occurrences[0]['field_id'],parameter=definition)
  except ValueError as exc:record['reason']=str(exc)
  result.append(record)
 return result

def key(ir,caption):
 return 'v3_parameter_action_'+hashlib.sha256(json.dumps([ir.get('source_file'),caption]).encode()).hexdigest()

def assign(engine,action,value):
 p=action['parameter'];dtype=p['datatype'];caption=p['caption']
 if dtype=='integer':
  if type(value) is not int:raise ValueError('Integer parameter requires an integer source value')
 elif dtype=='string':
  if not isinstance(value,str) or len(value)>1024 or any(ord(c)<32 for c in value):raise ValueError('Invalid parameter source text')
 else:raise ValueError('Parameter assignment type requires another contract')
 domain=engine.PARAM_DOMAIN.get(caption,[])
 if domain and str(value) not in {str(engine._param_display_value(v)) for v in domain}:raise ValueError('Parameter source value is outside its declared domain')
 engine.st.session_state[key(engine.IR,caption)]=value
 # Synchronize existing authored controls in the callback before rerender.
 for widget in list(engine.st.session_state):
  if isinstance(widget,str) and widget.startswith('param::') and widget.endswith('::'+caption):engine.st.session_state[widget]=str(value)
 engine.PARAMS[caption]=value

def widget_changed(engine,caption,widget,temporal=False):
 if caption not in engine.PARAM_DEFS:raise ValueError('Undeclared parameter control')
 value=engine.st.session_state[widget]
 stored=f"#{value}#" if temporal else value
 engine.st.session_state[key(engine.IR,caption)]=stored
 # Tableau parameters are workbook-wide, including controls in other views.
 defaults=engine.st.session_state.get(defaults_key(engine.IR),{})
 for other in defaults:
  if other!=widget and other in engine.st.session_state and other.startswith('param::') and other.endswith('::'+caption):
   engine.st.session_state[other]=value
 engine.PARAMS[caption]=stored

def apply(engine):
 for caption in engine.PARAMS:
  k=key(engine.IR,caption)
  if k in engine.st.session_state:engine.PARAMS[caption]=engine.st.session_state[k]

def defaults_key(ir):
 return 'v3_control_defaults_'+hashlib.sha256(str(ir.get('source_file')).encode()).hexdigest()

def register_default(ir,host,widget,value):
 if 'field_scopes' not in ir:return
 # Explicit source defaults let callbacks reset the browser-visible widget,
 # rather than deleting a key that Streamlit can restore from frontend state.
 state=getattr(host,'session_state',None)
 if not callable(getattr(state,'setdefault',None)):return
 state.setdefault(defaults_key(ir),{})[widget]=value

def reset(engine):
 from dashboard_actions import state_key
 keys={key(engine.IR,caption) for caption in engine.PARAM_DEFS}
 defaults=dict(engine.st.session_state.get(defaults_key(engine.IR),{}))
 dashboards=engine.IR.get('dashboards',[])
 for dash in dashboards:
  for action in dash.get('actions',[]):
   base=state_key(engine.IR,action);keys.update([base,base+'_chart',base+'_chart_values'])
  for sheet in dash.get('sheets',[]):
   for action in sheet.get('parameter_actions',[]):keys.add('v3_member_selector_'+action['id'])
 for widget in list(engine.st.session_state):
  param=isinstance(widget,str) and widget.startswith('param::') and any(widget.endswith('::'+c) for c in engine.PARAM_DEFS)
  filt=isinstance(widget,str) and any(widget.startswith(d['name']+'::') for d in dashboards)
  if widget in keys or param or filt:engine.st.session_state.pop(widget,None)
 for widget,value in defaults.items():engine.st.session_state[widget]=value
 for dash in dashboards:
  for action in dash.get('actions',[]):
   base=state_key(engine.IR,action)
   if base+'_chart_values' in defaults:engine.st.session_state[base]=[]
 engine.PARAMS=dict(engine.PARAM_DEFS)
