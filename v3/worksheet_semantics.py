"""Occurrence inventory and conservative grain/renderer boundary for V3.

This is not a Tableau query compiler. Only the explicitly mapped subset may
enter legacy renderers; all other source structures remain inspectable.
"""
from collections import Counter
import hashlib
import json
import re
import xml.etree.ElementTree as ET

VERSION = 'worksheet-occurrences/3'
AGGREGATES = {'sum', 'avg', 'min', 'max', 'cnt', 'ctd', 'med', 'stdev', 'var'}
BUILTINS = {'Measure Names', ':Measure Names', 'Multiple Values', 'Measure Values', ':Measure Values',
            'Latitude (generated)', 'Longitude (generated)'}


def _id(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:24]


def _evidence_paths(root):
    paths = {}
    def walk(node, path):
        paths[id(node)] = path
        counts = Counter()
        for child in node:
            counts[child.tag] += 1
            walk(child, f'{path}/{child.tag}[{counts[child.tag]}]')
    walk(root, '.')
    return paths


def extract(ws, spec, meta, catalog, parser, parameters=None):
    """Keep positional order separately from identity; never key by captions.

    IDs are worksheet-local semantic IDs. Identical unnamed panes/occurrences
    have a duplicate ordinal; exchanging indistinguishable duplicates has no
    identity guarantee. Changing semantic shelf order changes position, not ID.
    """
    parameters = parameters or {}
    paths = _evidence_paths(ws)
    local = meta.for_datasource(spec.get('datasource_id'))
    instances = {}
    for dep in ws.findall('.//datasource-dependencies'):
        for ci in dep.findall('column-instance'):
            instances[(dep.get('datasource'), ci.get('name'))] = ci
    panes, occurrences, duplicates = [], [], Counter()
    raw = ET.tostring(ws, encoding='unicode')

    def add(token, channel, pane_id, node, position, selector=False):
        fld = parser.parse_field(token, local)
        names = re.findall(r'\[([^\]]+)\]', token)
        inner = names[-1] if names else token
        ci = instances.get((fld.get('ds'), '[' + inner + ']'))
        calc_xml = [ET.tostring(t, encoding='unicode') for t in ci.findall('.//table-calc')] if ci is not None else []
        entry = catalog.get(fld.get('field_id'), {})
        builtin = inner if inner in BUILTINS else None
        calc = entry.get('calculation', {})
        planned_sql = calc.get('sql', '')
        # __TBL__ is also used by scalar FIXED subqueries. It identifies a
        # source binding, not a post-aggregation table calculation. Genuine
        # windows remain blocked here until a dedicated execution plan exists.
        level = ('table' if calc_xml or calc.get('window') or fld.get('pct_total') or
                 re.search(r'\bOVER\s*\(', planned_sql, re.I) else
                 'aggregate' if fld.get('agg') in AGGREGATES | {'attr'} or calc.get('agg_ready') or fld.get('count_records') else
                 'unknown' if not entry and not builtin else 'row')
        # Physical measure on Text without explicit derivation is not guessed.
        identity = [pane_id, channel, token, selector]
        duplicate = duplicates[json.dumps(identity)]
        duplicates[json.dumps(identity)] += 1
        item = {'id': _id([identity, duplicate]), 'pane_id': pane_id,
                'channel': channel, 'position': position, 'token': token,
                'caption': fld['caption'] or ('Measure Values' if builtin == ':Measure Values' else fld['caption']),
                'aggregation': fld.get('agg'),
                'role': fld.get('kind'), 'discrete': fld.get('discrete', False),
                'datepart': fld.get('datepart'), 'level': level,
                'selector': selector, 'builtin': builtin,
                'source_path': paths[id(node)], 'table_calculation_xml': calc_xml}
        parameter = parameters.get(inner) if names == ['Parameters', inner] else None
        if parameter and channel in ('text','lod') and parameter.get('datatype') in ('integer', 'real', 'string', 'boolean', 'date', 'datetime'):
            item.update(parameter=parameter, level='row', unresolved=False)
        elif fld.get('field_id') in catalog:
            item.update(field_id=fld['field_id'], datasource_id=entry['datasource_id'])
            item['field_definition'] = {key: entry.get(key) for key in
                                        ('physical', 'datatype', 'is_calculation')}
            if entry.get('formula'):
                item['calculation_source'] = entry['formula']
        else:
            item['unresolved'] = not bool(builtin)
        occurrences.append(item)
        return item

    for shelf in ('cols', 'rows'):
        node = ws.find('./table/' + shelf)
        if node is not None:
            for index, token in enumerate(parser.TOKEN_RE.findall(node.text or '')):
                add(token, shelf, None, node, index)
    # Measure Values is a generated container, not a physical field. Expand only
    # explicit positive member lists; conditional/exclusion selection needs its
    # own planner. Do not infer members from all datasource declarations.
    selected_members = []
    selections = [f for f in ws.findall('.//filter') if (f.get('column') or '').endswith('.[:Measure Names]')]
    if len(selections) == 1:
        groups = selections[0].findall('groupfilter')
        if len(groups) == 1 and groups[0].get('function') == 'union' and len(groups[0]) and all(
                child.get('function') == 'member' and child.get('member') for child in groups[0]):
            selected_members = [(child, child.get('member').strip('\"\'')) for child in groups[0]]
    pane_counts = Counter()
    for index, pane in enumerate(ws.findall('.//panes/pane')):
        marks = [m.get('class', 'Automatic') for m in pane.findall('mark')]
        axes = {key: pane.get(key) for key in ('x-axis-name', 'y-axis-name') if pane.get(key)}
        encs = [(e.tag, e.get('column')) for e in pane.findall('./encodings/*') if e.get('column')]
        fingerprint = _id([sorted(marks), axes, sorted(encs)])
        ordinal = pane_counts[fingerprint]
        pane_counts[fingerprint] += 1
        pid = 'pane:' + (pane.get('id') or f'{fingerprint}:{ordinal}')
        panes.append({'id': pid, 'position': index, 'marks': marks or ['Automatic'],
                      'mark_authored': bool(marks),
                      'axes': axes, 'source_path': paths[id(pane)],
                      'attributes': dict(pane.attrib)})
        for axis, token in axes.items():
            add(token, axis, pid, pane, 0, selector=True)
        for i, enc in enumerate(pane.findall('./encodings/*')):
            if enc.get('column'):
                item = add(enc.get('column'), enc.tag, pid, enc, i)
                if enc.tag == 'text' and item['builtin'] in ('Multiple Values', 'Measure Values', ':Measure Values'):
                    for member_index, (node, token) in enumerate(selected_members):
                        expanded = add(token, 'text', pid, node, member_index)
                        expanded['container_id'] = item['id']
    # Keep all filter/sort/fiscal metadata verbatim, even before its compiler exists.
    evidence = []
    for node in ws.iter():
        if any(part in node.tag for part in ('filter', 'sort', 'fiscal')):
            evidence.append({'source_path': paths[id(node)], 'xml': ET.tostring(node, encoding='unicode')})
    model = {'version': VERSION, 'panes': panes, 'occurrences': occurrences,
             'source_xml': raw, 'query_metadata': evidence}
    model['worksheet_id'] = 'worksheet:' + _id(sorted(o['id'] for o in occurrences))
    for item in occurrences:
        item['worksheet_id'] = model['worksheet_id']
    model['layers'] = derive_layers(model)
    model['grain'] = derive_grain(model)
    return model


