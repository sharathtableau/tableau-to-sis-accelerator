"""Measure Names/Values crosstabs with explicit occurrence columns and order."""
import json
import xml.etree.ElementTree as ET
from scatter_plan import AGGS
from visual_plan_common import reasons as shared_reasons


def candidate(model):
    return any(o.get('builtin') in {':Measure Names','Measure Names'} and o['channel']=='cols' for o in model['occurrences'])


def build(spec,model):
    errors=shared_reasons(spec,model);roles={};rows=[];measures=[];headers=[];containers=[]
    ws=ET.fromstring(model['source_xml'])
    if len(model['panes'])!=1 or model['panes'][0]['marks'] not in [['Text'],['Automatic']]:errors.append('Crosstab requires one Text/Automatic pane')
    for o in model['occurrences']:
        role=None
        if o.get('builtin') in {':Measure Names','Measure Names'} and o['channel']=='cols':headers.append(o);role='measure-headers'
        elif o.get('builtin') in {'Multiple Values','Measure Values'} and o['channel']=='text':containers.append(o);role='measure-container'
        elif not o.get('unresolved') and not o.get('builtin') and not o.get('datepart') and o.get('datasource_id')==spec.get('datasource_id'):
            if o['channel']=='rows' and o['level']=='row' and o['discrete']:rows.append(o);role='group'
            elif o['channel']=='text' and o['level']=='aggregate' and o['aggregation'] in AGGS and o.get('container_id'):measures.append(o);role='aggregate'
        if role:roles[o['id']]=role
        else:errors.append('Unplanned crosstab '+o['channel']+' occurrence '+o['id'])
    if len(headers)!=1 or len(containers)!=1 or len(rows)!=1 or not measures:
        errors.append('Crosstab requires one explicit row dimension, Measure Names header and expanded Measure Values')
    if any(o.get('container_id')!=(containers[0]['id'] if containers else None) for o in measures):errors.append('Ambiguous measure container')
    ordered=list(measures)
    manuals=ws.findall('.//manual-sort')
    if manuals:
        if len(manuals)!=1 or not headers or manuals[0].get('column')!=headers[0]['token'] or manuals[0].get('direction')!='ASC':errors.append('Unplanned manual crosstab ordering')
        else:
            try:
                manual=manuals[0]
                dictionaries=manual.findall('dictionary')
                # Unknown sort options/children cannot silently change ordering.
                if (set(manual.attrib)!={'column','direction'} or len(dictionaries)!=1
                        or len(manual)!=1 or dictionaries[0].attrib):raise ValueError()
                buckets=list(dictionaries[0])
                if not buckets or any(b.tag!='bucket' or b.attrib or len(b) for b in buckets):raise ValueError()
                tokens=[json.loads(b.text) for b in buckets]
                bytoken={o['token']:o for o in measures}
                if (any(not isinstance(t,str) for t in tokens) or len(bytoken)!=len(measures)
                        or len(set(tokens))!=len(tokens) or any(t not in bytoken for t in tokens)):raise ValueError()
                tail=[o for o in measures if o['token'] not in tokens]
                # One unlisted member has only one possible remainder order.
                # Multiple members need a proven collation/order contract; a
                # filter union's serialization order is not that contract.
                if len(tail)>1:
                    # An explicitly manual union records the complete chosen sequence.
                    # Ordinary set unions do not establish order. Require the manual
                    # prefix to agree and every occurrence to appear exactly once.
                    groups=[f.find('groupfilter') for f in ws.findall('.//filter')
                            if f.get('column')==headers[0]['token']]
                    group=groups[0] if len(groups)==1 else None
                    members=[]
                    if (group is not None and group.get('function')=='union'
                            and group.get('{http://www.tableausoftware.com/xml/user}op')=='manual'
                            and all(g.tag=='groupfilter' and g.get('function')=='member'
                                    and not len(g) for g in group)):
                        members=[json.loads(g.get('member','null')) for g in group]
                    if (len(members)!=len(measures) or any(not isinstance(t,str) for t in members)
                            or len(set(members))!=len(members) or set(members)!=set(bytoken)
                            or members[:len(tokens)]!=tokens):
                        errors.append('Partial Measure Names manual ordering requires a remainder-order contract')
                    else:
                        tail=[bytoken[t] for t in members[len(tokens):]]
                ordered=[bytoken[t] for t in tokens]+tail
            except (ValueError,TypeError):errors.append('Invalid measure header ordering')
    else:
        errors.append('Measure Names columns require explicit complete manual ordering')
    sorts=ws.findall('.//shelf-sort-v2');order=None
    if len(sorts)==1 and len(rows)==1:
        n=sorts[0];matches=[o for o in measures if o['token']==n.get('measure-to-sort-by')]
        if n.get('dimension-to-sort')==rows[0]['token'] and n.get('shelf')=='rows' and n.get('direction') in {'ASC','DESC'} and len(matches)==1:
            order={'occurrence_id':matches[0]['id'],'direction':n.get('direction')}
            if (n.get('is-on-innermost-dimension')=='true'
                    and rows[0].get('field_definition',{}).get('datatype') in {'integer','real'}):
                order['numeric_peer_direction']=n.get('direction')
    if order is None:errors.append('Crosstab requires one explicit visible-measure row sort')
    allowed={'manual-sort','shelf-sorts','shelf-sort-v2'}
    if any('sort' in n.tag and n.tag not in allowed for n in ws.iter()):errors.append('Unplanned crosstab sort clause')
    if spec.get('reflines') or spec.get('label_runs'):errors.append('Crosstab reference lines or composed labels require another plan')
    return {'status':'BLOCKED' if errors else 'READY','roles':roles,'rows':[o['id'] for o in rows],'measures':[o['id'] for o in ordered],'ordering':order,'limit':10000,'reasons':sorted(set(errors))}


