"""Authored INDEX over a complete physical record grain and repeated metric."""
import re,xml.etree.ElementTree as ET
from bounded_overlay import definitions,tree_reasons,parameter_scalar
from kpi_fixed_plan import expression,compile_sql

def build(spec,model):
 if spec.get('kind')!='table' or [p['marks'] for p in model['panes']]!=[['Bar'],['Bar'],['GanttBar']]:return None
 occ=model['occurrences'];cols=[o for o in occ if o['channel']=='cols'];rows=[o for o in occ if o['channel']=='rows']
 if len(cols)!=2 or cols[0]['token']!=cols[1]['token'] or len(rows)<2:return None
 defs=definitions(model,spec['datasource_id']);ws=ET.fromstring(model['source_xml']);errors=tree_reasons(spec,model);roles={};labels={};metric=cols[0]
 indexes=[o for o in rows if re.fullmatch(r'\s*INDEX\s*\(\s*\)\s*',defs.get(o.get('field_definition',{}).get('physical'),''),re.I)]
 dims=[o for o in rows if o['level']=='row' and o.get('field_definition',{}).get('is_calculation') is False and not o.get('datepart')]
 if len(indexes)!=1 or len(dims)!=len(rows)-1 or rows[0] not in indexes:errors.append('Detail needs leading INDEX and complete physical record fields')
 # Shared checks flag all table calculations. Only the exact addressing below
 # replaces that refusal; every other table occurrence remains unconsumed.
 if len(indexes)==1:
  index=indexes[0];xml=index.get('table_calculation_xml',[])
  if len(xml)!=1:errors.append('Detail INDEX addressing is incomplete')
  else:
   calc=ET.fromstring(xml[0]);address=[n.get('field') for n in calc]
   expected={'['+spec['datasource_id']+'].['+o['field_definition']['physical']+']' for o in dims}
   extra=set(address)-expected
   scalar_names={o['field_definition']['physical'] for o in occ if parameter_scalar(o,model,defs)}
   if calc.attrib!={'ordering-type':'Field'} or any(n.tag!='order' or set(n.attrib)!={'field'} for n in calc) or len(address)!=len(set(address)) or not expected<=set(address) or any(not any(n=='['+spec['datasource_id']+'].['+name+']' for name in scalar_names) for n in extra):errors.append('Detail INDEX must address every record field and only scalar parameter labels')
  if not errors or all(e in ['Unplanned blend, drill, table calculation, total or axis semantics','Totals and table calculations require a separate plan'] for e in errors):
   errors=[e for e in errors if e not in ['Unplanned blend, drill, table calculation, total or axis semantics','Totals and table calculations require a separate plan']]
  else:
   # Remove the broad flags only if their other triggering features are absent.
   if not any(spec.get(k) for k in ['blend','drill','forecast','subtotals','axis_flags','viz_tooltip']) and all(o['level']!='table' or o is index for o in occ) and not ws.findall('.//grand-total') and not ws.findall('.//subtotals'):
    errors=[e for e in errors if e not in ['Unplanned blend, drill, table calculation, total or axis semantics','Totals and table calculations require a separate plan']]
 proof=expression(spec,model,{'caption':metric['caption'],'field_id':metric.get('field_id'),'internal':metric.get('field_definition',{}).get('physical'),'agg':metric['aggregation']},metric)
 if not proof:errors.append('Detail aggregate dependencies cannot be proved')
 sorts=[n for n in ws.iter() if 'sort' in n.tag]
 if len(sorts)!=1 or sorts[0].tag!='computed-sort' or not dims or sorts[0].get('column')!=dims[0]['token'] or sorts[0].get('using')!=metric['token'] or sorts[0].get('direction') not in ['ASC','DESC']:errors.append('Detail INDEX requires authored leading-record metric sorting')
 if re.sub(r'[\s()]','',ws.findtext('./table/cols','').replace(metric['token'],'F'))!='F+F':errors.append('Detail repeated metric needs overlay shelf syntax')
 owners=[p for p in model['panes'] if p['axes']=={'x-axis-name':metric['token']}]
 if len(owners)!=2 or not any(p['attributes'].get('x-index')=='1' for p in owners):errors.append('Detail duplicate-axis ownership is incomplete')
 for o in occ:
  role=None
  if o in cols:role='shared-metric'
  elif o in dims:role='record-field'
  elif o in indexes:role='record-index'
  elif o['channel']=='x-axis-name' and o['token']==metric['token']:role='duplicate-axis-selector'
  elif o['channel'] in ('text','tooltip'):
   if o['token']==metric['token']:role='metric-label'
   else:
    scalar=parameter_scalar(o,model,defs)
    if scalar:labels[o['id']]=scalar;role='parameter-label'
  if not role:errors.append('Unconsumed detail occurrence: '+o['channel'])
  else:roles[o['id']]=role
  if o.get('unresolved') or o.get('builtin') or o.get('datasource_id')!=spec['datasource_id'] or (o.get('table_calculation_xml') and o not in indexes):errors.append('Detail field/calculation ownership is incomplete')
 return {'status':'REQUIRES_PLAN' if errors else 'MAPPED','renderer':'detail-record','bindings':roles,'reasons':sorted(set(errors)),
         'dimensions':[o['id'] for o in dims],'metric':metric['id'],'expression':proof,'labels':labels,
         'direction':sorts[0].get('direction') if len(sorts)==1 else None,
         'appearance_adaptation':'Complete record table with authored metric and INDEX; tied metric rows use ascending record fields pending Tableau tie-order acceptance'}