def derive_layers(model):
    """Declare inherited encodings without authorizing a layered renderer.

    A unique axis-free card alongside axis-specific cards is a shared-card
    candidate. Unknown channels/ambiguous selectors remain explicit blockers.
    Source IDs survive inheritance and overriding; no occurrence is deleted.
    """
    panes, occurrences = model['panes'], model['occurrences']
    shared = [p for p in panes if not p['axes']]
    children = [p for p in panes if p['axes']]
    common = shared[0] if len(shared) == 1 and children else None
    reasons, plans = [], []
    if children and len(shared) > 1:
        reasons.append('Multiple axis-free cards have ambiguous inheritance')
    exclusive = {'color', 'size', 'text', 'shape'}
    for pane in panes:
        if common and pane['id'] == common['id']:
            continue
        own = [o for o in occurrences if o['pane_id'] == pane['id']]
        inherited, shadowed = [], []
        if common:
            channels = {o['channel'] for o in own if not o['selector']}
            for item in occurrences:
                if item['pane_id'] != common['id']:
                    continue
                channel = item['channel']
                if channel in channels and channel in exclusive:
                    shadowed.append(item['id'])
                else:
                    inherited.append(item['id'])
                    if channel not in exclusive:
                        reasons.append(f'Inheritance for {channel!r} requires a channel policy')
        shelf = [o for o in occurrences if o['pane_id'] is None]
        excluded = []
        for axis, token in pane['axes'].items():
            channel = 'cols' if axis == 'x-axis-name' else 'rows'
            candidates = [o for o in shelf if o['channel'] == channel and o['token'] == token]
            if len(candidates) != 1:
                reasons.append(f'Axis selector on {pane["id"]} is missing or ambiguous')
                continue
            # An axis selector chooses a measure axis, not surrounding row facets.
            excluded.extend(o['id'] for o in shelf if o['channel'] == channel and
                            o['level'] == 'aggregate' and o['id'] != candidates[0]['id'])
        plans.append({'pane_id': pane['id'],
                      'shared_pane_id': common['id'] if common else None,
                      'marks': pane['marks'] if pane.get('mark_authored', True) or not common else common['marks'],
                      'active_occurrence_ids': [o['id'] for o in shelf if o['id'] not in excluded] +
                                               inherited + [o['id'] for o in own],
                      'inherited_occurrence_ids': inherited,
                      'shadowed_occurrence_ids': shadowed,
                      'excluded_axis_occurrence_ids': excluded})
    return {'status': 'REQUIRES_PLAN' if reasons else 'DECLARED_ONLY',
            'panes': plans, 'reasons': sorted(set(reasons))}


def derive_grain(model):
    """Declared grouping requirements; no claim of LOD/filter/window correctness."""
    plans = []
    for pane in derive_layers(model)['panes']:
        active = [o for o in model['occurrences'] if not o['selector'] and
                  o['id'] in pane['active_occurrence_ids']]
        groups = {}
        aggregate_ids, deferred, display = [], [], []
        for item in active:
            if item.get('parameter'):
                display.append(item['id'])
            elif item.get('builtin') in (':Measure Names', 'Measure Names', 'Multiple Values', 'Measure Values', ':Measure Values'):
                display.append(item['id'])
            elif item.get('unresolved') or item.get('builtin') or item['level'] in ('unknown', 'table'):
                deferred.append(item['id'])
            elif item['level'] == 'aggregate':
                aggregate_ids.append(item['id'])
            else:
                key = (item.get('field_id'), item.get('datepart'))
                group = groups.setdefault(key, {'field_id': key[0],
                                                'datasource_id': item.get('datasource_id'),
                                                'datepart': key[1], 'occurrence_ids': []})
                group['occurrence_ids'].append(item['id'])
        plans.append({'pane_id': pane['pane_id'], 'group_by': list(groups.values()),
                      'aggregate_occurrence_ids': aggregate_ids,
                      'display_occurrence_ids': display,
                      'deferred_occurrence_ids': deferred})
    return {'status': 'DECLARED_ONLY', 'panes': plans,
            'filter_order': 'NOT_COMPILED', 'relationships': 'NOT_COMPILED'}


