"""Authored include-member filters; typed row predicates shared by V3 plans.

No dashboard SQL, inferred domains, exclusion/null guesses or caption resolution.
"""
from copy import deepcopy
import hashlib
import json
import math
import re

VERSION = 'categorical-members/1'


def build(ws, datasource_id, fields):
    """Return a plan and a copy with ONLY fully consumed filter nodes removed."""
    clean = deepcopy(ws)
    allowed = clean.findall('./table/filter') + clean.findall('./table/view/filter')
    if len(allowed) != len(clean.findall('.//filter')):
        raise ValueError('Filter placement requires a separate contract')
    plans = []
    for position, node in enumerate(allowed):
        if set(node.attrib) != {'class', 'column'} or node.get('class') != 'categorical':
            raise ValueError('Only ordinary categorical member filters are planned; context/conditions refuse')
        token = node.get('column')
        match = re.fullmatch(r'\[([^\]]+)\]\.\[none:([^\]]+):nk\]', token or '')
        if not match or match[1] != datasource_id:
            raise ValueError('Filter requires a same-source discrete physical field token')
        internal = match[2]
        entries = [f for f in fields.values() if f.get('datasource_id') == datasource_id and f.get('internal') == internal]
        if len(entries) != 1:
            raise ValueError('Filter field is unresolved or ambiguous')
        entry = entries[0]
        if (entry.get('is_calculation') is not False or entry.get('role') != 'dimension' or
                entry.get('datatype') not in {'string', 'integer', 'real'}):
            raise ValueError('Filter needs a physical string/numeric dimension')
        if len(node) != 1 or node[0].tag != 'groupfilter':
            raise ValueError('Filter needs one explicit member selection')
        level = '[none:' + internal + ':nk]'
        def members(group, depth=0):
            if depth > 12 or group.tag != 'groupfilter' or (group.text or '').strip():
                raise ValueError('Unplanned categorical selection structure')
            fn = group.get('function')
            if fn == 'union' and group.attrib == {'function':'union'}:
                return [v for child in group for v in members(child, depth+1)]
            if (fn != 'member' or set(group.attrib) != {'function','level','member'} or
                    group.get('level') != level or len(group)):
                raise ValueError('Only explicit include members/unions are planned; ranges/exclusions/actions refuse')
            raw = group.get('member')
            try:
                value = None if raw == '%null%' else json.loads(raw)
            except (ValueError, TypeError):
                raise ValueError('Filter member needs a typed literal') from None
            dtype = entry['datatype']
            if value is not None and not ((dtype == 'string' and type(value) is str) or
                    (dtype in {'integer','real'} and type(value) in (int,float) and math.isfinite(value) and
                     (dtype != 'integer' or type(value) is int))):
                raise ValueError('Filter member type differs from its physical field')
            if isinstance(value,str) and ('\\' in value or any(ord(c)<32 for c in value)):
                raise ValueError('Filter backslash/control literal dialect is unplanned')
            return [value]
        values = members(node[0])
        if len(values) > 1000:
            raise ValueError('Filter member budget exceeded')
        values = list(dict.fromkeys(values))
        identity = hashlib.sha256(json.dumps([datasource_id, token, position]).encode()).hexdigest()[:24]
        plans.append({'id':identity, 'field_id':entry['id'], 'datasource_id':datasource_id,
                      'definition':deepcopy(entry), 'values':values})
    for parent in clean.iter():
        for child in list(parent):
            if child in allowed: parent.remove(child)
    return {'version':VERSION, 'datasource_id':datasource_id, 'level':'row',
            'composition':'AND', 'filters':plans}, clean


def validate_catalog(plan, fields):
    if plan.get('version') != VERSION or plan.get('level') != 'row' or plan.get('composition') != 'AND':
        raise ValueError('Unknown filter contract')
    for item in plan['filters']:
        fid = item['field_id']
        if fields.get(fid) != item['definition'] or item['datasource_id'] != plan['datasource_id']:
            raise ValueError('Filter catalog changed after planning')


def sql(plan, fields, physical):
    """Compile only validated typed literals, before aggregation/order/limit."""
    validate_catalog(plan, fields)
    predicates = []
    for item in plan['filters']:
        fid = item['field_id']
        column = physical(fid)
        parts = []
        for value in item['values']:
            if value is None: parts.append(column + ' IS NULL')
            else:
                literal = "'" + value.replace("'", "''") + "'" if isinstance(value,str) else str(value)
                parts.append(column + ' = ' + literal)
        predicates.append('(' + (' OR '.join(parts) if parts else 'FALSE') + ')')
    return ' WHERE ' + ' AND '.join(predicates) if predicates else ''
