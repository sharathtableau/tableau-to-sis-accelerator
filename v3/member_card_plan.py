"""Placeholder axes with physical member text, and authored parameter controls."""
import re,xml.etree.ElementTree as ET
from bounded_overlay import definitions,tree_reasons

def formula(o,defs):
 return re.sub(r'//[^\n]*','',defs.get(o.get('field_definition',{}).get('physical'),'')).strip()

def literal(o,defs):
 value=formula(o,defs)
 if re.fullmatch(r'-?\d+',value):return int(value)
 if re.fullmatch(r'-?\d+\.\d+',value):return float(value)
 if re.fullmatch(r"'(?:[^']|'')*'",value):return value[1:-1].replace("''", "'")
 if value=='""':return ''
 return None

def build(spec,model):
 occ=model['occurrences'];panes=model['panes'];marks=[p['marks'] for p in panes]
 if marks not in ([['Shape'],['Circle'],['Shape']],[['Circle'],['Circle'],['Shape']],[['GanttBar'],['GanttBar'],['GanttBar']],[['Shape']]) and not (spec.get('kind')=='strips' and 3<=len(panes)<=7 and all(p['marks']==['Shape'] for p in panes)):return None
 if spec.get('kind') not in ('strips','circle','bar','table'):return None
 defs=definitions(model,spec['datasource_id'])
 cols=[o for o in occ if o['channel']=='cols'];rows=[o for o in occ if o['channel']=='rows'];roles={};attributes=[];actions=spec.get('parameter_actions',[])
 placeholder=lambda o:bool(re.fullmatch(r'(?:MIN|AVG|MAX)\s*\(\s*-?(?:\d+(?:\.\d+)?)\s*\)',formula(o,defs),re.I))
 member=spec.get('kind') in ('strips','circle') and len(cols)==2 and all(placeholder(o) for o in cols) and marks in ([['Shape'],['Circle'],['Shape']],[['Circle'],['Circle'],['Shape']])
 selector=spec.get('kind')=='bar' and len(cols)==2 and all(placeholder(o) for o in cols) and marks==[['GanttBar'],['GanttBar'],['GanttBar']] and len(rows)==1
 buttons=spec.get('kind')=='strips' and len(cols)>=2 and len(cols)<=6 and all(placeholder(o) for o in cols) and all(p['marks']==['Shape'] for p in panes) and len(panes)==len(cols)+1 and not rows
 toggle=spec.get('kind')=='table' and not cols and not rows and marks==[['Shape']] and bool(actions)
 if not any((member,selector,buttons,toggle)):return None
 errors=tree_reasons(spec,model);family='member-card' if member else 'member-selector' if selector else 'parameter-buttons' if buttons else 'parameter-button'
 ready=[a for a in actions if a['status']=='READY']
 if selector or buttons or toggle:
  if len(ready)!=len(actions) or len(ready)!=(1 if selector or toggle else len(cols)):errors.append('Parameter controls need exact authored assignments')
 for o in occ:
  role=None;f=o.get('field_definition',{})
  if o.get('parameter') and toggle and o['channel']=='lod':role='parameter-state'
  elif o in cols:role='placeholder-column'
  elif o['channel']=='x-axis-name' and any(c['token']==o['token'] for c in cols):role='placeholder-selector'
  elif o['channel'] in ('rows','text','shape') and f.get('is_calculation') is False and o['level']=='row' and not o.get('datepart') and (member or selector):
   role='member-attribute'
   if o['field_id'] not in [x['field_id'] for x in attributes]:attributes.append(o)
  elif o['channel'] in ('lod','text','size') and literal(o,defs) is not None:role='literal-decoration'
  elif o['channel']=='color' and (selector or buttons):
   value=formula(o,defs)
   pattern=r'\[([^\]]+)\]\s*=\s*\[Parameters\]\.\[([^\]]+)\]' if selector else r'\[Parameters\]\.\[([^\]]+)\]\s*=\s*(-?\d+)'
   m=re.fullmatch(pattern,value)
   if m and ready:
    if selector and o.get('field_definition',{}).get('datatype')=='boolean' and m[2]==ready[0]['parameter']['internal'] and attributes and m[1]==attributes[0]['field_definition']['physical']:role='selected-member-color'
    elif buttons and any(a['parameter']['internal']==m[1] and literal(next(x for x in occ if x.get('field_id')==a['field_id']),defs)==int(m[2]) for a in ready):role='selected-parameter-color'
  if role is None:errors.append('Unconsumed member/control occurrence: '+o['channel'])
  else:roles[o['id']]=role
  if o.get('unresolved') or o.get('table_calculation_xml') or (o.get('datasource_id')!=spec['datasource_id'] and not o.get('parameter')):errors.append('Member/control source ownership is incomplete')
 if member or selector:
  if not attributes:errors.append('Member card needs physical attributes')
  if selector and ready and ready[0]['field_id']!=rows[0].get('field_id'):errors.append('Member selector action needs the authored row key')
 elif ready:
  for a in ready:
   matches=[o for o in occ if o.get('field_id')==a['field_id']]
   if not matches or literal(matches[0],defs) is None:errors.append('Parameter button must assign a source literal')
 ws=ET.fromstring(model['source_xml'])
 if any('sort' in n.tag for n in ws.iter()) or ws.findall('.//reference-line') or ws.findall('.//trendline[@enabled="true"]'):errors.append('Member/control ordering or annotations require another contract')
 return {'status':'REQUIRES_PLAN' if errors else 'MAPPED','renderer':'member-card','bindings':roles,'reasons':sorted(set(errors)),
         'family':family,'attributes':[o['id'] for o in attributes],'actions':ready,
         'appearance_adaptation':'Authored member text shown as a readable card/list; custom shape images are not reproduced' if member else 'Authored parameter selection shown as native controls; custom Shape/Gantt decoration adapted'}

