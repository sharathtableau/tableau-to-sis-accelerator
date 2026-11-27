"""Dashboard actions for scoped dashboards (E19).

Action links are parsed as data; never executed as URLs or SQL. Two paths:
  * the original bounded contract (one linked field on a bar / calendar chart, selected on the chart itself);
  * the general contract (E19): filter and highlight actions with any source sheet or every sheet of the
    dashboard, excluded targets, another target dashboard, several linked fields (explicit links, all fields,
    field captions), calendar parts and truncations, select / hover / menu activation. The source sheet gets a
    small selection control listing its own values (queried by code); targets are filtered by code-built
    predicates (filter) or keep every row with the matching marks emphasised (highlight).
"""
import hashlib,json,re,math
from urllib.parse import unquote
from field_identity import field_id

def extract(root,dashboards,fields):
 by_dash={d['name']:d for d in dashboards}
 for dash in dashboards:
  actions=[];sheets={s['name']:s for s in dash['sheets']}
  for node in root.findall('./actions/action'):
   source=node.find('source')
   if source is None or source.get('dashboard')!=dash['name']:continue
   record={'id':hashlib.sha256(__import__('xml.etree.ElementTree',fromlist=['tostring']).tostring(node)).hexdigest()[:24],
           'source':source.get('worksheet'),'target':dash['name'],'status':'BLOCKED'}
   try:
    activation=node.find('activation');command=node.find('command');link=node.find('link')
    if activation is None or activation.attrib!={'auto-clear':'true','type':'on-select'}:raise ValueError('Action activation/clear behavior requires another contract')
    if source.get('type')!='sheet' or source.get('worksheet') not in sheets:raise ValueError('Action source is unavailable')
    params=[(p.get('name'),p.get('value')) for p in command] if command is not None else []
    if command is not None and command.get('command')=='tsc:tsl-filter' and params==[('special-fields','all'),('target',dash['name'])] and link is None:
     sheet=sheets[source.get('worksheet')];model=sheet.get('worksheet_semantics',{})
     if model.get('projection',{}).get('status')!='MAPPED':raise ValueError('All-fields action needs a mapped source worksheet')
     groups=[o for o in model.get('occurrences',[]) if o['channel'] in ('cols','rows','lod','detail','color','text') and o['level']=='row' and o.get('field_id')]
     roles=model.get('projection',{}).get('bindings',{})
     groups=[o for o in groups if roles.get(o['id'])!='parameter-label']
     identities={o['field_id'] for o in groups}
     if len(identities)!=1:raise ValueError('All-fields action needs exactly one source grouping identity')
     fid=identities.pop();f=fields.get(fid);did=sheet.get('datasource_id')
     if not f or f.get('datasource_id')!=did or f.get('is_calculation'):raise ValueError('All-fields action grouping must be a physical source field')
     record.update(field_id=fid,datasource_id=did,caption=f['caption'],datepart=None,selection_kind='all-fields-single-group')
     if f.get('datatype') in ('date','datetime'):
      from worksheet_semantics import date_axis_plan
      calendars=[date_axis_plan(o) for o in groups]
      if not calendars or any(c is None for c in calendars) or any(c['mode']!=calendars[0]['mode'] or c['part']!=calendars[0]['part'] for c in calendars):raise ValueError('All-fields date action requires one calendar axis')
      if calendars[0]['mode']=='date-value' and calendars[0]['part']=='DAY':record['datepart']='day-value'
      elif calendars[0]['mode']=='date-part' and calendars[0]['part']=='MONTH':record['datepart']='month'
      else:raise ValueError('Calendar action grain requires another contract')
     record['status']='READY'
     actions.append(record);continue
    if command is None or command.get('command')!='tsc:tsl-filter' or params!=[('target',dash['name'])]:raise ValueError('Action target requires another contract')
    if link is None or any(link.get(k)!=v for k,v in {'delimiter':',','escape':'\\','include-null':'true','multi-select':'true','url-escape':'true'}.items()):raise ValueError('Action linking behavior requires another contract')
    expr=link.get('expression','');prefix='tsl:'+dash['name']+'?'
    if not expr.startswith(prefix):raise ValueError('Action link target differs')
    pair=expr[len(prefix):].split('~s0=')
    if len(pair)!=2:raise ValueError('Only one action link field is supported')
    target=unquote(pair[0]);selected=pair[1]
    raw=re.fullmatch(r'<\[([^\]]+)\]\.\[([^\]]+)\]~na>',selected)
    month=re.fullmatch(r'<MONTH\(\[([^\]]+)\]\.\[([^\]]+)\]\)~na>',selected)
    match=raw or month
    if not match:raise ValueError('Action expression is unsupported')
    did,internal=match.groups();fid=field_id(did,internal);f=fields.get(fid)
    expected=f'[{did}].[{internal}]' if raw else f'MONTH([{did}].[mn:{internal}:ok])'
    if target!=expected or not f:raise ValueError('Action source/target field identity differs')
    sheet=sheets[source.get('worksheet')]
    if sheet.get('datasource_id')!=did:raise ValueError('Cross-source action requires a mapping plan')
    if raw:
     axis=sheet.get('y') if sheet.get('orient')=='h' else sheet.get('x')
     if sheet.get('kind')!='bar' or not isinstance(axis,dict) or axis.get('field_id')!=fid:raise ValueError('Action needs a supported source chart axis')
    else:
     if sheet.get('kind')!='dual' or f.get('datatype') not in {'date','datetime'}:raise ValueError('Month action needs a calendar chart')
    calc=f.get('calculation')
    if f.get('is_calculation') and (not calc or calc.get('window') or calc.get('agg_ready')):raise ValueError('Action field must be a scalar row expression')
    record.update(status='READY',field_id=fid,datasource_id=did,caption=f['caption'],datepart='month' if month else None)
   except ValueError as exc:record['reason']=str(exc)
   if record['status']!='READY':
    try:
     general=_general(node,dash,by_dash,fields,record['id'])
    except ValueError as exc:
     record['reason']=str(exc)
    else:
     if general:
      actions.extend(general)
      continue
     record['reason']='Action links no field on any source sheet'
   actions.append(record)
  dash['actions']=actions
 incoming={}
 for dash in dashboards:
  for a in dash['actions']:
   if a.get('status')=='READY' and a.get('target') and a['target']!=dash['name']:
    incoming.setdefault(a['target'],[]).append(a)
 for dash in dashboards:
  dash['incoming_actions']=incoming.get(dash['name'],[])
  for sheet in dash['sheets']:
   sheet['dashboard_actions']=dash['actions']+dash['incoming_actions']
   sheet['action_dashboard']=dash['name']

