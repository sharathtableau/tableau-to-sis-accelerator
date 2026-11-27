"""Bounded category/value/percent-of-total text cards with duplicate axes."""
import re,xml.etree.ElementTree as ET

def build(spec,model):
 occ=model['occurrences'];cols=[o for o in occ if o['channel']=='cols'];rows=[o for o in occ if o['channel']=='rows']
 if spec.get('kind')!='circle' or len(model['panes'])!=3 or len(cols)!=2 or len(rows)!=1 or cols[0]['token']!=cols[1]['token']:return None
 texts=[o for o in occ if o['channel']=='text'];percent=[o for o in texts if o.get('table_calculation_xml')]
 if len(percent)!=1:return None
 from visual_plan_common import reasons
 from semantic_layer import _objects,join_plan
 from init_workbook import parse_relationships
 errors=reasons(spec,model);source=ET.fromstring(model['datasource_xml']);ws=ET.fromstring(model['source_xml']);roles={};group=rows[0];pct=percent[0]
 try:
  objects=_objects(source);graph=join_plan(objects,parse_relationships(source,strict=True))
  if not objects or len(graph['steps'])!=len(objects)-1 or source.findall('.//filter'):raise ValueError('Incomplete model')
 except Exception:errors.append('Percent summary requires one complete unfiltered fact tree')
 else:errors=[e for e in errors if e not in {'Joins, relationships, custom SQL and source filters require a data-model plan','Multiple physical source identities require a data-model plan'}]
 if any(o.get('datasource_id')!=spec.get('datasource_id') or o.get('unresolved') for o in occ):errors.append('Percent summary source ownership is incomplete')
 if group['level']!='row' or group['field_definition'].get('is_calculation') is not False or group.get('datepart'):errors.append('Percent summary needs one physical category')
 if any(not re.fullmatch(r'\s*(?:AVG|MIN|MAX)\s*\(\s*-?\d+(?:\.\d+)?\s*\)\s*',o.get('calculation_source',''),re.I) for o in cols):errors.append('Summary axes must be exact scalar constants')
 if re.sub(r'[\s()]','',ws.findtext('./table/cols','').replace(cols[0]['token'],'F'))!='F+F':errors.append('Summary needs a duplicate-axis overlay')
 if [p['marks'] for p in model['panes']]!=[['Circle'],['Circle'],['Circle']]:errors.append('Summary marks require another plan')
 panes=[p for p in model['panes'] if p['axes']]
 if len(panes)!=2 or any(p['axes']!={'x-axis-name':cols[0]['token']} for p in panes) or not any(p['attributes'].get('x-index')=='1' for p in panes):errors.append('Summary pane ownership requires an explicit overlay')
 values=[o for o in texts if o is not pct and o['level']=='aggregate'];labels=[o for o in texts if o['level']=='row'];colors=[o for o in occ if o['channel']=='color']
 value=values[0] if len(values)==1 else None
 if not value or len(labels)!=1 or len(colors)!=1 or len(texts)!=3:errors.append('Summary needs category, value and percent text')
 else:
  if value['field_id']!=pct.get('field_id') or value['aggregation']!=pct['aggregation'] or value['pane_id']!=pct['pane_id'] or value['aggregation'] not in ('sum','avg','min','max','cnt','ctd'):errors.append('Percent must address the same scalar measure and pane')
  if any(o.get('field_id')!=group['field_id'] or o['level']!='row' for o in labels+colors):errors.append('Color and label must use the row category')
 calculations=ws.findall('.//table-calc')
 exact=(len(calculations)==1 and calculations[0].attrib=={'ordering-type':'Columns','type':'PctTotal'} and not len(calculations[0]) and len(pct.get('table_calculation_xml',[]))==1 and ET.fromstring(pct['table_calculation_xml'][0]).attrib==calculations[0].attrib)
 table_calcs=spec.get('table_calcs',[])
 if exact and len(table_calcs)==1 and table_calcs[0].get('field_id')==pct.get('field_id') and table_calcs[0].get('agg')==pct['aggregation'] and table_calcs[0].get('type')=='PctTotal' and table_calcs[0].get('ordering')=='Columns' and not table_calcs[0].get('options') and not table_calcs[0].get('ordering_field'):
  errors=[e for e in errors if e not in {'Unplanned blend, drill, table calculation, total or axis semantics','Totals and table calculations require a separate plan'}]
  if any(spec.get(k) for k in ('blend','drill','forecast','subtotals','axis_flags','viz_tooltip')):errors.append('Percent summary has unplanned derived behavior')
 else:errors.append('Only complete-column PctTotal without restart/partition options is supported')
 if ws.findall('.//grand-total') or ws.findall('.//subtotals'):errors.append('Summary totals require another plan')
 if any('sort' in n.tag.lower() for n in list(ws.iter())+list(source.iter())):errors.append('Summary category ordering requires another plan')
 if ws.find('./table/tooltip-style') is None or ws.find('./table/tooltip-style').get('tooltip-mode')!='none':errors.append('Summary dynamic tooltip requires a separate plan')
 for o in occ:
  if o in cols:role='placeholder-axis'
  elif o is group or o in labels+colors:role='category'
  elif o is pct:role='percent-total'
  elif o is value:role='aggregate-value'
  elif o['channel']=='x-axis-name' and o['token']==cols[0]['token']:role='placeholder-selector'
  else:errors.append('Unconsumed summary occurrence');continue
  roles[o['id']]=role
  if o is not pct and o.get('table_calculation_xml'):errors.append('Additional table calculation is unplanned')
 return {'status':'REQUIRES_PLAN' if errors else 'MAPPED','renderer':'percent-summary','bindings':roles,'reasons':sorted(set(errors)),'group':group['id'],'value':value['id'] if value else None,'percent':pct['id'],'addressing':'ALL_VISIBLE_ROW_CATEGORIES','appearance_adaptation':'Duplicate constant circle axes represented as category/value/percentage rows; visual acceptance remains separate'}

