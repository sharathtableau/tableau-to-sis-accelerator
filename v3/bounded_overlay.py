"""Source-proved duplicate-axis bars and Area/Line calendar overlays.

Reuse the original aggregate resolver, FIXED hoister and Tableau formatter.
Repeated pills describe one value drawn twice, never two independent measures.
"""
import re
import xml.etree.ElementTree as ET
from kpi_fixed_plan import expression, compile_sql


def definitions(model, datasource):
    result = {}
    roots = [ET.fromstring(model['datasource_xml']), ET.fromstring(model['source_xml'])]
    columns = list(roots[0].findall('./column'))
    columns += [c for d in roots[1].findall('.//datasource-dependencies')
                if d.get('datasource') == datasource for c in d.findall('./column')]
    for c in columns:
        calc = c.find('calculation')
        if calc is None:
            continue
        name = c.get('name', '').strip('[]'); formula = calc.get('formula', '')
        if name in result and result[name] != formula:
            raise ValueError('Worksheet and datasource calculations disagree')
        result[name] = formula
    return result


def tree_reasons(spec, model):
    from visual_plan_common import reasons
    errors = reasons(spec, model)
    ws = ET.fromstring(model['source_xml'])
    generated = [f for f in ws.findall('.//filter') if f.findall('./groupfilter[@function="crossjoin"]')]
    if generated:
        valid = True
        for f in generated:
            groups = f.findall('./groupfilter')
            if len(groups) != 1:
                valid = False; break
            g = groups[0]; action = g.get('{http://www.tableausoftware.com/xml/user}ui-action-filter')
            evidence = model.get('declared_filter_actions', {}).get(action)
            if not evidence or not all(n.tag == 'groupfilter' and n.get('function') == 'level-members' and len(n) == 0 for n in g):
                valid = False; break
            levels = [n.get('level', '').strip('[]') for n in g]
            if len(levels) != len(set(levels)) or set(levels) != set(evidence['group_fields']):
                valid = False; break
            defs = definitions(model, spec['datasource_id'])
            # Every extra all-fields key must be a proved parameter-only label.
            physical_count = 0
            for name in levels:
                if name in defs:
                    if not parameter_scalar({'field_definition': {'physical': name}}, model, defs):
                        valid = False
                else:
                    columns = ET.fromstring(model['datasource_xml']).findall('./column')
                    matches = [c for c in columns if c.get('name') == '[' + name + ']' and c.find('calculation') is None]
                    if len(matches) != 1:
                        valid = False
                    physical_count += 1
            if physical_count != 1:
                valid = False
        nongenerated = [g for f in ws.findall('.//filter') if f not in generated for g in f.iter('groupfilter') if g.get('function') not in {'union','member','except','level-members'}]
        if valid and not nongenerated:
            errors = [e for e in errors if e != 'Unplanned categorical filter operation']
    source = ET.fromstring(model['datasource_xml'])
    if source.findall('.//relationship'):
        try:
            from semantic_layer import _objects, join_plan
            from init_workbook import parse_relationships
            objects = _objects(source); graph = join_plan(objects, parse_relationships(source, strict=True))
            if len(graph['steps']) != len(objects) - 1:
                raise ValueError('Incomplete fact tree')
            for rel in source.findall('.//relationship'):
                node = rel.find('expression')
                if node is None or node.get('op') != '=' or len(node) != 2:
                    raise ValueError('Unplanned relationship expression')
            if source.findall('.//filter'):
                raise ValueError('Source filter phase needs another contract')
        except Exception:
            errors.append('A complete bound fact tree is required')
        else:
            errors = [e for e in errors if e not in {
                'Joins, relationships, custom SQL and source filters require a data-model plan',
                'Multiple physical source identities require a data-model plan'}]
    return errors


def parameter_scalar(occurrence, model, defs):
    """Only one declared parameter CASE with literal branches; no field access."""
    formula = defs.get(occurrence.get('field_definition', {}).get('physical'), '')
    match = re.fullmatch(r'\s*CASE\s+\[Parameters\]\.\[([^\]]+)\]\s+(.+?)\s+END\s*', formula, re.I | re.S)
    if not match or match[1] not in model.get('parameter_definitions', {}):
        return None
    branches = re.findall(r"WHEN\s+(-?\d+)\s+THEN\s+'((?:[^']|'')*)'", match[2], re.I)
    remainder = re.sub(r"WHEN\s+-?\d+\s+THEN\s+'(?:[^']|'')*'", '', match[2], flags=re.I)
    if not branches or remainder.strip() or len({b[0] for b in branches}) != len(branches):
        return None
    return {'parameter': match[1], 'branches': dict(branches)}