CONTINUOUS_DATE_GRAINS = {'tyr': 'YEAR', 'tqr': 'QUARTER', 'tmn': 'MONTH', 'tdy': 'DAY'}
DISCRETE_DATE_PARTS = {'yr': 'YEAR', 'qr': 'QUARTER', 'mn': 'MONTH', 'dy': 'DAY'}


def date_axis_plan(item):
    """Date value uses truncation; date part extracts a component across years."""
    token = item.get('datepart')
    if token in CONTINUOUS_DATE_GRAINS:
        return {'occurrence_id': item['id'], 'mode': 'date-value',
                'part': CONTINUOUS_DATE_GRAINS[token], 'discrete': bool(item['discrete'])}
    if token in DISCRETE_DATE_PARTS and item.get('discrete'):
        return {'occurrence_id': item['id'], 'mode': 'date-part',
                'part': DISCRETE_DATE_PARTS[token], 'discrete': True}
    return None


def temporal_projection(spec, model):
    """Bind every pill to a one-series calendar-date renderer.

    Weeks, fiscal calendars and layered/stacked variants
    require separate contracts. A date token is never silently treated as month.
    """
    reasons, bindings, date_axis = [], {}, None
    occurrences = model['occurrences']
    kind = spec['kind']
    if len(model['panes']) != 1:
        reasons.append('Temporal adapter requires exactly one pane')
    allowed = {'line': {'Automatic', 'Line'}, 'area': {'Area'}, 'dtbar': {'Bar'}}[kind]
    if any(mark not in allowed for pane in model['panes'] for mark in pane['marks']):
        reasons.append('Temporal mark does not satisfy the renderer contract')
    if any(spec.get(k) for k in ('panel', 'drill', 'param_select', 'table_calcs',
                                'forecast', 'axis_flags', 'subtotals', 'blend')) or len(spec.get('ys') or []) > 1:
        reasons.append('Temporal panels, derived layers or dynamic grain require a query plan')
    context = model.get('temporal_context')
    if not isinstance(context, dict) or context.get('fiscal_attributes'):
        reasons.append('Fiscal date semantics require an explicit calendar plan')
    source = ET.fromstring(model['source_xml'])
    if source.findall('.//reference-line') or any(
            node.attrib != {'value': 'true'} or len(node) or (node.text or '').strip()
            for node in source.findall('.//aggregation')):
        reasons.append('Temporal reference lines or view aggregation require a separate contract')
    fixed_measure = None
    rows = [o for o in occurrences if o['channel'] == 'rows']
    if len(rows) == 1 and rows[0]['level'] == 'table' and not rows[0]['table_calculation_xml']:
        from kpi_fixed_plan import expression
        fixed_measure = expression(spec, model, spec.get('y') or {}, rows[0])
    maximum_color = None
    if kind == 'dtbar' and spec.get('color'):
        colors = [o for o in occurrences if o['channel'] == 'color']
        measures = [o for o in occurrences if o['channel'] == 'rows']
        if len(colors) == len(measures) == 1:
            c, y = colors[0], measures[0]
            internal = (spec.get('y') or {}).get('internal')
            ref = re.escape('[' + str(internal) + ']')
            pattern = r"\s*IF\s+" + ref + r"\s*=\s*WINDOW_MAX\s*\(\s*" + ref + r"\s*\)\s+THEN\s+'((?:''|[^'])*)'\s+ELSE\s+'((?:''|[^'])*)'\s+END\s*"
            match = re.fullmatch(pattern, c.get('calculation_source', ''), re.I)
            addressing = [ET.fromstring(v) for v in c['table_calculation_xml']]
            if (match and (y['level'] == 'aggregate' or fixed_measure) and c.get('datasource_id') == spec.get('datasource_id') and
                    all(n.attrib == {'ordering-type': 'Rows'} and not len(n) for n in addressing)):
                maximum_color = {'occurrence_id': c['id'], 'measure_id': y['id'],
                                 'maximum_label': match[1].replace("''", "'"), 'other_label': match[2].replace("''", "'"),
                                 'partition': 'entire-visible-calendar-series'}
    if kind in ('area', 'dtbar') and ((spec.get('color') and not maximum_color) or spec.get('labels')):
        reasons.append('Area/bar stacking and authored labels require an explicit renderer plan')

    def matches(item, ref, aggregate=False):
        if not isinstance(ref, dict): return False
        fid = ref.get('field_id') or spec.get('field_bindings', {}).get(ref.get('caption'))
        return bool(fid) and item.get('field_id') == fid and (
            not aggregate or item['aggregation'] == ref.get('agg'))

    for item in occurrences:
        slot = None
        channel = item['channel']
        maximum_occurrence = bool(maximum_color and maximum_color['occurrence_id'] == item['id'])
        fixed_occurrence = bool(fixed_measure and fixed_measure['field_id'] == item.get('field_id') and channel == 'rows')
        if (item.get('unresolved') or item.get('builtin') or (item['level'] in ('unknown', 'table') and not maximum_occurrence and not fixed_occurrence) or
                item.get('datasource_id') != spec.get('datasource_id')):
            reasons.append('Temporal field/calculation ownership or evaluation level is unsupported')
        definition = item.get('field_definition', {})
        if (channel == 'cols' and item['level'] == 'row' and matches(item, spec.get('x')) and
                date_axis_plan(item) is not None and
                definition.get('datatype') in ('date', 'datetime') and
                definition.get('is_calculation') is False and
                spec['x'].get('datepart') == item['datepart']):
            slot = 'x'
            date_axis = date_axis_plan(item)
        elif (channel == 'rows' and (item['level'] == 'aggregate' or fixed_occurrence) and not item['discrete'] and
              matches(item, spec.get('y'), True)):
            slot = 'y'
        elif channel == 'color' and maximum_occurrence:
            slot = 'color'
        elif (channel == 'color' and kind == 'line' and item['level'] == 'row' and
              not item.get('datepart') and matches(item, spec.get('color')) and
              spec['color'].get('kind') == 'dimension' and definition.get('is_calculation') is False):
            slot = 'color'
        elif (channel == 'text' and kind == 'line' and spec.get('labels') and
              item['level'] == 'aggregate' and matches(item, spec.get('y'), True)):
            slot = 'y:label'
        elif item['selector']:
            target = 'cols' if channel == 'x-axis-name' else 'rows'
            if any(o['channel'] == target and o['token'] == item['token'] for o in occurrences):
                slot = 'axis-selector:' + target
        if slot:
            bindings[item['id']] = slot
        else:
            reasons.append(f'Unconsumed temporal {channel} occurrence {item["id"]}')
    slots = list(bindings.values())
    if date_axis and date_axis['discrete'] and any(
            'sort' in ET.fromstring(entry['xml']).tag.lower() for entry in model['query_metadata']):
        reasons.append('Authored discrete-date ordering requires a sort contract')
    if slots.count('x') != 1 or slots.count('y') != 1:
        reasons.append('Temporal adapter requires one planned calendar-date axis and one aggregate axis')
    if slots.count('color') != bool(spec.get('color')) or slots.count('y:label') != bool(spec.get('labels')):
        reasons.append('Temporal color/label slots must exactly match authored occurrences')
    return {'status': 'REQUIRES_PLAN' if reasons else 'MAPPED', 'renderer': kind,
            'bindings': bindings, 'date_axis': date_axis, 'maximum_color': maximum_color, 'fixed_measure': fixed_measure, 'reasons': sorted(set(reasons))}


