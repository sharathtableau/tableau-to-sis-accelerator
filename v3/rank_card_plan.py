"""Authored two-pane rank cards: category, measure and signed change."""
import re,xml.etree.ElementTree as ET
from kpi_fixed_plan import expression,compile_sql

def build(spec,model):
    if spec.get('kind')!='circle' or len(model['panes'])!=3:return None
    source=ET.fromstring(model['datasource_xml']);defs={c.get('name','').strip('[]'):c.find('calculation').get('formula','') for c in source.findall('./column') if c.find('calculation') is not None}
    ws=ET.fromstring(model['source_xml'])
    for dep in ws.findall('.//datasource-dependencies'):
        if dep.get('datasource')!=spec.get('datasource_id'):continue
        for c in dep.findall('./column'):
            calc=c.find('calculation')
            if calc is None:continue
            key=c.get('name','').strip('[]');formula=calc.get('formula','')
            if key in defs and defs[key]!=formula:return None
            defs[key]=formula
    occ=model['occurrences'];cols=[o for o in occ if o['channel']=='cols'];rows=[o for o in occ if o['channel']=='rows']
    if len(cols)!=2 or len(rows)!=2:return None
    def internal(o):return o.get('field_definition',{}).get('physical')
    indexes=[o for o in rows if re.fullmatch(r'\s*INDEX\s*\(\s*\)\s*',defs.get(internal(o),''),re.I)]
    dimensions=[o for o in rows if o['level']=='row' and o.get('field_definition',{}).get('is_calculation') is False]
    if len(indexes)!=1 or len(dimensions)!=1:return None
    if not all(re.fullmatch(r'\s*MIN\s*\(\s*0\s*\)\s*',defs.get(internal(o),''),re.I) for o in cols):return None
    errors=[];bindings={};dim=dimensions[0];index=indexes[0]
    if any(o.get('datasource_id')!=spec.get('datasource_id') or o.get('unresolved') for o in occ):errors.append('Rank card source ownership is incomplete')
    if any(p['marks']!=['Shape'] for p in model['panes']):errors.append('Rank card pane marks are not Shape')
    panes=[p for p in model['panes'] if p['axes']]
    if len(panes)!=2 or any(set(p['axes'])!={'x-axis-name'} for p in panes):errors.append('Rank card needs two independent placeholder columns')
    texts=[o for o in occ if o['channel']=='text'];shapes=[o for o in occ if o['channel']=='shape'];colors=[o for o in occ if o['channel']=='color']
    if len(texts)!=2 or len(shapes)!=1 or len(colors)!=1:errors.append('Rank card needs value, delta, one sign shape and one sign color')
    metric=delta=None;plans={}
    if len(texts)==2 and len(shapes)==len(colors)==1:
        delta=next((o for o in texts if o['pane_id']==shapes[0]['pane_id']==colors[0]['pane_id']),None)
        metric=next((o for o in texts if o is not delta),None)
        if not delta or not metric:errors.append('Signed change must own its pane')
        else:
            for channel in (shapes[0],colors[0]):
                pattern=r'\s*SIGN\s*\(\s*'+re.escape('['+internal(delta)+']')+r'\s*\)\s*'
                if not re.fullmatch(pattern,defs.get(internal(channel),''),re.I):errors.append('Shape/color must be exact SIGN of the change measure')
            for o in texts:
                ref={'field_id':o.get('field_id'),'caption':o['caption'],'internal':internal(o),'agg':o['aggregation']}
                plan=expression(spec,model,ref,o)
                if not plan:errors.append('Rank card aggregate dependencies require another plan')
                else:plans[o['id']]=plan
    ws=ET.fromstring(model['source_xml']);sorts=ws.findall('.//computed-sort')
    sort_plan=None;alphabetical=not sorts
    if alphabetical:
        if any('sort' in node.tag for node in source.iter()) or any(node.tag.endswith('sort') for node in ws.iter()):errors.append('Default category order is overridden')
    elif len(sorts)!=1 or sorts[0].get('column')!=dim['token'] or sorts[0].get('direction') not in ('ASC','DESC'):
        errors.append('Rank INDEX requires exact authored category/measure sorting')
    else:
        token=sorts[0].get('using','');match=re.search(r'\.\[usr:(.*):qk\]$',token)
        if match:
            name=match.group(1);field_id=spec.get('datasource_id')+'::'+__import__('urllib.parse',fromlist=['quote']).quote(name,safe='')
            item={'id':'sort-proof','channel':'text','caption':name,'field_id':field_id,'datasource_id':spec.get('datasource_id'),'aggregation':'usr','field_definition':{'physical':name},'table_calculation_xml':[],'unresolved':False}
            sort_plan=expression(spec,model,{'field_id':field_id,'caption':name,'internal':name,'agg':'usr'},item)
        if not sort_plan:errors.append('Rank sort aggregate requires another contract')
    if any(ET.fromstring(v).attrib!={'ordering-type':'Columns'} or len(ET.fromstring(v)) for v in index['table_calculation_xml']):errors.append('Rank addressing is not the authored category order')
    for o in occ:
        if o['id']==dim['id']:role='category'
        elif o['id']==index['id']:role='rank'
        elif o in cols:role='placeholder-column'
        elif o['channel']=='x-axis-name' and any(c['token']==o['token'] for c in cols):role='placeholder-selector'
        elif o in texts:role='aggregate-text'
        elif o in shapes:role='signed-change-shape'
        elif o in colors:role='signed-change-color'
        else:errors.append('Unconsumed rank card occurrence');continue
        bindings[o['id']]=role
    # Top-N is executed by the original source-aware filter helper. Accept only
    # its explicit authored simple aggregate ordering family on this category.
    top=[f for f in spec.get('applied_filters',[]) if f.get('kind')=='top_n']
    if len(top)!=1 or top[0].get('field_id')!=dim.get('field_id') or not isinstance(top[0].get('n'),int) or not 1<=top[0]['n']<=100:
        errors.append('Rank card requires one bounded authored category Top-N')
    if len(top)==1 and not (re.fullmatch(r'\[[^\]]+\]',top[0].get('order_expr','')) and top[0]['order_expr'][1:-1] in defs) and not re.fullmatch(r'\s*(COUNT|COUNTD|SUM|AVG|MIN|MAX)\s*\(\s*\[[^\]]+\]\s*\)\s*',top[0].get('order_expr',''),re.I):errors.append('Top-N sort formula requires another contract')
    return {'status':'REQUIRES_PLAN' if errors else 'MAPPED','renderer':'rank-card','bindings':bindings,'reasons':sorted(set(errors)),
            'dimension':dim['id'],'metric':metric['id'] if metric else None,'delta':delta['id'] if delta else None,'expressions':plans,
            'direction':sorts[0].get('direction') if len(sorts)==1 else None,'sort_expression':sort_plan,'alphabetical':alphabetical,
            'appearance_adaptation':'Authored signed shape represented with direction glyph; numeric/order acceptance separate'}