def render(spec,where,engine):
 import html
 import parameter_actions
 from visual_plan_common import physical
 model=spec['worksheet_semantics'];plan=build(spec,model)
 if plan!=model['projection'] or plan['status']!='MAPPED':raise ValueError('Member/control plan is stale or incomplete')
 occ={o['id']:o for o in model['occurrences']};defs=definitions(model,spec['datasource_id'])
 if plan['family'] in ('parameter-buttons','parameter-button'):
  callback_context=parameter_actions.context(engine)
  hosts=engine.st.columns(len(plan['actions'])) if len(plan['actions'])>1 else [engine.st.container()]
  for host,action in zip(hosts,plan['actions']):
   o=next(o for o in model['occurrences'] if o.get('field_id')==action['field_id']);value=literal(o,defs)
   label=action['parameter'].get('aliases',{}).get(str(value),str(value))
   with host:
    engine.st.button(label,key='v3_parameter_button_'+action['id'],on_click=parameter_actions.assign,args=(callback_context,action,value),use_container_width=True)
  return
 fields=[occ[oid] for oid in plan['attributes']];selected=', '.join(physical(engine,spec,o)+' AS C'+str(i) for i,o in enumerate(fields))
 df=engine.q('SELECT DISTINCT '+selected+' FROM '+engine.tbl(spec)+' '+where+' ORDER BY '+', '.join(str(i+1) for i in range(len(fields)))+' LIMIT 50001')
 if len(df)>50000:raise ValueError('Member limit exceeded; no silent truncation')
 if df.empty:engine.st.info('No data for current filters');return
 df.columns=[o['caption'] for o in fields]
 if any(o['channel']=='rows' for o in fields) and df.iloc[:,0].duplicated().any():raise ValueError('Member key has conflicting attributes')
 if plan['family']=='member-selector':
  action=plan['actions'][0];caption=fields[0]['caption'];options=df[caption].dropna().tolist()
  if len(options)!=len(set(options)):raise ValueError('Member identity has conflicting attributes')
  current=engine._param_display_value(engine.PARAMS.get(action['parameter']['caption']));widget='v3_member_selector_'+action['id']
  callback_context=parameter_actions.context(engine)
  def changed():
   value=callback_context.st.session_state.get(widget)
   if value is not None:parameter_actions.assign(callback_context,action,value)
  default=engine._param_display_value(engine.PARAM_DEFS.get(action['parameter']['caption']))
  parameter_actions.register_default(engine.IR,engine.st,widget,default)
  if widget in engine.st.session_state and engine.st.session_state[widget] not in options:
   engine.st.session_state[widget]=None
  engine.st.selectbox(caption,options,index=options.index(current) if current in options else None,key=widget,on_change=changed,placeholder='Select a product')
  engine._safe_dataframe(df,use_container_width=True,hide_index=True,height=min(500,70+len(df)*35))
 elif len(df)==1:
  row=df.iloc[0];engine.st.markdown('<div class="v3-member-card" style="padding:16px;border:1px solid #dedede;border-radius:8px;overflow-wrap:anywhere">'+''.join('<div><strong>'+html.escape(str(col))+'</strong>: '+html.escape(str(row[col]))+'</div>' for col in df.columns)+'</div>',unsafe_allow_html=True)
 else:engine._safe_dataframe(df,use_container_width=True,hide_index=True,height=min(500,70+len(df)*35))
 engine.findings.record('APPEARANCE',spec['name'],'member-control-adaptation',plan['appearance_adaptation'])