def projection(spec, model):
    """Account for every occurrence before allowing a legacy rendering path."""
    if spec.get('kind') == 'planned' and spec.get('sheet_plan'):
        # Release 26: the exact plan consumes every occurrence (dimensions set the level of detail, measures
        # are aggregated, table calculations / blends follow the workbook); validated by v3_sheet_plan.validate
        return {'status': 'MAPPED', 'renderer': 'sheet-plan',
                'bindings': {o['id']: 'plan:' + str(o['channel']) for o in model['occurrences']}, 'reasons': []}
    from detail_record_plan import build as detail_build
    detail = detail_build(spec, model)
    if detail is not None:return detail
    from member_card_plan import build as member_build
    member = member_build(spec, model)
    if member is not None:return member
    from bounded_overlay import build as overlay_build
    overlay = overlay_build(spec, model)
    if overlay is not None:return overlay
    occurrences = model['occurrences']
    from decoration_plan import build as decoration_build
    decoration=decoration_build(spec,model)
    if decoration is not None:return decoration
    if spec.get('kind') == 'pie':
        from composed_pie import build as pie_build
        pie=pie_build(spec,model)
        if pie is not None:return pie
    if spec.get('kind') == 'circle':
        from percent_summary import build as percent_build
        percent=percent_build(spec,model)
        if percent is not None:return percent
        from rank_card_plan import build
        rank = build(spec, model)
        if rank is not None:
            return rank
    if spec.get('kind') == 'kpi' and any(o['level'] == 'table' for o in occurrences):
        from kpi_fixed_plan import build
        return build(spec, model)
    if spec.get('kind') == 'dual':
        from dual_plan import build
        plan = build(spec, model)
        return {'status': 'MAPPED' if plan['status'] == 'READY' else 'REQUIRES_PLAN',
                'renderer': 'dual', 'bindings': plan['roles'], 'reasons': plan['reasons']}
    from crosstab_plan import candidate as cross_candidate
    if spec.get('kind') == 'table' and cross_candidate(model):
        from crosstab_plan import build
        plan = build(spec, model)
        return {'status': 'MAPPED' if plan['status'] == 'READY' else 'REQUIRES_PLAN',
                'renderer': 'crosstab', 'bindings': plan['roles'], 'reasons': plan['reasons']}
    if spec.get('kind') == 'scatter':
        from logical_scatter import build as logical_build
        logical=logical_build(spec,model)
        if logical is not None:return logical
        from scatter_plan import build
        plan = build(spec, model)
        return {'status': 'MAPPED' if plan['status'] == 'READY' else 'REQUIRES_PLAN',
                'renderer': 'scatter', 'bindings': plan['roles'], 'reasons': plan['reasons']}
    if 'table_execution' in model:
        from table_plan import build
        plan = build(spec, model)
        return {'status': 'MAPPED' if plan['status'] == 'READY' else 'REQUIRES_PLAN',
                'renderer': 'text-table', 'bindings': plan['roles'], 'reasons': plan['reasons']}
    if 'pane_execution' in model:
        plan = build_pane_plan(spec, model)
        return {'status': 'MAPPED' if any(p['status'] == 'READY' for p in plan['panes']) else 'REQUIRES_PLAN',
                'renderer': 'independent-panes', 'bindings': {},
                'reasons': plan['reasons'] + [r for p in plan['panes'] for r in p['reasons']]}
    if spec.get('kind') in ('line', 'area', 'dtbar'):
        return temporal_projection(spec, model)
    reasons, bindings = [], {}
    kind = spec.get('kind')
    if kind not in {'bar', 'kpi', 'mbar'}:
        reasons.append(f'Occurrence adapter for {kind!r} is not implemented')
    if len(model['panes']) != 1:
        reasons.append('Multiple or absent panes require an explicit layer plan')
    text_card = (kind == 'kpi' and bool(spec.get('label_runs')) and len(model['panes']) == 1 and
                 not model['panes'][0]['axes'] and
                 all(o['channel'] == 'text' for o in occurrences))
    allowed_marks = {'Automatic', 'Bar'} if kind in ('bar', 'mbar') else {'Automatic', 'Text'}
    if text_card:
        allowed_marks.add('Shape')
        if 'Shape' in model['panes'][0]['marks']:
            model['appearance_adaptation'] = 'Unencoded Shape background rendered as composed text card'
    if any(mark not in allowed_marks for pane in model['panes'] for mark in pane['marks']):
        reasons.append('Authored mark does not satisfy the selected renderer contract')
    generated_kpi = (kind == 'kpi' and any(o.get('container_id') for o in occurrences))
    if any(o['level'] in ('table', 'unknown') or o.get('unresolved') or
           (o.get('builtin') and not generated_kpi) for o in occurrences):
        reasons.append('Unresolved, generated, or table-calculation occurrences require a query plan')
    if any(o.get('datasource_id') != spec.get('datasource_id') for o in occurrences if o.get('field_id')):
        reasons.append('Cross-datasource occurrences require a relationship plan')
    if any(o.get('datepart') for o in occurrences):
        reasons.append('Date-grain occurrences require a date-aware adapter')
    if spec.get('drill') or spec.get('param_select') or spec.get('table_calcs'):
        reasons.append('Dynamic grain or table calculations require a query plan')
    if kind == 'mbar':
        if spec.get('orient') != 'h' or not spec.get('dim'):
            reasons.append('Multi-measure adapter requires horizontal bars with one row dimension')
        if spec.get('color'):
            reasons.append('Multi-measure color requires an explicit stacking plan')
        if any(o['level'] == 'aggregate' and o['discrete'] for o in occurrences):
            reasons.append('Discrete aggregate headers require a separate adapter')

    def matches(item, ref, aggregate):
        if not isinstance(ref, dict):
            return False
        fid = ref.get('field_id') or spec.get('field_bindings', {}).get(ref.get('caption'))
        return (fid == item.get('field_id') and bool(fid) and
                (not aggregate or ref.get('agg') == item.get('aggregation')))

    horizontal = spec.get('orient') == 'h'
    dim_slot, measure_slot = ('y', 'x') if horizontal else ('x', 'y')
    for item in occurrences:
        slot = None
        channel = item['channel']
        if kind == 'kpi' and channel == 'text' and item.get('parameter'):
            bindings[item['id']] = 'parameter-text:' + item['parameter']['internal']
            continue
        if generated_kpi and item.get('builtin') in (':Measure Names', 'Measure Names') and channel in ('cols', 'rows'):
            bindings[item['id']] = 'measure-labels:' + channel
            continue
        if generated_kpi and item.get('builtin') in ('Multiple Values', 'Measure Values', ':Measure Values') and any(
                o.get('container_id') == item['id'] for o in occurrences):
            bindings[item['id']] = 'measure-values-container'
            continue
        if kind == 'bar':
            dim_channel, measure_channel = ('rows', 'cols') if horizontal else ('cols', 'rows')
            if channel == dim_channel and item['level'] == 'row' and matches(item, spec.get(dim_slot), False):
                slot = dim_slot
            elif channel == measure_channel and item['level'] == 'aggregate' and matches(item, spec.get(measure_slot), True):
                slot = measure_slot
            elif channel == 'color' and item['level'] == 'row' and matches(item, spec.get('color'), False):
                slot = 'color'
            elif channel == 'text' and spec.get('labels') and matches(item, spec.get(measure_slot), True):
                slot = measure_slot + ':label'
            elif channel == 'tooltip' and item.get('aggregation') in ('none', 'attr', None) and matches(item, spec.get(dim_slot), False):
                # ATTR of the exact GROUP BY expression is single-valued, including null.
                slot = dim_slot + ':tooltip'
            elif channel == 'text' and spec.get('labels') and item['level'] == 'row' and matches(item, spec.get(dim_slot), False):
                runs = spec.get('label_runs') or []
                allowed = {spec.get(dim_slot, {}).get('caption'), spec.get(measure_slot, {}).get('caption')}
                if runs and all(not r.get('field') or r['field'] in allowed for r in runs):
                    slot = dim_slot + ':label'
            elif item['selector']:
                # Axis references select an existing shelf occurrence, not a new group key.
                target = 'cols' if channel == 'x-axis-name' else 'rows'
                if any(o['channel'] == target and o['token'] == item['token'] for o in occurrences):
                    slot = 'axis-selector:' + target
        elif kind == 'mbar':
            if channel == 'rows' and item['level'] == 'row' and matches(item, {'caption': spec.get('dim')}, False):
                slot = 'dim'
            elif channel == 'cols' and item['level'] == 'aggregate':
                # Position matters: repeated fields with different aggregations
                # must map to the authored measure sequence, not a caption map.
                refs = spec.get('measures', [])
                i = item['position']
                if i < len(refs) and matches(item, refs[i], True):
                    slot = f'measures[{i}]'
        elif kind == 'kpi' and channel == 'text' and item['level'] == 'aggregate':
            for i, ref in enumerate(spec.get('measures', [])):
                if matches(item, ref, True):
                    slot = f'measures[{i}]'
                    break
        if slot:
            bindings[item['id']] = slot
        else:
            reasons.append(f'Unconsumed {channel} occurrence {item["id"]}')
    # The renderer must neither omit an occurrence nor synthesize a data axis.
    values = list(bindings.values())
    if kind == 'bar' and (values.count(dim_slot) != 1 or values.count(measure_slot) != 1):
        reasons.append('Bar requires exactly one dimension axis and one aggregate axis')
    if kind == 'kpi' and (not spec.get('measures') or len(spec['measures']) > 7 or any(
            values.count(f'measures[{i}]') != 1 for i in range(len(spec.get('measures', []))))):
        reasons.append('Every KPI measure must have an explicit text occurrence')
    if kind == 'kpi' and any(p['group_by'] for p in model['grain']['panes']):
        reasons.append('KPI has row-level dimensions and is not a scalar aggregate')
    if kind == 'mbar' and (values.count('dim') != 1 or len(spec.get('measures', [])) < 2 or any(
            values.count(f'measures[{i}]') != 1 for i in range(len(spec.get('measures', []))))):
        reasons.append('Every multi-measure axis and exactly one dimension must be consumed')
    return {'status': 'MAPPED' if not reasons else 'REQUIRES_PLAN',
            'renderer': kind, 'bindings': bindings, 'reasons': sorted(set(reasons))}


