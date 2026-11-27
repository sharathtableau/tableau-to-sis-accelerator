"""Axis-free constant shape used solely for authored static tooltip content."""
import re,xml.etree.ElementTree as ET

def build(spec,model):
 if len(model['panes'])!=1 or model['panes'][0]['axes'] or model['panes'][0]['marks']!=['Shape']:return None
 occ=model['occurrences']
 if len(occ)!=1 or occ[0]['channel']!='lod':return None
 o=occ[0];formula=o.get('calculation_source','')
 if not re.fullmatch(r"\s*'(?:[^']|'')*'\s*",formula):return None
 ws=ET.fromstring(model['source_xml']);tips=ws.findall('.//pane/customized-tooltip/formatted-text');errors=[]
 if len(tips)!=1:errors.append('Static decoration needs one authored tooltip')
 runs=[]
 if len(tips)==1:
  for n in tips[0]:
   value=n.text or ''
   if n.tag!='run' or '<[' in value or '<Parameters' in value or len(value)>8000:errors.append('Dynamic tooltip decoration needs another plan')
   runs.append({'text':value,'bold':n.get('bold')=='true','italic':n.get('italic')=='true'})
 if not runs or len(runs)>200:errors.append('Static decoration tooltip exceeds supported bounds')
 if o.get('unresolved') or o.get('datasource_id')!=spec.get('datasource_id') or o.get('table_calculation_xml') or spec.get('applied_filters'):errors.append('Static decoration has unplanned data behavior')
 return {'status':'REQUIRES_PLAN' if errors else 'MAPPED','renderer':'static-tooltip','bindings':{o['id']:'constant-decoration'},'reasons':sorted(set(errors)),'runs':runs,'appearance_adaptation':'Unencoded static shape replaced by an accessible information popover with authored tooltip text'}

def render(spec,engine):
 import html
 model=spec['worksheet_semantics'];plan=build(spec,model)
 if plan!=model['projection'] or plan['status']!='MAPPED':raise ValueError('Static tooltip plan is stale or incomplete')
 content=''
 for run in plan['runs']:
  value=html.escape(run['text']).replace('\ufffd','\n').replace('\n','<br>')
  if run['bold']:value='<strong>'+value+'</strong>'
  if run['italic']:value='<em>'+value+'</em>'
  content+=value
 with engine.st.popover('ⓘ',help='Dashboard information'):
  engine.st.markdown(content,unsafe_allow_html=True)
 engine.findings.record('APPEARANCE',spec['name'],'static-shape-tooltip',plan['appearance_adaptation'])
