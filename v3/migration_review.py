"""Cortex migration triage from minimized structural evidence, never execution approval."""
import xml.etree.ElementTree as ET
from cortex_assist import request, ProposalError, digest

PRIORITIES={
 'repair_calculations':'Resolve untranslated calculations',
 'repair_model_execution':'Verify relationship and join execution',
 'repair_visual_contracts':'Resolve unsupported worksheet behavior',
 'verify_interactions':'Verify authored dashboard interactions',
 'compare_results':'Compare numeric and visual results with Tableau'}

def evidence(ir):
 sheets=[s for d in ir.get('dashboards',[]) for s in d.get('sheets',[])]
 sources={s.get('datasource_id'):s.get('worksheet_semantics',{}).get('datasource_xml','') for s in sheets}
 counts={'sheet_occurrences':len(sheets),'blocked_occurrences':sum(s.get('support',{}).get('status') in {'INVALID','UNSUPPORTED'} for s in sheets),
         'untranslated_calculations':sum(len(s.get('calc_drops',{})) for s in ir.get('field_scopes',{}).values()),
         'relationship_sources':0,'physical_join_sources':0,
         'authored_actions':sum(len(d.get('actions',[])) for d in ir.get('dashboards',[])),
         'blocked_actions':sum(a.get('status')!='READY' for d in ir.get('dashboards',[]) for a in d.get('actions',[]))}
 for xml in sources.values():
  if not xml:continue
  ds=ET.fromstring(xml)
  counts['relationship_sources']+=bool(ds.findall('.//relationship'))
  counts['physical_join_sources']+=bool(ds.findall('.//relation[@type="join"]'))
 counts['authored_parameter_actions']=sum(len(s.get('parameter_actions',[])) for s in sheets)
 counts['blocked_parameter_actions']=sum(a.get('status')!='READY' for s in sheets for a in s.get('parameter_actions',[]))
 options=[]
 if counts['untranslated_calculations']:options.append('repair_calculations')
 if counts['relationship_sources'] or counts['physical_join_sources']:options.append('repair_model_execution')
 if counts['blocked_occurrences']:options.append('repair_visual_contracts')
 if counts['authored_actions'] or counts['authored_parameter_actions']:options.append('verify_interactions')
 options.append('compare_results')
 return {'schema':'migration-triage/1','counts':counts,'acceptance':'NOT_VERIFIED','test_results':'NOT_SUPPLIED_TO_THIS_REVIEW',
         'candidate_priorities':{k:PRIORITIES[k] for k in options}},options

def components(ir):
 """AI01: the components each priority is about, computed by code (the model never names an id)."""
 sheets=[(d.get('name'),s) for d in ir.get('dashboards',[]) for s in d.get('sheets',[])]
 calcs=[f"calculation: {n} ({sc.get('caption') or ds})" for ds,sc in (ir.get('field_scopes') or {}).items()
        for n in (sc.get('calc_drops') or {})]
 models=[]
 for ds in sorted({s.get('datasource_id') for _d,s in sheets if s.get('datasource_id')}):
  xml=next((s.get('worksheet_semantics',{}).get('datasource_xml') for _d,s in sheets
            if s.get('datasource_id')==ds and s.get('worksheet_semantics',{}).get('datasource_xml')),'')
  if xml:
   x=ET.fromstring(xml)
   if x.findall('.//relationship') or x.findall('.//relation[@type="join"]'):
    models.append(f"datasource: {((ir.get('field_scopes') or {}).get(ds) or {}).get('caption') or ds}")
 return {'repair_calculations':calcs,'repair_model_execution':models,
         'repair_visual_contracts':[f"sheet: {d} / {s.get('name')}" for d,s in sheets
                                    if s.get('support',{}).get('status') in {'INVALID','UNSUPPORTED'}],
         'verify_interactions':[f"action: {d.get('name')} / {a.get('name') or a.get('id')}"
                                for d in ir.get('dashboards',[]) for a in d.get('actions',[])]
                               +[f"parameter action: {d} / {s.get('name')}" for d,s in sheets if s.get('parameter_actions')],
         'compare_results':[f"sheet: {d} / {s.get('name')}" for d,s in sheets]}

def review(ir,session,model,context):
 source,options=evidence(ir)
 base={'evidence':source,'evidence_hash':digest(source),'acceptance':'NOT_VERIFIED',
       'execution_changed':False,'model':model}
 if session is None:return dict(base,status='UNAVAILABLE',reason='Connect to Snowflake to request Cortex migration review.')
 try:
  proposal=request(session,model,
   'Recommend the highest-priority next migration check from the offered options. Explain the structural evidence and the risk it addresses. Test results are not supplied to this review. NOT_VERIFIED denotes missing Tableau acceptance evidence; it says nothing about whether implementation tests were run or passed. Do not infer tests failed or were not run. Do not claim tests passed, migration succeeded, or execution is authorized. Counts are evidence; never invent workbook details.',
   source,options,context=context,scope={'purpose':'migration-triage','evidence_hash':digest(source)})
  if proposal['choice']=='refuse':return dict(base,status='REFUSED',reason=proposal['reason'])
  return dict(base,status='REVIEWED',priority=proposal['choice'],title=PRIORITIES[proposal['choice']],reason=proposal['reason'],
              components=components(ir).get(proposal['choice'],[])[:50])
 except ProposalError as exc:return dict(base,status='UNAVAILABLE',reason=str(exc),error_code=exc.code)
