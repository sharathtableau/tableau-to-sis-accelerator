"""Read-only fact-grain relationship binding; no view DDL or model-authored SQL."""
import hashlib,json,re
import xml.etree.ElementTree as ET
from calc_translator import to_phys
from init_workbook import parse_relationships,metadata_object_id
from semantic_layer import _objects,join_plan
from v3_source_binding import FQN,compatible,rows
VERSION='fact-tree-binding/1'
IDENT=re.compile(r'[A-Za-z_][A-Za-z_0-9]*\Z')

def quote(value):
    if not isinstance(value,str) or not value or len(value)>255 or any(ord(c)<32 for c in value):
        raise ValueError('Invalid source column identifier')
    return '"'+value.replace('"','""')+'"'

def build(ds):
    if any(n.tag in {'filter','initial-sql'} or (n.tag=='relation' and n.get('type') in {'join','union','text'}) for n in ds.iter()):
        raise ValueError('Only unfiltered logical relationship trees are supported')
    objects=_objects(ds);edges=parse_relationships(ds,strict=True,composite=True);graph=join_plan(objects,edges)
    if len(objects)<2 or not graph['steps'] or len(graph['steps'])!=len(objects)-1:
        raise ValueError('A complete single-fact relationship tree is required')
    connections=[c for c in ds.findall('.//named-connection/connection') if c.get('class')=='snowflake']
    if len(connections)!=1:raise ValueError('One authored Snowflake connection is required')
    tables={}
    for oid,obj in objects.items():
        value=obj['source'] or '';parts=re.findall(r'\[([^\]]+)\]',value)
        if '.'.join('['+p+']' for p in parts)!=value or len(parts)!=3:raise ValueError('Exact qualified source tables required')
        fqn='.'.join(parts)
        if not FQN.fullmatch(fqn) or fqn!=fqn.upper():raise ValueError('Unquoted uppercase source table required')
        tables[oid]={'table':fqn,'caption':obj['caption']}
    fields={}
    declared={(c.get('name') or '').strip('[]'):c.get('datatype') for c in ds.findall('./column')}
    # DM8: an extract's own metadata (<connection class='hyper'> inside <extract>) describes the snapshot, not the
    # live tables this plan reads: only the live connection's records decide ownership and types
    snapshot={id(m) for c in ds.iter('connection') if c.get('class')=='hyper' for m in c.iter('metadata-record')}
    for mr in ds.findall('.//metadata-record'):
        if mr.get('class')!='column' or id(mr) in snapshot:continue
        local=(mr.findtext('local-name') or '').strip('[]');owner=metadata_object_id(mr)
        remote=mr.findtext('remote-name');dtype=mr.findtext('local-type') or declared.get(local)
        if owner not in tables:
            # DM3: metadata that names its owner only by caption (<parent-name>, no <object-id>) belongs to the
            # one table with that caption; two tables with the caption stay ambiguous and refuse
            parent=(mr.findtext('parent-name') or '').strip('[]')
            same=[oid for oid,t in tables.items() if t['caption']==parent]
            if len(same)!=1:raise ValueError('Physical field owner is absent from graph')
            owner=same[0]
        quote(remote)
        definition={'owner':owner,'remote':remote,'datatype':dtype,'output':to_phys(local)}
        if local in fields and fields[local]!=definition:raise ValueError('Ambiguous physical field metadata')
        fields[local]=definition
    physical=[c for c in ds.findall('./column') if c.find('calculation') is None and c.get('datatype')!='table']
    for c in physical:
        internal=(c.get('name') or '').strip('[]')
        if internal not in fields or fields[internal]['datatype']!=c.get('datatype'):raise ValueError('Incomplete physical field ownership/type')
    output=[f['output'] for f in fields.values()]
    if len(output)!=len(set(output)):raise ValueError('Normalized physical columns collide')
    # Use exact relationship expressions for field ownership; never strip an
    # authored dimension qualifier and then guess which source owns the key.
    keys=[]
    for rel in ds.findall('.//relationship'):
        expr=rel.find('expression')
        # E04: a composite key is an AND of equalities; every pair must be owned exactly like a single key
        terms=list(expr) if expr.get('op')=='AND' else [expr]
        pairs=[]
        for t in terms:
            left,right=[e.get('op').strip('[]') for e in t]
            pairs.append((left,right))
        a,b=rel.find('first-end-point').get('object-id'),rel.find('second-end-point').get('object-id')
        for left,right in pairs:
            if left not in fields or right not in fields or fields[left]['owner']!=a or fields[right]['owner']!=b:
                raise ValueError('Relationship key ownership is not exact')
        key={'first':a,'second':b,'left':fields[pairs[0][0]]['remote'],'right':fields[pairs[0][1]]['remote']}
        if len(pairs)>1:
            key['pairs']=[[fields[l]['remote'],fields[r]['remote']] for l,r in pairs]
        keys.append(key)
    return {'version':VERSION,'datasource_id':ds.get('name'),'server':connections[0].get('server',''),
            'root':graph['root'],'tables':tables,'fields':fields,'keys':keys}