def build_pane_plan(spec, model):
    """Compile only independent physical aggregate bar axes; never infer overlays."""
    ws = ET.fromstring(model['source_xml'])
    occurrences = model['occurrences']
    lookup = {o['id']: o for o in occurrences}
    layers = derive_layers(model)
    reasons = list(layers['reasons'])
    source = ET.fromstring(model['datasource_xml']) if model.get('datasource_xml') else None
    relations = source.findall('.//relation') if source is not None else []
    if len(relations) != 1 or relations[0].get('type') != 'table':
        reasons.append('Exactly one physical table relation is required; no relationship/join inference')
    if source is not None and any(source.findall('.//' + tag) for tag in ('relationship', 'object-graph', 'filter')):
        reasons.append('Datasource relationships or filters need a query planner')
    if model['query_metadata'] or ws.findall('.//style'):
        reasons.append('Authored filters/sorts/fiscal or style/axis semantics require a plan')
    if ws.findall('.//aggregation'):
        reasons.append('Explicit view aggregation settings require a query contract')
    if any(spec.get(key) for key in ('blend', 'drill', 'param_select', 'table_calcs', 'forecast', 'subtotals')):
        reasons.append('Dynamic, blended or derived worksheet behavior is not supported by pane execution')
    shelves = {channel: sorted([o for o in occurrences if o['pane_id'] is None and o['channel'] == channel],
                              key=lambda o: o['position']) for channel in ('rows', 'cols')}
    dims, measures = shelves['rows'], shelves['cols']
    # Slash-separated axes are independent. Plus/union and product expressions
    # require a shelf algebra compiler and must not be guessed from pane count.
    expression = ws.findtext('./table/cols', '')
    for o in measures:
        expression = expression.replace(o['token'], 'F')
    expression = re.sub(r'[\s()]', '', expression)
    if (len(dims) != 1 or len(measures) < 2 or
            expression != '/'.join('F' for _ in measures) or
            len({o['token'] for o in measures}) != len(measures)):
        reasons.append('Independent axes require distinct slash-separated column pills and one row dimension')
    raw = {p['id']: p for p in model['panes']}
    selected = []
    for layer in layers['panes']:
        p = raw[layer['pane_id']]
        if set(p['axes']) != {'x-axis-name'} or any(k in p['attributes'] for k in ('x-index', 'y-index')):
            reasons.append('Pane axes must select a unique independent column; indexed/overlaid axes are unsupported')
        selected.append(p['axes'].get('x-axis-name'))
    if len(selected) != len(measures) or sorted(str(t) for t in selected) != sorted(o['token'] for o in measures):
        reasons.append('Every independent column axis must have exactly one pane')

    plans = []
    for layer in layers['panes']:
        pid = layer['pane_id']
        errors = list(reasons)
        active = [lookup[oid] for oid in layer['active_occurrence_ids']]
        roles, groups, aggregates = {}, [], []
        if layer['marks'] not in (['Bar'], ['Automatic']):
            errors.append('Pane mark requires another renderer contract')
        for o in active:
            definition = o.get('field_definition', {})
            if (o.get('unresolved') or o.get('builtin') or o.get('datepart') or
                    o.get('datasource_id') != spec.get('datasource_id') or
                    definition.get('is_calculation') is not False or not definition.get('physical')):
                errors.append(f'Occurrence {o["id"]} requires field/calculation/date planning')
            if o['selector']:
                roles[o['id']] = 'axis-selector'
            elif o['channel'] == 'rows' and o['level'] == 'row' and o['discrete']:
                roles[o['id']] = 'group'
                groups.append(o['id'])
            elif (o['channel'] == 'cols' and o['level'] == 'aggregate' and not o['discrete'] and
                  o['aggregation'] in {'sum', 'avg', 'min', 'max', 'cnt', 'ctd'} and
                  definition.get('datatype') in ('integer', 'real')):
                roles[o['id']] = 'aggregate'
                aggregates.append(o['id'])
            elif (o['channel'] == 'color' and o['level'] == 'row' and len(dims) == 1 and
                  o.get('field_id') == dims[0].get('field_id')):
                roles[o['id']] = 'dimension-color'
            else:
                roles[o['id']] = 'unsupported'
                errors.append(f'Unplanned {o["channel"]} occurrence {o["id"]}')
        if len(groups) != 1 or len(aggregates) != 1:
            errors.append('Pane requires exactly one discrete grouping field and one continuous aggregate')
        dispositions = {oid: role for oid, role in roles.items()}
        dispositions.update({oid: 'shadowed' for oid in layer['shadowed_occurrence_ids']})
        dispositions.update({oid: 'axis-excluded' for oid in layer['excluded_axis_occurrence_ids']})
        for o in occurrences:
            dispositions.setdefault(o['id'], 'other-pane')
        axis = lookup[aggregates[0]]['position'] if aggregates else raw[pid]['position']
        plans.append({'pane_id': pid, 'position': axis, 'status': 'BLOCKED' if errors else 'READY',
                      'reasons': sorted(set(errors)), 'renderer': 'horizontal-bar',
                      'datasource_id': spec.get('datasource_id'), 'group_by': groups,
                      'aggregates': aggregates, 'roles': roles, 'dispositions': dispositions,
                      'inherited': layer['inherited_occurrence_ids'],
                      'source_path': raw[pid]['source_path']})
    return {'version': 'independent-panes/1', 'worksheet_id': model['worksheet_id'],
            'reasons': sorted(set(reasons)), 'panes': sorted(plans, key=lambda p: p['position'])}


