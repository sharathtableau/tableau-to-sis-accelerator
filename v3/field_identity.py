"""Datasource-scoped field catalog and an adapter for legacy caption renderers.

IDs identify source declarations, never display captions. The adapter refuses
same-sheet ambiguities that the legacy renderer cannot yet represent.
"""
from collections import defaultdict
from copy import deepcopy
from urllib.parse import quote
import xml.etree.ElementTree as ET
import re


def field_id(datasource_id, internal):
    return quote(datasource_id, safe='') + '::' + quote(internal.strip('[]'), safe='')


def has_unplanned_qualified_reference(formula):
    # Ignore strings/comments. Qualified parameter references have a dedicated
    # binding; every other qualifier needs a relationship/query plan.
    code = re.sub(r"'(?:(?:\\.|'')|[^'])*'|\"(?:(?:\\.|\"\")|[^\"])*\"|//[^\n]*|/\*[\s\S]*?\*/", '', formula)
    return any(m.group(1) != 'Parameters' for m in
               re.finditer(r'\[([^\]]+)\]\s*\.\s*\[', code))


def scoped_roots(root):
    """Keep top-level declarations authoritative; dependency copies fill gaps."""
    roots = {}
    for ds in root.findall('./datasources/datasource'):
        did = ds.get('name')
        if did and did != 'Parameters':
            wrapper = ET.Element('workbook')
            ET.SubElement(wrapper, 'datasources').append(deepcopy(ds))
            roots[did] = wrapper
    for dep in root.findall('.//datasource-dependencies'):
        did = dep.get('datasource')
        if not did or did == 'Parameters':
            continue
        if did not in roots:
            wrapper = ET.Element('workbook')
            ET.SubElement(ET.SubElement(wrapper, 'datasources'), 'datasource', name=did)
            roots[did] = wrapper
        ds = roots[did].find('./datasources/datasource')
        known = {c.get('name'): c for c in ds.iter() if str(c.tag).endswith('column') and c.get('name')}
        for col in dep:
            if not str(col.tag).endswith('column') or not col.get('name'):
                continue
            old = known.get(col.get('name'))
            if old is None:
                ds.append(deepcopy(col))
                known[col.get('name')] = ds[-1]
            else:
                for key, value in col.attrib.items():
                    if key not in old.attrib:
                        old.set(key, value)
                if old.find('calculation') is None and col.find('calculation') is not None:
                    old.append(deepcopy(col.find('calculation')))
    return roots


class ScopedMetadata(dict):
    """Dict-compatible metadata for existing audits, scoped lookup for parsing."""
    def __init__(self, legacy, by_datasource, default=None):
        super().__init__(legacy)
        self.by_datasource = by_datasource
        self.default_datasource = default

    def for_datasource(self, did):
        # No inheritance from a different datasource when a local field is absent.
        local = dict(self.by_datasource.get('Parameters', {}))
        local.update(self.by_datasource.get(did, {}))
        return ScopedMetadata(local, self.by_datasource, did)

    def resolve(self, did, internal):
        if did in self.by_datasource:
            return self.by_datasource[did].get(internal, {})
        if self.default_datasource and did in (None, 'd'):
            return self.by_datasource.get(self.default_datasource, {}).get(internal, {})
        if did:
            return {}
        return self.get(internal, {})