def build_live(ds):
    """DM3: a live physical-join or custom-SQL datasource -> typed plan (v3_physical_binding)."""
    from v3_physical_binding import build_live as _b
    return _b(ds)

def compile_query(plan):
    if plan.get('version') in ('physical-join/1','custom-sql/1','live-union/1'):
        from v3_physical_binding import compile_query as _c
        return _c(plan)
    if plan.get('version')!=VERSION or len(plan['tables'])>32 or not 1<len(plan['tables'])<=32 or len(plan['fields'])>1000:
        raise ValueError('Invalid bounded relationship plan')
    for t in plan['tables'].values():
        if not FQN.fullmatch(t['table']) or t['table']!=t['table'].upper():raise ValueError('Invalid source table')
    rels=[{'first':k['first'],'second':k['second'],'lkey':k['left'],'rkey':k['right'],
           'pairs':[tuple(p) for p in k.get('pairs') or [[k['left'],k['right']]]]} for k in plan['keys']]
    graph=join_plan(plan['tables'],rels)
    if graph['root']!=plan['root'] or len(graph['steps'])!=len(plan['tables'])-1:raise ValueError('Invalid tree coverage')
    aliases={graph['root']:'f'}
    for step in graph['steps']:aliases[step['rel']['second']]=step['alias']
    selected=[];outputs=set()
    for field in plan['fields'].values():
        output=field['output']
        if not IDENT.fullmatch(output) or output in outputs or field['owner'] not in aliases:raise ValueError('Invalid projected field')
        outputs.add(output);selected.append(aliases[field['owner']]+'.'+quote(field['remote'])+' AS '+output)
    if not selected:raise ValueError('No model columns')
    sql='SELECT '+', '.join(selected)+' FROM '+plan['tables'][graph['root']]['table']+' f'
    for step in graph['steps']:
        k=step['rel'];sql+=' LEFT JOIN '+_source(plan,k['second'])+' '+step['alias']+' ON '+' AND '.join(step['parent_alias']+'.'+quote(l)+' = '+step['alias']+'.'+quote(r) for l,r in k['pairs'])
    return sql

def _table_columns(plan,oid):
    """Every column of one related table the model reads: its projected fields and the keys it joins on."""
    cols=[f['remote'] for f in plan['fields'].values() if f['owner']==oid]
    for k in plan['keys']:
        pairs=[tuple(p) for p in k.get('pairs') or [[k['left'],k['right']]]]
        if k['second']==oid:cols+=[r for _l,r in pairs]
        if k['first']==oid:cols+=[l for l,_r in pairs]
    return list(dict.fromkeys(cols))

def _key_of(plan,oid):
    k=next(k for k in plan['keys'] if k['second']==oid)
    return [r for _l,r in (tuple(p) for p in k.get('pairs') or [[k['left'],k['right']]])]

def one_row_per_key(session,plan,oid,measures,unique_keys):
    """DM8: a related table whose key repeats. Tableau reads it at its own grain; at the fact grain that is exact
    only when every column the model reads is constant per key (read as one row per key) and none of its fields is
    aggregated as a measure. Marks the table, or raises with a code naming the table and the field."""
    t=plan['tables'][oid];name=t['table']
    used=[local for local,f in plan['fields'].items() if f['owner']==oid and local in (measures or ())]
    if used:
        raise ValueError('KEY_REPEATS_MEASURE: '+name+' repeats its key and '+', '.join(sorted(used))+
                         ' is aggregated as a measure (Tableau sums it at that table\'s own grain)')
    key=_key_of(plan,oid);rq=[quote(c) for c in key]
    nonnull=' AND '.join(c+' IS NOT NULL' for c in rq)
    cols=_table_columns(plan,oid)
    per=rows(session,'SELECT COUNT(*) AS N FROM (SELECT DISTINCT '+', '.join(quote(c) for c in cols)+
             ' FROM '+name+' WHERE '+nonnull+')')[0]['N']
    if per!=unique_keys:
        varying=[c for c in cols if c not in key and rows(session,
                 'SELECT COUNT(*) AS N FROM (SELECT '+', '.join(rq)+' FROM '+name+' WHERE '+nonnull+' GROUP BY '+
                 ', '.join(rq)+' HAVING COUNT(DISTINCT '+quote(c)+') > 1 OR (COUNT('+quote(c)+') > 0 AND '
                 'COUNT('+quote(c)+') < COUNT(*)))')[0]['N']]
        raise ValueError('KEY_REPEATS_VARYING: '+name+' repeats its key and '+
                         ', '.join(name+'.'+c for c in varying[:3])+' differs within a key '
                         '(more than one related row per fact row)')
    t['distinct']=True