def render_panes(spec, where_parts, engine):
    """Execute validated occurrence roles without consulting legacy chart slots."""
    model = spec['worksheet_semantics']
    plan = build_pane_plan(spec, model)
    if plan != model['pane_execution']:
        raise ValueError('Stale executable pane plan')
    if where_parts:
        raise ValueError('Runtime filters require a typed pane filter plan')
    if spec.get('datasource') not in engine.config.DATASOURCES:
        raise ValueError('Pane datasource has no explicit runtime binding')
    scope = engine.IR.get('field_scopes', {}).get(spec.get('datasource_id'), {})
    if scope.get('caption') != spec.get('datasource'):
        raise ValueError('Pane datasource binding does not match its scoped identity')
    table = engine.tbl(spec)
    lookup = {o['id']: o for o in model['occurrences']}
    columns = engine.st.columns(len(plan['panes']))
    for pane, container in zip(plan['panes'], columns):
        with container:
            try:
                if pane['status'] != 'READY':
                    raise ValueError('; '.join(pane['reasons']))
                # Resolve from catalog identity, never from display captions or
                # curated profile formulas. Also detect catalog drift at runtime.
                def physical(oid):
                    o = lookup[oid]
                    entry = engine.IR.get('fields', {}).get(o['field_id'], {})
                    definition = {k: entry.get(k) for k in ('physical', 'datatype', 'is_calculation')}
                    if entry.get('datasource_id') != pane['datasource_id'] or definition != o['field_definition']:
                        raise ValueError('Field catalog changed after pane planning')
                    column = engine.to_phys(entry['physical'])
                    if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', column):
                        raise ValueError('Unsupported physical identifier')
                    return '"' + column + '"'
                dim = lookup[pane['group_by'][0]]
                measure = lookup[pane['aggregates'][0]]
                dsql, msql = physical(dim['id']), physical(measure['id'])
                agg = {'sum': 'SUM', 'avg': 'AVG', 'min': 'MIN', 'max': 'MAX', 'cnt': 'COUNT', 'ctd': 'COUNT'}[measure['aggregation']]
                distinct = 'DISTINCT ' if measure['aggregation'] == 'ctd' else ''
                sql = f'SELECT {dsql} AS DIM, {agg}({distinct}{msql}) AS VAL FROM {table} GROUP BY 1'
                data = engine.q(sql)
                chart = engine.alt.Chart(data).mark_bar().encode(
                    y=engine.alt.Y('DIM:N', title=dim['caption']),
                    x=engine.alt.X('VAL:Q', title=measure['caption'], stack=None),
                    tooltip=['DIM:N', 'VAL:Q'])
                if 'dimension-color' in pane['roles'].values():
                    chart = chart.encode(color=engine.alt.Color('DIM:N', title=dim['caption']))
                engine.st.altair_chart(chart, use_container_width=True)
            except Exception as exc:
                label = f'{spec["name"]} / {pane["pane_id"]}'
                engine.findings.record('BLOCKER', spec['name'], 'pane-execution-refused', f'{label}: {exc}')
                engine.st.warning(f'{label}: {exc}')