def state_key(ir,action):
 material=[ir.get('source_file'),action]
 return 'v3_action_'+hashlib.sha256(json.dumps(material,sort_keys=True).encode()).hexdigest()

def selected_values(event,name,alias):
 if not isinstance(event,dict):raise ValueError('Invalid action selection event')
 rows=event.get('selection',{}).get(name,[])
 # Vega emits an empty object when a point selection is cleared.
 if rows=={}:return []
 if not isinstance(rows,list) or len(rows)>250:raise ValueError('Selection exceeds action limits')
 values=[]
 for row in rows:
  if not isinstance(row,dict) or alias not in row:raise ValueError('Selection lacks the linked field')
  value=row[alias]
  if value is not None and (type(value) not in (str,int,float,bool) or (isinstance(value,float) and not math.isfinite(value))):raise ValueError('Invalid action value')
  if isinstance(value,str) and (len(value)>1024 or '\\' in value or any(ord(c)<32 for c in value)):raise ValueError('Unsupported action text literal')
  if value not in values:values.append(value)
 return values

def chart(chart,sheet,alias,engine,**kwargs):
 actions=[a for a in sheet.get('dashboard_actions',[]) if a['status']=='READY' and a['source']==sheet['name'] and a.get('mode')!='control']
 if not actions:return engine.st.altair_chart(chart,**kwargs)
 if len(actions)!=1:raise ValueError('Multiple chart actions require an interaction plan')
 import altair as alt
 action=actions[0];key=state_key(engine.IR,action);widget=key+'_chart';name='linked_selection'
 def changed():
  engine.st.session_state[key]=selected_values(engine.st.session_state.get(widget,{}),name,alias)
 # Warehouse Streamlit rejects selection events on any layered chart,
 # including a bar with text labels. Preserve every visual layer and expose
 # an equivalent explicit multi-value action control on these charts.
 if isinstance(chart,alt.LayerChart) or action.get('selection_kind')=='all-fields-single-group':
  import pandas as pd
  frames=[]
  def visit(node):
   data=getattr(node,'data',None)
   if isinstance(data,pd.DataFrame) and alias in data:frames.append(data)
   layers=getattr(node,'layer',None)
   if isinstance(layers,list):
    for child in layers:visit(child)
  visit(chart)
  if not frames:raise ValueError('Action chart lacks its declared selection values')
  options=[]
  for frame in frames:
   for value in frame[alias].tolist():
    value=None if pd.isna(value) else value.item() if hasattr(value,'item') else value
    if value is not None and action.get('datepart')=='day-value':
     value=pd.Timestamp(value).date().isoformat()
    if value not in options:options.append(value)
  options=selected_values({'selection':{'s':[{'v':v} for v in options]}},'s','v')
  current=engine.st.session_state.get(key,[])
  # Keep previously selected values visible if another action removes them.
  for value in current:
   if value not in options:options.append(value)
  control=widget+'_values'
  import parameter_actions
  parameter_actions.register_default(engine.IR,engine.st,control,[])
  def selected():
   values=engine.st.session_state.get(control,[])
   engine.st.session_state[key]=selected_values({'selection':{'s':[{'v':v} for v in values]}},'s','v')
  rendered=engine.st.altair_chart(chart,**kwargs)
  label='Filter dashboard by '+action['caption']
  # Keep the explicit warehouse-compatible action available without adding a
  # permanently expanded filter panel beneath every authored sparkline.
  import contextlib
  # Tableau shows no control (you click the marks); keep only a small filter icon in the card corner.
  panel=engine.st.popover(':material/filter_alt:' + (' '+str(len(current)) if current else ''))
  # Stateless inspection hosts may not implement layout context managers.
  with panel if hasattr(panel, '__enter__') else contextlib.nullcontext():
   engine.st.multiselect(label,options,default=None if control in engine.st.session_state else current,key=control,on_change=selected,
                        help='Select one or more values. Remove all values to clear this dashboard action.')
  return rendered
 selection=alt.selection_point(name=name,fields=[alias],clear='dblclick',toggle='event.shiftKey')
 return engine.st.altair_chart(chart.add_params(selection),key=widget,on_select=changed,selection_mode=[name],**kwargs)

