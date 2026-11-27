"""Bounded occurrence-to-query scatter contract; independent of display names."""
import xml.etree.ElementTree as ET

AGGS={'sum','avg','min','max','cnt','ctd','med','stdev','var','usr'}


def build(spec, model):
    from visual_plan_common import reasons as shared_reasons
    reasons=shared_reasons(spec, model);roles={};axes={};groups=[];encodings={}
    occurrences=model['occurrences']
    if len(model['panes'])!=1 or model['panes'][0]['marks'] not in [['Circle'],['Automatic']]:
        reasons.append('Scatter requires one Circle/Automatic pane')
    if any(spec.get(k) for k in ('blend','ydim','drill','table_calcs','forecast','subtotals','axis_flags','reflines','viz_tooltip')):
        reasons.append('Scatter has unplanned axis, layer, total or calculation semantics')
    if spec.get('trendline') and spec['trendline'].get('model')!='linear':
        reasons.append('Only linear scatter trendlines have a plan')
    source=ET.fromstring(model['datasource_xml']) if model.get('datasource_xml') else None
    if source is not None and any(n.tag in {'relationship','filter','initial-sql'} or
            (n.tag=='relation' and n.get('type') in {'join','union','text'}) for n in source.iter()):
        reasons.append('Scatter datasource joins, relationships and source filters need a plan')
    for o in occurrences:
        channel=o['channel'];role=None
        if o.get('unresolved') or o.get('builtin') or o.get('datepart') or o['level'] in {'unknown','table'} or o.get('datasource_id')!=spec.get('datasource_id'):
            reasons.append('Unresolved scatter occurrence '+o['id'])
        elif channel in {'cols','rows'} and o['level']=='aggregate' and not o['discrete'] and o['aggregation'] in AGGS:
            axis='x' if channel=='cols' else 'y'
            if axis in axes:reasons.append('Scatter requires one measure on each axis')
            axes[axis]=o['id'];role=axis
        elif channel in {'color','text','detail','lod'} and o['level']=='row' and o['discrete']:
            if o['field_id'] not in groups:groups.append(o['field_id'])
            if channel in {'color','text'} and channel in encodings:
                reasons.append('Repeated scatter visual channels require a composition plan')
            encodings[channel]=o['id'];role='group:'+channel
        elif channel in {'size','color','tooltip'} and o['level']=='aggregate' and o['aggregation'] in AGGS:
            if channel in encodings:reasons.append('Repeated scatter aggregate channels require a plan')
            encodings[channel]=o['id'];role='aggregate:'+channel
        elif channel=='tooltip' and o['aggregation'] in {'attr','none',None}:
            role='group:tooltip';encodings['tooltip']=o['id']
        if role:roles[o['id']]=role
        else:reasons.append('Unplanned scatter '+channel+' occurrence '+o['id'])
    lookup={o['id']:o for o in occurrences}
    for oid,role in roles.items():
        if role=='group:tooltip' and lookup[oid]['field_id'] not in groups:
            reasons.append('ATTR tooltip must reference an existing scatter grouping field')
    if set(axes)!={'x','y'}:reasons.append('Scatter requires two explicit continuous aggregate axes')
    captions={o['caption'] for o in occurrences if o['id'] in roles}
    for run in spec.get('label_runs',[]):
        refs={(o['field_id'],o['aggregation']) for o in occurrences if o.get('caption')==run.get('field')}
        if run.get('field') and len(refs)>1:
            reasons.append('Ambiguous label aggregation requires explicit occurrence references')
    ws=ET.fromstring(model['source_xml'])
    trends=ws.findall('.//trendline')
    trend=None
    if trends:
        if len(trends)!=1 or trends[0].get('fit')!='linear' or trends[0].get('exclude-intercept','false')!='false' or trends[0].get('enable-confidence-bands','false')!='false':
            reasons.append('Only linear regression with intercept and no confidence band is planned')
        elif trends[0].get('enabled')=='true':
            trend={'group_color':trends[0].get('exclude-color','false')=='false'}
    if any(r.get('field') and r['field'] not in captions for r in spec.get('label_runs',[])):
        reasons.append('Scatter label references an unplanned field')
    return {'status':'BLOCKED' if reasons else 'READY','roles':roles,'axes':axes,
            'trend':trend,'groups':groups,'encodings':encodings,'limit':1000,'reasons':sorted(set(reasons))}


