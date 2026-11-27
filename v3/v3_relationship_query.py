"""DM4: per-table relationship queries -- Tableau's relationship semantics for a sheet that uses a RELATED table the
flatten left out (its key repeats, so a flat join would multiply the other table's rows).

Each measure is aggregated from its OWN table's rows at the view's dimensions. The other table only contributes a
DISTINCT projection of (its key, the columns the view needs), so a lookup never multiplies another table's measure;
a related table's record count is COUNT(*) of that table. The groups are combined on the view's dimensions (NULL
dimension values included). Built from the engine's own field resolution; one read-only SELECT.

C12: a filter on a related table's field keeps that table's matching rows and the parent rows they relate to
(semi-join on the key); a calculation over the related table alone is computed on that table's rows; a calculation
that mixes both tables is computed on their join (as Tableau does), grouped at the view's dimensions.

Scope (refused with the reason otherwise): related tables attached directly to the flattened table; no measure
filter; no filter or dimension that mixes two tables; between two FACT tables no filter or mixed calculation.
"""

PARENT = 'P'          # the flattened table (the datasource itself)


class RelationshipQueryError(ValueError):
    pass


def related_fields(plan, engine):
    """plan field -> {'source', 'column' | 'count'} for fields owned by a related table (IR 'related')."""
    fields_ir = (getattr(engine, 'IR', {}) or {}).get('fields', {})
    out = {}
    for f in plan.get('fields') or []:
        e = fields_ir.get(f.get('field_id') or '') or {}
        if e.get('related'):
            out[id(f)] = e['related']
    return out