def render(spec,where,engine):
    model=spec['worksheet_semantics'];plan=build(spec,model)
    if plan['status']!='READY':raise ValueError('; '.join(plan['reasons']))
    lookup={o['id']:o for o in model['occurrences']};selected=[];labels={};aliases={};fmts={};display={}
    for oid in plan['rows']+plan['measures']:
        o=lookup[oid];alias='O_'+oid.upper();aliases[oid]=alias
        if oid in plan['rows']:expr=engine.rdim(o['caption']);label=o['caption']
        else:
            m=engine.resolve_measure(engine.tbl(spec),spec['name'],{'caption':o['caption'],'agg':o['aggregation'],'field_id':o['field_id']})
            if not m or m.get('window'):raise ValueError('Crosstab measure is not a scalar aggregate')
            expr=m['sql'];label=o['caption']+' ('+o['aggregation'].upper()+')';fmts[alias]=m.get('fmt')
            display[alias]=tableau_measure_name(o)
        selected.append(expr+' AS '+alias);labels[alias]=label
        display.setdefault(alias,label)
    key=aliases[plan['ordering']['occurrence_id']];dim=aliases[plan['rows'][0]]
    peer=plan['ordering'].get('numeric_peer_direction')
    tie_check='0=1' if peer else '__TIES > 1'
    secondary=(', '+dim+' '+peer) if peer else ''
    grouped='SELECT '+', '.join(selected)+' FROM '+engine.tbl(spec)+' '+where+' GROUP BY 1'
    sql=('WITH grouped AS ('+grouped+'), checked AS (SELECT *, COUNT(*) OVER (PARTITION BY '+key+') AS __TIES FROM grouped) SELECT *, COUNT(*) OVER () AS __TOTAL, MAX(CASE WHEN '+tie_check+' OR '+key+' IS NULL OR '+dim+' IS NULL THEN 1 ELSE 0 END) OVER () AS __AMBIGUOUS FROM checked ORDER BY '+key+' '+plan['ordering']['direction']+secondary+' LIMIT '+str(plan['limit']))
    frame=engine.q(sql)
    if len(frame) and frame['__AMBIGUOUS'].max():raise ValueError('Crosstab row order has ties or nulls; an explicit tie/null policy is required')
    total=int(frame['__TOTAL'].iloc[0]) if len(frame) else 0
    if spec.get('_canvas'):
        # CX2 rule (F5): on a dashboard canvas the text table is drawn as Tableau draws it -- Measure Names
        # headers in Tableau's naming, every cell in its authored text-format, row banding, header height --
        # inside its zone (it scrolls there); the row count goes to the migration notes, not into the zone.
        engine.st.markdown(canvas_table(spec,model,plan,lookup,aliases,display,fmts,frame,engine),unsafe_allow_html=True)
        engine.findings.record('INFO',spec['name'],'crosstab-rows',f'{len(frame)} of {total} rows in authored measure order.')
    else:
        engine.st.dataframe(frame[list(labels)],column_config=labels,hide_index=True,use_container_width=True)
        engine.st.caption(f'{len(frame)} of {total} rows in authored measure order.')
    if total>len(frame):engine.st.warning(f'Display truncated to {len(frame)} of {total} rows after sorting.')