def build(spec, model):
    occ = model['occurrences']; cols = [o for o in occ if o['channel'] == 'cols']; rows = [o for o in occ if o['channel'] == 'rows']
    panes = model['panes']; marks = [p['marks'] for p in panes]
    bar = spec.get('kind') == 'mbar' and marks == [['Bar'], ['Bar'], ['GanttBar']]
    calendar = spec.get('kind') == 'dual' and marks == [['Area'], ['Line'], ['Area']]
    if not bar and not calendar:
        return None
    axes, dimensions, axis_name, shelf = (cols, rows, 'x-axis-name', 'cols') if bar else (rows, cols, 'y-axis-name', 'rows')
    if len(axes) != 2 or axes[0]['token'] != axes[1]['token'] or len(dimensions) != 1:
        return None
    metric = axes[0]; dim = dimensions[0]; ws = ET.fromstring(model['source_xml']); errors = tree_reasons(spec, model)
    defs = definitions(model, spec['datasource_id']); roles = {}; labels = {}; date = None
    if re.sub(r'[\s()]', '', ws.findtext('./table/' + shelf, '').replace(metric['token'], 'F')) != 'F+F':
        errors.append('Duplicate axes require explicit overlay shelf syntax')
    owners = [p for p in panes if p['axes'] == {axis_name: metric['token']}]
    if len(owners) != 2 or not any(p['attributes'].get('x-index' if bar else 'y-index') == '1' for p in owners):
        errors.append('Duplicate overlay needs explicit second-axis ownership')
    if dim['level'] != 'row' or dim.get('field_definition', {}).get('is_calculation') is not False:
        errors.append('Overlay grouping needs a physical source field')
    if calendar:
        from worksheet_semantics import date_axis_plan
        date = date_axis_plan(dim)
        if not date:
            errors.append('Calendar overlay needs an authored calendar grain')
    elif dim.get('datepart'):
        errors.append('Categorical bar cannot infer a calendar grain')
    ref = {'caption': metric['caption'], 'field_id': metric.get('field_id'), 'internal': metric.get('field_definition', {}).get('physical'), 'agg': metric['aggregation']}
    proof = expression(spec, model, ref, metric)
    if not proof:
        errors.append('Overlay aggregate dependency cannot be proved')
    for o in occ:
        role = None
        if o in axes:
            role = 'shared-metric'
        elif o is dim:
            role = 'calendar' if calendar else 'category'
        elif o['channel'] == axis_name and o['token'] == metric['token']:
            role = 'duplicate-axis-selector'
        elif bar and o['channel'] in ('text', 'tooltip'):
            if o['token'] == metric['token']:
                role = 'metric-label'
            else:
                scalar = parameter_scalar(o, model, defs)
                if scalar:
                    labels[o['id']] = scalar; role = 'parameter-label'
        elif calendar and o['channel'] == 'tooltip' and o.get('field_id') == dim.get('field_id') and o['aggregation'] == 'attr' and not o.get('datepart'):
            role = 'raw-date-attr'
        if role is None:
            errors.append('Unconsumed overlay occurrence: ' + o['channel'])
        else:
            roles[o['id']] = role
        if o.get('unresolved') or o.get('builtin') or o.get('table_calculation_xml') or o.get('datasource_id') != spec['datasource_id']:
            errors.append('Overlay field ownership or calculation is unplanned')
    sorts = [n for n in ws.iter() if 'sort' in n.tag]
    if bar:
        if len(sorts) != 1 or sorts[0].tag != 'computed-sort' or sorts[0].get('column') != dim['token'] or sorts[0].get('using') != metric['token'] or sorts[0].get('direction') not in ('ASC', 'DESC'):
            errors.append('Metric bar requires exact authored category/metric sorting')
    elif sorts:
        errors.append('Calendar ordering override requires another plan')
    if ws.findall('.//reference-line') or ws.findall('.//trendline[@enabled="true"]') or spec.get('panel'):
        errors.append('Overlay annotations or panels require another plan')
    return {'status': 'REQUIRES_PLAN' if errors else 'MAPPED', 'renderer': 'bounded-overlay', 'bindings': roles,
            'reasons': sorted(set(errors)), 'family': 'metric-bar' if bar else 'calendar-area-line',
            'dimension': dim['id'], 'metric': metric['id'], 'expression': proof, 'labels': labels,
            'date_axis': date, 'direction': sorts[0].get('direction') if bar and len(sorts) == 1 else None,
            'appearance_adaptation': 'Repeated measure drawn once with its authored bar/label or Area/Line layers'}


