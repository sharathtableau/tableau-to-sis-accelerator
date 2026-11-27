"""Bounded text tables: physical occurrences and explicit manual row ordering.

No legacy table slots, caption resolution, inferred pivots, or SQL from XML.
"""
import json
import math
import re
import xml.etree.ElementTree as ET

VERSION = 'text-table/3'
DISPLAY_LIMIT = 200
AGGS = {'sum': 'SUM', 'avg': 'AVG', 'min': 'MIN', 'max': 'MAX',
        'cnt': 'COUNT', 'ctd': 'COUNT'}


def candidate(ws):
    panes = ws.findall('./table/panes/pane')
    rows = ws.findtext('./table/rows', '')
    return (len(panes) == 1 and
            [m.get('class') for m in panes[0].findall('mark')] == ['Text'] and
            bool(rows.strip()) and ':Measure Names]' not in rows and '[Measure Names]' not in rows and
            not ws.findtext('./table/cols', '').strip())


def build(spec, model):
    ws = ET.fromstring(model['source_xml'])
    source = ET.fromstring(model['datasource_xml']) if model.get('datasource_xml') else None
    errors, roles, dims, measures = [], {}, [], []
    from filter_plan import build as build_filters
    filter_plan = None
    try:
        filter_plan, ws = build_filters(ws, spec.get('datasource_id'), model.get('filter_fields', {}))
    except ValueError as exc:
        errors.append(str(exc))
    if not candidate(ws):
        errors.append('Text table requires one explicit Text pane, row dimensions and no column shelf/pivot')
    relations = source.findall('.//relation') if source is not None else []
    if len(relations) != 1 or relations[0].get('type') != 'table' or list(relations[0]):
        errors.append('Text table requires exactly one physical table; joins, unions and relationships are unplanned')
    if source is not None:
        for node in source.iter():
            if (node.tag in {'relationship', 'relationships', 'object-graph', 'filter', 'initial-sql',
                             'aliases', 'group', 'set', 'default-sorts'} or 'sort' in node.tag.lower()):
                errors.append('Datasource aliases, filters, ordering and relationship semantics require a plan')
    if any(spec.get(k) for k in ('blend', 'drill', 'param_select', 'table_calcs', 'forecast',
                                'subtotals', 'reflines')) or (not (filter_plan and filter_plan['filters']) and
                                any(spec.get(k) for k in ('applied_filters', 'filters'))):
        errors.append('Text table has unplanned filter, total, calculation or dynamic semantics')
    table = ws.find('table')
    sort_tags = {'manual-sort', 'sorts', 'computed-sort', 'shelf-sorts', 'shelf-sort-v2'}
    allowed = {'view', 'datasource-dependencies', 'panes', 'rows', 'cols', 'aggregation'} | sort_tags
    if table is None or table.attrib or any(c.tag not in allowed for c in table):
        errors.append('Unplanned table attributes or structural clauses')
    for view in ws.findall('./table/view'):
        if view.attrib or any(c.tag not in ({'datasources', 'datasource-dependencies', 'aggregation'} | sort_tags) for c in view):
            errors.append('Unplanned view clauses')
    for node in ws.findall('./table/rows') + ws.findall('./table/cols') + ws.findall('./table/panes'):
        if node.attrib:
            errors.append('Shelf/pane container attributes require a contract')
    for node in ws.iter():
        tag = node.tag.lower()
        if ('total' in tag or 'filter' in tag or 'fiscal' in tag or
                tag in {'style', 'table-calc', 'reference-line', 'aliases'}):
            errors.append('Text table filters/totals/calculations/style/aliases require separate contracts')
        if tag == 'aggregation' and (node.attrib != {'value': 'true'} or list(node)):
            errors.append('Text table requires an aggregated view')
        if 'sort' in tag and tag not in sort_tags:
            errors.append('Unplanned ordering construct; no default/locale ordering is inferred')
        if tag in {'sorts', 'shelf-sorts'} and (node.attrib or any(c.tag not in sort_tags for c in node)):
            errors.append('Unplanned ordering container')
    for pane in model['panes']:
        if pane['axes'] or set(pane['attributes']) - {'id'}:
            errors.append('Text table pane selectors and indexed axes are unplanned')
    for item in model['occurrences']:
        definition = item.get('field_definition', {})
        role = 'unsupported'
        if (item.get('unresolved') or item.get('builtin') or item.get('datepart') or
                item.get('datasource_id') != spec.get('datasource_id') or
                definition.get('is_calculation') is not False or not definition.get('physical') or
                item.get('table_calculation_xml') or item.get('selector')):
            errors.append('Unplanned physical identity/date/calculation for occurrence ' + item['id'])
        elif (item['channel'] == 'rows' and item['level'] == 'row' and
              item['role'] == 'dimension' and item['discrete'] and
              definition.get('datatype') in {'string', 'integer', 'real'}):
            role = 'group'
            dims.append(item)
        elif (item['channel'] == 'text' and item['level'] == 'aggregate' and
              item['aggregation'] in AGGS and definition.get('datatype') in {'integer', 'real'}):
            role = 'aggregate'
            measures.append(item)
        else:
            errors.append('Unplanned ' + item['channel'] + ' occurrence ' + item['id'])
        roles[item['id']] = role
    dims.sort(key=lambda o: o['position'])
    measures.sort(key=lambda o: o['position'])
    if not dims or not measures:
        errors.append('Text table needs explicit physical dimension rows and aggregate text measures')
    expression = ws.findtext('./table/rows', '')
    for item in dims:
        expression = expression.replace(item['token'], 'F')
    if re.sub(r'[\s()]', '', expression) != '/'.join('F' for _ in dims):
        errors.append('Only slash-separated row dimensions are planned')
    # Non-field text formatting can contain interpolations absent from encodings.
    for pane in ws.findall('./table/panes/pane'):
        if any(m.attrib != {'class': 'Text'} or list(m) for m in pane.findall('mark')):
            errors.append('Custom text mark attributes require a contract')
        if any(child.tag not in {'mark', 'encodings'} for child in pane):
            errors.append('Custom pane text/formatting requires a display contract')
        if any(e.tag != 'text' or set(e.attrib) != {'column'} or list(e)
               for e in pane.findall('./encodings/*')):
            errors.append('Only explicit aggregate text encodings are planned')
    orders = {}
    def matching(token):
        return [o for o in dims if token in {o['token'], o['token'].split('].', 1)[-1]}]
    measure_order = None
    sort_nodes = ws.findall('.//computed-sort') + ws.findall('.//shelf-sort-v2')
    if sort_nodes:
        if len(sort_nodes) != 1 or len(dims) != 1 or ws.findall('.//manual-sort'):
            errors.append('Measure ordering requires one row occurrence and one explicit sort; mixed/nested orders need a contract')
        else:
            node = sort_nodes[0]
            if node.tag == 'computed-sort':
                attrs = {'column', 'using', 'direction'}
                target, key = node.get('column'), node.get('using')
                valid_options = set(node.attrib) == attrs
            else:
                attrs = {'dimension-to-sort', 'measure-to-sort-by', 'direction', 'shelf'}
                target, key = node.get('dimension-to-sort'), node.get('measure-to-sort-by')
                valid_options = (attrs <= set(node.attrib) <= attrs | {'is-on-innermost-dimension'} and
                                 node.get('shelf') == 'rows' and
                                 node.get('is-on-innermost-dimension', 'true') == 'true')
            candidates = [m for m in measures if key in {m['token'], m['token'].split('].', 1)[-1]}]
            if (not valid_options or list(node) or node.get('direction') not in {'ASC', 'DESC'} or
                    len(matching(target)) != 1 or not candidates or
                    len({(m['field_id'], m['aggregation']) for m in candidates}) != 1):
                errors.append('Measure sort needs an unambiguous same-source row and explicit visible aggregate key/direction')
            else:
                measure_order = {'mode': 'measure', 'occurrence_id': candidates[0]['id'],
                                 'dimension_occurrence_id': dims[0]['id'],
                                 'direction': node.get('direction'), 'ties': 'REFUSE', 'nulls': 'REFUSE'}
    for node in ws.findall('.//manual-sort'):
        targets = matching(node.get('column'))
        if (not targets or len({o['field_id'] for o in targets}) != 1 or
                node.attrib.keys() != {'column', 'direction'} or node.get('direction') != 'ASC' or
                len(node) != 1 or node[0].tag != 'dictionary' or node[0].attrib):
            errors.append('Manual row ordering is ambiguous or has unsupported options')
            continue
        values = []
        for bucket in node[0]:
            raw = (bucket.text or '').strip()
            try:
                if bucket.tag != 'bucket' or bucket.attrib or list(bucket):
                    raise ValueError()
                value = None if raw == '%null%' else json.loads(raw)
                dtype = targets[0]['field_definition']['datatype']
                if value is not None and not ((dtype == 'string' and isinstance(value, str)) or
                        (dtype in {'integer', 'real'} and type(value) in (int, float) and
                         math.isfinite(value) and (dtype != 'integer' or int(value) == value))):
                    raise ValueError()
                if isinstance(value, str) and ('\\' in value or any(ord(c) < 32 for c in value)):
                    raise ValueError()  # Backslash/control literal dialect rules are not shared.
                if value in values:
                    raise ValueError()
                values.append(value)
            except (ValueError, TypeError):
                errors.append('Manual ordering needs distinct typed literals or an explicit %null% bucket')
        fid = targets[0]['field_id']
        if fid in orders or not values:
            errors.append('Missing or conflicting manual row ordering')
        orders[fid] = values
    if measure_order is None and any(d['field_id'] not in orders for d in dims):
        errors.append('Every row dimension requires explicit manual ordering; no locale/default order is inferred')
    authored_text = [e.get('column') for e in ws.findall('./table/panes/pane/encodings/text')]
    if authored_text != [o['token'] for o in measures]:
        errors.append('Every authored text occurrence must be consumed in source order')
    columns = [{'occurrence_id': o['id'], 'field_id': o['field_id'],
                'alias': 'O_' + o['id'].upper(), 'role': roles[o['id']],
                'aggregation': o['aggregation'], 'definition': o['field_definition'],
                'caption': o['caption']} for o in dims + measures]
    return {'version': VERSION, 'worksheet_id': model['worksheet_id'],
            'datasource_id': spec.get('datasource_id'), 'status': 'BLOCKED' if errors else 'READY',
            'reasons': sorted(set(errors)), 'columns': columns, 'roles': roles,
            'ordering': [measure_order] if measure_order else [
                {'mode': 'manual', 'occurrence_id': d['id'], 'values': orders.get(d['field_id'], [])} for d in dims],
            'display_limit': DISPLAY_LIMIT, 'filter_plan': filter_plan}


