"""AI03.2 typed INCLUDE proposals. Planning only; never clears sheet refusals.
The compiler is an unfiltered evaluation harness, not a worksheet executor.
Source bindings, filter phases and renderer integration need separate validation.
"""
import json
import re
from copy import deepcopy
VERSION = 'include-lod/2'
AGGREGATES = {'sum':'SUM', 'avg':'AVG', 'min':'MIN', 'max':'MAX'}

def build(field, fields, view_dimensions, outer_aggregation, parameters=None):
    from cortex_assist import digest
    from typed_include_expression import Resolver
    if (not isinstance(field,dict) or fields.get(field.get('id')) != field
            or field.get('is_calculation') is not True
            or field.get('datatype') not in {'integer','real'}):
        raise ValueError('INCLUDE requires a scoped numeric calculated field')
    if (not isinstance(view_dimensions,list) or len(view_dimensions)>8
            or any(not isinstance(v,str) for v in view_dimensions)
            or len(set(view_dimensions))!=len(view_dimensions)
            or not isinstance(outer_aggregation,str) or outer_aggregation not in AGGREGATES):
        raise ValueError('INCLUDE view grain or outer aggregation is unsupported')
    formula=field.get('formula','')
    if not isinstance(formula,str) or len(formula)>4000:
        raise ValueError('INCLUDE formula budget exceeded')
    match=re.fullmatch(r'\s*\{\s*INCLUDE\s+(.+?)\s*:\s*(.*?)\s*\}\s*',formula,re.I|re.S)
    if not match:
        raise ValueError('INCLUDE needs explicit dimensions and one supported aggregate; nested dependencies need another contract')
    owner=field['datasource_id']
    resolver=Resolver(fields,owner,parameters)
    resolver.stack.append(field['id'])
    keys=[]
    # Commas inside bracketed physical names are data, not separators.
    pattern=re.compile(r'\s*(?:\[([^\]]+)\]|(DAY|MONTH|YEAR)\s*\(\s*\[([^\]]+)\]\s*\))\s*',re.I)
    text=match.group(1);pos=0
    while pos<len(text):
        part=pattern.match(text,pos)
        if not part:raise ValueError('INCLUDE key expression is unsupported')
        key, datatype, level=resolver.field(part.group(1) or part.group(3))
        if level!='row':raise ValueError('INCLUDE keys must be row expressions')
        if part.group(2):
            if datatype not in {'date','datetime'}:raise ValueError('INCLUDE date part requires a date or datetime field')
            key=['datepart',part.group(2).lower(),key[1]] if key[0]=='field' else ['datepart_expr',part.group(2).lower(),key]
        if key in keys:raise ValueError('Duplicate INCLUDE key')
        keys.append(key);pos=part.end()
        if pos<len(text):
            if text[pos]!=',':raise ValueError('INCLUDE key expression is unsupported')
            pos+=1
            if not text[pos:].strip():raise ValueError('Trailing INCLUDE key separator')
    if not 1<=len(keys)<=8:raise ValueError('INCLUDE key budget exceeded')
    for fid in view_dimensions:
        f=fields.get(fid,{})
        if (f.get('datasource_id')!=owner or f.get('is_calculation') is not False
                or f.get('datatype') not in {'string','integer','real','date','datetime','boolean'}):
            raise ValueError('View dimensions must be physical same-source fields')
    aggregate, datatype, level=resolver.parse(match.group(2))
    if level!='aggregate' or aggregate[0] not in AGGREGATES:
        raise ValueError('INCLUDE requires one supported aggregate or aggregate calculation dependency')
    expression=['include',keys,aggregate[0],aggregate[1],[['field',fid] for fid in view_dimensions],outer_aggregation]
    ids={field['id'],*view_dimensions,*resolver.dependencies}
    used_parameters={name:deepcopy(parameters[name]) for name in sorted(resolver.used_parameters)}
    return {'version':VERSION,'field_id':field['id'],'datasource_id':owner,
            'source_formula':formula,'view_dimensions':list(view_dimensions),'outer_aggregation':outer_aggregation,
            'expression':expression,'dependency_ids':sorted(ids),'parameters':used_parameters,
            'dependency_hash':digest({fid:fields[fid] for fid in sorted(ids)}),
            'filter_phase':'dimension filters before detail aggregation; measure filters after reaggregation',
            'execution':'NOT_ENABLED'}

def validate(plan, fields, parameters=None):
    field=fields.get(plan.get('field_id'),{})
    expected=build(field,fields,plan.get('view_dimensions'),plan.get('outer_aggregation'),parameters)
    if plan!=expected:raise ValueError('INCLUDE plan is stale or differs from independent source semantics')
    return expected

def evidence(plan, fields, parameters=None):
    validate(plan,fields,parameters)
    # No unrelated catalog, physical binding, data rows, worksheet XML or captions.
    return {'formula':plan['source_formula'],
            'fields':[{k:fields[fid][k] for k in ('id','internal','datatype','is_calculation','formula') if k in fields[fid]} for fid in plan['dependency_ids'] if fid!=plan['field_id']],
            'parameters':plan['parameters'],'view_dimensions':plan['view_dimensions'],'outer_aggregation':plan['outer_aggregation'],
            'grammar':{'root':['include','include_keys','inner_aggregate','row_expression','view_keys','outer_aggregate'],
                       'key':[['field','field_id'],['datepart','day|month|year','base_field_id'],['datepart_expr','day|month|year','date_expression']],
                       'aggregates':sorted(AGGREGATES),
                       'row_expression':[['field','field_id'],['number',0],['add','left','right'],['subtract','left','right'],['multiply','left','right'],['abs','child'],['zn','child'],['positive','child'],['negative','child'],['if','condition','then','else'],['null'],['eq|ne|gt|ge|lt|le','left','right'],['parameter','internal_parameter_name'],['dateadd','day','integer_expression','date_expression']]}}