def render(spec, where, engine):
    import altair as alt
    from visual_plan_common import physical, finite
    model = spec['worksheet_semantics']; plan = build(spec, model)
    if plan != model['projection'] or plan['status'] != 'MAPPED':
        raise ValueError('Overlay plan is stale or incomplete')
    occ = {o['id']: o for o in model['occurrences']}; dim = occ[plan['dimension']]; metric = occ[plan['metric']]
    proof = plan['expression']; actual = engine.IR['fields'].get(proof['field_id'], {}).get('calculation', {}).get('sql')
    if actual != proof['sql']:
        raise ValueError('Overlay source translation changed')
    group = physical(engine, spec, dim); date = plan['date_axis']
    if date:
        group = ('DATE_PART' if date['mode'] == 'date-part' else 'DATE_TRUNC') + "('" + date['part'] + "', " + group + ')'
    selected = group + ' AS D, ' + proof['sql'] + ' AS V'
    attrs = [o for o in model['occurrences'] if plan['bindings'][o['id']] == 'raw-date-attr']
    if attrs:
        raw = physical(engine, spec, attrs[0])
        selected += ', MIN(' + raw + ') AS ATTR_MIN, MAX(' + raw + ') AS ATTR_MAX, COUNT(' + raw + ') AS ATTR_COUNT'
    df = engine.q(compile_sql(selected, engine.tbl(spec), where, engine, group_by='1') + ' ORDER BY D LIMIT 10001')
    if len(df) > 10000:
        raise ValueError('Overlay group limit exceeded; no silent truncation')
    if df.empty:
        engine.st.info('No data for current filters'); return
    if df['D'].isna().any():
        raise ValueError('Null overlay group needs an explicit policy')
    # Tableau SUM(IF...END) returns NULL for categories with no qualifying rows.
    # Preserve missing marks, never convert them to zero.
    if df['V'].notna().any():
        finite(df.loc[df.V.notna()], ['V'])
    ref = {'caption': metric['caption'], 'agg': metric['aggregation'], 'field_id': metric['field_id'], 'tableau_format': engine.IR['fields'][metric['field_id']].get('format')}
    resolved = engine.resolve_measure(engine.tbl(spec), spec['name'], ref)
    df['LABEL'] = df.V.map(lambda v: engine.fmt_tableau(v, ref, resolved))
    for oid, scalar in plan['labels'].items():
        param = model['parameter_definitions'][scalar['parameter']]['caption']
        value = str(engine.param_value(param)); value = str(int(float(value))) if re.fullmatch(r'-?\d+(\.0+)?', value) else value
        label = scalar['branches'].get(value)
        if label is None:
            raise ValueError('Parameter label has no authored branch')
        if occ[oid]['channel'] == 'text':
            df['LABEL'] = label + df['LABEL']
    color = spec.get('mark_color') or '#f4284e'
    tooltip = [alt.Tooltip('D:N', title=dim['caption']), alt.Tooltip('LABEL:N', title=metric['caption'])]
    if not date:
        df = df.sort_values(['V', 'D'], ascending=[plan['direction'] == 'ASC', True], na_position='last')
        order = df.D.tolist(); vmin = min(0., float(df.V.min())) if df.V.notna().any() else 0.; vmax = max(0., float(df.V.max())) if df.V.notna().any() else 1.
        span = max(vmax - vmin, 1.); fraction = min(.05 * df.LABEL.str.len().max() + .05, .55)
        scale = alt.Scale(domain=[vmin, vmax + span * fraction / (1 - fraction)])
        base = alt.Chart(df).encode(y=alt.Y('D:N', sort=order, title=None, axis=alt.Axis(labelOverlap=False, labelLimit=140)), x=alt.X('V:Q', scale=scale, axis=None, title=None), tooltip=tooltip)
        chart = alt.layer(base.mark_bar(color=color), base.mark_text(align='left', dx=3, color='#333333', clip=False).encode(text='LABEL:N')).properties(height=max(90, len(df) * 28))
    else:
        import pandas as pd
        if date['mode'] == 'date-part':
            df['D'] = df.D.astype('int64')
        elif date['discrete']:
            df['D'] = pd.to_datetime(df.D).dt.strftime('%Y-%m-%d')
        else:
            df['D'] = pd.to_datetime(df.D)
        dtype = 'O' if date['discrete'] else 'T'
        tooltip[0] = alt.Tooltip('D:' + dtype, title=dim['caption'])
        if attrs:
            df['ATTR'] = ['*' if a != b else str(a) if c else None for a, b, c in zip(df.ATTR_MIN, df.ATTR_MAX, df.ATTR_COUNT)]
            tooltip.append(alt.Tooltip('ATTR:N', title='ATTR(' + dim['caption'] + ')'))
        series = spec.get('ys') or []
        colors = {m.get('mark'): m.get('color') for m in series}
        base = alt.Chart(df).encode(x=alt.X('D:' + dtype, title=None, sort='ascending'), y=alt.Y('V:Q', title=metric['caption'], stack=None), tooltip=tooltip)
        chart = alt.layer(base.mark_area(color=colors.get('area') or color, opacity=.5), base.mark_line(color=colors.get('line') or color, point=True)).properties(height=170 if spec.get('_compact') else 260)
    from dashboard_actions import chart as action_chart
    action_chart(chart, spec, 'D', engine, use_container_width=True)