def render(spec,where,engine):
    model=spec['worksheet_semantics'];plan=build(spec,model)
    if plan!=model['projection'] or plan['status']!='MAPPED':raise ValueError('Rank card plan is stale or incomplete')
    occ={o['id']:o for o in model['occurrences']};dim=occ[plan['dimension']]
    from visual_plan_common import physical
    dimension=physical(engine,spec,dim);selected=dimension+' AS CATEGORY'
    for i,key in enumerate(('metric','delta')):
        proof=plan['expressions'][plan[key]];actual=engine.IR['fields'].get(proof['field_id'],{}).get('calculation',{}).get('sql')
        if actual!=proof['sql']:raise ValueError('Rank measure translation changed')
        selected+=', '+proof['sql']+' AS M'+str(i)
    proof=plan['sort_expression']
    if proof:
        actual=engine.IR['fields'].get(proof['field_id'],{}).get('calculation',{}).get('sql')
        if actual!=proof['sql']:raise ValueError('Rank sort translation changed')
        selected+=', '+proof['sql']+' AS SORT_VALUE'
    df=engine.q(compile_sql(selected,engine.tbl(spec),where,engine,group_by='1'))
    if df['CATEGORY'].isna().any():raise ValueError('Null category rank ordering needs an explicit contract')
    if plan['alphabetical']:
        if not df.CATEGORY.map(lambda v:isinstance(v,str) and v.isascii()).all():raise ValueError('Locale-dependent category sort requires a collation contract')
        if df.CATEGORY.str.casefold().duplicated().any():raise ValueError('Case-equivalent category ordering needs reference acceptance')
        df=df.sort_values('CATEGORY',key=lambda s:s.str.casefold()).reset_index(drop=True)
    else:
        df=df.sort_values(['SORT_VALUE','CATEGORY'],ascending=[plan['direction']=='ASC',True],na_position='last').reset_index(drop=True)
        if df.SORT_VALUE.dropna().duplicated().any():raise ValueError('Tied category rank order requires Tableau reference acceptance')
    df['RANK']=range(1,len(df)+1)
    metric=occ[plan['metric']];delta=occ[plan['delta']]
    def fmt(value,o):
        ref=next((m for m in spec.get('measures',[]) if m.get('field_id')==o.get('field_id')),{'caption':o['caption'],'agg':o['aggregation']})
        ref=dict(ref);ref['tableau_format']=engine.IR['fields'].get(o.get('field_id'),{}).get('format')
        return engine.fmt_tableau(value,ref,{'fmt':'pct' if o is delta else engine._fmt_guess(o['caption'])})
    df['VALUE']=df.M0.map(lambda v:fmt(v,metric));df['CHANGE']=df.M1.map(lambda v:fmt(v,delta))
    df['DIRECTION']=df.M1.map(lambda v:'▲' if v>0 else '▼' if v<0 else '—')
    palette=spec.get('color_measure',{}).get('scale',{}).get('colors',[])
    if not palette:raise ValueError('Rank sign color has no authored scale')
    negative,positive=palette[0],palette[-1]
    if spec.get('_presentation') == 'native':
        # Reuse the original rank table renderer on the independently validated
        # category, order, metric and signed delta, never the legacy SQL path.
        category=dim['caption'];label=re.sub(r'^(?:C|CP)\.', '', metric['caption'])
        display=df[['CATEGORY','VALUE']].rename(columns={'CATEGORY':category,'VALUE':label})
        display['Δ%']=[change+' '+direction if change != '—' else '—' for change,direction in zip(df.CHANGE,df.DIRECTION)]
        display.index=df.RANK
        engine._rank_html(display,category,{category:False,label:True,'Δ%':True})
        engine.findings.record('APPEARANCE',spec['name'],'rank-shape-adaptation',plan['appearance_adaptation'])
        return
    import html
    # Release 27 (F5): the card as the worksheet XML formats it -- the INDEX() rank column, header widths,
    # each field's label font and colour, the label runs' font for the values, and Tableau's row pitch
    # (default for the label size, squeezed so every Top-N row fits the zone as Tableau's fit does).
    style_fields=(spec.get('ws_style') or {}).get('fields') or {}
    def field_style(o):
        name=o.get('field_definition',{}).get('physical') or o.get('caption')
        return next((v for k,v in style_fields.items() if k.split(':',1)[-1]==name),{})
    index_occ=next((occ[i] for i,role in plan['bindings'].items() if role=='rank'),None)
    rank_st=field_style(index_occ) if index_occ else {};dim_st=field_style(dim)
    runs=[r for r in spec.get('label_runs') or [] if r.get('fontname') or r.get('fontcolor')]
    value_font=next((r.get('fontname') for r in runs if r.get('fontname')),None)
    value_color=next((r.get('fontcolor') for r in runs if r.get('fontcolor')),None)
    try:pt=float(dim_st.get('label.font-size') or (spec.get('ws_style') or {}).get('label',{}).get('font-size') or 9)
    except ValueError:pt=9.0
    pitch=round(pt*4/3*1.6)
    if spec.get('_hpx') and len(df):pitch=max(12,min(pitch,int(spec['_hpx'])//len(df)))
    def css(font,color):
        out=[]
        if font:out.append('font-family:'+engine._font_stack(font))
        if color:out.append('color:'+color)
        return ';'.join(out)
    def width(st_,default):
        try:return f"{int(float(st_.get('header.width')))}px"
        except (TypeError,ValueError):return default
    rows=[]
    for row in df.itertuples(index=False):
        category=html.escape(str(row.CATEGORY));value=html.escape(row.VALUE);change=html.escape(row.CHANGE)
        color=positive if row.M1>0 else negative if row.M1<0 else palette[len(palette)//2]
        rank_td=('<td class="rank-index" style="'+html.escape(css(rank_st.get('label.font-family'),rank_st.get('label.color')),quote=True)+'">'+str(row.RANK)+'</td>') if index_occ else ''
        rows.append('<tr>'+rank_td+'<td class="rank-category" title="'+category+'" style="'+html.escape(css(dim_st.get('label.font-family'),dim_st.get('label.color')),quote=True)+'">'+category+'</td><td class="rank-value" style="'+html.escape(css(value_font,value_color),quote=True)+'">'+value+'</td><td class="rank-change" style="color:'+html.escape(color,quote=True)+'">'+html.escape(row.DIRECTION)+' '+change+'</td></tr>')
    cols=(f'<col style="width:{width(rank_st,"10%")}">' if index_occ else '')+f'<col style="width:{width(dim_st,"40%")}"><col><col>'
    style=('<style>.v3-rank-card{width:100%;table-layout:fixed;border-collapse:collapse;font-size:'+f'{pt}pt'+'}'
           '.v3-rank-card td{padding:0 2px;height:'+f'{pitch}px;line-height:{pitch}px'+';border:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'
           '.v3-rank-card .rank-index{text-align:left}.v3-rank-card .rank-value,.v3-rank-card .rank-change{text-align:right}</style>')
    engine.st.markdown(style+'<table class="v3-rank-card" aria-label="'+html.escape(spec.get('title') or spec['name'],quote=True)+'"><colgroup>'+cols+'</colgroup>'+''.join(rows)+'</table>',unsafe_allow_html=True)
    engine.findings.record('APPEARANCE',spec['name'],'rank-shape-adaptation',plan['appearance_adaptation'])