def propose(plan, fields, session, model, context, parameters=None):
    from cortex_assist import request, _strict_json, digest
    result=request(session,model,'Convert the source INCLUDE formula into expression_json: exactly ["include", keys, inner_aggregate, row_node, view_keys, outer_aggregate]. Recursively inline ALL calculated dependencies in BOTH keys and row_node. Only base fields may appear as ["field", id] or ["datepart", part, id]. A calculated date key MUST use ["datepart_expr", part, expanded_date_node]; expand DATEADD and its parameters fully. Parameter nodes use their internal declaration names, never values. Elements 2 and 5 MUST be lowercase aggregate STRINGS (sum/avg/min/max), never arrays. Element 3 is ONE node starting with an operator string, never a list of nodes. Shape example: ["include", [["field","k"]], "sum", ["field","v"], [], "avg"]. IF without ELSE uses ["null"]. Preserve source key/view order and source IDs. Return arrays per the grammar, no objects. Keep reason under 150 characters; refuse if uncertain.',
                   evidence(plan,fields,parameters),['use_validated_include_plan'],calculation=True,context=context,
                   scope={'capability':VERSION,'dependency_hash':plan['dependency_hash'],'plan_hash':digest(plan)})
    if result['choice']!='use_validated_include_plan':raise ValueError('Cortex declined INCLUDE planning')
    if digest(_strict_json(result['expression_json']))!=digest(plan['expression']):
        raise ValueError('Cortex INCLUDE proposal differs from independent source semantics')
    return deepcopy(plan)

def compile_unfiltered_sql(plan, fields, table, parameters=None, parameter_values=None, dialect="snowflake"):
    """Compile source-validated two-stage SQL for explicit, unfiltered evaluation.
    Not wired to engine dispatch: a valid plan alone cannot authorize execution.
    Table identity must be verified by an eventual runtime binding adapter.
    """
    from typed_include_expression import expression_sql
    validate(plan,fields,parameters)
    if dialect not in {"snowflake","duckdb"}:raise ValueError("Unsupported SQL dialect")
    if not isinstance(table,str) or not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*){0,2}',table):
        raise ValueError('Evaluation table identifier is invalid')
    physical_names={}
    def physical(fid):
        name=fields[fid].get('physical')
        if not isinstance(name,str) or not name or any(ord(c)<32 for c in name):raise ValueError('Invalid physical field name')
        if name in physical_names and physical_names[name]!=fid:raise ValueError('Physical field collision')
        physical_names[name]=fid
        return '"'+name.replace('"','""')+'"'
    _,includes,inner,row,view,outer=plan['expression']
    grain=[]
    for key in view+includes:
        if key not in grain:grain.append(key)
    def key_sql(key):
        return expression_sql(key,physical,parameter_values or {},dialect)
    detail=', '.join(key_sql(k)+' AS K'+str(i) for i,k in enumerate(grain))
    inner_sql='SELECT '+detail+', '+AGGREGATES[inner]+'('+expression_sql(row,physical,parameter_values or {},dialect)+') AS V FROM '+table+' GROUP BY '+', '.join(str(i+1) for i in range(len(grain)))
    selected=['K'+str(grain.index(k)) for k in view]
    output=', '.join(selected+[AGGREGATES[outer]+'(V) AS VALUE'])
    return 'WITH detail AS ('+inner_sql+') SELECT '+output+' FROM detail'+(' GROUP BY '+', '.join(selected) if selected else '')

def analyze(ir, session, model, context):
    """Create separate planning evidence; never mutate sheets/calcs/support."""
    fields=ir.get('fields',{});records=[]
    for dashboard in ir.get('dashboards',[]):
        for sheet in dashboard.get('sheets',[]):
            occurrences=sheet.get('worksheet_semantics',{}).get('occurrences',[])
            for occurrence in occurrences:
                field=fields.get(occurrence.get('field_id'),{})
                if not re.match(r'\s*\{\s*INCLUDE\b',field.get('formula') or '',re.I):continue
                record={'dashboard':dashboard.get('name'),'sheet':sheet.get('name'),'field_id':field.get('id'),'status':'REFUSED','execution':'NOT_ENABLED'}
                start=len(context.traces)
                try:
                    dimensions=[]
                    for o in occurrences:
                        if o is occurrence:continue
                        if (o.get('level')!='row' or o.get('datepart') or o.get('builtin') or o.get('unresolved')
                                or o.get('selector') or o.get('table_calculation_xml') or o.get('channel') not in {'rows','cols','color','detail','lod'}):
                            raise ValueError('INCLUDE worksheet grain requires another occurrence contract')
                        if o.get('field_id') not in dimensions:dimensions.append(o.get('field_id'))
                    if occurrence.get('table_calculation_xml') or occurrence.get('level')!='aggregate':
                        raise ValueError('INCLUDE table-calculation use requires an ordered execution contract')
                    from typed_include_expression import parameter_declarations
                    parameters=parameter_declarations(sheet)
                    plan=build(field,fields,dimensions,occurrence.get('aggregation'),parameters)
                    record['plan']=propose(plan,fields,session,model,context,parameters)
                    record.update(status='PLAN_VALIDATED',reason='Cortex plan matches independent source semantics. Filter, binding and renderer integration remain unverified.')
                except Exception as exc:
                    from cortex_assist import ProposalError
                    record['reason']=str(exc) if isinstance(exc,(ValueError,ProposalError)) else 'INCLUDE planning failed; original refusal retained'
                    if isinstance(exc,ProposalError):record['error_code']=exc.code
                record['request_ids']=[t['request_id'] for t in context.traces[start:]]
                records.append(record)
    return records