def _source(plan,oid):
    """DM8: a related table whose key repeats but whose columns are constant per key is read as one row per key
    (validate() proved it); every other table is read as it is."""
    t=plan['tables'][oid]
    if not t.get('distinct'):
        return t['table']
    cols=', '.join(quote(c) for c in _table_columns(plan,oid))
    nonnull=' AND '.join(quote(c)+' IS NOT NULL' for c in _key_of(plan,oid))
    return '(SELECT DISTINCT '+cols+' FROM '+t['table']+' WHERE '+nonnull+')'

_CODES=(('Source account identity mismatch','SOURCE_ACCOUNT_MISMATCH'),
        ('Authored source account','SOURCE_ACCOUNT_MISMATCH'),
        ('Join key types differ','JOIN_KEY_TYPES_DIFFER'),
        ('Model physical field schema mismatch','FIELD_SCHEMA_MISMATCH'),
        ('Model binding does not match IR physical fields','FIELDS_DO_NOT_MATCH_WORKBOOK'),
        ('Fact row count changed','FACT_ROWS_CHANGED'),
        ('does not exist or not authorized','OBJECT_NOT_FOUND_OR_NOT_AUTHORIZED'),
        ('A complete single-fact relationship tree','NOT_A_SINGLE_FACT_TREE'),
        ('Only unfiltered logical relationship trees','FILTERED_OR_JOINED_TREE'),
        # R47: a file / extract datasource has no Snowflake connection to check: its data is loaded (or an
        # existing table bound) on the Data model page -- not a failed check
        ('Automatic binding requires exactly one authored Snowflake connection','DATA_NOT_IN_SNOWFLAKE'))

def reason_code(exc):
    """DM8: the code shown on the Data model page for a binding that did not pass (code-built text only)."""
    text=str(exc)
    m=re.match(r'([A-Z][A-Z_]{3,}):',text)
    if m:return m.group(1)
    return next((code for needle,code in _CODES if needle in text),'BINDING_CHECK_FAILED')