def build_scopes(root, parser, params, param_alias):
    """Compile each datasource separately, including nested calc dependencies."""
    from calc_translator import translate_formula, WIN_ORDER, is_window_sql
    roots = scoped_roots(root)
    catalog, scopes, metadata = {}, {}, {}
    captions = defaultdict(list)
    for did, scoped in roots.items():
        ds = scoped.find('./datasources/datasource')
        ds_caption = ds.get('caption') or did
        captions[ds_caption].append(did)
        # Connection relation columns describe the remote schema, not Tableau
        # field declarations. Dependency declarations have already been merged
        # at datasource level by scoped_roots. Keep these namespaces distinct.
        cols = {c.get('name'): c for c in ds
                if str(c.tag).endswith('column') and c.get('name')}
        local_meta, _ = parser.column_meta(scoped, _scoped=False)
        metadata[did] = local_meta
        formulas = {}
        for name, col in cols.items():
            calc = col.find('calculation')
            if calc is not None and calc.get('class') == 'tableau' and calc.get('formula'):
                formulas[name.strip('[]')] = calc.get('formula')
            elif calc is not None and calc.get('class') == 'bin' and calc.get('formula') and calc.get('size'):
                # C10: a bin is its lower bound -- FLOOR((x - peg) / size) * size + peg (size may be a parameter)
                size = calc.get('size')
                size = size if size.startswith('[') else str(float(size)).rstrip('0').rstrip('.')
                peg = calc.get('peg') or '0'
                formulas[name.strip('[]')] = f"FLOOR(({calc.get('formula')} - {peg}) / {size}) * {size} + {peg}"
        groups = parser.extract_groups(scoped)
        safe_formulas = {name: None if has_unplanned_qualified_reference(formula) else formula
                         for name, formula in formulas.items()}
        sets = parser.extract_sets(scoped, formulas, params)
        aliases = parser.extract_aliases(scoped)
        scope = {'datasource_id': did, 'caption': ds_caption, 'fields': {},
                 'calcs': {}, 'calc_drops': {}, 'colmap': {}, 'aliases': aliases,
                 'color_maps': parser.extract_color_maps(scoped, local_meta),
                 'palette_refs': parser.extract_palette_refs(scoped, local_meta),
                 'hierarchies': parser.drill_hierarchies(scoped, local_meta),
                 'measure_colors': [{'caption': c, 'agg': a, 'color': color}
                                    for (c, a), color in parser.measure_colors(scoped, local_meta).items()],
                 'ambiguous_captions': [],
                 # X12: a Tableau object model with relationships between logical tables (not a physical join)
                 'relationship_model': ds.find('.//object-graph/relationships/relationship') is not None}
        by_caption = defaultdict(list)
        for name, col in cols.items():
            internal = name.strip('[]')
            cap = col.get('caption') or internal
            fid = field_id(did, internal)
            calc = col.find('calculation')
            entry = {'id': fid, 'datasource_id': did, 'internal': internal,
                     'caption': cap, 'datatype': col.get('datatype'), 'role': col.get('role'),
                     'format': col.get('default-format'), 'physical': internal,
                     'is_calculation': calc is not None}
            translated = None
            if internal in formulas:
                sql, agg_ready = translate_formula(safe_formulas[internal], params, safe_formulas,
                                                   param_alias=param_alias)
                entry['formula'] = formulas[internal]
                if sql is not None:
                    translated = {'sql': sql, 'agg_ready': agg_ready}
                    if is_window_sql(sql):           # C6: the one window rule (calc_translator)
                        translated['window'] = True
                        try:                         # EN26: nested calculations' windows, tagged by calculation
                            from calc_translator import translate_tagged
                            tagged, _a = translate_tagged(safe_formulas[internal], params, safe_formulas,
                                                          param_alias=param_alias)
                            if tagged and tagged != sql:
                                translated['sql_tagged'] = tagged
                        except Exception:
                            pass
                else:
                    scope['calc_drops'][fid] = formulas[internal]
                    from calc_translator import refusal
                    entry['refusal'] = refusal(safe_formulas[internal])    # C14: why, shown with the field
            elif cap in groups:
                translated = {'sql': groups[cap], 'agg_ready': False}
            if translated:
                entry['calculation'] = translated
            catalog[fid] = entry
            scope['fields'][internal] = fid
            by_caption[cap].append(fid)
        # Sets are named datasource objects, not necessarily column declarations.
        for group in ds.findall('./group'):
            internal = (group.get('name') or '').strip('[]')
            cap = group.get('caption') or internal
            translated = sets.get(internal) or sets.get(cap)
            if not internal or not translated or internal in scope['fields']:
                continue
            fid = field_id(did, internal)
            catalog[fid] = {'id': fid, 'datasource_id': did, 'internal': internal,
                            'caption': cap, 'is_calculation': True, 'calculation': translated}
            scope['fields'][internal] = fid
            by_caption[cap].append(fid)
        # Resolve internal names first; a display caption cannot shadow an internal.
        candidates = {key: [fid] for key, fid in scope['fields'].items()}
        for cap, ids in by_caption.items():
            if cap not in candidates:
                candidates[cap] = ids
            if len(ids) > 1:
                scope['ambiguous_captions'].append(cap)
        for key, ids in candidates.items():
            if len(ids) != 1:
                continue
            entry = catalog[ids[0]]
            if entry.get('calculation'):
                scope['calcs'][key] = entry['calculation']
            elif not entry['is_calculation']:
                scope['colmap'][key] = entry['physical']
        scopes[did] = scope
    duplicate_captions = {cap for cap, ids in captions.items() if len(ids) > 1}
    for scope in scopes.values():
        if scope['caption'] in duplicate_captions:
            scope['binding_error'] = ('Distinct datasource IDs share caption ' + repr(scope['caption'])
                                      + '; caption-based table routing is ambiguous')
    return catalog, scopes, metadata


