"""Bounded aggregate-of-FIXED KPI execution before regular dimension filters."""
import re,xml.etree.ElementTree as ET
from calc_translator import translate_formula

def build(spec,model):
    errors=[];bindings={};source=ET.fromstring(model['datasource_xml']);ws=ET.fromstring(model['source_xml'])
    definitions={c.get('name','').strip('[]'):c.find('calculation').get('formula') for c in source.findall('./column') if c.find('calculation') is not None and c.find('calculation').get('formula')}
    params=model.get('parameter_definitions',{});values={};alias={}
    for name,p in params.items():
        values[name]=values[p['caption']]=p.get('value');alias[name]=p['caption']
    if len(model['panes'])!=1 or model['panes'][0]['axes'] or not spec.get('label_runs') or any(mark not in ('Text','Automatic','Shape') for mark in model['panes'][0]['marks']):errors.append('FIXED KPI needs one composed text card')
    if len(spec.get('measures',[]))>7:errors.append('KPI measure count exceeds implemented limit')
    if source.findall('.//filter') or any(f.get('context') for f in spec.get('applied_filters',[])):errors.append('Context/source filter phase requires a separate FIXED contract')
    # Conservatively reject table-calculation functions anywhere in the source
    # dependency closure. Explicit source FIXED windows are the only new family.
    def safe(name,trail=()):
        if name in trail:return False
        formula=definitions.get(name)
        if formula is None:return True
        code=re.sub(r"'(?:(?:'')|[^'])*'|\"[^\"]*\"|//[^\n]*",'',formula)
        if re.search(r'\b(WINDOW_\w+|LOOKUP|LAST|FIRST|INDEX|RANK\w*|RUNNING_\w+)\s*\(',code,re.I):return False
        if re.search(r'\{\s*(INCLUDE|EXCLUDE)\b',code,re.I):return False
        return all(safe(ref,trail+(name,)) for ref in re.findall(r'\[([^\]]+)\]',code) if ref in definitions)
    planned=[]
    for i,ref in enumerate(spec.get('measures',[])):
        internal=ref.get('internal')
        if not internal or internal not in definitions or not safe(internal):errors.append('KPI dependency contains an unplanned calculation');continue
        sql,aggregate=translate_formula(definitions[internal],values,definitions,param_alias=alias)
        if not sql or not aggregate or any(not re.match(r'PARTITION\s+BY\s+',m.group(1),re.I) for m in re.finditer(r'\bOVER\s*\(([^()]*)\)',sql,re.I)):
            errors.append('KPI must be aggregate over row-level FIXED partitions');continue
        planned.append({'field_id':ref.get('field_id'),'sql':sql})
    for o in model['occurrences']:
        if o['channel']!='text' or o.get('table_calculation_xml') or o.get('unresolved') or o.get('builtin'):errors.append('KPI has unplanned occurrences');continue
        if o.get('parameter'):bindings[o['id']]='parameter-text:'+o['parameter']['internal'];continue
        matches=[i for i,r in enumerate(spec.get('measures',[])) if r.get('field_id')==o.get('field_id') and r.get('agg')==o['aggregation']]
        if len(matches)!=1 or o.get('datasource_id')!=spec.get('datasource_id'):errors.append('KPI occurrence does not match one exact measure')
        else:bindings[o['id']]='measures['+str(matches[0])+']'
    if len(planned)!=len(spec.get('measures',[])):errors.append('Incomplete KPI calculation plan')
    return {'status':'REQUIRES_PLAN' if errors else 'MAPPED','renderer':'kpi-fixed','bindings':bindings,'reasons':sorted(set(errors)),'measures':planned,'filter_phase':'FIXED-before-regular-dimension'}

def compile_sql(selected,table,where,engine,group_by=None):
    # Reuse the original deterministic hoister, with filters deliberately kept
    # outside every FIXED precomputation layer. Never rely on retry-after-error.
    sql='SELECT '+selected+' FROM '+table + (' GROUP BY ' + group_by if group_by else '')
    for _ in range(12):
        next_sql=engine._hoist_windows(sql)
        if next_sql is None:break
        sql=next_sql
    head=sql[:next(k[1] for k in engine._tl_keywords(sql) if k[0]=='FROM')]
    if re.search(r'\bOVER\s*\(',head,re.I):raise ValueError('Unplanned outer window calculation')
    if group_by:
        position=next(k[1] for k in engine._tl_keywords(sql) if k[0]=='GROUP BY')
        return sql[:position]+' '+where+' '+sql[position:]
    return sql+' '+where


def expression(spec,model,ref,occurrence):
    """Validate one aggregate-of-FIXED expression using the same source proof."""
    from copy import deepcopy
    candidate=deepcopy(spec);candidate['measures']=[ref];candidate['label_runs']=[{'field':ref['caption']}]
    proof=deepcopy(model);proof['panes']=[{'axes':{},'marks':['Text']}]
    item=deepcopy(occurrence);item['channel']='text';proof['occurrences']=[item]
    plan=build(candidate,proof)
    return plan['measures'][0] if plan['status']=='MAPPED' and len(plan['measures'])==1 else None