def attach(spec, ws, meta, catalog, parser, datasource=None, parameters=None, declared_filter_actions=None, action_root=None, dashboard_name=None):
    try:
        model = extract(ws, spec, meta, catalog, parser, parameters)
        model['parameter_definitions'] = parameters or {}
        model['declared_filter_actions'] = declared_filter_actions or {}
        model['datasource_xml'] = ET.tostring(datasource, encoding='unicode') if datasource is not None else ''
        if ws.findall('.//filter'):
            from copy import deepcopy
            model['filter_fields'] = {fid: deepcopy(f) for fid, f in catalog.items()
                                      if f.get('datasource_id') == spec.get('datasource_id')}
        from table_plan import candidate, build
        if candidate(ws):
            spec['kind'] = 'table'
            model['datasource_xml'] = ET.tostring(datasource, encoding='unicode') if datasource is not None else ''
            model['table_execution'] = build(spec, model)
            if spec.get('support', {}).get('reason') == 'No exact chart mapping selected; table approximation':
                spec['support'] = {'status': 'UNVERIFIED', 'reason': 'Bounded physical text-table plan; parity not verified'}
        if spec.get('kind') in ('line', 'area', 'dtbar'):
            model['temporal_context'] = {'fiscal_attributes': [
                {'tag': node.tag, 'attribute': key, 'value': value}
                for root in (ws, datasource) if root is not None for node in root.iter()
                for key, value in node.attrib.items() if 'fiscal' in key.lower()] + [
                {'tag': node.tag, 'text': node.text, 'attributes': dict(node.attrib)}
                for root in (ws, datasource) if root is not None for node in root.iter()
                if 'fiscal' in node.tag.lower()]}
        if len(model['panes']) > 1 and spec.get('kind') != 'dual':
            model['datasource_xml'] = ET.tostring(datasource, encoding='unicode') if datasource is not None else ''
            model['pane_execution'] = build_pane_plan(spec, model)
        if action_root is not None:
            from parameter_actions import extract as extract_parameter_actions
            spec['worksheet_semantics'] = model
            spec['parameter_actions'] = extract_parameter_actions(action_root, dashboard_name, spec, catalog, parameters or {})
        model['projection'] = projection(spec, model)
        if model['projection'].get('renderer') in ('rank-card','logical-scatter','composed-donut','percent-summary','bounded-overlay','member-card','detail-record'):
            model.pop('pane_execution', None)
        spec['worksheet_semantics'] = model
        if model['projection']['status'] != 'MAPPED':
            prior = spec.get('support', {})
            if prior.get('status') not in ('INVALID', 'UNSUPPORTED'):
                spec['support'] = {'status': 'UNSUPPORTED', 'reason': '; '.join(model['projection']['reasons'])}
        elif 'pane_execution' in model and any(p['status'] != 'READY' for p in model['pane_execution']['panes']):
            if spec.get('support', {}).get('status') not in ('INVALID', 'UNSUPPORTED'):
                spec['support'] = {'status': 'PARTIAL', 'reason': 'Some independent panes are unsupported; see pane findings'}
    except Exception as exc:
        spec['support'] = {'status': 'INVALID', 'reason': f'Occurrence extraction failed: {type(exc).__name__}: {exc}'}
    return spec