def bind_sheet(spec, ws, meta, catalog, scopes, parser):
    """Attach durable IDs; build a sheet-local bridge for legacy string slots."""
    did = spec.get('datasource_id')
    scope = scopes.get(did)
    if not scope:
        return spec
    local = meta.for_datasource(did)
    spec['color_maps'] = parser.extract_color_maps(ws, local)
    spec['palette_refs'] = parser.extract_palette_refs(ws, local)
    selected = defaultdict(set)
    for element in ws.iter():
        if element.tag in {'column', 'column-instance'}:
            continue  # declarations are not references used by this worksheet
        texts = [value for key, value in element.attrib.items()
                 if key in {'column', 'field', 'axis-column', 'ordering-field', 'using',
                            'measure-to-sort-by', 'dimension-to-sort', 'value-column'}]
        if element.tag in {'rows', 'cols', 'run', 'bucket'}:
            texts.append(element.text or '')
        for text in texts:
            tokens = parser.TOKEN_RE.findall(text)
            if not tokens and text.startswith('['):
                tokens = [text]
            for token in tokens:
                fld = parser.parse_field(token, local)
                fid = fld.get('field_id')
                if fid in catalog and catalog[fid]['datasource_id'] == did:
                    selected[fld['caption']].add(fid)
    # Inference creates references from Measure Names filters and other metadata.
    # Bind those only where a caption denotes one local declaration.
    candidates = defaultdict(set)
    for fid in scope['fields'].values():
        candidates[catalog[fid]['caption']].add(fid)
        candidates[catalog[fid]['internal']].add(fid)
    def annotate(value):
        if isinstance(value, dict):
            cap = value.get('caption')
            if isinstance(cap, str):
                ids = selected.get(cap) or candidates.get(cap, set())
                if value.get('field_id') in catalog:
                    ids = {value['field_id']}
                if len(ids) == 1:
                    fid = next(iter(ids))
                    value['field_id'] = fid
                    value['datasource_id'] = catalog[fid]['datasource_id']
                    selected[cap].add(fid)
            for child in list(value.values()):
                annotate(child)
        elif isinstance(value, list):
            for child in value:
                annotate(child)
    annotate(spec)
    # Caption adapters are needed only for references the renderer consumes.
    # Tableau dependency sections may declare unused fields with shared captions.
    consumed = set()
    def collect(value):
        if isinstance(value, dict):
            if isinstance(value.get('caption'), str):
                consumed.add(value['caption'])
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)
        elif isinstance(value, str) and value in candidates:
            consumed.add(value)
    collect(spec)
    selected = {cap: ids for cap, ids in selected.items() if cap in consumed}
    # Several inline declarations can represent the exact same calculation.
    # Preserve occurrence IDs; the caption bridge may share a binding only if
    # compiled expression, type, role, and formatting all agree within this scope.
    def semantics(fid):
        entry = catalog[fid]
        return (entry.get('calculation') if entry.get('is_calculation') else entry.get('physical'),
                entry.get('datatype'), entry.get('role'), entry.get('format'),
                entry.get('is_calculation'))
    for cap, ids in selected.items():
        first = min(ids)
        if len(ids) > 1 and catalog[first].get('calculation') and all(
                semantics(fid) == semantics(first) for fid in ids):
            selected[cap] = {first}
    bindings = {cap: next(iter(ids)) for cap, ids in selected.items() if len(ids) == 1}
    spec['field_bindings'] = bindings
    from table_plan import candidate
    # Text tables resolve every occurrence directly; their renderer never uses
    # the caption bridge. All other source/calculation failures still apply.
    problems = [] if candidate(ws) else [
        f'Ambiguous worksheet caption {cap!r}' for cap, ids in selected.items() if len(ids) > 1]
    if scope.get('binding_error'):
        problems.append(scope['binding_error'])
    failed = [catalog[fid]['caption'] for fid in bindings.values()
              if catalog[fid]['is_calculation'] and not catalog[fid].get('calculation')]
    if failed:
        problems.append('Untranslated field dependencies: ' + ', '.join(sorted(set(failed))))
    if problems:
        spec['support'] = {'status': 'INVALID', 'reason': '; '.join(problems)}
    return spec


def compatibility_maps(catalog):
    """Unscoped consumers may see only names whose definitions agree."""
    candidates = defaultdict(list)
    for entry in catalog.values():
        for key in {entry['caption'], entry['internal']}:
            candidates[key].append(entry)
    calcs, colmap, ambiguous = {}, {}, []
    for key, entries in candidates.items():
        first = entries[0]
        # A calculation's internal identifier may differ while SQL is equal.
        def meaning(entry):
            return (True, entry.get('calculation')) if entry.get('is_calculation') else (False, entry.get('physical'))
        if any(meaning(entry) != meaning(first) for entry in entries[1:]):
            ambiguous.append(key)
        elif first.get('calculation'):
            calcs[key] = first['calculation']
        elif not first.get('is_calculation'):
            colmap[key] = first['physical']
        else:
            ambiguous.append(key)  # never fall through to a profile or raw column
    return calcs, colmap, sorted(ambiguous)