COMPACT_ACTION_CSS=("<style>[data-testid='stPopover']{display:flex;justify-content:flex-end;margin-top:-34px;"
 "position:relative;z-index:2}"
 "[data-testid='stPopover'] button,[data-testid='stPopoverButton']{min-height:0;padding:0 6px;width:auto;"
 "border:none;background:transparent;color:#8a9bab;box-shadow:none}"
 "[data-testid='stPopover'] button:hover,[data-testid='stPopoverButton']:hover{color:#3366ff}"
 "[data-testid='stPopover'] button [data-testid='stIconMaterial']{font-size:1.05rem}</style>")


def compact_controls(st):
 """Dashboard actions use a small filter icon in the corner of the source chart
 (hosted Streamlit cannot select layered charts). Keep it visually secondary."""
 st.markdown(COMPACT_ACTION_CSS,unsafe_allow_html=True)


def predicates(sheet,engine):
 clauses=general_predicates(sheet,engine)
 for action in sheet.get('dashboard_actions',[]):
  if action.get('mode')=='control':continue
  if action['status']!='READY' or action.get('datasource_id')!=sheet.get('datasource_id'):continue
  # Keep source marks available so a user can add values or clear the selection.
  if action['source']==sheet['name']:continue
  values=engine.st.session_state.get(state_key(engine.IR,action),[])
  if not values:continue
  values=selected_values({'selection':{'s':[{'v':v} for v in values]}},'s','v')
  field=engine.IR['fields'].get(action['field_id'])
  if not field or field['datasource_id']!=sheet['datasource_id']:raise ValueError('Stale action field identity')
  caption=field['caption']
  if field.get('is_calculation'):
   calc=field.get('calculation')
   if not calc or calc.get('agg_ready') or calc.get('window') or engine.CALCS.get(caption)!=calc:
    raise ValueError('Action calculation identity does not match the active field scope')
  elif engine.COLMAP.get(caption)!=field.get('physical') or caption in engine.CALCS:
   raise ValueError('Action physical identity does not match the active field scope')
  expr=engine.rdim(caption)
  if action['datepart']:
   if action['datepart']=='day-value':
    import datetime
    for v in values:
     if v is not None and (not isinstance(v,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',v)):raise ValueError('Invalid calendar action date')
     if v is not None:datetime.date.fromisoformat(v)
    expr="DATE_TRUNC('day', "+expr+')'
   elif action['datepart']=='month':
    if any(type(v) not in (int,float) or not 1<=v<=12 or int(v)!=v for v in values):raise ValueError('Invalid action month')
    expr="DATE_PART('month', "+expr+')'
   else:raise ValueError('Unplanned calendar action part')
  literals=[];has_null=False
  for v in values:
   if v is None:has_null=True
   elif isinstance(v,str):literals.append(("DATE '" if action.get('datepart')=='day-value' else "'")+v.replace("'","''")+"'")
   elif type(v) is bool:literals.append('TRUE' if v else 'FALSE')
   else:literals.append(str(v))
  parts=([expr+' IN ('+', '.join(literals)+')'] if literals else [])+([expr+' IS NULL'] if has_null else [])
  clauses.append('('+' OR '.join(parts)+')')
 return clauses


# --------------------------------------------------------------------------- E19: the general action contract
CONTINUOUS = {'tyr': 'YEAR', 'tqr': 'QUARTER', 'tmn': 'MONTH', 'tdy': 'DAY', 'my': 'MONTH', 'mdy': 'DAY'}
DISCRETE = {'yr': 'YEAR', 'qr': 'QUARTER', 'mn': 'MONTH', 'dy': 'DAY', 'wd': 'DAYOFWEEK'}
KINDS = {'tsc:tsl-filter': 'filter', 'tsc:brush': 'highlight'}
ACTIVATIONS = ('on-select', 'on-hover', 'on-menu')
MAX_FIELDS = 16
_SRC = re.compile(r'<(?:([A-Z]+)\()?\[(?:([^\]]+)\]\.\[)?([^\]]+)\]\)?~na>')


def _datepart(token):
 if token in (None, 'none', ''):return None
 if token in CONTINUOUS:return ['trunc', CONTINUOUS[token]]
 if token in DISCRETE:return ['part', DISCRETE[token]]
 raise ValueError(f'Calendar grain {token!r} of an action field requires another contract')


def _internal_name(token):
 """'[mn:Order Date:ok]' / 'Order Date' -> ('mn', 'Order Date')."""
 m = re.fullmatch(r'([a-z]+):(.+):[a-z]+', token)
 return (m.group(1), m.group(2)) if m else (None, token)


def _occurrence_fields(sheet, fields):
 """Row-level dimensions of a source sheet (Tableau's 'all fields'): [(field_id, datepart)]."""
 model = sheet.get('worksheet_semantics') or {}
 roles = (model.get('projection') or {}).get('bindings') or {}
 out = []
 for o in model.get('occurrences') or []:
  if o.get('channel') not in ('cols', 'rows', 'lod', 'detail', 'color', 'text', 'shape', 'size', 'label'):continue
  fid = o.get('field_id')
  if not fid or o.get('builtin') or roles.get(o.get('id')) == 'parameter-label':continue
  f = fields.get(fid)
  if not f:raise ValueError('Action field is not declared in the workbook')   # never drop a field Tableau passes
  if f.get('role') == 'measure' or o.get('aggregation') not in (None, 'none'):continue
  if o.get('level') not in ('row', 'unknown') and not f.get('is_calculation'):continue
  calc = f.get('calculation') or {}
  if f.get('is_calculation') and (calc.get('agg_ready') or calc.get('window')):continue      # an aggregate: not passed
  if f.get('is_calculation') and not calc:
   raise ValueError(f"Action field '{f.get('caption')}' is a calculation that was not translated")
  dp = _datepart(o.get('datepart')) if f.get('datatype') in ('date', 'datetime') else None
  if dp is None and f.get('datatype') == 'date':
   dp = ['trunc', 'DAY']
  elif dp is None and f.get('datatype') == 'datetime':
   raise ValueError('An exact date-time action value requires another contract')
  key = (fid, json.dumps(dp))
  if key not in {(a, json.dumps(b)) for a, b in out}:out.append((fid, dp))
 return out


def _entry(fields, source_fid, target_fid, dp):
 sf, tf = fields.get(source_fid), fields.get(target_fid)
 if not sf or not tf:raise ValueError('Action field is not declared in the workbook')
 calc = tf.get('calculation') or {}
 if tf.get('is_calculation') and (not calc or calc.get('window') or calc.get('agg_ready')):
  raise ValueError('Action field must be a scalar row expression')
 return {'source_field_id': source_fid, 'source_caption': sf['caption'], 'field_id': target_fid,
         'datasource_id': tf.get('datasource_id'), 'caption': tf['caption'], 'datepart': dp}


def _link_fields(link, target_dash, sheet, fields):
 expr = link.get('expression', '')
 head, sep, body = expr.partition('?')
 if not sep or unquote(head) != 'tsl:' + target_dash:raise ValueError('Action link target differs')
 out = []
 for part in body.split('&'):
  pair = part.split('~s0=')
  if len(pair) != 2:raise ValueError('Action link expression is unsupported')
  target = unquote(pair[0])
  tm = re.fullmatch(r'(?:([A-Z]+)\()?\[([^\]]+)\]\.\[([^\]]+)\]\)?', target)
  sm = _SRC.fullmatch(pair[1])
  if not tm or not sm:raise ValueError('Action link expression is unsupported')
  t_fn, t_ds, t_tok = tm.groups()
  s_fn, s_ds, s_tok = sm.groups()
  s_ds = s_ds or sheet.get('datasource_id')
  t_pre, t_name = _internal_name(t_tok)
  s_pre, s_name = _internal_name(s_tok)
  fn = s_fn or t_fn
  if fn and fn not in ('YEAR', 'QUARTER', 'MONTH', 'DAY'):raise ValueError(f'Action link function {fn} is unsupported')
  dp = ['part', fn] if fn else _datepart(s_pre or t_pre)   # [mn:Order Date:ok] carries its own grain
  out.append(_entry(fields, field_id(s_ds, s_name), field_id(t_ds, t_name), dp))
 return out


def _caption_fields(captions, sheet, fields):
 """'Customer Name' or 'MONTH(Order Date)' -> the field as this sheet shows it; [] when the sheet lacks one."""
 did = sheet.get('datasource_id')
 shown = _occurrence_fields(sheet, fields)
 out = []
 for raw in captions:
  m = re.fullmatch(r'([A-Z]+)\((.+)\)', raw.strip())
  fn, cap = (m.group(1), m.group(2)) if m else (None, raw.strip())
  fid = next((k for k, f in fields.items() if f.get('datasource_id') == did and f.get('caption') == cap), None)
  hits = [dp for k, dp in shown if k == fid and (fn is None or (dp and dp[1] == fn))]
  if fid is None or not hits:return []                  # this sheet does not show the field: not a source
  out.append(_entry(fields, fid, fid, hits[0]))
 return out


def _general(node, dash, by_dash, fields, base_id):
 """READY records (one per source sheet) for an action the original contract refuses; ValueError otherwise."""
 activation, command, link, source = node.find('activation'), node.find('command'), node.find('link'), node.find('source')
 if activation is None or activation.get('type') not in ACTIVATIONS:raise ValueError('Action activation is unsupported')
 if command is None or command.get('command') not in KINDS:raise ValueError('Action command is unsupported')
 params = {p.get('name'): p.get('value') for p in command}
 if set(params) - {'target', 'exclude', 'special-fields', 'field-captions'}:
  raise ValueError('Action parameters require another contract')
 if params.get('special-fields') not in (None, 'all'):
  raise ValueError('Action special fields require another contract')
 target = params.get('target') or dash['name']
 only = None
 if target not in by_dash:
  # a WORKSHEET target: Tableau filters that sheet (on the dashboard that shows it)
  host = next((d['name'] for d in [dash] + list(by_dash.values()) if any(s['name'] == target for s in d['sheets'])), None)
  if host is None:raise ValueError('Action target is not migrated')
  only, link_target, target = [target], target, host
 else:
  link_target = target
 exclude = [x for x in (params.get('exclude') or '').split(',') if x]
 sheets = {s['name']: s for s in dash['sheets']}
 if source is None or source.get('type') != 'sheet':raise ValueError('Action source is unsupported')
 if source.get('worksheet'):
  if source.get('worksheet') not in sheets:raise ValueError('Action source sheet is not on the dashboard')
  sources = [source.get('worksheet')]
 else:
  skip = {e.get('name') for e in source.findall('exclude-sheet')}
  sources = [n for n in sheets if n not in skip]
 out, self_only = [], False
 for name in sources:
  sheet = sheets[name]
  if only and only == [name]:
   self_only = True                                     # Tableau's deselect pattern: the sheet filters itself
   continue
  if link is not None:
   entries = _link_fields(link, link_target, sheet, fields)
  elif params.get('special-fields') == 'all':
   entries = [_entry(fields, fid, fid, dp) for fid, dp in _occurrence_fields(sheet, fields)]
  elif params.get('field-captions'):
   entries = _caption_fields([c for c in params['field-captions'].split(',') if c], sheet, fields)
  else:
   raise ValueError('Action names no fields')
  if not entries:
   continue
  if len(entries) > MAX_FIELDS:raise ValueError('Action links too many fields')
  rid = hashlib.sha256((base_id + '\x1f' + name).encode()).hexdigest()[:24]
  out.append({'id': rid, 'source': name, 'target': target, 'status': 'READY', 'mode': 'control',
              'kind': KINDS[command.get('command')], 'activation': activation.get('type'),
              'clear': 'all' if activation.get('auto-clear') == 'true' else 'keep', 'exclude': exclude,
              'fields': entries, 'caption': node.get('caption') or ' · '.join(e['caption'] for e in entries),
              **({'only': only} if only else {})})
 if not out:
  # every source shows none of the linked fields: in Tableau the action passes nothing (it changes no view)
  return [{'id': base_id, 'source': source.get('worksheet'), 'target': target, 'status': 'NO_OP', 'mode': 'control',
           'kind': KINDS[command.get('command')],
           'reason': ('The action targets only its own source sheet (Tableau\'s deselect pattern): no view changes'
                      if self_only else 'The source sheet shows none of the linked fields: Tableau passes nothing, '
                                        'no view changes')}]
 return out


def _applies(action, sheet):
 return (action.get('mode') == 'control' and action.get('status') == 'READY'
         and action.get('target') == sheet.get('action_dashboard') and sheet.get('name') not in action.get('exclude', [])
         and (not action.get('only') or sheet.get('name') in action['only']))


def _target_caption(entry, sheet, engine):
 """The caption the target sheet uses for an action field: the field itself, or the same caption in the target's
 datasource (Tableau matches related fields by name); None = the sheet does not have it."""
 fields = (engine.IR or {}).get('fields') or {}
 f = fields.get(entry['field_id'])
 if f and f.get('datasource_id') == sheet.get('datasource_id'):
  return f['caption']
 cap = entry['caption']
 return cap if any(g.get('datasource_id') == sheet.get('datasource_id') and g.get('caption') == cap
                   for g in fields.values()) else None


def _expr(caption, dp, engine):
 e = engine.rdim(caption)
 if not dp:return e
 kind, part = dp
 if kind == 'part':return f"DATE_PART('{part.lower()}', {e})"
 return f"CAST(DATE_TRUNC('{part.lower()}', {e}) AS DATE)"


def _literal(v, dp):
 if v is None:return None
 if dp and dp[0] == 'trunc':
  if not isinstance(v, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', v):raise ValueError('Invalid calendar action date')
  import datetime
  datetime.date.fromisoformat(v)
  return "DATE '" + v + "'"
 if dp and dp[0] == 'part':
  if type(v) not in (int, float) or int(v) != v:raise ValueError('Invalid calendar action part')
  return str(int(v))
 if isinstance(v, str):
  if len(v) > 1024 or '\\' in v or any(ord(c) < 32 for c in v):raise ValueError('Unsupported action text literal')
  return "'" + v.replace("'", "''") + "'"
 if type(v) is bool:return 'TRUE' if v else 'FALSE'
 if type(v) in (int, float) and math.isfinite(v):return repr(v)
 raise ValueError('Invalid action value')


def rows_state(engine, action):
 rows = engine.st.session_state.get(state_key(engine.IR, action), [])
 if not isinstance(rows, list) or len(rows) > 250:raise ValueError('Selection exceeds action limits')
 out = []
 for r in rows:
  if not isinstance(r, list) or len(r) != len(action['fields']):raise ValueError('Invalid action selection')
  out.append(r)
 return out


def general_predicates(sheet, engine):
 """Filter predicates on a target sheet from the general actions (in the target sheet's field scope)."""
 clauses = []
 for action in sheet.get('dashboard_actions', []):
  if not _applies(action, sheet) or action['kind'] != 'filter' or action['source'] == sheet.get('name'):continue
  rows = rows_state(engine, action)
  if not rows:continue
  caps = [_target_caption(e, sheet, engine) for e in action['fields']]
  if any(c is None for c in caps):continue                # the sheet does not have every linked field
  exprs = [_expr(c, e['datepart'], engine) for c, e in zip(caps, action['fields'])]
  ors = []
  for r in rows:
   ands = []
   for expr, e, v in zip(exprs, action['fields'], r):
    lit = _literal(v, e['datepart'])
    ands.append(expr + (' IS NULL' if lit is None else ' = ' + lit))
   ors.append('(' + ' AND '.join(ands) + ')')
  clauses.append('(' + ' OR '.join(ors) + ')')
 return clauses


def source_options(sheet, action, engine, where=''):
 """The values a person can pick on the source sheet: distinct rows of the linked source fields (code-built)."""
 T = engine.tbl(sheet)
 sel = []
 for i, e in enumerate(action['fields']):
  sel.append(_expr(e['source_caption'], e['datepart'], engine) + f' AS V{i}')
 df = engine.q(f"SELECT DISTINCT {', '.join(sel)} FROM {T} {where or ''} ORDER BY 1 LIMIT 250")
 out = []
 for row in df.itertuples(index=False):
  vals = []
  for v, e in zip(row, action['fields']):
   if v is None or (isinstance(v, float) and v != v):vals.append(None);continue
   if hasattr(v, 'item'):v = v.item()
   if e['datepart'] and e['datepart'][0] == 'trunc':v = str(v)[:10]
   elif e['datepart']:v = int(v)
   elif hasattr(v, 'isoformat'):v = v.isoformat()[:10]
   vals.append(v)
  out.append(vals)
 return out


def _label(row):
 return ' · '.join('(blank)' if v is None else str(v) for v in row)


def source_control(sheet, where_parts, engine):
 """The selection control on a source sheet of general actions (Tableau: click / hover / menu on the marks)."""
 actions = [a for a in sheet.get('dashboard_actions', []) if a.get('mode') == 'control' and a.get('status') == 'READY'
            and a['source'] == sheet.get('name')]
 if not actions:
  return
 import contextlib
 st = engine.st
 for action in actions:
  key = state_key(engine.IR, action)
  try:
   with engine.field_scope(sheet):
    try:
     import v3_sheet_plan as P
     where = P.sheet_where(sheet, engine, where_parts)
    except Exception:
     where = ''
    options = source_options(sheet, action, engine, where)
  except Exception as exc:
   engine.findings.record('WARNING', sheet.get('name'), 'action-source-values', type(exc).__name__)
   continue
  current = [r for r in st.session_state.get(key, []) if isinstance(r, list)]
  for r in current:
   if r not in options:options.append(r)
  labels = [_label(r) for r in options]
  control = key + '_rows'

  def changed(key=key, control=control, options=options, labels=labels, action=action):
   picked = st.session_state.get(control, [])
   st.session_state[key] = [options[labels.index(l)] for l in picked if l in labels]
   nav = getattr(engine, 'DASH_NAV', None)
   if picked and nav and action['target'] in nav[1] and action['target'] != sheet.get('action_dashboard'):
    st.session_state[nav[0]] = nav[1].index(action['target'])       # Tableau opens the target dashboard
  verb = 'Highlight' if action['kind'] == 'highlight' else 'Filter'
  where_to = '' if action['target'] == sheet.get('action_dashboard') else f" (opens {action['target']})"
  how = {'on-hover': ' — hover in Tableau', 'on-menu': ' — menu in Tableau'}.get(action['activation'], '')
  panel = st.popover(':material/' + ('highlight' if action['kind'] == 'highlight' else 'filter_alt') + ':'
                     + (' ' + str(len(current)) if current else ''))
  with panel if hasattr(panel, '__enter__') else contextlib.nullcontext():
   st.multiselect(f"{verb} by {' · '.join(e['source_caption'] for e in action['fields'])}{where_to}{how}",
                  labels, default=None if control in st.session_state else [_label(r) for r in current],
                  key=control, on_change=changed,
                  help='Pick one or more marks; remove them all to clear the action.')


def highlight_rows(sheet, engine):
 """[(fields, rows)] of the highlight actions that apply to this sheet with a selection."""
 out = []
 for action in sheet.get('dashboard_actions', []):
  if action.get('kind') != 'highlight' or action.get('status') != 'READY':continue
  if not (_applies(action, sheet) or action.get('source') == sheet.get('name')):continue
  rows = rows_state(engine, action)
  if rows:out.append((action['fields'], rows))
 return out


def _column(df, caption, dp):
 grain = 'none' if not dp else dp[1].lower() + ('_trunc' if dp[0] == 'trunc' else '')
 for c in (f'D|{grain}|{caption}', f'D|none|{caption}'):
  if c in getattr(df, 'columns', []):return c
 return None


def _norm(v):
 if v is None or (isinstance(v, float) and v != v):return None
 if hasattr(v, 'isoformat'):return v.isoformat()[:10]
 if isinstance(v, float) and v.is_integer():return str(int(v))
 return str(v)


def emphasize(chart, sheet, engine):
 """Highlight actions on a planned chart: every row stays; marks that match the selection keep full opacity, the
 rest are dimmed (Tableau's highlight). Charts without the linked columns are returned unchanged."""
 hits = highlight_rows(sheet, engine)
 if not hits or chart is None:return chart
 import pandas as pd
 alt = engine.alt

 def mark(node):
  data = getattr(node, 'data', None)
  if isinstance(data, pd.DataFrame):
   for fields_, rows in hits:
    cols = [_column(data, e['caption'], e['datepart']) for e in fields_]
    if any(c is None for c in cols):continue
    wanted = {tuple(_norm(v) for v in r) for r in rows}
    data = data.copy()
    data['__HL'] = [tuple(_norm(v) for v in t) in wanted for t in zip(*[data[c] for c in cols])]
    node.data = data
    try:
     return node.encode(opacity=alt.condition('datum.__HL', alt.value(1.0), alt.value(0.2)))
    except Exception:
     return node
  layers = getattr(node, 'layer', None)
  if isinstance(layers, list):
   node.layer = [mark(x) for x in layers]
  return node
 return mark(chart)