def render(spec,where,engine):
 import html
 from visual_plan_common import physical,finite
 from kpi_fixed_plan import compile_sql
 model=spec['worksheet_semantics'];plan=build(spec,model)
 if plan!=model['projection'] or plan['status']!='MAPPED':raise ValueError('Percent summary source plan changed')
 occ={o['id']:o for o in model['occurrences']};group=occ[plan['group']];value=occ[plan['value']];pct=occ[plan['percent']]
 measure=engine.resolve_measure(engine.tbl(spec),spec['name'],{'caption':value['caption'],'agg':value['aggregation'],'field_id':value['field_id']})
 if not measure or measure.get('window'):raise ValueError('Percent summary value must be scalar')
 df=engine.q(compile_sql(physical(engine,spec,group)+' AS CATEGORY, '+measure['sql']+' AS VALUE',engine.tbl(spec),where,engine,group_by='1'))
 if df.empty:engine.st.caption('No data for the selected filters.');return
 if len(df)>1000:raise ValueError('Percent summary exceeds the category limit')
 finite(df,['VALUE'])
 if df.CATEGORY.isna().any() or not all(str(c).isascii() for c in df.CATEGORY):raise ValueError('Summary null/locale category ordering requires another plan')
 df=df.sort_values('CATEGORY',key=lambda c:c.astype(str).str.casefold());total=float(df.VALUE.sum());rows=[]
 ws=ET.fromstring(model['source_xml']);formats=[f.get('value') for f in ws.findall('.//format[@attr="text-format"]') if f.get('field')==pct['token']]
 if len(formats)!=1:raise ValueError('Percent summary lacks one authored percentage format')
 value_ref={'caption':value['caption'],'tableau_format':engine.IR['fields'][value['field_id']].get('format','')};pct_ref={'caption':pct['caption'],'tableau_format':formats[0]}
 domain,palette=engine.cat_colors(group['caption'],df.CATEGORY.tolist());color_map=dict(zip(domain or [],palette or []))
 # Release 27 (F5/F9): Tableau draws each member as its coloured Circle mark plus the authored label text
 # (label-run colour / font / size), rows spread over the pane -- not a bordered table in the mark colour.
 runs=[r for r in spec.get('label_runs') or [] if r.get('fontcolor') or r.get('fontname') or r.get('fontsize')]
 tcolor=next((r['fontcolor'] for r in runs if r.get('fontcolor')),'#666666');tfont=next((r['fontname'] for r in runs if r.get('fontname')),None)
 tsize=next((r['fontsize'] for r in runs if r.get('fontsize')),'9')
 for r in df.itertuples():
  number=engine.fmt_tableau(r.VALUE,value_ref,measure);share='—' if total==0 else engine.fmt_tableau(r.VALUE/total,pct_ref,{'fmt':'pct'})
  dot=html.escape(color_map.get(str(r.CATEGORY),'#666666'),quote=True)
  rows.append('<div style="display:flex;justify-content:space-between;align-items:center;gap:6px">'
              '<span><span style="display:inline-block;width:.75em;height:.75em;border-radius:50%;background:'+dot+';margin-right:.45em;vertical-align:-.05em"></span>'
              +html.escape(str(r.CATEGORY))+'</span><span>'+html.escape(number)+' ('+html.escape(share)+')</span></div>')
 box=('display:flex;flex-direction:column;justify-content:space-around;'+(f"min-height:{int(spec['_hpx'])}px;" if spec.get('_hpx') else '')
      +f'font-size:{tsize}pt;color:{tcolor};'+(f'font-family:{engine._font_stack(tfont)};' if tfont else '')+'padding:0 4px')
 engine.st.markdown('<div class="v3-percent-summary" style="'+html.escape(box,quote=True)+'">'+''.join(rows)+'</div>',unsafe_allow_html=True)
 engine.findings.record('APPEARANCE',spec['name'],'percent-summary-overlay',plan['appearance_adaptation'])
