"""Build isolated, hash-inventoried V3 candidates. No Snowflake mutations."""
import ast
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parent
TARGET = {'database':'WBR_DB', 'schema':'TABLEAU_ACCELERATOR_V3',
          'name':'TABLEAU_TO_SIS_V3_CORTEX'}   # Cortex lab: separate app; stable build untouched


def validate_bindings(ir, bindings):
    declared = set(ir.get('datasources', []))
    if not declared or set(bindings) != declared:
        raise ValueError('Candidate requires an explicit binding for every declared datasource')
    for cap, value in bindings.items():
        if isinstance(value, dict):
            from v3_model_binding import compile_query
            compile_query(value)
            if value.get('semantic_view'):
                from v3_semantic import bound_relation
                bound_relation(value)   # attached view must be the one generated from this plan
            if ir.get('field_scopes', {}).get(value.get('datasource_id'), {}).get('caption') != cap:
                raise ValueError('Candidate model binding datasource identity mismatch')
        elif not isinstance(value, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*\.[A-Za-z_][A-Za-z_0-9]*\.[A-Za-z_][A-Za-z_0-9]*', value):
            raise ValueError('Candidate bindings must be DATABASE.SCHEMA.OBJECT identifiers or typed relationship plans')
    return dict(bindings)


def validate_ask(bindings, ask):
    """Ask-the-data views for single-table datasources: each over exactly its bound table."""
    if not isinstance(ask, dict) or not ask:
        raise ValueError('Invalid Ask-the-data views')
    from v3_semantic import check_single
    for cap, entry in ask.items():
        binding = (bindings or {}).get(cap)
        if not isinstance(binding, str):
            raise ValueError('Ask-the-data view needs a table-bound datasource')
        check_single(entry, binding)
    return dict(ask)


def modules(entries, root=ROOT):
    """Include transitive local imports, including lazy imports, without data files."""
    pending=list(entries); result={}
    while pending:
        name=pending.pop()
        if name in result: continue
        path=root/name
        if not path.is_file(): raise ValueError('Missing candidate module: '+name)
        payload=path.read_bytes()
        tree=ast.parse(payload.decode('utf-8-sig'),filename=name)
        result[name]=payload
        for node in ast.walk(tree):
            imports=([a.name.split('.')[0] for a in node.names] if isinstance(node,ast.Import) else
                     [node.module.split('.')[0]] if isinstance(node,ast.ImportFrom) and node.module else [])
            for module in imports:
                candidate=module+'.py'
                if (root/candidate).is_file() and candidate not in result: pending.append(candidate)
    return result


def manifest(main, files, target):
    # JSON is a YAML subset accepted by the CLI; no interpolation or glob upload.
    return json.dumps({'definition_version':2,'entities':{'v3_candidate':{
        'type':'streamlit','identifier':target,'stage':'V3_CANDIDATE_STAGE',
        'query_warehouse':'POWERHOUSE','runtime_name':'SYSTEM$WAREHOUSE_RUNTIME','main_file':main,'title':'Tableau accelerator V3 - Cortex lab',
        'external_access_integrations':['TABLEAU_ACCESS_INTEGRATION'],
        'secrets':{'tableau_pat':'WBR_DB.PUBLIC.TABLEAU_PAT_SECRET'},
        'artifacts':sorted(files)}}},indent=2).encode()


def archive(files,main,target,provenance):
    files=dict(files)
    files['environment.yml']=(ROOT/'environment_v3.yml').read_bytes()
    files['environment_v3.yml']=files['environment.yml']
    hashes={name:hashlib.sha256(data).hexdigest() for name,data in sorted(files.items())}
    build=hashlib.sha256(json.dumps(hashes,sort_keys=True).encode()).hexdigest()
    files['candidate_build.json']=json.dumps({'build_sha256':build,'files':hashes,
        'target':target,'provenance':provenance,'acceptance':'NOT_VERIFIED',
        'deployed':False},indent=2).encode()
    files['snowflake.yml']=manifest(main,files,target)
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        for name,payload in sorted(files.items()):
            info=zipfile.ZipInfo(name,date_time=(2026,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
            z.writestr(info,payload)
    return out.getvalue(),build


def workbench_bundle():
    return archive(modules(['v3_sis_app.py','v3_console.py','v3_app.py','v3_runtime.py']), 'v3_sis_app.py',TARGET,
                   {'kind':'migration-workbench','scope':'Migration console (original workflow) and classic workbench: Cloud/upload assessment, verified binding, preview and candidate export'})


def workbook_bundle(ir,bindings,ask=None,account=None):
    """account: the organization-account hostname the bindings were verified in (E14); the app refuses to run
    its bindings in any other account."""
    from ir_contract import require_valid_ir, assess_ir
    require_valid_ir(ir)
    if not ir.get('fields') or not ir.get('field_scopes'):
        raise ValueError('Standalone candidate requires scoped V3 metadata')
    bindings=validate_bindings(ir,bindings)
    ask=validate_ask(bindings,ask) if ask else None
    safe=deepcopy(ir)
    digest=hashlib.sha256(json.dumps(ir,sort_keys=True).encode()).hexdigest()
    safe['source_file']=digest+'.twb'  # never select a curated filename profile
    if account is not None and not re.fullmatch(r'[a-z0-9][a-z0-9\-]*\.snowflakecomputing\.com',str(account)):
        raise ValueError('Binding account must be an organization-account hostname')
    payload=json.dumps(dict({'ir':safe,'bindings':bindings},**({'ask':ask} if ask else {}),
                            **({'account':account} if account else {})),sort_keys=True)
    call="run_candidate(PAYLOAD['ir'], PAYLOAD['bindings'], PAYLOAD['ask']" if ask else "run_candidate(PAYLOAD['ir'], PAYLOAD['bindings']"
    call+=(", account=PAYLOAD['account'])" if account else ")")
    source=("import json\nfrom v3_runtime import run_candidate\n"
            "PAYLOAD = json.loads("+repr(payload)+")\n"+call+"\n")
    ast.parse(source)
    files=modules(['v3_runtime.py'])
    files['app.py']=source.encode()
    files['assessment.json']=json.dumps(assess_ir(safe),indent=2).encode()
    target=dict(TARGET,name='V3_WORKBOOK_'+hashlib.sha256(payload.encode()).hexdigest()[:16].upper())
    provenance={'kind':'standalone-workbook','ir_sha256':digest,'bindings':bindings,'tableau_parity':'NOT_VERIFIED'}
    if ask:
        provenance['ask_semantic_views']={cap:e['semantic_view'] for cap,e in ask.items()}
    return archive(files,'app.py',target,provenance)


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args(); data,build=workbench_bundle()
    with args.output.open('xb') as f:f.write(data)
    print(json.dumps({'output':str(args.output),'build_sha256':build,'deployed':False}))