def render(spec,where,engine):
    import altair as alt
    import pandas as pd
    model=spec['worksheet_semantics'];plan=build(spec,model)
    if plan['status']!='READY':raise ValueError('; '.join(plan['reasons']))
    lookup={o['id']:o for o in model['occurrences']}
    selects=[];alias={};captions={};formats={};group_alias=[]
    for fid in plan['groups']:
        o=next(o for o in model['occurrences'] if o.get('field_id')==fid and o['level']=='row')
        name='G'+str(len(group_alias));group_alias.append(name)
        selects.append(engine.rdim(o['caption'])+' AS '+name)
        for item in model['occurrences']:
            if item.get('field_id')==fid and plan['roles'].get(item['id'],'').startswith('group:'):
                alias[item['id']]=name
        captions[o['caption']]=name
    for oid,role in plan['roles'].items():
        if oid in alias:continue
        o=lookup[oid];name='M'+str(len(selects))
        resolved=engine.resolve_measure(engine.tbl(spec),spec['name'],{'caption':o['caption'],'agg':o['aggregation'],'field_id':o['field_id']})
        if not resolved or resolved.get('window'):raise ValueError('Scatter expression is not a scalar aggregate')
        selects.append(resolved['sql']+' AS '+name);alias[oid]=name
        captions[o['caption']]=name;formats[name]=resolved.get('fmt','float')
    table=engine.tbl(spec)
    group=' GROUP BY '+', '.join(group_alias) if group_alias else ''
    order=' ORDER BY '+', '.join(g+' ASC NULLS FIRST' for g in group_alias) if group_alias else ''
    df=engine.q('SELECT '+', '.join(selects)+' FROM '+table+' '+where+group+order+' LIMIT '+str(plan['limit']+1))
    if len(df)>plan['limit']:
        raise ValueError('Scatter exceeds 1000 marks; no truncated visualization or trendline is inferred')
    from visual_plan_common import finite
    finite(df,list(formats))
    axes={key:alias[oid] for key,oid in plan['axes'].items()}
    for name in axes.values():
        if df[name].isna().any() or not pd.api.types.is_numeric_dtype(df[name]):
            raise ValueError('Scatter axes contain null or nonnumeric values')
    # CX2 rules (release 27): axes, marks, legends and trend line as the worksheet XML authors them
    import axis_xml
    rules=axis_xml.parse(model['source_xml'])
    fields=(getattr(engine,'IR',None) or {}).get('fields',{})
    enc={}
    for key,name in axes.items():
        o=lookup[plan['axes'][key]];token=o.get('token')
        fmt=axis_xml.d3(fields.get(o.get('field_id'),{}).get('format'))
        ticks=axis_xml.axis_tick_count(int(spec['_wpx'])-60) if key=='x' and spec.get('_wpx') else None
        axis=None if axis_xml.axis_hidden(rules,token) else alt.Axis(grid=False,labelOverlap=True,
            **({'format':fmt} if fmt else {}),**({'tickCount':ticks} if ticks else {}))
        # domain-expand='false' fits the data; absent, Tableau includes zero (axis_xml.include_zero)
        enc[key]=(alt.X if key=='x' else alt.Y)(name+':Q',title=axis_xml.axis_title(rules,token,o['caption']),
            axis=axis,scale=alt.Scale(zero=axis_xml.include_zero(rules,token)))
    tips=[]
    seen=set()
    for oid,name in alias.items():
        if name in seen:continue
        seen.add(name);o=lookup[oid]
        title=o['caption']+(' ('+o['aggregation'].upper()+')' if name in formats else '')
        tips.append(alt.Tooltip(name+(':Q' if name in formats else ':N'),title=title))
    enc['tooltip']=tips
    for channel in ('color','size'):
        oid=plan['encodings'].get(channel)
        if not oid:continue
        name=alias[oid];quant=plan['roles'][oid].startswith('aggregate:')
        if channel=='size' and (df[name].isna().any() or (df[name]<0).any()):
            raise ValueError('Scatter size must be nonnegative and nonnull')
        # Tableau legends are dashboard zones: none is drawn inside a chart on a dashboard canvas (F14)
        extra={'legend':None} if spec.get('_canvas') else {}
        if channel=='color' and not quant:
            # F9 rule: the workbook's colour map, else Tableau 10 in member sort order (Tableau's default palette)
            members=sorted(str(v) for v in df[name].dropna().unique())
            dom,rng=engine.cat_colors(lookup[oid]['caption'],members)
            if not dom:
                pal=engine.TABLEAU_PALETTES['tableau-10'];dom,rng=members,[pal[i%len(pal)] for i in range(len(members))]
            df[name]=df[name].astype(str);extra['scale']=alt.Scale(domain=dom,range=rng)
        enc[channel]=(alt.Color if channel=='color' else alt.Size)(name+(':Q' if quant else ':N'),title=lookup[oid]['caption'],**extra)
    fill,fill_opacity,stroke=axis_xml.mark_style(rules)
    mark=dict(opacity=fill_opacity if rules['marks'] else 0.7)
    if stroke:mark.update(stroke=stroke,strokeWidth=1)
    if 'size' not in enc and axis_xml.circle_size(rules):mark['size']=axis_xml.circle_size(rules)
    if fill and 'color' not in enc:mark['color']=fill
    layers=[alt.Chart(df).mark_circle(**mark).encode(**enc)]
    text=plan['encodings'].get('text')
    if text:
        runs=spec.get('label_runs') or [{'field':lookup[text]['caption']}]
        def label(row):
            return ''.join((engine.fmt_val(row[captions[r['field']]],formats[captions[r['field']]]) if captions[r['field']] in formats else str(row[captions[r['field']]])) if r.get('field') else r.get('text','') for r in runs)
        df['LABEL']=df.apply(label,axis=1)
        layers.append(alt.Chart(df).mark_text(align='left',dx=8,dy=6).encode(x=enc['x'],y=enc['y'],text='LABEL:N'))
    if plan['trend'] and len(df)>1:
        group=[];trend_enc={'x':enc['x'],'y':enc['y']}
        color=plan['encodings'].get('color')
        # per-colour lines only where a colour group has two or more marks; when no group can be fitted Tableau
        # still draws one line over all marks (observed on Tableau Cloud: one mark per colour, one trend line)
        if (plan['trend']['group_color'] and color and plan['roles'][color].startswith('group:')
                and df.groupby(alias[color]).size().max()>=2):
            group=[alias[color]];trend_enc['color']=enc['color']
        tcolor,topacity,tdash,twidth=axis_xml.trend_line_style(rules)
        line=dict(strokeWidth=twidth,opacity=topacity,**({'strokeDash':tdash} if tdash else {}))
        if not group:line['color']=tcolor
        layers.append(alt.Chart(df).transform_regression(axes['x'],axes['y'],groupby=group).mark_line(**line).encode(**trend_enc))
    chart=alt.layer(*layers)
    # the zone holds the whole chart, axes included: a fixed 380px chart in a ~200px zone hid the lowest marks
    chart=(chart.properties(height=int(spec['_hpx']),autosize=alt.AutoSizeParams(type='fit',contains='padding'))
           if spec.get('_hpx') else chart.properties(height=380))
    engine.st.altair_chart(chart,use_container_width=True)