def render(spec,where,engine):
 from visual_plan_common import physical,finite
 import pandas as pd
 model=spec['worksheet_semantics'];plan=build(spec,model)
 if plan!=model['projection'] or plan['status']!='MAPPED':raise ValueError('Detail plan is stale or incomplete')
 occ={o['id']:o for o in model['occurrences']};fields=[occ[oid] for oid in plan['dimensions']];proof=plan['expression']
 if engine.IR['fields'].get(proof['field_id'],{}).get('calculation',{}).get('sql')!=proof['sql']:raise ValueError('Detail aggregate translation changed')
 selected=', '.join(physical(engine,spec,o)+' AS D'+str(i) for i,o in enumerate(fields))+', '+proof['sql']+' AS V'
 df=engine.q(compile_sql(selected,engine.tbl(spec),where,engine,group_by=', '.join(str(i+1) for i in range(len(fields))))+' LIMIT 50001')
 if len(df)>50000:raise ValueError('Detail record limit exceeded; no silent truncation')
 if df.empty:engine.st.info('No data for current filters');return
 # The authored outer record dimension must identify one complete record.
 if df.D0.isna().any() or df.D0.duplicated().any():raise ValueError('Detail leading record identity is null or conflicting')
 if df.V.notna().any():finite(df.loc[df.V.notna()],['V'])
 df=df.sort_values(['V']+['D'+str(i) for i in range(len(fields))],ascending=[plan['direction']=='ASC']+[True]*len(fields),na_position='last').reset_index(drop=True)
 display=pd.DataFrame({'#':range(1,len(df)+1)})
 for i,o in enumerate(fields):display[o['caption']]=df['D'+str(i)]
 metric=occ[plan['metric']];ref={'caption':metric['caption'],'agg':metric['aggregation'],'field_id':metric['field_id'],'tableau_format':engine.IR['fields'][metric['field_id']].get('format')};resolved=engine.resolve_measure(engine.tbl(spec),spec['name'],ref)
 caption=metric['caption'];symbol=''
 for oid,scalar in plan['labels'].items():
  value=str(engine.param_value(model['parameter_definitions'][scalar['parameter']]['caption']));value=str(int(float(value))) if re.fullmatch(r'-?\d+(\.0+)?',value) else value
  label=scalar['branches'].get(value)
  if label is None:raise ValueError('Detail parameter has no authored branch')
  if occ[oid]['channel']=='text':symbol=label
  elif label.strip() and label not in ['$',' ']:caption=label
 display[caption]=df.V.map(lambda v:symbol+engine.fmt_tableau(v,ref,resolved))
 engine._safe_dataframe(display,use_container_width=True,hide_index=True,height=500)
 engine.findings.record('APPEARANCE',spec['name'],'detail-record-adaptation',plan['appearance_adaptation'])