def tableau_measure_name(o):
    """Tableau's Measure Names member text for an occurrence: the ONE rule the sheet-plan tables already use
    (v3_sheet_plan.mv_name: workbook alias first, else 'Avg. Discount' / 'Count of Orders'; SUM and
    calculations show the caption)."""
    from v3_sheet_plan import mv_name
    return mv_name({'agg': o.get('aggregation'), 'caption': o.get('caption') or '', 'field_id': o.get('field_id')})


def _style(ws_style,el,attr,field=None):
    if field:
        return ((ws_style.get('fields') or {}).get(field) or {}).get(el+'.'+attr)
    return (ws_style.get(el+'|rows') or ws_style.get(el) or {}).get(attr)


def canvas_table(spec,model,plan,lookup,aliases,display,fmts,frame,engine):
    import html
    from tableau_format import format_number
    st_=spec.get('ws_style') or {};fields=(getattr(engine,'IR',None) or {}).get('fields',{})
    def key(o):
        t=o.get('token') or '';t=t[t.rfind('[')+1:].rstrip(']') if '[' in t else t
        return t.rsplit(':',1)[0] if t.count(':')>=2 else t
    band=_style(st_,'pane','band-color') or _style(st_,'header','band-color')
    try:band_size=max(1,int(_style(st_,'table','band-size') or 1))
    except ValueError:band_size=1
    header_h=None
    for f,v in (st_.get('fields') or {}).items():
        if v.get('header.height'):header_h=int(float(v['header.height']))
    cols=[];rows_dim=lookup[plan['rows'][0]]
    show_dim=not any(v=='false' for k,v in (st_.get('worksheet') or {}).items() if k=='display-field-labels')
    cols.append((aliases[plan['rows'][0]],rows_dim.get('caption','') if show_dim else '',None,'left'))
    for oid in plan['measures']:
        o=lookup[oid];a=aliases[oid]
        pattern=_style(st_,'cell','text-format',key(o)) or fields.get(o.get('field_id'),{}).get('format')
        cols.append((a,display[a],pattern,'right'))
    dim_align=_style(st_,'label','text-align',key(rows_dim)) or 'left'
    dim_px=_style(st_,'header','width',key(rows_dim))
    font_pt=_style(st_,'label','font-size',key(rows_dim)) or 8
    def cell(a,v,pattern):
        if a not in fmts:return html.escape(str(v))
        shown=format_number(v,pattern) if pattern else None
        if shown is None:
            try:integral=float(v).is_integer()
            except (TypeError,ValueError):integral=False
            # no authored format: Tableau's automatic format prints whole numbers without decimals
            shown=f'{float(v):,.0f}' if integral else engine.fmt_val(v,fmts[a] or 'num2')
        return html.escape(shown)
    hh=f'height:{header_h}px;' if header_h else ''
    head=''.join(f"<th style='text-align:{al if i else dim_align};{hh}{f'width:{dim_px}px;' if (i==0 and dim_px) else ''}'>{html.escape(t)}</th>"
                 for i,(a,t,p,al) in enumerate(cols))
    body=[]
    for r,row in enumerate(frame.itertuples(index=False)):
        rec=row._asdict()
        bg=f"background:{band[:7]};" if band and (r//band_size)%2==1 else ''
        body.append('<tr style="'+bg+'">'+''.join(
            f"<td style='text-align:{al if i else dim_align}'>{cell(a,rec[a],p)}</td>" for i,(a,t,p,al) in enumerate(cols))+'</tr>')
    h=int(spec.get('_hpx') or 300)
    return (f"<div class='v3-xtab' style='max-height:{h}px;overflow:auto'><table style='border-collapse:collapse;width:100%;"
            f"font-size:{float(font_pt)*4/3:.1f}px;color:#333'><thead style='position:sticky;top:0;background:#fff'>"
            f"<tr style='border-bottom:1px solid #d4d4d4'>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>"
            "<style>.v3-xtab th{font-weight:normal;padding:2px 6px;white-space:nowrap}.v3-xtab td{padding:1px 6px;"
            "white-space:nowrap;line-height:1.35}</style>")
