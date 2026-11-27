"""Logical dimension counts on an explicitly repeated scatter axis."""
import re,xml.etree.ElementTree as ET
from calc_translator import to_phys

def build(spec,model):
 occ=model['occurrences'];xs=[o for o in occ if o['channel']=='cols'];ys=[o for o in occ if o['channel']=='rows']
 if spec.get('kind')!='scatter' or len(model['panes'])!=3 or len(ys)!=2 or ys[0]['token']!=ys[1]['token']:return None
 match=re.fullmatch(r'\['+re.escape(spec.get('datasource_id',''))+r'\]\.\[__tableau_internal_object_id__\]\.\[cnt:([^\]]+):qk\]',ys[0]['token'])
 if not match:return None
 from visual_plan_common import reasons
 from semantic_layer import _objects,join_plan
 from init_workbook import parse_relationships
 errors=reasons(spec,model);roles={};source=ET.fromstring(model['datasource_xml']);ws=ET.fromstring(model['source_xml']);owner=match.group(1);key=None
 try:
  objects=_objects(source);graph=join_plan(objects,parse_relationships(source,strict=True))
  if len(graph['steps'])!=len(objects)-1 or owner not in objects or owner==graph['root']:raise ValueError('Not a single dimension grain')
  incoming=[s for s in graph['steps'] if s['rel']['second']==owner]
  if len(incoming)!=1:raise ValueError('Dimension needs one unique join key')
  declared=[]
  for rel in source.findall('.//relationship'):
   expr=rel.find('expression')
   if expr is None or expr.get('op')!='=' or len(expr)!=2:raise ValueError('Unplanned relationship key expression')
   for i,endpoint in enumerate(('first-end-point','second-end-point')):
    if rel.find(endpoint).get('object-id')==owner:declared.append(expr[i].get('op','').strip('[]'))
  if len(declared)!=1:raise ValueError('Dimension key declaration is ambiguous')
  keys={(mr.findtext('local-name') or '').strip('[]') for mr in source.findall('.//metadata-record') if mr.get('class')=='column' and __import__('init_workbook').metadata_object_id(mr)==owner and (mr.findtext('local-name') or '').strip('[]')==declared[0]}
  if len(keys)!=1:raise ValueError('Dimension key ownership is incomplete')
  key=keys.pop()
  if source.findall('.//filter') or any(r.get('context') for r in spec.get('applied_filters',[])):raise ValueError('Filter phase requires another contract')
 except Exception:errors.append('Logical scatter needs a complete fact tree and unique dimension key ownership')
 else:errors=[r for r in errors if r not in {'Joins, relationships, custom SQL and source filters require a data-model plan','Multiple physical source identities require a data-model plan'}]
 lod=[o for o in occ if o['channel']=='lod'];group=None
 if len(lod)!=3 or len({o.get('field_id') for o in lod})!=1 or any(o['level']!='row' or o['field_definition'].get('is_calculation') is not False for o in lod):errors.append('Logical scatter needs one shared physical mark grain')
 else:group=lod[0]['id']
 if len(xs)!=1 or xs[0]['level']!='aggregate' or xs[0]['aggregation'] not in ('sum','avg','min','max','cnt','ctd') or xs[0].get('unresolved'):errors.append('Logical scatter x axis requires one scalar aggregate')
 if [p['marks'] for p in model['panes']]!=[['Circle'],['Circle'],['Shape']]:errors.append('Logical scatter has unplanned marks')
 owners=[p for p in model['panes'] if p['axes']=={'y-axis-name':ys[0]['token']}]
 if len(owners)!=2 or not any(p['attributes'].get('y-index')=='1' for p in owners):errors.append('Logical scatter requires explicit duplicate-axis overlay')
 expression=ws.findtext('./table/rows','')
 expression=expression.replace(ys[0]['token'],'F')
 if re.sub(r'[\s()]','',expression)!='F+F':errors.append('Logical scatter shelf is not a duplicate overlay')
 for o in occ:
  if o in xs:role='scalar-x'
  elif o in ys:role='logical-count'
  elif o in lod:role='mark-grain'
  elif o['channel']=='y-axis-name' and o['token']==ys[0]['token']:role='logical-count-selector'
  else:errors.append('Unconsumed logical scatter occurrence');continue
  roles[o['id']]=role
  if o.get('table_calculation_xml') or (role not in ('logical-count','logical-count-selector') and o.get('datasource_id')!=spec.get('datasource_id')):errors.append('Unplanned logical scatter source or calculation')
 references=[]
 for line in ws.findall('.//reference-line'):
  axis=line.get('axis-column');slot='X' if len(xs)==1 and axis==xs[0]['token'] else 'Y' if axis==ys[0]['token'] else None
  if not slot or line.get('value-column')!=axis or line.get('formula')!='average' or line.get('scope')!='per-table' or len(line):errors.append('Logical scatter annotation needs another plan')
  else:references.append(slot)
 if len(references)!=len(set(references)) or ws.findall('.//trendline[@enabled="true"]'):errors.append('Logical scatter annotation needs another plan')
 return {'status':'REQUIRES_PLAN' if errors else 'MAPPED','renderer':'logical-scatter','bindings':roles,'reasons':sorted(set(errors)),'x':xs[0]['id'] if len(xs)==1 else None,'group':group,'count_owner':owner,'count_key':key,'count_aggregation':'COUNT_DISTINCT_DIMENSION_KEY','references':references,'appearance_adaptation':'Duplicate count axes represented as one circle layer; unencoded shape overlay requires visual acceptance'}