def validate_sheet(spec):
    """Return (code, relative path, message) issues; usable by runtime and bundler."""
    model = spec.get('worksheet_semantics')
    if model is None:
        return []  # legacy compatibility path
    issues = []
    def issue(code, path, message):
        issues.append((code, 'worksheet_semantics.' + path, message))
    if not isinstance(model, dict) or model.get('version') != VERSION:
        return [('occurrence-schema', 'worksheet_semantics', 'Unknown occurrence schema')]
    try:
        panes = model['panes']
        occurrences = model['occurrences']
        if not isinstance(panes, list) or not isinstance(occurrences, list):
            raise ValueError('Pane and occurrence inventories must be lists')
        pane_ids = [p['id'] for p in panes]
        ids = [o['id'] for o in occurrences]
        if len(set(pane_ids)) != len(pane_ids) or len(set(ids)) != len(ids):
            issue('occurrence-identity', 'occurrences', 'Duplicate pane or occurrence ID')
        for i, item in enumerate(occurrences):
            path = f'occurrences[{i}]'
            if item['worksheet_id'] != model['worksheet_id']:
                issue('occurrence-owner', path, 'Incorrect worksheet ownership')
            if item['pane_id'] is not None and item['pane_id'] not in pane_ids:
                issue('occurrence-pane', path, 'Unknown pane')
            if not isinstance(item['position'], int) or isinstance(item['position'], bool) or item['position'] < 0:
                issue('occurrence-position', path, 'Position must be a nonnegative integer')
            if not item.get('source_path') or not item.get('token'):
                issue('occurrence-evidence', path, 'Source path and token are required')
            if not item.get('field_id') and not item.get('builtin') and not item.get('unresolved') and not item.get('parameter'):
                issue('occurrence-reference', path, 'Missing explicit field resolution state')
            if item.get('parameter'):
                parameter = item['parameter']
                if (item['token'] != '[Parameters].[' + parameter['internal'] + ']' or
                        item['caption'] != parameter['caption'] or item['channel'] not in ('text','lod') or
                        parameter['datatype'] not in ('integer', 'real', 'string', 'boolean', 'date', 'datetime')):
                    issue('parameter-reference', path, 'Parameter text binding does not match its source identity')
            if item['level'] not in ('row', 'aggregate', 'table', 'unknown'):
                issue('occurrence-level', path, 'Unknown evaluation level')
            if item['aggregation'] in AGGREGATES and item['level'] == 'row':
                issue('occurrence-level', path, 'An aggregated pill cannot be a row grouping key')
        if model['layers'] != derive_layers(model):
            issue('pane-inheritance', 'layers', 'Layer inheritance does not match source occurrences')
        if model['grain'] != derive_grain(model):
            issue('query-grain', 'grain', 'Grain does not match the occurrence inventory')
        if len(panes) > 1 and spec.get('kind') != 'dual' and model.get('projection', {}).get('renderer') not in ('rank-card','logical-scatter','composed-donut','percent-summary','bounded-overlay','member-card','detail-record','sheet-plan') and model.get('pane_execution') != build_pane_plan(spec, model):
            issue('pane-plan', 'pane_execution', 'Executable pane plan is missing, stale or invalid')
        from table_plan import candidate, build
        if candidate(ET.fromstring(model['source_xml'])):
            if model.get('table_execution') != build(spec, model):
                issue('table-plan', 'table_execution', 'Text-table plan is missing, stale or invalid')
        elif 'table_execution' in model:
            issue('table-plan', 'table_execution', 'Text-table source shape changed')
        expected = projection(spec, model)
        if model['projection'] != expected:
            issue('occurrence-projection', 'projection', 'Renderer projection is stale or incomplete')
        if expected['status'] != 'MAPPED' and spec.get('support', {}).get('status') not in ('INVALID', 'UNSUPPORTED'):
            issue('occurrence-support', 'projection', 'Unplanned occurrences must block this sheet')
        plan = model.get('pane_execution', {})
        states = {p['status'] for p in plan.get('panes', [])}
        if states == {'READY', 'BLOCKED'} and spec.get('support', {}).get('status') not in ('PARTIAL', 'INVALID', 'UNSUPPORTED'):
            issue('pane-support', 'pane_execution', 'A refused pane requires explicit partial support')
    except (KeyError, TypeError, ValueError, ET.ParseError) as exc:
        issue('occurrence-schema', '', f'Malformed occurrence contract: {exc}')
    return issues