def compile_relationship(plan, sheet, engine, where, related, P, rwhere='', joined=None):
    """-> (sql, cols) like v3_sheet_plan.compile_query. related: id(field) -> IR 'related' entry, or (C12) a
    calculation over the related table alone: {'source', 'expr'} (a row-level dimension) / {'source', 'sql'} (an
    aggregate). rwhere: the sheet's conditions on the related table's own columns. joined: id(measure) -> SQL of a
    calculation that mixes both tables, evaluated on their join (Tableau computes such a calculation on the joined
    rows), grouped at the view's dimensions."""
    joined = joined or {}
    if plan.get('blend'):
        raise RelationshipQueryError('A related table on a blended sheet is not supported')
    if not plan.get('aggregate', True):
        raise RelationshipQueryError('A related table in a disaggregated view is not supported')
    scope = ((getattr(engine, 'IR', {}) or {}).get('field_scopes') or {}).get(sheet.get('datasource_id')) or {}
    links = scope.get('related_tables') or {}
    dims, measures = P.query_fields(plan)
    week = plan.get('week_start', 'sunday')
    T = engine.tbl(sheet)
    sources = {r['source'] for r in related.values()}
    if len(sources) > 1:
        raise RelationshipQueryError('More than one related table on one sheet is not supported yet')
    source = next(iter(sources))
    link = links.get(source)
    if not link:
        raise RelationshipQueryError(f'{source}: no relationship link to the datasource')
    R = engine.tbl({'datasource': source})
    rkey, pkey = link['key'], link['parent_key']            # physical column names (UPPER_SNAKE)

    def side(f):
        return 'J' if id(f) in joined else 'R' if id(f) in related else PARENT

    def rcol(f, alias):
        rel = related[id(f)]
        if rel.get('expr'):
            return f"({rel['expr'].replace('__TBL__', R)})"
        return f"{alias}.{rel['column']}" if alias else rel['column']

    def expr(f, alias):                                    # a related table's own dimension column / calculation
        return P._date_sql(rcol(f, alias), f['date_part'], week, f.get('fiscal', 1))

    # the parent's dimension expressions resolve against the parent's own columns (unqualified, engine SQL)
    r_dims = [d for d in dims if side(d) == 'R']
    p_dims = [d for d in dims if side(d) == PARENT]
    groups = {PARENT: [m for m in measures if side(m) == PARENT], 'R': [m for m in measures if side(m) == 'R'],
              'J': [m for m in measures if side(m) == 'J']}
    if any(side(d) == 'J' for d in dims):
        raise RelationshipQueryError('A dimension that mixes two related tables is not supported yet')
    rw = f'({rwhere})' if (rwhere or '').strip() else ''
    for m in groups['R']:
        if not (related[id(m)].get('count') or related[id(m)].get('column') or related[id(m)].get('sql')):
            raise RelationshipQueryError(f"'{m['caption']}' cannot be computed from {link['table']}")
    if link.get('role') == 'fact':
        # another FACT table (multi-fact model): never break one fact's measure down by the other fact's dimensions,
        # and no sheet filter across facts -- how Tableau splits those is not reproduced, so it refuses
        shared = set(link.get('shared_columns') or [])
        if groups[PARENT] and r_dims:
            raise RelationshipQueryError(f"Measures of one fact table by dimensions of {link['table']} (another fact) "
                                         'are not supported')
        if groups['R'] and any(engine.rdim(d['caption']) not in shared for d in p_dims):
            raise RelationshipQueryError(f"{link['table']} measures break down only by its own or shared-dimension "
                                         'fields')
        if (where or '').strip() or rw or groups['J']:
            raise RelationshipQueryError('A filter across two fact tables (or a calculation over both) is not supported yet')
    keys = [f'D{i}' for i in range(len(dims))]
    selects = []
    # parent measures: parent rows LEFT JOIN distinct(related key, related dims) -> never multiplies a parent row
    # unless the related table holds several different values for one key (then each value, as in Tableau)
    if groups[PARENT] or not (groups['R'] or groups['J']):
        rd = ', '.join([f'{rkey} AS __K'] + [f"{rcol(d, None)} AS __R{i}" for i, d in enumerate(r_dims)])
        join = f' LEFT JOIN (SELECT DISTINCT {rd} FROM {R}) r ON {pkey} = r.__K' if r_dims else ''
        dim_sql = [P._date_sql(f'r.__R{r_dims.index(d)}', d['date_part'], week, d.get('fiscal', 1)) if side(d) == 'R'
                   else P._date_sql(engine.rdim(d['caption']), d['date_part'], week, d.get('fiscal', 1)) for d in dims]
        mexprs = []
        for m in groups[PARENT]:
            ref = {'caption': m['caption'], 'agg': None if m['agg'] in ('usr', 'none', 'attr') else m['agg']}
            if m.get('field_id'):
                ref['field_id'] = m['field_id']
            if m.get('count_records'):
                ref['count_records'] = True
            r = engine.resolve_measure(T, sheet.get('name'), ref)
            if not r or r.get('window'):
                raise RelationshipQueryError(f"Measure '{m['caption']}' could not be translated")
            mexprs.append(r['sql'])
        cols = [f'{s} AS {k}' for s, k in zip(dim_sql, keys)] + \
               [f'{e} AS M{measures.index(m)}' for e, m in zip(mexprs, groups[PARENT])] + \
               [f'NULL AS M{measures.index(m)}' for m in groups['R'] + groups['J']]
        pwhere = (where or '').strip()
        if rw:                                 # a related table's filter keeps the parent rows it relates to
            semi = f'{pkey} IN (SELECT {rkey} FROM {R} WHERE {rw.replace("__TBL__", R)})'
            pwhere = f'{pwhere} AND {semi}' if pwhere else f'WHERE {semi}'
        sql = f"SELECT {', '.join(cols)} FROM {T}{join} {pwhere}"
        selects.append(sql + (' GROUP BY ' + ', '.join(str(i + 1) for i in range(len(dims))) if dims else ''))
    # related measures: related rows LEFT JOIN distinct(parent key, parent dims) of the FILTERED parent (the sheet's
    # filters are on parent fields); with filters, a related row whose parent rows are all filtered out drops out
    if groups['R']:
        pd_cols = ', '.join([f'{pkey} AS __K'] + [
            f"{P._date_sql(engine.rdim(d['caption']), d['date_part'], week, d.get('fiscal', 1))} AS __P{i}" for i, d in enumerate(p_dims)])
        how = 'INNER' if (where or '').strip() else 'LEFT'
        join = f" {how} JOIN (SELECT DISTINCT {pd_cols} FROM {T} {where or ''}) p ON r.{rkey} = p.__K" \
            if (p_dims or (where or '').strip()) else ''
        if rw:
            join += f' WHERE {rw.replace("__TBL__", R)}'
        dim_sql = [f'p.__P{p_dims.index(d)}' if side(d) == PARENT else expr(d, 'r') for d in dims]
        mexprs = []
        for m in groups['R']:
            rel = related[id(m)]
            agg = 'COUNT(*)' if rel.get('count') else rel['sql'].replace('__TBL__', R) if rel.get('sql') else None
            if agg is None:
                fn = {'sum': 'SUM', 'avg': 'AVG', 'min': 'MIN', 'max': 'MAX', 'cnt': 'COUNT', 'ctd': 'COUNT(DISTINCT',
                      'med': 'MEDIAN'}.get(m['agg'])
                if not fn:
                    raise RelationshipQueryError(f"'{m['caption']}': aggregation {m['agg']} on {link['table']}")
                agg = f"{fn} r.{rel['column']})" if fn.endswith('DISTINCT') else f"{fn}(r.{rel['column']})"
            mexprs.append(agg)
        cols = [f'{s} AS {k}' for s, k in zip(dim_sql, keys)] + \
               [f'NULL AS M{measures.index(m)}' for m in groups[PARENT]] + \
               [f'{e} AS M{measures.index(m)}' for e, m in zip(mexprs, groups['R'])] + \
               [f'NULL AS M{measures.index(m)}' for m in groups['J']]
        sql = f"SELECT {', '.join(cols)} FROM {R} r{join}"
        selects.append(sql + (' GROUP BY ' + ', '.join(str(i + 1) for i in range(len(dims))) if dims else ''))
    # C12: calculations that mix both tables -- on the join of the two tables (the parent's own filters and the
    # related table's both apply); a dimension resolves against the joined row's own columns
    if groups['J']:
        J = f'(SELECT * FROM {T} __p INNER JOIN {R} __r ON __p.{pkey} = __r.{rkey})'
        dim_sql = [expr(d, 'j') if side(d) == 'R' else P._date_sql(engine.rdim(d['caption']), d['date_part'], week, d.get('fiscal', 1))
                   for d in dims]
        conds = [c for c in ((where or '').strip()[len('WHERE'):].strip() if (where or '').strip() else '',
                             rw.replace('__TBL__', R)) if c]
        cols = [f'{s} AS {k}' for s, k in zip(dim_sql, keys)] + \
               [f'NULL AS M{measures.index(m)}' for m in groups[PARENT] + groups['R']] + \
               [f"{joined[id(m)].replace('__TBL__', J)} AS M{measures.index(m)}" for m in groups['J']]
        sql = f"SELECT {', '.join(cols)} FROM {J} j" + (' WHERE ' + ' AND '.join(conds) if conds else '')
        selects.append(sql + (' GROUP BY ' + ', '.join(str(i + 1) for i in range(len(dims))) if dims else ''))
    order = [f'M{measures.index(m)}' for m in measures]
    # each group yields at most one row per mark: MAX picks that group's value; GROUP BY keeps NULL marks
    outer = ', '.join(keys + [f'MAX({c}) AS {c}' for c in order])
    sql = f"SELECT {outer} FROM ({' UNION ALL '.join(selects)}) u" + \
        (' GROUP BY ' + ', '.join(keys) if keys else '') + f' LIMIT {P.MAX_ROWS + 1}'
    cols = [(f'D{i}', d, False) for i, d in enumerate(dims)] + [(f'M{j}', m, True) for j, m in enumerate(measures)]
    return engine.sub_params(sql), cols
