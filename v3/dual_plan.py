"""Two explicitly overlaid aggregate axes on one validated calendar axis."""
import re
import xml.etree.ElementTree as ET
from scatter_plan import AGGS
from visual_plan_common import reasons as shared_reasons, finite


def build(spec,model):
    from worksheet_semantics import date_axis_plan
    enriched=composed_build(spec,model)
    if enriched is not None:return enriched
    errors=shared_reasons(spec,model);roles={};series=[];date=None
    ws=ET.fromstring(model['source_xml']);occ=model['occurrences']
    xs=[o for o in occ if o['channel']=='cols'];ys=[o for o in occ if o['channel']=='rows']
    if len(xs)!=1 or len(ys)!=2:
        errors.append('Dual axis needs one calendar column and exactly two row aggregates')
    else:
        x=xs[0];date=date_axis_plan(x)
        if not date or x.get('field_definition',{}).get('is_calculation') is not False or x['level']!='row':
            errors.append('Dual axis needs a physical calendar date')
        roles[x['id']]='calendar'
        expression=ws.findtext('./table/rows','')
        for y in ys:
            expression=expression.replace(y['token'],'F')
            if y['level']!='aggregate' or y['discrete'] or y['aggregation'] not in AGGS:
                errors.append('Dual axes must be continuous scalar aggregates')
            roles[y['id']]='series'
            owners=[p for p in model['panes'] if p['axes']=={'y-axis-name':y['token']}]
            if len(owners)!=1 or owners[0]['marks'] not in [['Bar'],['Line']]:
                errors.append('Each dual axis needs one explicit Bar or Line pane')
            else:series.append({'occurrence_id':y['id'],'pane_id':owners[0]['id'],'mark':owners[0]['marks'][0].lower()})
        if re.sub(r'[\s()]','',expression)!='F+F':errors.append('Dual axes require explicit plus-overlay shelf syntax')
    for p in model['panes']:
        if p['axes'] and not any(s['pane_id']==p['id'] for s in series):errors.append('Unplanned dual pane axis')
    if len([p for p in model['panes'] if not p['axes']])>1:errors.append('Ambiguous shared dual pane')
    for o in occ:
        if o.get('unresolved') or o.get('builtin') or o.get('datasource_id')!=spec.get('datasource_id') or o['level'] in {'table','unknown'}:
            errors.append('Unresolved dual occurrence '+o['id'])
        if o['channel']=='y-axis-name' and any(y['token']==o['token'] for y in ys):roles[o['id']]='selector'
        if o['id'] not in roles:errors.append('Unplanned dual '+o['channel']+' occurrence')
    if any('sort' in n.tag for n in ws.iter()):errors.append('Authored calendar ordering requires a separate contract')
    refs=[]
    for r in ws.findall('.//reference-line'):
        matches=[y for y in ys if y['token']==r.get('axis-column')==r.get('value-column')]
        if len(matches)!=1 or r.get('formula')!='average' or r.get('scope')!='per-table' or len(r):
            errors.append('Only mean-of-visible-aggregates reference lines on the owning axis are planned')
        else:refs.append(matches[0]['id'])
    if spec.get('panel') or spec.get('color') or ws.findall('.//trendline[@enabled="true"]'):
        errors.append('Dual panels, color partitions and trendlines require another plan')
    return {'status':'BLOCKED' if errors else 'READY','roles':roles,'date_axis':date,'series':series,'references':refs,'reasons':sorted(set(errors))}