def render(spec, where_parts, engine):
    model = spec['worksheet_semantics']
    plan = build(spec, model)
    if plan != model.get('table_execution') or plan['status'] != 'READY':
        raise ValueError('Missing, stale or unsupported text-table plan')
    scope = engine.IR.get('field_scopes', {}).get(plan['datasource_id'], {})
    if (scope.get('caption') != spec.get('datasource') or scope.get('binding_error') or
            spec.get('datasource') not in engine.config.DATASOURCES):
        raise ValueError('Text-table datasource needs an unambiguous explicit runtime binding')
    selected, groups, labels = [], [], {}
    physical_ids = {}
    for i, col in enumerate(plan['columns']):
        entry = engine.IR.get('fields', {}).get(col['field_id'], {})
        if (entry.get('datasource_id') != plan['datasource_id'] or
                {k: entry.get(k) for k in ('physical', 'datatype', 'is_calculation')} != col['definition']):
            raise ValueError('Field catalog changed after text-table planning')
        physical = engine.to_phys(entry['physical'])
        if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', physical):
            raise ValueError('Unsupported physical identifier')
        previous = physical_ids.setdefault(physical, col['field_id'])
        if previous != col['field_id']:
            raise ValueError('Distinct field identities collide after physical column normalization')
        expr = '"' + physical + '"'
        if col['role'] == 'aggregate':
            expr = AGGS[col['aggregation']] + '(' + ('DISTINCT ' if col['aggregation'] == 'ctd' else '') + expr + ')'
        else:
            groups.append(str(i + 1))
            if entry['datatype'] == 'string':
                expr = 'CAST(' + expr + ' AS VARCHAR)'
        selected.append(expr + ' AS ' + col['alias'])
        role = col['aggregation'].upper() if col['role'] == 'aggregate' else 'row'
        labels[col['alias']] = f"{col['caption']} ({role}, {i + 1})"
    def literal(value):
        if isinstance(value, str):
            return "'" + value.replace("'", "''") + "'"
        return str(value)
    lookup = {c['occurrence_id']: c for c in plan['columns']}
    order, valid = [], []
    measure_term = next((t for t in plan['ordering'] if t['mode'] == 'measure'), None)
    for term in plan['ordering']:
        alias = lookup[term['occurrence_id']]['alias']
        if term['mode'] == 'measure':
            dimension = lookup[term['dimension_occurrence_id']]['alias']
            order.append(alias + ' ' + term['direction'])
            valid.append(f'({alias} IS NOT NULL AND {dimension} IS NOT NULL AND __TIES = 1)')
            continue
        predicates = [alias + ' IS NULL' if v is None else alias + ' = ' + literal(v) for v in term['values']]
        order.append('CASE ' + ' '.join(f'WHEN {p} THEN {i}' for i, p in enumerate(predicates)) + ' END ASC')
        valid.append('(' + ' OR '.join(predicates) + ')')
    def filter_physical(fid):
        entry = engine.IR['fields'][fid]
        name = engine.to_phys(entry['physical'])
        if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', name) or physical_ids.setdefault(name, fid) != fid:
            raise ValueError('Filter physical identifier is invalid or collides')
        return '"' + name + '"'
    runtime_predicates, governed_fields = [], set()
    if where_parts:
        if not isinstance(where_parts, list):
            raise ValueError('Runtime text-table filters require typed scoped controls')
        applicable = engine._parts_for_sheet(where_parts, spec)
        for item in applicable:
            if not isinstance(item, dict):
                raise ValueError('Runtime text-table filters require typed scoped controls')
            fid = item.get('field_id')
            field = engine.IR.get('fields', {}).get(fid)
            if (not fid or item.get('datasource_id') != plan['datasource_id'] or
                    not field or field.get('datasource_id') != plan['datasource_id'] or
                    field.get('is_calculation') is not False or
                    item.get('table') != engine.tbl(spec)):
                raise ValueError('Runtime table filter lacks verified same-source field identity')
            dtype = field.get('datatype')
            column = filter_physical(fid)
            if engine.to_phys(item.get('col', '')) != engine.to_phys(field.get('physical', '')):
                raise ValueError('Runtime table filter column differs from its field identity')
            selection = item.get('runtime_filter')
            if not isinstance(selection, dict):
                raise ValueError('Runtime table filter lacks typed selection evidence')
            kind = selection.get('kind')
            value = selection.get('value')
            if item.get('governs'):
                governed_fields.add(fid)
            if kind == 'categorical' and isinstance(selection.get('values'), list) and selection['values']:
                lits = []
                for v in selection['values']:
                    if dtype == 'string' and isinstance(v, str):
                        lits.append("'" + v.replace("'", "''") + "'")
                    elif dtype == 'integer' and type(v) is int:
                        lits.append(str(v))
                    elif dtype == 'real' and type(v) in (int, float) and math.isfinite(v):
                        lits.append(str(v))
                    else:
                        raise ValueError('Runtime categorical selection type differs from its field')
                runtime_predicates.append(f'{column} IN ({", ".join(lits)})')
                continue
            if kind == 'categorical':
                if value is None:
                    continue
                if dtype == 'string' and isinstance(value, str):
                    literal = "'" + value.replace("'", "''") + "'"
                elif dtype == 'integer' and type(value) is int:
                    literal = str(value)
                elif dtype == 'real' and type(value) in (int, float) and math.isfinite(value):
                    literal = str(value)
                elif dtype == 'boolean' and type(value) is bool:
                    literal = 'TRUE' if value else 'FALSE'
                else:
                    raise ValueError('Runtime categorical selection type differs from its field')
                runtime_predicates.append(f'{column} = {literal}')
            elif kind == 'date_part':
                part = str(selection.get('part', '')).lower()
                sql_part = {'yr': 'YEAR', 'tyr': 'YEAR', 'qr': 'QUARTER', 'tqr': 'QUARTER',
                            'mn': 'MONTH', 'tmn': 'MONTH', 'wk': 'WEEK', 'twk': 'WEEK',
                            'dy': 'DAY', 'tdy': 'DAY'}.get(part)
                if dtype not in {'date', 'datetime'} or not sql_part:
                    raise ValueError('Runtime date-part filter needs a typed calendar field')
                many = selection.get('values')
                if isinstance(many, list) and many:
                    if any(type(v) is not int for v in many):
                        raise ValueError('Runtime date-part selection must be an integer')
                    runtime_predicates.append(f'EXTRACT({sql_part} FROM {column}) IN ({", ".join(map(str, many))})')
                elif value is not None:
                    if type(value) is not int:
                        raise ValueError('Runtime date-part selection must be an integer')
                    runtime_predicates.append(f'EXTRACT({sql_part} FROM {column}) = {value}')
            elif kind == 'date_range':
                if dtype not in {'date', 'datetime'}:
                    raise ValueError('Runtime date-range filter needs a typed calendar field')
                start, end = selection.get('start'), selection.get('end')
                if (not isinstance(start, str) or not isinstance(end, str) or
                        not re.fullmatch(r'\d{4}-\d{2}-\d{2}', start) or
                        not re.fullmatch(r'\d{4}-\d{2}-\d{2}', end) or start > end):
                    raise ValueError('Runtime date range must use ordered ISO dates')
                runtime_predicates.append(f"{column} BETWEEN '{start}' AND '{end}'")
            else:
                raise ValueError('Unsupported runtime table filter kind')
    from copy import deepcopy
    from filter_plan import sql as filter_sql
    fixed_plan = deepcopy(plan['filter_plan'])
    fixed_plan['filters'] = [f for f in fixed_plan['filters'] if f['field_id'] not in governed_fields]
    fixed_predicate = filter_sql(fixed_plan, engine.IR.get('fields', {}), filter_physical)
    predicates = runtime_predicates + ([fixed_predicate[7:]] if fixed_predicate else [])
    predicate = ' WHERE ' + ' AND '.join(predicates) if predicates else ''
    grouped = 'SELECT ' + ', '.join(selected) + ' FROM ' + engine.tbl(spec) + predicate + ' GROUP BY ' + ', '.join(groups)
    ctes = 'WITH grouped AS (' + grouped + ')'
    query_source = 'grouped'
    if measure_term:
        alias = lookup[measure_term['occurrence_id']]['alias']
        ctes += f', ordered AS (SELECT *, COUNT(*) OVER (PARTITION BY {alias}) AS __TIES FROM grouped)'
        query_source = 'ordered'
    # Domain guard spans ALL groups, including any hidden by the display limit.
    sql = (ctes + ' SELECT *, COUNT(*) OVER () AS __TOTAL, '
           'MAX(CASE WHEN ' + ' AND '.join(valid) + ' THEN 0 ELSE 1 END) OVER () AS __UNPLANNED '
           'FROM ' + query_source + ' ORDER BY ' + ', '.join(order) + f' LIMIT {plan["display_limit"]}')
    data = engine.q(sql)
    if not data.empty and data['__UNPLANNED'].max():
        if measure_term:
            raise ValueError('Measure ordering has tied aggregates or null keys/headers; no tie/null order is planned')
        raise ValueError('Data contains a row member outside the explicit ordering, including unplanned nulls')
    total = int(data['__TOTAL'].iloc[0]) if not data.empty else 0
    output = data[[c['alias'] for c in plan['columns']]].copy()
    # Stable IDs remain Arrow/dataframe column names; captions are display-only.
    engine.st.dataframe(output, column_config=labels, hide_index=True, use_container_width=True)
    mode = 'measure' if measure_term else 'manual'
    engine.st.caption(f'{len(output)} of {total} grouped rows displayed in authored {mode} order.')
    if total > len(output):
        message = f'Display truncated to {len(output)} of {total} grouped rows after sorting.'
        engine.st.warning(message)
        engine.findings.record('WARNING', spec['name'], 'table-display-truncated', message)