def validate(session,plan,ir=None,measures=None):
    if plan.get('version') in ('physical-join/1','custom-sql/1','live-union/1'):
        from v3_physical_binding import validate as _v
        return _v(session,plan,ir)
    if plan.get('source')=='loaded':
        # Release 21b: tables this accelerator loaded from the workbook's own data (v3_data_entities).
        from v3_data_entities import check
        return check(session,plan)
    sql=compile_query(plan)
    identity=rows(session,'SELECT CURRENT_ACCOUNT() AS LOCATOR, CURRENT_ACCOUNT_NAME() AS ACCOUNT_NAME, CURRENT_ORGANIZATION_NAME() AS ORG, CURRENT_REGION() AS REGION')[0]
    expected=(str(identity['ORG'])+'-'+str(identity['ACCOUNT_NAME'])).lower().replace('_','-')+'.snowflakecomputing.com'
    if plan['server'].lower().rstrip('.')!=expected:raise ValueError('Source account identity mismatch')
    schemas={oid:{r['NAME']:r['TYPE'] for r in rows(session,'DESCRIBE TABLE '+t['table'])} for oid,t in plan['tables'].items()}
    if ir is not None:
        fields={f['internal']:f for f in ir['fields'].values() if f['datasource_id']==plan['datasource_id'] and not f['is_calculation'] and f.get('datatype')!='table'}
        for internal,f in fields.items():
            if internal not in plan['fields'] or plan['fields'][internal]['output']!=to_phys(f['physical']) or plan['fields'][internal]['datatype']!=f['datatype']:raise ValueError('Model binding does not match IR physical fields')
    for f in plan['fields'].values():
        actual=schemas[f['owner']].get(f['remote'])
        if not actual or not compatible(f['datatype'],actual):raise ValueError('Model physical field schema mismatch')
    if measures is None and ir is not None:
        # numeric fields the workbook aggregates (Tableau computes them at their own table's grain)
        measures=[f['internal'] for f in ir['fields'].values() if f['datasource_id']==plan['datasource_id']
                  and not f.get('is_calculation') and f.get('role')=='measure' and f.get('datatype') in ('integer','real')]
    measures=set(measures or [])
    checks=[]
    for t in plan['tables'].values():
        t.pop('distinct',None)                       # decided again on every check, from the data
    for key in plan['keys']:
        pairs=[tuple(p) for p in key.get('pairs') or [[key['left'],key['right']]]]
        for kl,kr in pairs:
            if schemas[key['first']].get(kl)!=schemas[key['second']].get(kr):raise ValueError('Join key types differ')
        oid=key['second'];b=plan['tables'][oid]['table']
        rq=[quote(kr) for _kl,kr in pairs];lq=[quote(kl) for kl,_kr in pairs]
        nonnull=' AND '.join(c+' IS NOT NULL' for c in rq)
        # E04: a composite key is checked as a whole
        nn=('COUNT('+rq[0]+')') if len(rq)==1 else ('COUNT_IF('+nonnull+')')
        check=rows(session,'SELECT COUNT(*) AS N, '+nn+' AS NONNULL, COUNT(DISTINCT '+', '.join(rq)+') AS UNIQUE_KEYS FROM '+b)[0]
        edge={'table':b,'key_rows':check['N'],'null_keys':check['N']-check['NONNULL'],'distinct_keys':check['UNIQUE_KEYS']}
        if check['UNIQUE_KEYS']<check['NONNULL']:
            one_row_per_key(session,plan,oid,measures,check['UNIQUE_KEYS'])
            edge['read_as']='one row per key'
        a_src=_source(plan,key['first']) if key['first']!=plan['root'] else plan['tables'][key['first']]['table']
        on=' AND '.join('a.'+l+' = b.'+r for l,r in zip(lq,rq))
        # Tableau keeps every fact row: an unmatched key shows Null for the related fields (recorded, not refused)
        edge['unmatched_fact_keys']=rows(session,'SELECT COUNT(*) AS N FROM '+a_src+' a LEFT JOIN '+_source(plan,oid)+' b ON '+on+
                                         ' WHERE '+' AND '.join('a.'+l+' IS NOT NULL' for l in lq)+' AND b.'+rq[0]+' IS NULL')[0]['N']
        checks.append(edge)
    sql=compile_query(plan)                          # DM8: with the one-row-per-key reads decided above
    fact=rows(session,'SELECT COUNT(*) AS N FROM '+plan['tables'][plan['root']]['table'])[0]['N']
    count=rows(session,'SELECT COUNT(*) AS N FROM ('+sql+')')[0]['N']
    if fact!=count:raise ValueError('Fact row count changed')
    rows(session,'SELECT * FROM ('+sql+') LIMIT 0')
    return {'fact_rows':fact,'output_rows':count,'edges':checks,'grain':'fact','tableau_relationship_equivalence':'NOT_VERIFIED'}

def table_name(plan):
    compile_query(plan)
    return 'V3_MODEL.RUNTIME.M_'+hashlib.sha256(json.dumps(plan,sort_keys=True).encode()).hexdigest()[:24].upper()

def replace_bindings(sql,sources):
    # Only replace FROM/JOIN object references outside literals/quoted names.
    # The typed plan is recompiled, never treated as arbitrary supplied SQL.
    chunks=re.split(r"('(?:''|[^'])*'|\"(?:\"\"|[^\"])*\")",sql)
    for i in range(0,len(chunks),2):
        for source in sources.values():
            if source.get('model_plan'):
                plan=source['model_plan'];table=source['table']
                if plan.get('semantic_view'):
                    # Dashboard reads through the verified Snowflake Semantic View (v3_semantic).
                    from v3_semantic import bound_relation
                    query=bound_relation(plan)
                else:
                    query=compile_query(plan)
                chunks[i]=re.sub(r'\b(FROM|JOIN)(\s+)'+re.escape(table)+r'(?![A-Za-z_0-9.])',lambda m:m.group(1)+m.group(2)+'('+query+')',chunks[i],flags=re.I)
    return ''.join(chunks)