def render(spec,where,engine):
    import altair as alt
    model=spec['worksheet_semantics'];plan=build(spec,model)
    if plan['status']!='READY':raise ValueError('; '.join(plan['reasons']))
    lookup={o['id']:o for o in model['occurrences']};date=plan['date_axis'];x=lookup[date['occurrence_id']]
    from visual_plan_common import physical as field_sql
    physical=field_sql(engine,spec,x);fn='DATE_PART' if date['mode']=='date-part' else 'DATE_TRUNC'
    expression=fn+"('"+date['part']+"', "+physical+')'
    selected=[expression+' AS D'];resolved=[]
    for i,series in enumerate(plan['series']):
        o=lookup[series['occurrence_id']]
        ref={'caption':o['caption'],'agg':o['aggregation'],'field_id':o['field_id']}
        measure=engine.resolve_measure(engine.tbl(spec),spec['name'],ref)
        if plan.get('fixed_measures'):
            proof=plan['fixed_measures'][o['id']]
            actual=engine.IR['fields'].get(proof['field_id'],{}).get('calculation',{}).get('sql')
            if not measure or actual!=proof['sql'] or measure['sql']!=proof['sql']:raise ValueError('Dual source translation changed')
        elif not measure or measure.get('window'):raise ValueError('Dual series is not a scalar aggregate')
        resolved.append(measure);selected.append(measure['sql']+' AS V'+str(i))
    if plan.get('fixed_measures'):
        from kpi_fixed_plan import compile_sql
        sql=compile_sql(', '.join(selected),engine.tbl(spec),where,engine,group_by='1')
    else:sql='SELECT '+', '.join(selected)+' FROM '+engine.tbl(spec)+' '+where+' GROUP BY 1'
    df=engine.q(sql+' ORDER BY D ASC LIMIT 10001')
    if len(df)>10000:raise ValueError('Dual axis exceeds 10000 calendar groups; no truncated plot is inferred')
    if df['D'].isna().any():raise ValueError('Null calendar keys require an explicit display policy')
    finite(df,['V0','V1'])
    # Release 27 (F3/F8): axes, scales, marks and reference lines as the worksheet XML authors them
    import axis_xml
    rules=axis_xml.parse(model['source_xml']);shared=axis_xml.synchronized(rules)
    kind='O' if date['discrete'] else 'T'
    angle=axis_xml.label_angle(rules,x.get('token'))
    x_axis=None if axis_xml.axis_hidden(rules,x.get('token')) else alt.Axis(
        labelOverlap=True,**({} if date['discrete'] else {'format':axis_xml.time_format(date['part'])}),
        **({'labelAngle':angle} if angle is not None else {}))
    import pandas as _pd
    # month NUMBERS (date part 'mn') -> Jan..Dec; a truncated month ('tmn') arrives as a date and keeps its own order
    month_names=bool(date['discrete'] and date['part'].upper()=='MONTH' and _pd.api.types.is_numeric_dtype(df['D']))
    if month_names:
        df['D']=df['D'].map(lambda m:['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][int(m)-1] if m==m else m)
    # CX2 rule (mark-sizing automatic + continuous date): a bar fills one date unit, as Tableau draws it
    bar_px=None
    if not date['discrete'] and any(se['mark']=='bar' for se in plan['series']) and axis_xml.automatic_mark_size(model['source_xml']):
        bar_px=axis_xml.date_bar_px(df['D'],date['part'],int(spec.get('_wpx') or 0)-60)
    xscale=alt.Scale(padding=bar_px/2+2) if bar_px else alt.Undefined
    xenc=alt.X('D:'+kind,scale=xscale,sort=(['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'] if month_names else 'ascending'),
               title=(axis_xml.axis_title(rules,x.get('token'),x['caption'])
                      if not date['discrete'] or axis_xml.field_labels_shown(rules,'cols') else None),axis=x_axis)
    layers=[];shown_axis=False
    lo=float(min(df['V0'].min(),df['V1'].min()));hi=float(max(df['V0'].max(),df['V1'].max()))
    for i,series in enumerate(plan['series']):
        o=lookup[series['occurrence_id']];value='V'+str(i)
        authored=next((r for r in spec.get('ys',[]) if r.get('field_id')==o['field_id'] and r.get('agg')==o['aggregation']),{})
        color=authored.get('color') or ['#4e79a7','#f28e2b'][i]
        token=o.get('token');fmt=engine.IR['fields'].get(o.get('field_id'),{}).get('format')
        if shared:
            # one scale, one axis: every layer carries the same axis (Vega-Lite merges them; a None would win)
            first=next(lookup[se['occurrence_id']] for se in plan['series'] if not axis_xml.axis_hidden(rules,lookup[se['occurrence_id']].get('token')))                 if any(not axis_xml.axis_hidden(rules,lookup[se['occurrence_id']].get('token')) for se in plan['series']) else None
            ffmt=engine.IR['fields'].get(first.get('field_id'),{}).get('format') if first else None
            hidden=first is None;fmt_axis=ffmt
        else:
            hidden=axis_xml.axis_hidden(rules,token);fmt_axis=fmt
        axis=None if hidden else alt.Axis(orient='left' if (shared or not shown_axis) else 'right',grid=False,
                                          **({'format':axis_xml.d3(fmt_axis)} if axis_xml.d3(fmt_axis) else {}))
        shown_axis=shown_axis or not hidden
        scale=alt.Scale(zero=axis_xml.include_zero(rules,token),**({'domain':[min(0,lo) if axis_xml.include_zero(rules,token) else lo,hi]} if shared else {}))
        # stack=None: Vega stacks bars from zero and forces zero into the domain, which overrode domain-expand='false'
        y=alt.Y(value+':Q',title=axis_xml.axis_title(rules,token,authored.get('label') or o['caption']),axis=axis,scale=scale,stack=None)
        base=alt.Chart(df).encode(x=xenc,y=y,tooltip=[alt.Tooltip('D:'+kind,title=x['caption']),alt.Tooltip(value+':Q',title=o['caption'])])
        layer=base.mark_bar(color=color,opacity=0.85,clip=not axis_xml.include_zero(rules,token),**({'size':bar_px} if bar_px else {})) if series['mark']=='bar' else base.mark_line(color=color,point=False)
        if series['occurrence_id'] in plan['references']:
            ref_xml=axis_xml.refline_for(rules,token);mean=float(df[value].mean())
            style=rules['refline_style'];rcolor=style.get('stroke-color','#898989')[:7]
            ref=alt.Chart(df).transform_aggregate(REF='mean('+value+')').mark_rule(color=rcolor,strokeDash=[5,3]).encode(y=alt.Y('REF:Q',scale=scale))
            label=axis_xml.refline_label(rules,ref_xml,mean,fmt)
            if label:
                ref=alt.layer(ref,alt.Chart(df).transform_aggregate(REF='mean('+value+')').mark_text(
                    align='right',baseline='bottom',dx=0,dy=-2,fontWeight='bold',color=style.get('color','#333333'),
                    text=label,x='width').encode(y=alt.Y('REF:Q',scale=scale)))
            layer=alt.layer(layer,ref)
        layers.append((series['mark']!='bar',layer))
    height=int(spec.get('_hpx') or 320)
    chart=alt.layer(*(x[1] for x in sorted(layers,key=lambda x:x[0]))).resolve_scale(y='shared' if shared else 'independent')
    # the zone holds the whole chart, axes included (Tableau never pushes axis labels out of the zone)
    chart=chart.properties(height=height,autosize=alt.AutoSizeParams(type='fit',contains='padding'))
    from dashboard_actions import chart as action_chart
    action_chart(chart,spec,'D',engine,use_container_width=True)


def composed_build(spec,model):
    """Shared Measure Names color with source-proved scalar/FIXED series."""
    occ=model['occurrences'];colors=[o for o in occ if o['channel']=='color']
    if len(model['panes'])!=3 or not colors or not all(o.get('builtin')==':Measure Names' for o in colors):return None
    from worksheet_semantics import date_axis_plan
    from kpi_fixed_plan import expression
    from semantic_layer import _objects,join_plan
    from init_workbook import parse_relationships
    errors=shared_reasons(spec,model);source=ET.fromstring(model['datasource_xml']);ws=ET.fromstring(model['source_xml'])
    graph=None
    try:
        objects=_objects(source);edges=parse_relationships(source,strict=True);graph=join_plan(objects,edges)
        if len(objects)<2 or len(graph['steps'])!=len(objects)-1:raise ValueError('Incomplete fact tree')
        for rel in source.findall('.//relationship'):
            expr=rel.find('expression')
            if expr is None or expr.get('op')!='=' or len(expr)!=2:raise ValueError('Unplanned relationship expression')
        if source.findall('.//filter'):raise ValueError('Source filter phase is unplanned')
    except Exception:errors.append('Composed dual needs a complete bound fact tree')
    else:
        errors=[r for r in errors if r not in {'Joins, relationships, custom SQL and source filters require a data-model plan','Multiple physical source identities require a data-model plan'}]
    xs=[o for o in occ if o['channel']=='cols'];ys=[o for o in occ if o['channel']=='rows'];roles={};series=[];proofs={};date=None
    if len(xs)!=1 or len(ys)!=2:errors.append('Composed dual needs one date and two series')
    else:
        x=xs[0];date=date_axis_plan(x)
        if not date or x['level']!='row' or x.get('field_definition',{}).get('is_calculation') is not False:errors.append('Composed dual needs a physical calendar date')
        roles[x['id']]='calendar';shelf=ws.findtext('./table/rows','')
        for y in ys:
            shelf=shelf.replace(y['token'],'F');internal=y.get('field_definition',{}).get('physical')
            ref={'caption':y['caption'],'field_id':y['field_id'],'internal':internal,'agg':y['aggregation']}
            proof=expression(spec,model,ref,y)
            if not proof:errors.append('Composed dual source aggregate cannot be proved')
            else:proofs[y['id']]=proof
            if y['discrete']:errors.append('Composed dual series must be continuous')
            owners=[p for p in model['panes'] if p['axes']=={'y-axis-name':y['token']}]
            if len(owners)!=1 or owners[0]['marks'] not in [['Bar'],['Line']]:errors.append('Composed dual needs exact Bar/Line owners')
            else:series.append({'occurrence_id':y['id'],'pane_id':owners[0]['id'],'mark':owners[0]['marks'][0].lower()})
            roles[y['id']]='series'
        if re.sub(r'[\s()]','',shelf)!='F+F':errors.append('Composed dual requires authored overlay syntax')
    for o in occ:
        if o['channel']=='color' and o.get('builtin')==':Measure Names':roles[o['id']]='series-color'
        elif o['channel']=='y-axis-name' and any(o['token']==y['token'] for y in ys):roles[o['id']]='selector'
        elif o['channel']=='tooltip' and any(o['field_id']==y['field_id'] and o['aggregation']==y['aggregation'] for y in ys):roles[o['id']]='other-series-tooltip'
        if o['id'] not in roles:errors.append('Unconsumed composed dual occurrence')
        if o.get('unresolved') and not o.get('builtin') or o.get('table_calculation_xml') or (o.get('datasource_id')!=spec.get('datasource_id') and not (o.get('builtin')==':Measure Names' and o['token']=='['+spec['datasource_id']+'].[:Measure Names]')):errors.append('Unresolved composed dual source')
    if any(p['axes'] and not any(s['pane_id']==p['id'] for s in series) for p in model['panes']):errors.append('Unplanned composed dual pane')
    if ws.findall('.//reference-line') or ws.findall('.//trendline[@enabled="true"]') or spec.get('panel'):errors.append('Composed dual annotations need another plan')
    return {'status':'BLOCKED' if errors else 'READY','roles':roles,'date_axis':date,'series':series,'references':[],'reasons':sorted(set(errors)),'fixed_measures':proofs,'fact_tree':graph}