def render(spec,where,engine):
 import altair as alt
 from visual_plan_common import physical,finite
 model=spec['worksheet_semantics'];plan=build(spec,model)
 if plan!=model['projection'] or plan['status']!='MAPPED':raise ValueError('Logical scatter plan is stale or incomplete')
 occ={o['id']:o for o in model['occurrences']};g=occ[plan['group']];x=occ[plan['x']]
 group=physical(engine,spec,g);key=physical(engine,spec,{'field_definition':{'physical':plan['count_key']}})
 measure=engine.resolve_measure(engine.tbl(spec),spec['name'],{'caption':x['caption'],'agg':x['aggregation'],'field_id':x['field_id']})
 if not measure or measure.get('window'):raise ValueError('Logical scatter x must be a scalar aggregate')
 from kpi_fixed_plan import compile_sql
 df=engine.q(compile_sql(group+' AS CATEGORY, '+measure['sql']+' AS X, COUNT(DISTINCT '+key+') AS Y',engine.tbl(spec),where,engine,group_by='1')+' ORDER BY CATEGORY LIMIT 1001')
 if len(df)>1000:raise ValueError('Logical scatter exceeds the mark limit')
 finite(df,['X','Y'])
 source=ET.fromstring(model['source_xml']);pane=source.find('.//pane[@id="1"]');colors=[n.get('value') for n in pane.findall('.//format[@attr="mark-color"]')]
 color=colors[0] if len(set(colors))==1 else '#4e79a7'
 # Release 27 (F3/F8): axis titles, zero, mark fill/outline and labelled reference lines from the XML
 import axis_xml
 rules=axis_xml.parse(model['source_xml'])
 y_occ=next((o for o in model['occurrences'] if o.get('channel')=='rows'),{})
 xt=x.get('token');yt=y_occ.get('token')
 fields=(getattr(engine,'IR',None) or {}).get('fields',{})
 xfmt=fields.get(x.get('field_id'),{}).get('format');yfmt=fields.get(y_occ.get('field_id'),{}).get('format')
 fill,fill_opacity,stroke=axis_xml.mark_style(rules,'1')
 fill=fill or color
 ticks=axis_xml.axis_tick_count(int(spec.get('_wpx') or 0)-50) if spec.get('_wpx') else None
 xaxis=None if axis_xml.axis_hidden(rules,xt) else alt.Axis(grid=False,labelOverlap=True,**({'tickCount':ticks} if ticks else {}),**({'format':axis_xml.d3(xfmt)} if axis_xml.d3(xfmt) else {}))
 yaxis=None if axis_xml.axis_hidden(rules,yt) and len([o for o in model['occurrences'] if o.get('channel')=='rows'])<2 else alt.Axis(grid=False,**({'format':axis_xml.d3(yfmt)} if axis_xml.d3(yfmt) else {}))
 xs=alt.Scale(zero=axis_xml.include_zero(rules,xt));ys=alt.Scale(zero=axis_xml.include_zero(rules,yt))
 chart=alt.Chart(df).mark_circle(size=axis_xml.circle_size(rules,'1') or 70,fill=fill,fillOpacity=max(fill_opacity,.15),stroke=stroke or fill,strokeWidth=1,opacity=1).encode(
  x=alt.X('X:Q',title=axis_xml.axis_title(rules,xt,x['caption']),axis=xaxis,scale=xs),
  y=alt.Y('Y:Q',title=axis_xml.axis_title(rules,yt,spec['y']['caption']),axis=yaxis,scale=ys),
  tooltip=[alt.Tooltip('CATEGORY:N',title=g['caption']),alt.Tooltip('X:Q',title=x['caption']),alt.Tooltip('Y:Q',title=spec['y']['caption'])])
 layers=[chart]
 style=rules['refline_style'];rcolor=(style.get('stroke-color') or '#898989')[:7];tcolor=style.get('color') or '#898989'
 for axis in plan['references']:
  alias='REF_'+axis;token,fmt=(xt,xfmt) if axis=='X' else (yt,yfmt)
  enc={axis.lower():alt.X(alias+':Q',scale=xs) if axis=='X' else alt.Y(alias+':Q',scale=ys)}
  layers.append(alt.Chart(df).transform_aggregate(**{alias:'mean('+axis+')'}).mark_rule(color=rcolor,strokeDash=[4,4]).encode(**enc))
  label=axis_xml.refline_label(rules,axis_xml.refline_for(rules,token),float(df[axis].mean()),fmt)
  if label:
   pos=({'y':'height','align':'left','baseline':'bottom','dx':3,'dy':-2} if axis=='X' else {'x':'width','align':'right','baseline':'bottom','dy':-2})
   layers.append(alt.Chart(df).transform_aggregate(**{alias:'mean('+axis+')'}).mark_text(text=label,color=tcolor,fontSize=10,
                 font=(getattr(engine,'_font_stack',None) or (lambda f:f))(style.get('font-family')) if style.get('font-family') else alt.Undefined,**pos).encode(**enc))
 out=alt.layer(*layers).properties(height=int(spec.get('_hpx') or 320),autosize=alt.AutoSizeParams(type='fit',contains='padding'))
 from dashboard_actions import chart as action_chart
 action_chart(out,spec,'CATEGORY',engine,use_container_width=True)
 engine.findings.record('APPEARANCE',spec['name'],'logical-count-overlay',plan['appearance_adaptation'])
