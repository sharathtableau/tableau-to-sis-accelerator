"""Release 26: exact sheet plans for sheets the V3 adapters refuse (the "exact loop").

Tableau defines what a worksheet computes by its shelves: every dimension on Rows, Columns and the Marks
card (colour, size, label, detail, tooltip, path, shape) sets the view's level of detail; every measure is
aggregated at that level; table calculations then run over the aggregated table; the sheet's filters
restrict the rows first. The scoped parse records each of those as an *occurrence* (channel, field,
aggregation, date part, table-calculation XML). This module turns the occurrences of one sheet into a
typed plan (schema `sheet-plan/1`) built from closed lists only, compiles it to ONE read-only aggregate
query with the engine's own field/calculation/filter resolution, applies the table calculations in code,
and draws the result with one generic renderer that follows the authored shelves and mark.

Proof is separate (v3_exact_loop): a plan is "Verified" only when its rows equal Tableau's own summary data
for that sheet. Nothing here uses workbook or sheet names as logic, and no model output is executed:
a Cortex proposal can only choose values from the closed lists below, and validate() rejects anything else.
"""
from copy import deepcopy
import hashlib
import json
import re

SCHEMA = 'sheet-plan/1'

# Tableau date-part tokens (occurrence 'datepart') -> plan date part. 't*' = truncated (continuous) dates.
DATE_PARTS = {None: 'none', 'none': 'none', 'yr': 'year', 'qr': 'quarter', 'mn': 'month', 'wk': 'week',
              'dy': 'day', 'wd': 'weekday', 'tyr': 'year_trunc', 'tqr': 'quarter_trunc', 'tmn': 'month_trunc',
              'twk': 'week_trunc', 'tdy': 'day_trunc', 'mdy': 'day_trunc', 'my': 'month_trunc'}
# discrete Month / Year ('my') and Month / Day / Year ('mdy') group exactly like the truncated month / day
PLAN_DATE_PARTS = frozenset(DATE_PARTS.values())
AGGS = frozenset({'sum', 'avg', 'min', 'max', 'cnt', 'ctd', 'med', 'usr', 'none', 'attr', 'stdev', 'var', 'mv'})
TABLE_CALCS = frozenset({'Rank', 'PctTotal', 'CumTotal', 'Diff', 'PctDiff'})
ADDRESSING = frozenset({'rows', 'cols', 'table', 'pane_rows', 'pane_cols', 'field'})
RANK_KINDS = frozenset({'competition', 'modified', 'dense', 'unique'})
MARKS = frozenset({'bar', 'line', 'area', 'circle', 'square', 'shape', 'text', 'gantt', 'pie', 'map', 'point'})
CHANNELS = frozenset({'rows', 'cols', 'color', 'size', 'text', 'label', 'detail', 'lod', 'tooltip', 'path',
                      'shape', 'angle', 'geometry', 'wedge-size', 'sort', 'refline'})
MARK_CHANNELS = frozenset({'color', 'size', 'text', 'label', 'detail', 'lod', 'tooltip', 'path', 'shape',
                           'angle', 'wedge-size'})
AXIS_SELECTORS = {'x-axis-name': 'cols', 'y-axis-name': 'rows'}
PLACEHOLDERS = frozenset({'', 'Multiple Values', 'Measure Names', 'Measure Values', 'Number of Records'})
GENERATED = re.compile(r'^(Latitude|Longitude|Geometry) \(generated\)$')
# Dimensions Tableau itself adds to a view (not workbook fields): the forecast's Actual/Estimate split
TABLEAU_GENERATED_DIMS = frozenset({'Forecast Indicator'})
MAX_ROWS = 20000
_SCROLL = set()          # ids of charts taller than a dashboard zone (drawn in a scrolling container)


class PlanError(ValueError):
    """A plan could not be built or is not valid (safe, short message)."""


# --------------------------------------------------------------------------- plan from the authored shelves
def _tc_element(text):
    """The <table-calc> element of an occurrence's table-calculation XML (either quote style), or None."""
    import xml.etree.ElementTree as ET
    m = re.search(r'<table-calc\b[^>]*/>|<table-calc\b[^>]*>.*?</table-calc>', str(text), re.S)
    if not m:
        return None
    try:
        return ET.fromstring(m.group(0))
    except ET.ParseError:
        return None


def _table_calc(xml_list):
    """First <table-calc> on an occurrence -> typed dict, or None. Unknown types are kept as 'unknown:<type>'.
    E07: Specific-dimension addressing keeps its <order field> list; difference calculations keep Tableau's
    relative-to (diff-options + <address> offset, First, Last); an option the plan cannot compute is 'unknown:'."""
    for text in xml_list or []:
        el = _tc_element(text)
        if el is None:
            continue
        attrs = el.attrib
        kind = attrs.get('type', '')
        ordering = (attrs.get('ordering-type') or 'Table').lower()
        addressing = {'rows': 'rows', 'columns': 'cols', 'table': 'table', 'cellinpane': 'table',
                      'paneacross': 'pane_cols', 'panedown': 'pane_rows', 'field': 'field'}.get(ordering, 'table')
        orders = [o.get('field') for o in el.findall('order') if o.get('field')]
        base = {'addressing': addressing, 'ordering_field': attrs.get('ordering-field'),
                'xml_ordering': attrs.get('ordering-type'), 'ordering_fields': orders}
        if not kind:                      # addressing of a calculated field's own table functions (INDEX() ...)
            return dict(base, type='Window')
        tc = dict(base, type=kind if kind in TABLE_CALCS else f'unknown:{kind}')
        if kind == 'Rank':
            opts = [o.strip().lower() for o in (attrs.get('rank-options') or 'Competition,Descending').split(',')]
            tc['rank'] = next((o for o in opts if o in RANK_KINDS), 'competition')
            tc['descending'] = 'ascending' not in opts
        if kind == 'CumTotal':
            tc['aggregation'] = (attrs.get('aggregation') or 'Sum').lower()
        if kind in ('Diff', 'PctDiff'):
            opt = (attrs.get('diff-options') or 'Relative').lower()
            if opt == 'relative':
                vals = [v.text for v in el.findall('address/value')]
                try:
                    tc['relative_to'] = ('offset', int(vals[0]) if vals else -1)
                except ValueError:
                    tc['type'] = f'unknown:{kind}({vals[0]})'
            elif opt in ('first', 'last'):
                tc['relative_to'] = (opt, None)
            else:
                tc['type'] = f'unknown:{kind}({opt})'
        return tc
    return None


def _field_ordering(token):
    """'[ds].[none:Calc:nk]' -> ('Calc' physical name) for ordering-field matching."""
    m = re.search(r'(?:^|\]\.)\[(?:\w+:)?([^:\]]+)(?::\w+)?\]$', str(token or ''))
    return m.group(1) if m else None       # with or without the datasource prefix ([none:Path Frame (bin):ok])


def _mark(sheet):
    ws = sheet.get('worksheet_semantics') or {}
    marks = [m for p in ws.get('panes') or [] for m in (p.get('marks') or [])]
    authored = next((m for m in marks if m and m != 'Automatic'), None)
    return (authored or 'Automatic'), marks


def _infer_mark(fields):
    """Tableau's Automatic mark from the shelves (its documented defaults)."""
    axis = [f for f in fields if f['channel'] in ('rows', 'cols')]
    cont_dates = [f for f in axis if f['kind'] == 'dim' and not f['discrete'] and f['date_part'] != 'none']
    axis_measures = [f for f in axis if f['kind'] == 'measure' or f.get('measure_values')]
    if not axis_measures:
        if cont_dates or any(f['kind'] == 'dim' and not f['discrete'] and f.get('datatype') in ('date', 'datetime')
                             for f in axis):
            return 'gantt'               # Tableau's automatic mark for a continuous date axis without a measure
        return 'text' if any(f['channel'] == 'text' for f in fields) else 'square'
    if cont_dates:
        return 'line'
    if any(f['kind'] == 'measure' for f in axis if f['channel'] == 'rows') and \
            any(f['kind'] == 'measure' for f in axis if f['channel'] == 'cols'):
        return 'circle'
    return 'bar'


MARK_MAP = {'Bar': 'bar', 'Line': 'line', 'Area': 'area', 'Circle': 'circle', 'Square': 'square',
            'Shape': 'shape', 'Text': 'text', 'GanttBar': 'gantt', 'Pie': 'pie', 'Multipolygon': 'map',
            'Polygon': 'map', 'Point': 'point'}


def _member_unescape(name):
    """C8: Tableau escapes '%' (and '\\') in Measure Names member strings: '\\% Open Rate' -> '% Open Rate'."""
    return re.sub(r'\\(.)', r'\1', name)


def _measure_values(sheet):
    """The measures behind a Measure Values occurrence, from the parse (legacy 'measures'/'ys' lists) or the
    sheet XML's Measure Names filter. Returns [{'caption', 'agg'}] in authored order, [] when unknown."""
    xml = (sheet.get('worksheet_semantics') or {}).get('source_xml') or ''
    m = re.search(r"<filter[^>]*column=\"(?:\[[^\]]+\]\.)?\[:Measure Names\]\"[^>]*>(.*?)</filter>", xml, re.S)
    out = []
    if m:
        names = _physical_captions(sheet)
        for ds, agg, name in re.findall(r'member="&quot;\[([^\]]+)\]\.\[(\w+):([^:\]]+):\w+\]&quot;"', m.group(1)):
            name = _member_unescape(name)
            out.append({'caption': names.get(name, name), 'agg': agg if agg in AGGS else 'sum',
                        'member_id': f'{ds}::{name}'})    # two calculations may share a caption (not a contract field id)
    if out:
        return out
    for key in ('measure_values', 'measures', 'ys'):
        items = sheet.get(key)
        if isinstance(items, list) and items and all(isinstance(m, dict) and m.get('caption') for m in items):
            return [{'caption': _member_unescape(m['caption']), 'agg': (m.get('agg') or 'sum')} for m in items]
    return []


_SELECTOR = re.compile(r'^\s*\[Measure Names\]\s*=\s*\[Parameters\]\.\[([^\]]+)\]\s*$|'
                       r'^\s*\[Parameters\]\.\[([^\]]+)\]\s*=\s*\[Measure Names\]\s*$')


def measure_selector(sheet):
    """A4: the parameter of a 'Measure Names = [Parameter]' filter (Tableau's native measure selector), else None."""
    xml = (sheet.get('worksheet_semantics') or {}).get('source_xml') or ''
    m = re.search(r'<filter[^>]*column="(?:\[[^\]]+\]\.)?\[:Measure Names\]"[^>]*>\s*'
                  r'<groupfilter[^>]*function="formula"[^>]*formula="([^"]*)"', xml)
    if not m:
        return None
    f = _SELECTOR.match(_xml_unescape(m.group(1)))
    return (f.group(1) or f.group(2)) if f else None


def select_measures(plan, engine):
    """A4: the measures the selector parameter picks now (by caption, quotes and case ignored); every measure when
    the value names none of them (the engine's rule for a measure swap)."""
    name = plan.get('measure_param')
    if not name or not plan.get('measure_values'):
        return plan
    params = getattr(engine, 'PARAMS', {}) or {}
    raw = params.get(name)
    if raw is None:
        cap = next((k for k, v in ((getattr(engine, 'IR', {}) or {}).get('param_captions') or {}).items()
                    if v == name), None)
        raw = params.get(cap) if cap else None
    value = str(raw or '').strip().strip('"\'').casefold()
    keep = [m for m in plan['measure_values'] if str(m.get('caption', '')).casefold() == value]
    if not keep:
        return plan
    return dict(plan, measure_values=keep)


def _set_caption(ws, name):
    """The caption of a set / group named `name` in the sheet's datasource XML (else the name)."""
    xml = (ws.get('datasource_xml') or '') + (ws.get('source_xml') or '')
    for attrs in re.findall(r'<group\b([^>]*)>', xml):
        a = dict(re.findall(r'([\w:-]+)=[\'"]([^\'"]*)[\'"]', attrs))
        if a.get('name', '').strip('[]') == name and a.get('caption'):
            return _xml_unescape(a['caption'])
    return name


def _physical_captions(sheet):
    """Physical / internal field name -> caption, from the sheet's datasource-dependencies XML."""
    xml = (sheet.get('worksheet_semantics') or {}).get('source_xml') or ''
    out = {}
    for attrs in re.findall(r'<column\b([^>]*)>', xml):
        a = dict(re.findall(r'([\w-]+)="([^"]*)"', attrs))
        if a.get('name') and a.get('caption'):
            out[a['name'].strip('[]')] = a['caption'].replace('&amp;', '&')
    return out


def from_sheet(sheet, ir=None):
    """Typed plan from the sheet's authored occurrences (code proposer 'authored'). Raises PlanError."""
    ws = sheet.get('worksheet_semantics') or {}
    occ = ws.get('occurrences') or []
    if not occ:
        raise PlanError('The sheet has no field occurrences to plan from')
    fields, seen = [], set()
    for o in occ:
        caption = o.get('caption') or ''
        if '\\' in caption and not o.get('field_id'):
            caption = _member_unescape(caption)          # C8: a Measure Names member token ('\\%' -> '%')
        channel = o.get('channel')
        if channel in AXIS_SELECTORS:
            # a pane's x-/y-axis-name SELECTS an existing Cols / Rows field (worksheet_semantics: selector); it is
            # not a field of its own. One that points at nothing is refused.
            shelf = AXIS_SELECTORS[channel]
            if any(x.get('channel') == shelf and x.get('token') == o.get('token') for x in occ):
                continue
            raise PlanError(f'Axis selector {channel!r} points at no {shelf} field')
        if channel not in CHANNELS:
            raise PlanError(f'Unknown shelf {channel!r}')
        if GENERATED.match(caption) or channel == 'geometry':
            fields.append({'id': o['id'], 'caption': caption, 'channel': channel, 'kind': 'geo',
                           'discrete': False, 'date_part': 'none', 'agg': 'none'})
            continue
        if caption in ('Multiple Values', 'Measure Values'):
            fields.append({'id': o['id'], 'caption': 'Measure Values', 'channel': channel, 'kind': 'measure',
                           'measure_values': True, 'discrete': False, 'date_part': 'none', 'agg': 'none'})
            continue
        if caption in TABLEAU_GENERATED_DIMS and not o.get('field_definition'):
            fields.append({'id': o['id'], 'caption': caption, 'channel': channel, 'kind': 'generated',
                           'discrete': True, 'date_part': 'none', 'agg': 'none'})
            continue
        if caption in ('', 'Measure Names'):
            fields.append({'id': o['id'], 'caption': 'Measure Names', 'channel': channel, 'kind': 'measure_names',
                           'discrete': True, 'date_part': 'none', 'agg': 'none'})
            continue
        agg = (o.get('aggregation') or 'none').lower()
        if agg in DATE_PARTS and agg not in (None, 'none'):
            agg = 'none'
        if o.get('datepart') not in DATE_PARTS:
            # never fall back to the raw date: a part the plan does not model (Month/Year 'my', hour ...) would
            # silently group at the wrong grain
            raise PlanError(f"'{caption}': date part '{o.get('datepart')}' is not supported yet")
        date_part = DATE_PARTS[o.get('datepart')]
        role = 'measure' if o.get('role') == 'measure' else 'dim'
        tc = _table_calc(o.get('table_calculation_xml'))
        f = {'id': o['id'], 'caption': caption, 'channel': channel, 'kind': role,
             'discrete': bool(o.get('discrete')), 'date_part': date_part,
             'agg': agg if agg in AGGS else 'sum', 'field_id': o.get('field_id'),
             'datasource_id': o.get('datasource_id'),
             'datatype': (o.get('field_definition') or {}).get('datatype'),
             'fiscal': _fiscal_start(ws, o.get('field_id')) if date_part in ('year', 'quarter', 'year_trunc',
                                                                             'quarter_trunc') else 1,
             'full_range': ((o.get('field_id') or '').split('::')[-1] in _full_range_fields(ws)
                            or caption in _full_range_fields(ws)),
             'calculation': bool((o.get('field_definition') or {}).get('is_calculation')),
             'formula': o.get('calculation_source') or None}
        if (o.get('field_definition') or {}).get('datatype') == 'table':
            f['count_records'] = True          # COUNT of a logical table = its row count
            f['agg'] = 'cnt'
        if role == 'dim' and agg == 'usr':
            f['aggregate_dimension'] = True   # an aggregate calc used as a discrete header (IF SUM(..) ..)
        elif role == 'dim' and agg in ('sum', 'avg', 'min', 'max', 'cnt', 'ctd', 'med', 'cntd', 'count'):
            # C2: a DISCRETE aggregated measure pill (sum:Sales:ok on Rows) -- a header computed at the view's level
            # of detail, never GROUP BY the raw values
            f['aggregate_dimension'] = True
        if tc and tc['type'] == 'Window':
            f['window'] = tc              # applies to the calculation's own table functions
            nested = nested_table_calcs(o.get('table_calculation_xml'))
            if nested:
                f['nested_windows'] = nested  # EN26: each nested calculation's own addressing
        elif tc:
            f['table_calc'] = tc
        if re.search(r'\[io:[^\]]+\]$', o.get('token') or ''):
            # a set shown In / Out: Tableau heads the column IN/OUT(<set caption>)
            f['header'] = f'IN/OUT({_set_caption(ws, caption)})'
        key = (channel, caption, date_part, f['agg'], json.dumps(tc, sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        fields.append(f)
    authored, marks = _mark(sheet)
    mark = MARK_MAP.get(authored) or _infer_mark(fields)
    plan = {'schema': SCHEMA, 'source': 'authored', 'datasource': sheet.get('datasource'),
            'datasource_id': sheet.get('datasource_id'), 'mark': mark, 'authored_mark': authored,
            'pane_marks': marks, 'fields': fields, 'measure_values': [], 'sort': _sorts(sheet),
            'aggregate': _aggregated(ws), 'week_start': _week_start(ws),
            'forecast': _forecast_spec(ws),
            'mark_labels': bool(re.search(r'<format attr="mark-labels-show" value="true"', ws.get('source_xml') or ''))}
    if any(f.get('measure_values') for f in fields):
        plan['measure_values'] = _measure_values(sheet)
        param = measure_selector(sheet)
        if param:
            plan['measure_param'] = param
    sheet = dict(sheet)
    color, default_fmt = _presentation(sheet, fields)
    plan['hide_axis'] = [s for s in sheet.get('_hidden_axes') or [] if s in ('rows', 'cols')]
    # the authored height of one header row (px): a cell / header height. An axis 'height' is the depth of a
    # horizontal axis band, not a row (release 26g: it made a Gantt's rows 37 px instead of Tableau's).
    heights = []
    for rule in re.findall(r'<style-rule element="(cell|header)">(.*?)</style-rule>', ws.get('source_xml') or '', re.S):
        heights += [int(v) for v in re.findall(r'<format attr="height" field="[^"]+" value="(\d+)"', rule[1])]
    if heights:
        plan['row_height'] = max(heights)
    xml_all = ws.get('source_xml') or ''
    plan['hide_field_labels'] = sorted(set(re.findall(r'<format attr="display-field-labels" scope="(\w+)" value="false"',
                                                       xml_all)))
    if color:
        plan['color_scales'] = color
    if sheet.get('_shape_maps'):                 # EN25 / A10: authored shapes per member, hollow when 'open'
        plan['shape_maps'] = sheet['_shape_maps']
    wrap = {(sheet.get('ws_style') or {}).get(el, {}).get('wrap') for el in ('cell', 'label', 'header')} & {'on', 'true'}
    if wrap:
        plan['ws_style_wrap'] = wrap.pop()
    style = mark_style(sheet.get('ws_style'))
    if style:
        plan['mark_style'] = style
    order = next((s['manual'] for s in plan['sort'] if s.get('field') == 'Measure Names'), None)
    if order and plan['measure_values']:
        names = _physical_captions(sheet)
        keyed = []
        for tok in order:
            mt = re.search(r'\[(\w+):([^:\]]+):\w+\]$', tok)
            if mt:
                nm = _member_unescape(mt.group(2))
                keyed.append((mt.group(1), names.get(nm, nm)))
        rank = {k: i for i, k in enumerate(keyed)}
        plan['measure_values'].sort(key=lambda m: rank.get((m['agg'], m['caption']), len(rank)))
    for m in plan['measure_values']:
        fmt = default_fmt.get(m['caption'])
        if fmt:
            m['format'] = fmt
    blend = _blend(sheet, ir, fields)
    if blend:
        plan['blend'] = blend
    unlinked = _unlinked(sheet, ir, fields, blend)
    if unlinked:
        plan['unlinked'] = unlinked
    axes, mmarks = _measure_axes(sheet, occ, fields)
    if axes:
        plan['measure_axes'] = axes       # EN26: several measures on a shelf -- panes side by side / dual axes
        plan['measure_marks'] = mmarks
    names = _physical_captions(sheet)
    import v3_tooltips
    tip = v3_tooltips.parse(ws.get('source_xml') or '')
    if tip:
        ids = {f['id']: f for f in fields}
        plan['tooltip_lines'] = v3_tooltips.bind(tip, {o.get('token'): ids[o['id']] for o in occ if o.get('id') in ids})
    import v3_crosstab
    tot = v3_crosstab.parse_totals(ws.get('source_xml') or '')
    if tot:
        plan['totals'] = tot              # V7: grand totals / subtotals of a text table (display only)
    refl = _reference_lines(sheet, occ, fields, names, plan.get('measure_values'))
    if refl:
        plan['reflines'] = refl           # EN15: computed from the view's marks, drawn on the chart
    for s in plan['sort']:                # a computed sort's measure is part of Tableau's query (not drawn)
        by = s.get('by')
        cap = names.get(by, by)
        if cap and not any(f['caption'] == cap and f['kind'] == 'measure' for f in fields):
            agg = s.get('by_agg') or 'sum'
            fields.append({'id': 'sort:' + cap, 'caption': cap, 'channel': 'sort', 'kind': 'measure',
                           'discrete': False, 'date_part': 'none', 'agg': agg if agg in AGGS else 'sum',
                           'calculation': cap in names.values()})
    return plan


def _measure_axes(sheet, occ, fields):
    """EN26: for a shelf holding more than one measure, its axes in order: [{'measures': [field ids], 'synchronized'}]
    -- a measure the workbook folds onto the previous axis (<encoding attr='space' field=M fold='true'>) shares it
    (dual axis). Also each measure's own pane mark ({field id: mark})."""
    ws = sheet.get('worksheet_semantics') or {}
    xml = ws.get('source_xml') or ''
    folds = {}
    for attrs in re.findall(r"<encoding\b([^>]*attr=['\"]space['\"][^>]*)>", xml):
        a = dict(re.findall(r"([\w:-]+)=['\"]([^'\"]*)['\"]", attrs))
        if a.get('fold') == 'true' and a.get('field'):
            folds[a['field']] = a.get('synchronized') == 'true'
    token = {o['id']: o.get('token') for o in occ}
    out, marks = {}, {}
    for shelf in ('rows', 'cols'):
        ms = [f for f in fields if f['channel'] == shelf and f['kind'] == 'measure' and not f.get('measure_values')]
        if len(ms) < 2:
            continue
        groups = []
        for f in ms:
            t = token.get(f['id'])
            if groups and t in folds:
                groups[-1]['measures'].append(f['id'])
                groups[-1]['synchronized'] = groups[-1]['synchronized'] or folds[t]
            else:
                groups.append({'measures': [f['id']], 'synchronized': False})
        out[shelf] = groups
        for f in ms:
            pane = next((p for p in ws.get('panes') or [] if token.get(f['id']) in (p.get('axes') or {}).values()), None)
            mk = next((m for m in (pane or {}).get('marks') or [] if m and m != 'Automatic'), None)
            if MARK_MAP.get(mk):
                marks[f['id']] = MARK_MAP[mk]
    return out, marks


def _measure_panes(plan, df, engine, size, shelf):
    """EN26: one chart per measure on the shelf; measures folded onto one axis are overlaid (dual axis, scales
    independent unless synchronised), the axes stand side by side (Rows: stacked, Columns: next to each other)."""
    alt = engine.alt
    groups = plan['measure_axes'][shelf]
    on_shelf = [f for f in plan['fields'] if f['channel'] == shelf and f['kind'] == 'measure']
    size = dict(size or {})
    key = 'h' if shelf == 'rows' else 'w'
    if size.get(key):
        size[key] = max(60, int(size[key] / len(groups)) - 10)
    axis = 'y' if shelf == 'rows' else 'x'
    hoisted = {}

    def nested(c, shared_off=False):
        """A pane as a chart that can sit inside a layer / concat: its data inline, config and sizing hoisted.
        shared_off: the discrete headers the panes share are printed once (on the first pane, as in Tableau)."""
        d = c.to_dict()
        if shared_off:
            hide = 'y' if shelf == 'cols' else 'x'

            def strip(node):
                if isinstance(node, dict):
                    e = node.get('encoding')
                    if isinstance(e, dict) and isinstance(e.get(hide), dict) and \
                            e[hide].get('type') in ('nominal', 'ordinal'):
                        e[hide]['axis'] = {'labels': False, 'ticks': False, 'title': None, 'domain': False}
                    for v in node.values():
                        strip(v)
                elif isinstance(node, list):
                    for v in node:
                        strip(v)
            strip(d)
        named = d.pop('datasets', None) or {}
        if d.get('config') is not None:
            hoisted.setdefault('config', d['config'])
        for k in ('$schema', 'autosize', 'config'):
            d.pop(k, None)
        if isinstance(d.get('spacing'), dict):
            d['spacing'] = max(d['spacing'].values())

        def inline(node):
            if isinstance(node, dict):
                if isinstance(node.get('data'), dict) and node['data'].get('name') in named:
                    node['data'] = {'values': named[node['data']['name']]}
                for v in node.values():
                    inline(v)
            elif isinstance(node, list):
                for v in node:
                    inline(v)
        inline(d)
        return type(c).from_dict(d)
    parts = []
    for g in groups:
        charts = []
        for mid in g['measures']:
            # the shelf's other measures stay in the plan off the axes (a computed sort may order by them)
            sub = dict(plan, fields=[f if (f not in on_shelf or f['id'] == mid) else dict(f, channel='sort')
                                     for f in plan['fields']],
                       mark=(plan.get('measure_marks') or {}).get(mid, plan['mark']), measure_axes=None)
            c = build_chart(sub, df, engine, size)
            if c is not None:
                last = mid == groups[-1]['measures'][-1]
                first = mid == groups[0]['measures'][0]
                charts.append(nested(c, shared_off=(not first) if shelf == 'cols' else (not last)))
        if len(charts) > 1 and not any(isinstance(c, (alt.FacetChart, alt.HConcatChart, alt.VConcatChart))
                                       for c in charts):
            layered = alt.layer(*charts)
            parts.append(layered if g['synchronized'] else layered.resolve_scale(**{axis: 'independent'}))
        else:
            parts.extend(charts)          # a faceted pane cannot be overlaid: its axes stand side by side
    if not parts:
        return None
    out = parts[0] if len(parts) == 1 else alt.vconcat(*parts) if shelf == 'rows' else alt.hconcat(*parts)
    if hoisted.get('config'):
        out.config = alt.Config.from_dict(hoisted['config'])
    return out


def _reference_lines(sheet, occ, fields, names, measure_values=None):
    """EN15: the worksheet's reference lines bound to the plan's fields (v3_reflines)."""
    import v3_reflines as R
    specs = R.parse((sheet.get('worksheet_semantics') or {}).get('source_xml') or '')
    if not specs:
        return []
    ids = {f['id']: f for f in fields}
    by_token = {o.get('token'): ids[o['id']] for o in occ if o.get('id') in ids and ids[o['id']].get('kind') == 'measure'}
    holder = {'fields': fields, 'reflines': specs, 'measure_values': measure_values or []}
    R.bind(holder, by_token=by_token, captions=names, primary=sheet.get('datasource_id'))
    return specs


def _blend(sheet, ir, fields):
    """EN41: Tableau data blending for this sheet: every secondary datasource its fields (or cross-datasource
    calculations) use, each with its link fields (primary field = secondary field). Links are the workbook's custom
    links for that pair (<datasource-relationship>) when it defines any, else Tableau's automatic links: dimensions
    with the same display name in both. None when the sheet uses one datasource. With one secondary the old keys
    (secondary, secondary_id, links) are kept alongside 'secondaries'."""
    if not ir:
        return None
    primary_id = sheet.get('datasource_id')
    scopes = ir.get('field_scopes') or {}
    formulas = ' '.join(f.get('formula') or '' for f in fields)
    used = []
    for f in fields:
        ds = f.get('datasource_id')
        if ds and ds != primary_id and ds not in used:
            used.append(ds)
    for ds, sc in scopes.items():
        cap = sc.get('caption') or ds
        if ds != primary_id and ds not in used and (ds in formulas or f'[{cap}].' in formulas):
            used.append(ds)
    custom = {b.get('secondary_name'): b for b in ir.get('blends') or [] if b.get('primary_name') == primary_id}
    for b in custom.values():
        if b.get('secondary_name') not in used and (b.get('secondary_name', '') in formulas or
                                                    f"[{b.get('secondary')}]." in formulas):
            used.append(b['secondary_name'])
    if not used:
        return None
    names = _blend_names(ir)
    out = []
    for ds in used:
        cap = (custom.get(ds) or {}).get('secondary') or (scopes.get(ds) or {}).get('caption') or ds
        if ds in custom:
            links = [{'primary': l['primary_field'], 'secondary': l['secondary_field']}
                     for l in custom[ds].get('links') or []]
        else:
            prim, sec = names.get(primary_id) or {}, names.get(ds) or {}
            links = [{'primary': n, 'secondary': n} for n in prim if n in sec]
        out.append({'secondary': cap, 'secondary_id': ds, 'links': links})
    blend = {'secondaries': out}
    if len(out) == 1:
        blend.update(out[0])
    return blend


def _blend_names(ir):
    """Display names of each datasource's dimensions (the workbook's blend_fields; older IRs: the field list)."""
    if ir.get('blend_fields'):
        return {ds: set(v) for ds, v in ir['blend_fields'].items()}
    out = {}
    for e in (ir.get('fields') or {}).values():
        if e.get('role') == 'dimension' and e.get('datasource_id'):
            out.setdefault(e['datasource_id'], set()).add(e.get('caption') or e.get('internal'))
    return out


def _unlinked(sheet, ir, fields, blend):
    """DM5: captions of the datasources a sheet's fields come from that the blend does not cover (none since EN41:
    a secondary without links is aggregated over all its rows, as Tableau does)."""
    primary_id = sheet.get('datasource_id')
    covered = {b['secondary_id'] for b in (blend or {}).get('secondaries') or []}
    others = {f.get('datasource_id') for f in fields
              if f.get('datasource_id') and f.get('datasource_id') not in (primary_id,) and
              f.get('datasource_id') not in covered}
    return sorted(((ir or {}).get('field_scopes') or {}).get(o, {}).get('caption', o) for o in others)


def _xml_unescape(t):
    return (str(t or '').replace('&quot;', '"').replace('&lt;', '<').replace('&gt;', '>').replace('&apos;', "'")
            .replace('&amp;', '&'))


def _presentation(sheet, fields):
    """Workbook presentation for the plan: per-field number format (worksheet style rule, else the field's
    default format), hidden axis titles, and the authored colour scale (palette, range)."""
    xml = (sheet.get('worksheet_semantics') or {}).get('source_xml') or ''
    token_fmt, token_title = {}, {}
    for m in re.finditer(r'<format attr="text-format" field="([^"]+)" value="([^"]*)"', xml):
        if m.group(2):
            token_fmt.setdefault(m.group(1), _xml_unescape(m.group(2)))
    for m in re.finditer(r'<format attr="title" class="0" field="([^"]+)" scope="\w+" value="([^"]*)"', xml):
        token_title[m.group(1)] = _xml_unescape(m.group(2))
    hidden_axis, hidden_header = set(), set()
    for m in re.finditer(r'<format attr="display"( class="0")? field="([^"]+)"(?: scope="(\w+)")? value="false"', xml):
        if m.group(1):                         # class 0 = the axis of that field
            hidden_axis.add(m.group(3) or '')
        else:                                  # no class = the field's header
            hidden_header.add(m.group(2))
    default_fmt = {}
    for attrs in re.findall(r'<column\b([^>]*)>', xml):
        a = dict(re.findall(r'([\w-]+)="([^"]*)"', attrs))
        if a.get('default-format'):
            for key in (a.get('caption'), (a.get('name') or '').strip('[]')):
                if key:
                    default_fmt.setdefault(key, _xml_unescape(a['default-format']))
    occ = {o['id']: o for o in (sheet.get('worksheet_semantics') or {}).get('occurrences') or []}
    for f in fields:
        token = (occ.get(f['id']) or {}).get('token')
        phys = ((occ.get(f['id']) or {}).get('field_definition') or {}).get('physical')
        fmt = token_fmt.get(token) or default_fmt.get(f['caption']) or default_fmt.get(phys)
        if fmt:
            f['format'] = fmt
        if token in token_title:
            f['axis_title'] = token_title[token]
        if token in hidden_header:
            f['header_hidden'] = True
    # colour scales per field: the worksheet's own encoding first, then the datasource default for the field
    color = {}
    by_token = {(occ.get(f['id']) or {}).get('token'): f for f in fields}
    by_phys = {((occ.get(f['id']) or {}).get('field_definition') or {}).get('physical'): f for f in fields}
    ds_xml = str((sheet.get('worksheet_semantics') or {}).get('datasource_xml') or '')
    for source, text in (('sheet', xml), ('datasource', ds_xml)):
        for m in re.finditer(r'<encoding attr="color" field="([^"]+)"([^>]*?)/?>', text):
            a = dict(re.findall(r'([\w-]+)="([^"]*)"', m.group(2)))
            if a.get('type') not in ('interpolated', 'custom-interpolated') or not a.get('palette'):
                continue
            token = m.group(1)
            f = by_token.get(token) if source == 'sheet' else None
            if f is None:
                name = re.sub(r'^\[(?:[^\]]+\]\.\[)?(?:\w+:)?([^:\]]+)(?::\w+)?\]$', r'\1', token)
                f = by_phys.get(name)
            if f is None or f['caption'] in color:
                continue
            scale = {'palette': a['palette'], 'type': a['type']}
            for k in ('min', 'max'):
                try:
                    scale[k] = float(a[k])
                except (KeyError, ValueError):
                    pass
            color[f['caption']] = scale
    sheet['_hidden_axes'] = sorted(hidden_axis)
    shapes = _shape_maps(xml, by_token)
    if shapes:
        sheet['_shape_maps'] = shapes
    return color, default_fmt


# EN25 / A10: Tableau's built-in shape names -> Vega shapes; 'open' palettes draw hollow marks
TABLEAU_SHAPES = {'circle': 'circle', 'square': 'square', 'triangle': 'triangle-up', 'diamond': 'diamond',
                  'plus': 'cross', 'cross': 'cross', 'x': 'cross', 'star': 'circle', 'asterisk': 'cross',
                  'up-triangle': 'triangle-up', 'down-triangle': 'triangle-down', 'left-triangle': 'triangle-left',
                  'right-triangle': 'triangle-right', 'line': 'stroke', 'arrow': 'arrow', 'wedge': 'wedge'}


def mark_style(style):
    """A10 / EN25: the worksheet's mark formatting (ws_style['mark'], from <style-rule element='mark'> on the table
    or every pane) -- mark colour, transparency (0-255), shape (':filled/diamond'; ':open/circle' = hollow).
    {} when the sheet sets none."""
    a = (style or {}).get('mark') or {}
    out = {}
    if re.fullmatch(r'#[0-9a-fA-F]{6}', a.get('mark-color', '')):
        out['color'] = a['mark-color']
    try:
        alpha = int(float(a['mark-transparency']))
        if 0 <= alpha < 255:
            out['opacity'] = round(alpha / 255, 3)
    except (KeyError, ValueError):
        pass
    if a.get('has-stroke') == 'true' and re.fullmatch(r'#[0-9a-fA-F]{6}', a.get('stroke-color', '')):
        out['stroke'] = a['stroke-color']      # the mark border Tableau draws
    if a.get('mark-markers-mode') in ('all', 'none'):
        out['markers'] = a['mark-markers-mode'] == 'all'
    kind, _, name = a.get('shape', '').lstrip(':').partition('/')
    if kind == 'custom' or '/' in name or name.lower().endswith(('.png', '.svg', '.jpg', '.gif')):
        out['custom_shape'] = a['shape'].split('/')[-1]   # a custom shape image: not packaged in the .twbx
    elif TABLEAU_SHAPES.get(name.lower()):
        out['shape'] = TABLEAU_SHAPES[name.lower()]
        if kind == 'open':
            out['open'] = True
    return out


def wrap_labels(plan):
    """A10: Format > Cells / Labels 'Wrap' on: header text wraps (Tableau's default truncates with an ellipsis)."""
    st = plan.get('ws_style_wrap')
    return st in ('on', 'true')


def wrap_expr(values, width=16, max_lines=3):
    """A Vega labelExpr giving each long label its wrapped lines (word boundaries, at most max_lines; the last
    line keeps the rest). None when no label is longer than width."""
    import json
    import textwrap
    table = {}
    for v in values:
        if len(v) > width:
            lines = textwrap.wrap(v, width) or [v]
            if len(lines) > max_lines:
                lines = lines[:max_lines - 1] + [' '.join(lines[max_lines - 1:])]
            table[v] = lines
    if not table or len(table) > 300:
        return None
    return f"({json.dumps(table, ensure_ascii=False)})[datum.label] || datum.label"


def _shape_maps(xml, by_token):
    """{caption: {'members': {member: vega shape}, 'open': bool, 'custom': [files]}} from the sheet's shape
    encodings (<map to=':filled/circle'><bucket>...</bucket></map>). A custom shape image that is not packaged
    falls back to Tableau's own default order (named in 'custom')."""
    out = {}
    for m in re.finditer(r'<encoding attr="shape" field="([^"]+)"[^>]*>(.*?)</encoding>', xml or '', re.S):
        f = by_token.get(m.group(1))
        if f is None:
            continue
        members, opened, custom = {}, [], []
        for to, body in re.findall(r'<map to="([^"]+)">(.*?)</map>', m.group(2), re.S):
            kind, _, name = to.lstrip(':').partition('/')
            shape = TABLEAU_SHAPES.get(name.lower()) if kind in ('filled', 'open', 'shapes', '') else None
            if kind == 'custom':
                custom.append(name)
            for b in re.findall(r'<bucket>(.*?)</bucket>', body, re.S):
                member = _xml_unescape(b).strip().strip('"')
                if shape:
                    members[member] = shape
                    opened.append(kind == 'open')
        if members or custom:
            out[f['caption']] = {'members': members, 'open': bool(opened) and all(opened), 'custom': custom}
    return out


def _aggregated(ws):
    """Analysis > Aggregate Measures: <aggregation value='false'/> shows one mark per data row."""
    m = re.search(r'<aggregation value="(true|false)"', ws.get('source_xml') or '')
    return not (m and m.group(1) == 'false')


def _week_start(ws):
    """The datasource's first day of week (Tableau default: Sunday)."""
    for text in (ws.get('datasource_xml') or '', ws.get('source_xml') or ''):
        m = re.search(r'start-of-week=["\'](\w+)["\']', str(text))
        if m:
            return m.group(1).lower()
    return 'sunday'


def _sorts(sheet):
    """Authored sorts from the sheet XML: computed (by a measure) and manual member orders."""
    xml = (sheet.get('worksheet_semantics') or {}).get('source_xml') or ''
    out = []
    for m in re.finditer(r'<computed-sort\b([^>]*)/?>', xml):
        a = dict(re.findall(r'([\w-]+)="([^"]*)"', m.group(1)))
        out.append({'field': _field_ordering(a.get('column')), 'by': _field_ordering(a.get('using')),
                    'by_agg': (re.search(r'\[(\w+):', a.get('using') or '') or [None, None])[1],
                    'direction': 'desc' if a.get('direction', 'ASC').upper() == 'DESC' else 'asc'})
    for m in re.finditer(r'<manual-sort column="([^"]+)"[^>]*>(.*?)</manual-sort>', xml, re.S):
        buckets = [_xml_unescape(b).strip() for b in re.findall(r'<bucket>(.*?)</bucket>', m.group(2), re.S)]
        values = []
        for b in buckets:
            if b == '%all%':
                continue
            b = b[1:-1] if len(b) > 1 and b[0] == '"' and b[-1] == '"' else b
            values.append(b.replace('\\%', '%'))
        token = m.group(1)
        if token.endswith('[:Measure Names]'):
            out.append({'field': 'Measure Names', 'manual': values})
        else:
            out.append({'field': _field_ordering(token), 'manual': values})
    if not any(s.get('manual') for s in out):
        manual = sheet.get('manual_sort')
        if isinstance(manual, dict) and manual.get('on') and manual.get('order'):
            out.append({'field': manual['on'], 'manual': [v for v in manual['order'] if v != '%all%']})
    return out


# --------------------------------------------------------------------------- validation (closed lists)
def validate(plan, captions=None):
    """Problems with a plan (empty list = valid). `captions` = fields known in the sheet's datasource."""
    p = []
    if not isinstance(plan, dict) or plan.get('schema') != SCHEMA:
        return ['not a sheet-plan/1 object']
    if plan.get('mark') not in MARKS:
        p.append(f"mark {plan.get('mark')!r} is not in the closed list")
    fields = plan.get('fields')
    if not isinstance(fields, list) or not fields:
        return p + ['fields must be a non-empty list']
    for i, f in enumerate(fields):
        if not isinstance(f, dict):
            p.append(f'fields[{i}] is not an object')
            continue
        if f.get('channel') not in CHANNELS:
            p.append(f'fields[{i}].channel not allowed')
        if f.get('kind') not in ('dim', 'measure', 'geo', 'measure_names', 'generated'):
            p.append(f'fields[{i}].kind not allowed')
        if f.get('date_part', 'none') not in PLAN_DATE_PARTS:
            p.append(f'fields[{i}].date_part not allowed')
        if f.get('agg', 'none') not in AGGS:
            p.append(f'fields[{i}].agg not allowed')
        w = f.get('window')
        if w is not None and (not isinstance(w, dict) or w.get('addressing') not in ADDRESSING):
            p.append(f'fields[{i}].window addressing not allowed')
        tc = f.get('table_calc')
        if tc is not None:
            if not isinstance(tc, dict) or tc.get('type') not in TABLE_CALCS:
                p.append(f'fields[{i}].table_calc type not supported')
            elif tc.get('addressing') not in ADDRESSING:
                p.append(f'fields[{i}].table_calc addressing not allowed')
            elif tc['type'] == 'Rank' and tc.get('rank', 'competition') not in RANK_KINDS:
                p.append(f'fields[{i}].table_calc rank kind not allowed')
        if captions is not None and f.get('kind') in ('dim', 'measure') and not f.get('measure_values') \
                and f.get('caption') not in captions:
            p.append(f"fields[{i}] refers to unknown field {f.get('caption')!r}")
    if plan.get('reflines'):
        import v3_reflines
        p += v3_reflines.validate(plan['reflines'])
    ids = {f.get('id') for f in fields if isinstance(f, dict)}
    for shelf, groups in (plan.get('measure_axes') or {}).items():
        if shelf not in ('rows', 'cols') or not all(isinstance(g, dict) and set(g.get('measures') or []) <= ids
                                                    for g in groups):
            p.append('measure_axes refer to unknown fields')
    for mid, mk in (plan.get('measure_marks') or {}).items():
        if mid not in ids or mk not in MARKS:
            p.append('measure_marks not valid')
    for i, m in enumerate(plan.get('measure_values') or []):
        if not isinstance(m, dict) or m.get('agg', 'sum') not in AGGS or \
                (captions is not None and m.get('caption') not in captions):
            p.append(f'measure_values[{i}] not valid')
    return p


def fingerprint(plan):
    body = {k: v for k, v in plan.items() if k not in ('source', 'evidence')}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- query
def query_fields(plan):
    """(dimensions, measures) that define the query: dims are grouped at the view's level of detail;
    measures are aggregated. Aggregate dimensions (IF SUM(..)) are computed like measures."""
    dims, measures, seen_d, seen_m = [], [], set(), set()
    for f in plan['fields']:
        if f['kind'] == 'dim' and not f.get('aggregate_dimension') and not f.get('table_calc'):
            key = (f['caption'], f['date_part'])
            if key not in seen_d:
                seen_d.add(key)
                dims.append(f)
        elif f['kind'] == 'measure' and f.get('measure_values'):
            for m in plan.get('measure_values') or []:
                key = (m['caption'], m.get('agg', 'sum'))
                if key not in seen_m:
                    seen_m.add(key)
                    measures.append({'caption': m['caption'], 'agg': m.get('agg', 'sum'), 'kind': 'measure',
                                     'channel': 'measure_values', **({'mv_field_id': m['member_id']}
                                                                    if m.get('member_id') else {})})
        elif f['kind'] == 'measure' or f.get('aggregate_dimension'):
            key = (f['caption'], f['agg'])
            if key not in seen_m:
                seen_m.add(key)
                measures.append(f)
    return dims, measures


def col_name(f, measure=False):
    """Stable result-column name for a field (also the key used by the comparison)."""
    if measure:
        return f"M|{f['agg']}|{f['caption']}"
    return f"D|{f['date_part']}|{f['caption']}"


WEEKDAY_INDEX = {'sunday': 0, 'monday': 1, 'tuesday': 2, 'wednesday': 3, 'thursday': 4, 'friday': 5,
                 'saturday': 6}


def _date_sql(expr, part, week_start='sunday', fiscal=1):
    """SQL for a date field at a date part. EN17: fiscal (1-12) is the month the fiscal year starts; year / quarter
    and their truncations follow it (a fiscal year is named by the calendar year it ends in, as in Tableau). Week and
    weekday follow Tableau's numbering on the datasource's first day of week, independent of session settings."""
    fiscal = int(fiscal or 1)
    if fiscal > 1 and part in ('year', 'quarter', 'year_trunc', 'quarter_trunc'):
        shift = 13 - fiscal
        moved = f"DATEADD('month', {shift}, {expr})"
        if part in ('year', 'quarter'):
            return f"EXTRACT({part.upper()} FROM {moved})"
        unit = part.split('_')[0]
        return f"CAST(DATEADD('month', {-shift}, DATE_TRUNC('{unit}', {moved})) AS DATE)"
    if part == 'week_trunc':
        # SQL weeks start on Monday; shift so the week starts on the datasource's first day of week
        shift = (1 - WEEKDAY_INDEX.get(week_start, 0)) % 7
        if not shift:
            return f"CAST(DATE_TRUNC('week', {expr}) AS DATE)"
        return (f"CAST(CAST(DATE_TRUNC('week', CAST({expr} AS DATE) + INTERVAL '{shift} DAY') AS DATE) "
                f"- INTERVAL '{shift} DAY' AS DATE)")
    if part == 'none':
        return expr
    if part in ('year', 'quarter', 'month', 'day'):
        return f"EXTRACT({part.upper()} FROM {expr})"
    if part in ('week', 'weekday'):
        # days since a known day that starts a week (1970-01-04 is a Sunday), mod 7: the weekday counted from the
        # datasource's first day of week; Tableau week 1 holds 1 January
        start = f"DATE '1970-01-{4 + WEEKDAY_INDEX.get(week_start, 0):02d}'"

        def dow(d):
            return f"(((DATEDIFF('day', {start}, {d}) % 7) + 7) % 7)"
        if part == 'weekday':                 # 0 = Sunday (Tableau's weekday member order, key_text)
            return f"(((DATEDIFF('day', DATE '1970-01-04', {expr}) % 7) + 7) % 7)"
        return f"(FLOOR((EXTRACT(DOY FROM {expr}) - 1 + {dow(chr(68) + 'ATE_TRUNC(' + chr(39) + 'year' + chr(39) + ', ' + expr + ')')}) / 7) + 1)"
    unit = part.split('_')[0]
    return f"CAST(DATE_TRUNC('{unit}', {expr}) AS DATE)"


_MONTHS_LOWER = ['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 'september', 'october',
                 'november', 'december']


def _month_number(v):
    v = str(v or '').strip().lower()
    if v.isdigit() and 1 <= int(v) <= 12:
        return int(v)
    return next((i + 1 for i, m in enumerate(_MONTHS_LOWER) if v and m.startswith(v[:3])), 1)


def _full_range_fields(ws):
    """DENS: fields with Tableau's "Show Missing Values" (<show-full-range>) on this worksheet."""
    out = set()
    for block in re.findall(r'<show-full-range>(.*?)</show-full-range>', str(ws.get('source_xml') or ''), re.S):
        for col in re.findall(r'<column>(.*?)</column>', block, re.S):
            m = re.search(r'\[([^\[\]]+)\]\s*$', col.strip())
            if m:
                out.add(m.group(1))
    return out


def _fiscal_start(ws, field_id):
    """EN17: the month a date field's fiscal year starts -- the field's own setting, else the datasource's, else 1."""
    xml = str(ws.get('datasource_xml') or '') + str(ws.get('source_xml') or '')
    name = (field_id or '').split('::')[-1]
    if name:
        for m in re.finditer(r"<column\b[^>]*>", xml):
            tag = m.group(0)
            if re.search(r"name=['\"]\[" + re.escape(name) + r"\]['\"]", tag):
                f = re.search(r"fiscal[-_]year[-_]start=['\"](\w+)['\"]", tag)
                if f:
                    return _month_number(f.group(1))
    f = re.search(r"<date-options\b[^>]*fiscal[-_]year[-_]start=['\"](\w+)['\"]", xml)
    return _month_number(f.group(1)) if f else 1

def measure_having(sheet, engine, where_parts=None):
    """E06: the sheet's measure filters (aggregated measure or aggregate calculation). They reach every query
    through the engine's sheet WHERE (engine._measure_filter_predicate); a blended sheet with one refuses. A column
    a dashboard filter widget governs is that widget's (as in sheet_where). Call inside engine.field_scope(sheet)."""
    applicable = engine._parts_for_sheet(where_parts, sheet) if isinstance(where_parts, list) else where_parts
    governed = {p['col'] for p in applicable if isinstance(p, dict)} if isinstance(applicable, list) else set()
    return [f for f in sheet.get('applied_filters') or []
            if engine.is_measure_filter(f) and engine.px(f['caption']) not in governed]


def compile_query(plan, sheet, engine, where='', lod_where=None, extra_select=(), having=()):
    """One read-only aggregate SELECT for the plan, built only from the engine's field resolution.
    Returns (sql, columns) where columns = [(result column, field dict, is_measure)]."""
    dims, measures = query_fields(plan)
    T = engine.tbl(sheet)
    select, cols, group = [], [], []
    scope = ((getattr(engine, 'IR', {}) or {}).get('field_scopes') or {}).get(sheet.get('datasource_id')) or {}
    key_grain = []                            # X12: (measure index, key) aggregated at the key's grain
    lod_rows = []                             # C14: INCLUDE / EXCLUDE row windows over the filtered rows
    for i, d in enumerate(dims):
        expr = _date_sql(engine.rdim(d['caption']), d['date_part'], plan.get('week_start', 'sunday'), d.get('fiscal', 1))
        select.append(f'{expr} AS D{i}')
        cols.append((f'D{i}', d, False))
        group.append(str(i + 1))
    for j, m in enumerate(measures):
        ref = {'caption': m['caption'], 'agg': None if m['agg'] in ('usr', 'none', 'attr') else m['agg']}
        if m.get('count_records'):
            ref['count_records'] = True
        if m.get('field_id'):
            ref['field_id'] = m['field_id']
        elif m.get('mv_field_id'):
            irf = (getattr(engine, 'IR', None) or {}).get('fields') or {}
            entry = irf.get(m['mv_field_id'])
            if entry and entry.get('datasource_id') == sheet.get('datasource_id') and \
                    sum(1 for v in irf.values() if v.get('caption') == m['caption']) > 1:
                ref['field_id'] = m['mv_field_id']      # a Measure Values member whose caption is shared
        if m['caption'] in getattr(engine, 'PARAMS', {}) and (m.get('field_id') or '').startswith('[Parameters]') \
                or (m['caption'] in getattr(engine, 'PARAMS', {}) and m['caption'] not in engine.CALCS
                    and not (m.get('field_id') or '').count('::')):
            import calc_translator
            expr = calc_translator.param_sql_literal(engine.PARAMS[m['caption']])     # a parameter on the view
        elif not plan.get('aggregate', True) and not m.get('count_records') and \
                not (m['caption'] in engine.CALCS and engine.CALCS[m['caption']].get('agg_ready')):
            expr = engine.rdim(m['caption'])         # disaggregated view: one value per data row
        elif m['agg'] == 'attr':
            expr = f"CASE WHEN MIN({engine.rdim(m['caption'])}) = MAX({engine.rdim(m['caption'])}) " \
                   f"THEN MIN({engine.rdim(m['caption'])}) END"
        else:
            r = engine.resolve_measure(T, sheet.get('name'), ref)
            if not r:
                raise PlanError(f"Measure '{m['caption']}' could not be translated")
            expr = r['sql']
            if '__INCLUDE__(' in expr or '__EXCLUDE__(' in expr:
                expr = lod_view_sql(expr, m, dims, engine, plan.get('week_start', 'sunday'), rows=lod_rows)
            elif r.get('window'):
                calc = engine.CALCS.get(m['caption']) or {}
                if m.get('nested_windows') and calc.get('sql_tagged') and calc.get('sql') == expr:
                    expr = calc['sql_tagged']             # EN26: windows tagged by their nested calculation
                expr = window_sql(expr, plan, m, engine, dims)
            if scope.get('relationship_model') and plan.get('aggregate', True):
                expr = _apply_key_grain(expr, engine, key_grain)
        select.append(f'{expr} AS M{j}')
        cols.append((f'M{j}', m, True))
    if not select:
        raise PlanError('Nothing to query')
    select += [f'{e} AS {a}' for e, a in extra_select]          # C6: table-calculation filter flags
    having = list(having or [])
    if having:                                # C10: a measure filter's FIXED is computed before the filters too
        select = select + [f'({h}) AS __HAVING{i}__' for i, h in enumerate(having)]
    if (key_grain or lod_rows) and lod_where is None:
        lod_where = ''
    select, T, where = _fixed_before_filters(select, T, where, lod_where, engine, force=bool(key_grain or lod_rows))
    if having:                                # the HAVING predicates come back out of the (hoisted) select list
        joined = ', '.join(select)
        having, kept = [], []
        for item in _split_top(joined):
            m = re.fullmatch(r'\((.*)\) AS __HAVING\d+__', item.strip(), re.S)
            (having if m else kept).append(m.group(1) if m else item)
        select = [', '.join(kept)]
    if key_grain or lod_rows:                 # numbered / windowed over the FILTERED rows, per mark (and key)
        items = _split_top(', '.join(select))
        marks = [re.sub(r'\s+AS\s+D\d+\s*$', '', it.strip(), flags=re.I) for it in items
                 if re.search(r'\s+AS\s+D\d+\s*$', it, re.I)]
        rn = [f"ROW_NUMBER() OVER (PARTITION BY {', '.join(marks + [k])} ORDER BY {k}) AS __KG{i}__"
              for i, k in enumerate(key_grain)] + [f'{w} AS {a}' for w, a in lod_rows]
        T, where = f"(SELECT *, {', '.join(rn)} FROM {T} {where or ''}) __kg", ''
    pad = _padding(dims, select, engine) if plan.get('aggregate', True) else None
    if pad:
        # DENS: Show Missing Values -- the marks padded to the field's full range, then every window over them
        if having:
            raise PlanError('Show Missing Values with a measure filter is not supported yet')
        masked, spans = _mask_subqueries(', '.join(select), engine)
        sql = _unmask_subqueries(_layered_windows(_split_top(masked), T, where, group, pad=pad), spans)
        return engine.sub_params(sql + f' LIMIT {MAX_ROWS + 1}'), cols
    if _nested_window(', '.join(select)):
        # C14: a table calculation over another one -- aggregates at the marks first, then each window level
        if having or not plan.get('aggregate', True):
            raise PlanError('A table calculation over another one, with a measure filter or in a disaggregated '
                            'view, is not supported yet')
        masked, spans = _mask_subqueries(', '.join(select), engine)      # scalar subqueries keep their own FROM
        sql = _unmask_subqueries(_layered_windows(_split_top(masked), T, where, group), spans)
        return engine.sub_params(sql + f' LIMIT {MAX_ROWS + 1}'), cols
    sql = f"SELECT {', '.join(select)} FROM {T} {where or ''}"
    if group and plan.get('aggregate', True):
        sql += ' GROUP BY ' + ', '.join(group)
    if having:
        sql += ' HAVING ' + ' AND '.join(f'({h})' for h in having)
    if group and plan.get('aggregate', True):
        # R45: GROUP BY has no order in DuckDB (threads) or Snowflake; Tableau lists headers by their values --
        # ordered by the dimensions, the rows and the ones the cap keeps are the same on every run
        sql += ' ORDER BY ' + ', '.join(group)
    sql += f' LIMIT {MAX_ROWS + 1}'
    return engine.sub_params(sql), cols


_LEAF = re.compile(r'\b(SUM|AVG|MIN|MAX|COUNTD|COUNT|MEDIAN|ATTR)\s*\(\s*\[(?:([^\]]+)\]\.\[)?([^\]]+)\]\s*\)', re.I)
_LEAF_AGG = {'SUM': 'sum', 'AVG': 'avg', 'MIN': 'min', 'MAX': 'max', 'COUNT': 'cnt', 'COUNTD': 'ctd',
             'MEDIAN': 'med', 'ATTR': 'attr'}


def compile_blend(plan, sheet, engine, where='', lod_where=None):
    """Tableau data blending (EN41): the primary at the view's level of detail; each secondary aggregated at its
    active link fields (links whose primary field is a view dimension, at the same date part) and LEFT JOINed on
    those keys; a secondary with no link at all is aggregated over all its rows (every mark gets that value, as in
    Tableau); calculations that mix datasources are evaluated on the joined rows. Returns (sql, columns)."""
    import calc_translator
    blend = plan['blend']
    secs = blend.get('secondaries') or [{k: blend[k] for k in ('secondary', 'secondary_id', 'links')}]
    dims, measures = query_fields(plan)
    for sec in secs:
        links = {l['primary']: l['secondary'] for l in sec['links']}
        sec['_links'] = links
        sec['_active'] = [(i, d) for i, d in enumerate(dims) if d['caption'] in links]
        if links and not sec['_active']:
            raise PlanError(f"Blend secondary '{sec['secondary']}' has no active link field in the view")
    by_id = {sec['secondary_id']: n for n, sec in enumerate(secs)}

    def owner(qual):
        for n, sec in enumerate(secs):
            if qual in (sec['secondary_id'], sec['secondary']):
                return n
        return None
    prim_measures, sec_measures, cross = [], {n: [] for n in range(len(secs))}, []
    for m in measures:
        if m.get('datasource_id') in by_id:
            sec_measures[by_id[m['datasource_id']]].append(m)
        elif m.get('formula') and any(sec['secondary_id'] in m['formula'] or f"[{sec['secondary']}]." in m['formula']
                                      for sec in secs):
            cross.append(m)
        else:
            prim_measures.append(m)
    # leaves of the cross-datasource calculations: (side, name, agg); side = 'p' or the secondary's index
    leaves, cross_sql = [], {}
    for m in cross:
        formula = m['formula']

        def leaf(mt):
            agg, qual, name = mt.group(1).upper(), mt.group(2), mt.group(3)
            n = owner(qual) if qual else None
            key = ('p' if n is None else n, name, _LEAF_AGG[agg])
            if key not in leaves:
                leaves.append(key)
            return f'[BLENDLEAF{leaves.index(key)}]'
        replaced = _LEAF.sub(leaf, formula)
        sql, _ = calc_translator.translate_formula(replaced, params=getattr(engine, 'PARAMS', None))
        if not sql or 'BLENDLEAF' not in sql and _LEAF.search(formula):
            raise PlanError(f"'{m['caption']}' could not be translated across the blend")
        cross_sql[id(m)] = sql
    ws = plan.get('week_start', 'sunday')
    # the primary at the view's level of detail through the ordinary compiler (sets, LODs, windows, filters)
    leaf_fields = {k: {'id': f'blendleaf{k}', 'caption': name, 'kind': 'measure', 'channel': 'blend', 'agg': agg,
                       'discrete': False, 'date_part': 'none'}
                   for k, (side, name, agg) in enumerate(leaves) if side == 'p'}
    mkey = lambda f: ('m', f['caption'], f.get('agg'))
    dkey = lambda f: ('d', f['caption'], f.get('date_part'))
    not_primary = {mkey(m) for m in measures if not any(m is x for x in prim_measures)}
    is_measure = lambda f: f['kind'] == 'measure' or f.get('aggregate_dimension')
    p_plan = dict(plan, fields=[f for f in plan['fields'] if not (is_measure(f) and mkey(f) in not_primary)]
                  + list(leaf_fields.values()))
    p_plan.pop('blend', None)
    p_sql, p_cols = compile_query(p_plan, sheet, engine, where, lod_where=lod_where)
    alias = {(mkey(f) if is_m else dkey(f)): c for c, f, is_m in p_cols}
    alias.update({id(f): alias.get(mkey(f) if (is_measure(f) or f in prim_measures or f in leaf_fields.values())
                                   else dkey(f)) for f in list(dims) + prim_measures + list(leaf_fields.values())})
    cols = []
    for i, d in enumerate(dims):
        if id(d) not in alias:
            raise PlanError(f"Dimension '{d['caption']}' could not be placed in the blend")
        cols.append((f'D{i}', d, False))
    dim_ref = {i: f'P.{alias[id(d)]}' for i, d in enumerate(dims)}
    ctes, joins = [f'P AS ({p_sql})'], []
    for n, sec in enumerate(secs):
        scope = {'datasource_id': sec['secondary_id'], 'datasource': sec['secondary']}
        T2 = engine.tbl(scope)                 # DM2: an unbound secondary refuses, never a default table
        with engine.field_scope(scope):
            s_sel = [f"{_date_sql(engine.rdim(sec['_links'][d['caption']]), d['date_part'], ws, d.get('fiscal', 1))} AS K{q}"
                     for q, (_i, d) in enumerate(sec['_active'])]
            for j, m in enumerate(sec_measures[n]):
                r = engine.resolve_measure(T2, sheet.get('name'), {'caption': m['caption'],
                                           'agg': None if m['agg'] in ('usr', 'none') else m['agg']})
                if not r:
                    raise PlanError(f"Secondary measure '{m['caption']}' could not be translated")
                s_sel.append(f"{r['sql']} AS SM{j}")
            for k, (side, name, agg) in enumerate(leaves):
                if side == n:
                    r = engine.resolve_measure(T2, sheet.get('name'), {'caption': name, 'agg': agg})
                    if not r:
                        raise PlanError(f"Secondary '{name}' could not be translated")
                    s_sel.append(f"{r['sql']} AS L{k}")
        if not s_sel:
            continue
        grp = (' GROUP BY ' + ', '.join(str(q + 1) for q in range(len(sec['_active'])))) if sec['_active'] else ''
        ctes.append(f"S{n} AS (SELECT {', '.join(s_sel)} FROM {T2}{grp})")
        if sec['_active']:
            on = ' AND '.join(f'{dim_ref[i]} IS NOT DISTINCT FROM S{n}.K{q}' for q, (i, _d) in enumerate(sec['_active']))
            joins.append(f'LEFT JOIN S{n} ON {on}')
        else:
            joins.append(f'CROSS JOIN S{n}')      # no link: the whole secondary, one row
    out = [f'{dim_ref[i]} AS D{i}' for i in range(len(dims))]
    leaf_ref = lambda k: (f'P.{alias[id(leaf_fields[k])]}' if leaves[k][0] == 'p' else f'S{leaves[k][0]}.L{k}')
    j = 0
    for m in measures:
        if m in prim_measures:
            expr = f'P.{alias[id(m)]}'
        elif any(m in v for v in sec_measures.values()):
            n = next(n for n, v in sec_measures.items() if m in v)
            expr = f'S{n}.SM{sec_measures[n].index(m)}'
        else:
            expr = re.sub(r'\bBLENDLEAF(\d+)\b', lambda mt: leaf_ref(int(mt.group(1))), cross_sql[id(m)])
        out.append(f'{expr} AS M{j}')
        cols.append((f'M{j}', m, True))
        j += 1
    sql = (f"WITH {', '.join(ctes)} SELECT {', '.join(out)} FROM P {' '.join(joins)} LIMIT {MAX_ROWS + 1}")
    return engine.sub_params(sql), cols


def _sort_expr(srt, engine):
    """The SQL a computed sort orders by (the sort measure at its aggregation), None if it cannot be resolved."""
    cap, agg = srt.get('by'), (srt.get('by_agg') or 'sum').lower()
    calc = engine.CALCS.get(cap) or {}
    if agg in ('usr', 'none', 'attr') or calc:
        return calc.get('sql') if calc.get('agg_ready') else None
    fn = {'sum': 'SUM', 'avg': 'AVG', 'min': 'MIN', 'max': 'MAX', 'cnt': 'COUNT', 'med': 'MEDIAN'}.get(agg)
    if agg == 'ctd':
        return f'COUNT(DISTINCT {engine.rdim(cap)})'
    return f'{fn}({engine.rdim(cap)})' if fn else None


def _along_order(plan, along, engine, by, reverse=False):
    """C6: ORDER BY terms of the addressing in Tableau's order -- each addressed dimension in its own sort (a
    manual member order, a computed sort by a measure in its direction, else ascending), never an arbitrary order.
    None when a computed sort cannot be resolved (an order-dependent calculation then refuses)."""
    flip = {'ASC': 'DESC', 'DESC': 'ASC'}
    terms = []
    for d, base in zip(along, by(along)):
        phys = (d.get('field_id') or '').split('::')[-1]
        srt = next((x for x in plan.get('sort') or [] if x.get('field') in (d['caption'], phys)), None)
        if srt and srt.get('manual'):
            whens = ' '.join(f"WHEN '{str(v).replace(chr(39), chr(39) * 2)}' THEN {i}"
                             for i, v in enumerate(srt['manual']))
            pairs = [(f'CASE CAST({base} AS VARCHAR) {whens} ELSE {len(srt["manual"])} END', 'ASC'), (base, 'ASC')]
        elif srt and srt.get('by'):
            m = _sort_expr(srt, engine)
            if m is None:
                return None
            pairs = [(m, 'DESC' if srt.get('direction') == 'desc' else 'ASC'), (base, 'ASC')]
        else:
            pairs = [(base, 'ASC')]
        for e, o in pairs:                    # ascending is written bare: an unsorted view's SQL is unchanged
            o = flip[o] if reverse else o
            terms.append(e if o == 'ASC' else f'{e} DESC')
    return terms


def _fill_nested(out, plan, f, engine, by):
    """EN26: the windows of calculations nested in this one, each with its OWN addressing from the pill
    (<table-calc field='[Inner]'>); a nested calculation the pill does not address follows the outer one."""
    nested = f.get('nested_windows') or {}
    outer = f.get('window') or {'addressing': 'table'}
    for t in sorted(set(re.findall(r'@([A-Za-z0-9_~]+)__', out))):
        along, part = _addressing(plan, {'table_calc': nested.get(t) or outer})
        partition = ('PARTITION BY ' + ', '.join(by(part))) if part else ''
        asc = _along_order(plan, along, engine, by)
        desc = _along_order(plan, along, engine, by, reverse=True)
        if asc is None or desc is None:
            raise PlanError(f"'{f.get('caption')}': the sheet's sort for a nested table calculation could not be "
                            f"resolved")
        order = ', '.join(asc) or '1'
        out = out.replace(f'__TC_ORDER_DESC@{t}__', (partition + ' ORDER BY ' + (', '.join(desc) or '1 DESC')).strip())
        out = out.replace(f'__TC_ORDER@{t}__', (partition + ' ORDER BY ' + order).strip())
        out = out.replace(f'__WIN_ORDER@{t}__', (partition + ' ORDER BY ' + order).strip())
        out = out.replace(f'OVER (__TABLE@{t}__)', f'OVER ({partition})')
    return out


def nested_table_calcs(xml_list):
    """EN26: {calculation tag: addressing} for every calculation nested in a pill's table calculation, from its
    <table-calc field='[ds].[Inner]' ...> elements."""
    import calc_translator
    out = {}
    for text in xml_list or []:
        el = _tc_element(text)
        if el is None or not el.get('field'):
            continue
        name = re.sub(r'^\[[^\]]+\]\.', '', el.get('field')).strip('[]')
        tc = _table_calc([text])
        if tc:
            out[calc_translator.tag_name(name)] = tc
    return out


def window_sql(expr, plan, f, engine, dims):
    """A calculated field's table functions (INDEX, LOOKUP, RUNNING_*, FIRST/LAST, WINDOW_*, TOTAL, RANK*) evaluated
    over the aggregated view with Tableau's addressing: PARTITION BY the partitioning dimensions, ORDER BY the
    addressed ones in the sheet's own sort."""
    addressing = (f.get('window') or {'addressing': 'table'})
    along, part = _addressing(plan, {'table_calc': addressing})
    by = lambda ds: [_date_sql(engine.rdim(d['caption']), d['date_part'], plan.get('week_start', 'sunday'), d.get('fiscal', 1))
                     for d in ds]
    partition = ('PARTITION BY ' + ', '.join(by(part))) if part else ''
    ordered = '__WIN_ORDER__' in expr or '__TC_ORDER' in expr
    asc = _along_order(plan, along, engine, by) if ordered else []
    desc = _along_order(plan, along, engine, by, reverse=True) if ordered else []
    if ordered and (asc is None or desc is None):
        raise PlanError(f"'{f.get('caption')}': the sheet's sort for this table calculation could not be resolved")
    order = ', '.join(asc or []) or '1'
    out = expr.replace('__WIN_ORDER__', (partition + ' ORDER BY ' + order).strip())
    # C6: LOOKUP / RUNNING_* / FIRST (__TC_ORDER__) along the addressing; LAST() counts from the end of the
    # partition -- the same order, reversed
    out = out.replace('__TC_ORDER_DESC__', (partition + ' ORDER BY ' + (', '.join(desc or []) or '1 DESC')).strip())
    out = out.replace('__TC_ORDER__', (partition + ' ORDER BY ' + order).strip())
    out = re.sub(r'OVER \(\s*\)', f'OVER ({partition})', out)
    if partition:
        out = re.sub(r'OVER \(ORDER BY', f'OVER ({partition} ORDER BY', out)
    if '@' in out:                            # EN26: nested calculations' own addressing (after the outer fill)
        out = _fill_nested(out, plan, f, engine, by)
    # after the OVER () rewrite above: an EXCLUDE over every view dimension is a plain OVER () (whole view)
    return expand_view_except(out, dims, engine, plan.get('week_start', 'sunday'))


_VIEW_EXCEPT = re.compile(r'__VIEW_EXCEPT\[([^\]]*)\]__')


def expand_view_except(expr, dims, engine, week_start='sunday'):
    """CX3-02 EXCLUDE: __VIEW_EXCEPT[A|B]__ -> PARTITION BY the view's dimensions minus the excluded columns (a
    date dimension is dropped when its column is excluded). Empty -> '' so the window spans the whole view."""
    def norm(x):
        return str(x or '').replace('"', '').strip().upper()

    def fill(m):
        drop = {norm(c) for c in m.group(1).split('|') if c.strip()}
        keep = [d for d in dims if norm(engine.rdim(d['caption'])) not in drop and norm(d['caption']) not in drop]
        exprs = [_date_sql(engine.rdim(d['caption']), d.get('date_part', 'none'), week_start, d.get('fiscal', 1)) for d in keep]
        return ('PARTITION BY ' + ', '.join(exprs)) if exprs else ''
    return _VIEW_EXCEPT.sub(fill, expr)


def sheet_where(sheet, engine, where_parts=None):
    """The sheet's WHERE exactly as the engine's renderers build it (dashboard parts + sheet filters)."""
    T = engine.tbl(sheet)
    # The adapter-specific filter path (scatter/dual/crosstab projections) accepts categorical filters only;
    # a plan always uses the engine's general sheet-filter path (ranges, relative dates, IN / NOT IN, top-N).
    ws = {k: v for k, v in (sheet.get('worksheet_semantics') or {}).items() if k != 'projection'}
    sheet = dict(sheet, worksheet_semantics=ws)
    applicable = engine._parts_for_sheet(where_parts, sheet) if isinstance(where_parts, list) else where_parts
    where = engine._where_for(T, applicable) if isinstance(applicable, list) else (applicable or '')
    governed = ({p['col'] for p in applicable if isinstance(p, dict)} if isinstance(applicable, list) else set())
    dparts = applicable if isinstance(applicable, list) else None
    where = engine._apply_sheet_filters(sheet, where, T, governed=governed, dash_parts=dparts)
    # AC02: an active dashboard action narrows its target sheets exactly as in the renderer (engine._render_sheet_impl)
    from dashboard_actions import predicates as action_predicates
    action_parts = action_predicates(sheet, engine)
    if action_parts:
        where += (' AND ' if where.strip() else 'WHERE ') + ' AND '.join(action_parts)
    return where


def lod_where(sheet, engine, where_parts=None):
    """C5: the WHERE a FIXED LOD is computed under -- the sheet's context filters only (context filters, data source
    filters, the dashboard value of a context column), never its dimension filters, dashboard controls on other
    fields or filter actions: Tableau computes FIXED before those."""
    applicable = engine._parts_for_sheet(where_parts, sheet) if isinstance(where_parts, list) else where_parts
    governed = ({p['col'] for p in applicable if isinstance(p, dict)} if isinstance(applicable, list) else set())
    dparts = applicable if isinstance(applicable, list) else None
    conds = engine.context_conditions(sheet, governed, dparts)
    return ('WHERE ' + ' AND '.join(conds)) if conds else ''


def _split_top(text):
    """Top-level comma-separated items of a select list (parentheses and quotes respected)."""
    out, depth, cur, quote = [], 0, [], None
    for ch in text:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
        elif ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        elif ch == ',' and depth == 0:
            out.append(''.join(cur))
            cur = []
            continue
        cur.append(ch)
    if cur:
        out.append(''.join(cur))
    return out


def _mask_subqueries(text, engine):
    """Every '(SELECT ...)' span replaced by a token (they have their own FROM; their windows are theirs)."""
    out, spans, i = [], [], 0
    while True:
        m = re.search(r'\(\s*SELECT\b', text[i:], re.I)
        if not m:
            out.append(text[i:])
            return ''.join(out), spans
        start = i + m.start()
        end = engine._match_paren_engine(text, start)
        if end < 0:
            out.append(text[i:])
            return ''.join(out), spans
        span = text[start:end]
        if span not in spans:                 # the same subquery is one token (texts that repeat it stay equal)
            spans.append(span)
        out.append(text[i:start] + f'\u2400SQ{spans.index(span)}\u2400')
        i = end


def _unmask_subqueries(text, spans):
    for k, span in enumerate(spans):
        text = text.replace(f'\u2400SQ{k}\u2400', span)
    return text


_VIEW_AGGS = {'sum': 'SUM', 'avg': 'AVG', 'min': 'MIN', 'max': 'MAX'}
_LOD_MARK = re.compile(r'__(INCLUDE|EXCLUDE)__\(')


def _marker(text):
    """(kind, inner aggregate SQL, [key SQL], start, end) of the first INCLUDE / EXCLUDE marker, else None."""
    m = _LOD_MARK.search(text)
    if not m:
        return None
    end = _paren_end(text, m.end() - 1)
    args = _split_top(text[m.end():end - 1])
    if len(args) != 2 or not args[1].strip().startswith('__KEYS__('):
        raise PlanError('INCLUDE / EXCLUDE: unexpected translation')
    keys = args[1].strip()[len('__KEYS__('):-1]
    return m.group(1), args[0].strip(), [k.strip() for k in _split_top(keys)], m.start(), end


def _paren_end(text, i):
    depth, quote = 0, None
    for j in range(i, len(text)):
        ch = text[j]
        if quote:
            quote = None if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                return j + 1
    raise PlanError('INCLUDE / EXCLUDE: unbalanced expression')


def lod_view_sql(expr, m, dims, engine, week_start='sunday', rows=None):
    """C11 / C14: INCLUDE / EXCLUDE LODs at the sheet's own grain, computed after the dimension filters (Tableau's
    order), exact or refused. Each marker sits under an aggregation: the calc's own (AVG(__INCLUDE__(..))) or, for a
    measure that is just the LOD, the view's (m['agg']).
      Decomposable shapes stay in the grouped query: INCLUDE SUM/SUM, MIN/MIN, MAX/MAX, SUM/COUNT = the inner
      aggregate; AVG/SUM, AVG/COUNT = inner / the number of include groups with a value; EXCLUDE SUM / MIN / MAX /
      COUNT = a window over the marks.
      Every other shape uses the plan's row layer (rows: a list this appends (window, alias) to; computed over the
      filtered rows): INCLUDE k -> the inner aggregate per (marks + k) on every row, read once per group
      (ROW_NUMBER = 1) under the outer aggregation; EXCLUDE k -> the inner aggregate per (marks - k) on every row,
      one value per mark."""
    body = expr.strip()
    while body.startswith('(') and _paren_end(body, 0) == len(body):
        body = body[1:-1].strip()
    mk = _marker(body)
    if mk and mk[3] == 0 and mk[4] == len(body):                 # just the LOD: the view's aggregation applies
        view = _VIEW_AGGS.get((m.get('agg') or '').lower())
        if view is None:
            if mk[0] == 'EXCLUDE':
                view = 'MIN'                                     # one value per mark: any aggregation is it
            else:
                raise PlanError(f"'{m.get('caption')}': INCLUDE needs the view's aggregation (SUM / AVG / MIN / MAX)")
        body = f'{view}({body})'
    marks = [_date_sql(engine.rdim(d['caption']), d.get('date_part', 'none'), week_start, d.get('fiscal', 1)) for d in dims]
    rx = re.compile(r'\b(SUM|AVG|MIN|MAX|COUNT|MEDIAN)\s*\(\s*__(INCLUDE|EXCLUDE)__\(', re.I)
    while True:
        w = rx.search(body)
        if not w:
            break
        op = body.index('(', w.start())
        end = _paren_end(body, op)
        arg = body[op + 1:end - 1].strip()
        mk = _marker(arg)
        if not mk or mk[3] != 0 or mk[4] != len(arg):
            raise PlanError(f"'{m.get('caption')}': an INCLUDE / EXCLUDE LOD inside a larger expression under one "
                            'aggregation is not supported')
        body = body[:w.start()] + _lod_term(w.group(1).upper(), mk, dims, marks, engine, week_start, rows,
                                            m.get('caption')) + body[end:]
    if _LOD_MARK.search(body):
        raise PlanError(f"'{m.get('caption')}': an INCLUDE / EXCLUDE LOD outside an aggregation is not supported")
    return body


def _lod_term(view, mk, dims, marks, engine, week_start, rows, caption):
    kind, inner, keys, _s, _e = mk
    single = re.fullmatch(r'(SUM|COUNT|MIN|MAX)\s*\((.*)\)', inner, re.S | re.I)
    single = single if single and _paren_end(inner, inner.index('(')) == len(inner) else None
    agg = single.group(1).upper() if single else None
    if kind == 'EXCLUDE' and agg:
        outer = {'SUM': 'SUM', 'MIN': 'MIN', 'MAX': 'MAX', 'COUNT': 'SUM'}[agg]
        drop = '|'.join(k.replace(']', '') for k in keys)
        return expand_view_except(f'{outer}({inner}) OVER (__VIEW_EXCEPT[{drop}]__)', dims, engine, week_start)
    if kind == 'INCLUDE' and agg:
        arg = single.group(2)
        if (view, agg) in (('SUM', 'SUM'), ('MIN', 'MIN'), ('MAX', 'MAX'), ('SUM', 'COUNT')):
            return inner
        if (view, agg) in (('AVG', 'SUM'), ('AVG', 'COUNT')):
            groups = f"HASH({', '.join(keys)})"
            if agg == 'SUM':                   # a group whose values are all NULL has no SUM: AVG skips it
                groups = f'CASE WHEN ({arg}) IS NOT NULL THEN {groups} END'
            return f'({inner}) / NULLIF(COUNT(DISTINCT {groups}), 0)'
    if rows is None:
        raise PlanError(f"'{caption}': this INCLUDE / EXCLUDE shape needs the plan's row layer")
    import calc_translator as CT
    if kind == 'INCLUDE':
        part = marks + [k for k in keys if k not in marks]
    else:
        def norm(x):
            return str(x or '').replace('"', '').strip().upper()
        drop = {norm(k) for k in keys}
        part = [mk_ for d, mk_ in zip(dims, marks) if norm(engine.rdim(d['caption'])) not in drop
                and norm(d['caption']) not in drop and norm(mk_) not in drop]
    over = ', '.join(part)
    value = CT._window_each_agg(inner, over) if over else None
    if over and value is None:
        value = f'{inner} OVER (PARTITION BY {over})'
    if not over:                              # every view dimension excluded: the whole filtered table
        value = CT._window_each_agg(inner, '__ALL__')
        value = value.replace(' OVER (PARTITION BY __ALL__)', ' OVER ()') if value else f'{inner} OVER ()'
    n = len(rows)
    rows.append((value, f'__IG{n}__'))
    if kind == 'EXCLUDE':
        return f'MIN(__IG{n}__)'
    rows.append((f"ROW_NUMBER() OVER (PARTITION BY {', '.join(part)} ORDER BY 1)", f'__IR{n}__'))
    return f'{view}(CASE WHEN __IR{n}__ = 1 THEN __IG{n}__ END)'


def _split_and(text):
    """Top-level AND-separated conditions (parentheses and quotes respected)."""
    out, depth, quote, start, i = [], 0, None, 0, 0
    while i < len(text):
        ch = text[i]
        if quote:
            quote = None if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        elif depth == 0 and re.match(r'\sAND\s', text[i:i + 5], re.I):
            out.append(text[start:i])
            start = i + 5
            i += 4
        i += 1
    out.append(text[start:])
    return out


_AGG_CALL = re.compile(r'\b(SUM|AVG|MIN|MAX|COUNT|MEDIAN|STDDEV_SAMP|STDDEV_POP|VAR_SAMP|VAR_POP|COVAR_SAMP|'
                       r'COVAR_POP|CORR|ANY_VALUE)\s*\(', re.I)
_WIN_HEAD = re.compile(r'\b([A-Z_][A-Z0-9_]*)\s*\(', re.I)


def _is_window_call(text, start):
    """(end, over_end) when the call at text[start] is followed by OVER (..), else None."""
    op = text.index('(', start)
    end = _paren_end(text, op)
    om = re.match(r'\s*OVER\s*\(', text[end:], re.I)
    if not om:
        return None
    return end, _paren_end(text, end + om.end() - 1)


def _hoist_aggregates(text, hoisted):
    """Every plain aggregate call (one that is not a window function's head) -> a column __Ak of the marks layer."""
    out, i = [], 0
    while True:
        m = _AGG_CALL.search(text, i)
        if not m:
            out.append(text[i:])
            return ''.join(out)
        win = _is_window_call(text, m.start())
        if win:                               # a window: keep its head, look inside its arguments and OVER
            out.append(text[i:m.end()])
            i = m.end()
            continue
        end = _paren_end(text, text.index('(', m.start()))
        call = text[m.start():end]
        name = next((a for e, a in hoisted if e == call), None)
        if name is None:
            name = f'__A{len(hoisted)}'
            hoisted.append((call, name))
        out.append(text[i:m.start()] + name)
        i = end


def _innermost_windows(text):
    """[(start, end)] of window calls holding no other window (arguments or OVER clause)."""
    found = []
    for m in _WIN_HEAD.finditer(text):
        try:
            win = _is_window_call(text, m.start())
        except PlanError:
            continue
        if win and not re.search(r'\bOVER\s*\(', text[m.end():win[1] - 1][:win[0] - m.end() - 1] +
                                 ' ' + text[win[0]:win[1] - 1].split('(', 1)[-1], re.I):
            found.append((m.start(), win[1]))
    return found


def _padding(dims, select, engine):
    """DENS: [(D alias, next-member SQL over v)] for the dimensions with Show Missing Values, or None. Bins step by
    their size, dates by their unit, whole-number date parts by 1; any other field is not padded."""
    out = []
    for i, d in enumerate(dims):
        if not d.get('full_range'):
            continue
        part = d.get('date_part') or 'none'
        expr = (engine.rdim(d['caption']) or '').strip()
        m = re.search(r'\)\s*\*\s*([\w.]+)\s*\+\s*[-\w.]+\s*$', expr)
        if part.endswith('_trunc'):
            out.append((f'D{i}', f"DATEADD('{part.split('_')[0]}', 1, v)"))
        elif part in ('year', 'quarter', 'month', 'day', 'week', 'weekday'):
            out.append((f'D{i}', 'v + 1'))
        elif m and re.match(r'FLOOR\s*\(', expr, re.I):
            out.append((f'D{i}', f'v + {m.group(1)}'))
    return out or None                        # a field that is not a bin or a date has nothing to pad between


def _layered_windows(items, T, where, group, pad=None):
    """C14: SQL for a select list whose windows nest. Layer 0 groups the marks (dimensions + every plain aggregate
    as __Ak); each next layer computes the innermost windows over the marks as __Wk; the last selects the result.
    Dimension expressions in the windows' OVER clauses read the marks' D columns."""
    dims, rest = [], []
    for it in items:
        m = re.fullmatch(r'(.*)\s+AS\s+(D\d+)\s*', it.strip(), re.S | re.I)
        (dims if m else rest).append((m.group(1).strip(), m.group(2)) if m else it)
    hoisted = []
    measures = []
    for it in rest:
        m = re.fullmatch(r'(.*)\s+AS\s+(\w+)\s*', it.strip(), re.S | re.I)
        if not m:
            raise PlanError('A table calculation over another one could not be laid out')
        measures.append((_hoist_aggregates(m.group(1), hoisted), m.group(2)))
    for expr, alias in sorted(dims, key=lambda d: -len(d[0])):        # dimensions in OVER clauses -> D columns
        rx = (r'(?<![\w.])' + re.escape(expr) + r'(?![\w(])') if re.fullmatch(r'[\w.]+', expr) else re.escape(expr)
        measures = [(re.sub(rx, alias, e), a) for e, a in measures]
    marks = (f"SELECT {', '.join([f'{e} AS {a}' for e, a in dims] + [f'{e} AS {a}' for e, a in hoisted])} "
             f"FROM {T} {where or ''}" + (' GROUP BY ' + ', '.join(group) if group else ''))
    prefix = ''
    if pad:
        padded = [a for a, _n in pad]
        others = [a for _e, a in dims if a not in padded]
        ctes = [f'__l0 AS ({marks})']
        for k, (alias, nxt) in enumerate(pad):
            ctes.append(f"__rng{k} (v, n) AS (SELECT MIN({alias}), 0 FROM __l0 UNION ALL SELECT {nxt}, n + 1 "
                        f"FROM __rng{k} WHERE {nxt} <= (SELECT MAX({alias}) FROM __l0) AND n < 10000)")
        prefix = 'WITH RECURSIVE ' + ', '.join(ctes) + ' '
        cols = ([f'__o.{a}' for a in others] + [f'__r{k}.v AS {a}' for k, (a, _n) in enumerate(pad)]
                + [f'__m.{h}' for _e, h in hoisted])
        on = ' AND '.join([f'__m.{a} IS NOT DISTINCT FROM __o.{a}' for a in others]
                          + [f'__m.{a} = __r{k}.v' for k, (a, _n) in enumerate(pad)])
        base = ' CROSS JOIN '.join(([f"(SELECT DISTINCT {', '.join(others)} FROM __l0) __o"] if others else [])
                                   + [f'__rng{k} __r{k}' for k in range(len(pad))])
        nulls = ' OR '.join(f'{a} IS NULL' for a in padded)    # a NULL member is a mark of its own, kept as is
        keep = ', '.join([a for a in others] + padded + [h for _e, h in hoisted])
        src = (f"(SELECT {', '.join(cols)} FROM {base} LEFT JOIN __l0 __m ON {on} "
               f"UNION ALL SELECT {keep} FROM __l0 WHERE {nulls}) __l0p")
    else:
        src = f'({marks}) __l0'
    level, n = 0, 0
    while any(_nested_window(e) for e, _a in measures):
        new = []
        for e, a in measures:
            spans = _innermost_windows(e) if _nested_window(e) else []
            parts, last = [], 0
            for st, en in spans:
                new.append((e[st:en], f'__W{n}'))
                parts.append(e[last:st] + f'__W{n}')
                last, n = en, n + 1
            parts.append(e[last:])
            e = ''.join(parts)
            measures[measures.index(next(x for x in measures if x[1] == a))] = (e, a)
        if not new:
            raise PlanError('A table calculation over another one could not be laid out')
        level += 1
        src = f"(SELECT *, {', '.join(f'{w} AS {a}' for w, a in new)} FROM {src}) __l{level}"
    return prefix + f"SELECT {', '.join([a for _e, a in dims] + [f'{e} AS {a}' for e, a in measures])} FROM {src}"


def _nested_window(text):
    """True when a window call's own argument holds another window (a table calculation over a table calculation;
    SQL refuses it). Row-level FIXED windows were already layered out by _fixed_before_filters."""
    for m in re.finditer(r'\)\s*OVER\s*\(', text, re.I):
        close, depth = m.start(), 0
        for k in range(close, -1, -1):                 # the call's opening parenthesis
            depth += {')': 1, '(': -1}.get(text[k], 0)
            if depth == 0:
                if re.search(r'\bOVER\s*\(', text[k:close], re.I):
                    return True
                break
    return False


def _window_in_over(text, engine):
    """True when a window's OVER (...) clause itself holds a window (SQL refuses that; the inner one is layered)."""
    for m in re.finditer(r'\bOVER\s*\(', text, re.I):
        end = engine._match_paren_engine(text, m.end() - 1)
        if end > 0 and re.search(r'\bOVER\s*\(', text[m.end():end - 1], re.I):
            return True
    return False


_SQL_WORDS = {'CASE', 'WHEN', 'THEN', 'ELSE', 'END', 'AND', 'OR', 'NOT', 'NULL', 'IS', 'IN', 'TRUE', 'FALSE', 'AS',
              'INTERVAL', 'DISTINCT', 'BETWEEN', 'LIKE', 'DAY', 'MONTH', 'YEAR', 'WEEK', 'QUARTER', 'HOUR', 'MINUTE',
              'SECOND', 'DATE', 'TIMESTAMP', 'INTEGER', 'VARCHAR', 'DOUBLE', 'FROM'}


def _key_grain(expr, engine):
    """X12: (AGG, inner, key) when a measure is AGG(inner) with AGG in AVG / SUM / COUNT / MEDIAN and inner reads only
    FIXED LOD values that share ONE partition key (no row-level field, no parameter-free column); else None."""
    m = re.fullmatch(r'\s*(AVG|SUM|COUNT|MEDIAN)\s*\((.*)\)\s*', expr or '', re.S | re.I)
    if not m:
        return None
    try:
        if _paren_end(expr.strip(), expr.strip().index('(')) != len(expr.strip()):
            return None
    except PlanError:
        return None
    inner = m.group(2)
    text, found, n = inner, [], 0
    for _ in range(4):                        # a FIXED inside a FIXED (second purchase after the first): all levels
        text, level = engine.scan_row_windows(text, n)
        if not level:
            break
        found += level
        n += len(level)
    if not found or re.search(r'\bOVER\s*\(', text, re.I) or '(SELECT' in text.upper():
        return None
    keys = set()
    for w, _alias in found:                   # (an outer level reads the inner one by its alias)
        over = re.search(r'\bOVER\s*\(\s*PARTITION\s+BY\s+(.*)\)\s*$', w, re.S | re.I)
        if not over:
            return None
        keys.add(' '.join(over.group(1).split()))
    if len(keys) != 1:
        return None
    rest = re.sub(r"'[^']*'", "''", text)
    for t in re.finditer(r'\b([A-Za-z_][A-Za-z0-9_]*)\b(\s*\()?', rest):
        word = t.group(1)
        if t.group(2) or word.upper() in _SQL_WORDS or re.fullmatch(r'__W\d+__', word) or word.startswith('__PARAM'):
            continue
        return None                           # a row-level field: the aggregate stays at row level
    return m.group(1).upper(), inner, next(iter(keys))


def _apply_key_grain(expr, engine, key_grain):
    """X12: every AVG / SUM / COUNT / MEDIAN call in a measure that reads only FIXED values on one key becomes one
    value per (mark, key) -- the first row of each key in the mark (__KGi__ = 1, numbered over the filtered rows).
    key_grain collects the keys (index i). A window over such an aggregate (WINDOW_MAX(AVG(..))) sees the same values."""
    out, i = [], 0
    rx = re.compile(r'\b(AVG|SUM|COUNT|MEDIAN)\s*\(', re.I)
    while True:
        m = rx.search(expr, i)
        if not m:
            out.append(expr[i:])
            return ''.join(out)
        try:
            end = _paren_end(expr, m.end() - 1)
        except PlanError:
            out.append(expr[i:])
            return ''.join(out)
        kg = None if re.match(r'\s*OVER\s*\(', expr[end:], re.I) else _key_grain(expr[m.start():end], engine)
        if kg:
            out.append(expr[i:m.start()] + f'{kg[0]}(CASE WHEN __KG{len(key_grain)}__ = 1 THEN {kg[1]} END)')
            key_grain.append(kg[2])
            i = end
        else:
            out.append(expr[i:m.end()])
            i = m.end()


def _fixed_before_filters(select, T, where, lod, engine, force=False):
    """C5: (select list, FROM clause, WHERE) with every row-level FIXED window computed in base queries under the
    context filters only (lod) and the sheet's other filters applied outside them; a no-key FIXED ((SELECT .. FROM
    __TBL__)) reads the context rows. A FIXED nested in another is computed layer by layer (inner first), every
    layer over the same context rows. Windows in the WHERE are left as built (they belong to the engine's own filter
    subqueries); its no-key FIXED read the context rows too. Unchanged when the sheet has no filter beyond its
    context."""
    first, found = engine.scan_row_windows(', '.join(select), 0)
    nested = bool(found) and (bool(engine.scan_row_windows(first, 0)[1])    # FIXED over / keyed on a FIXED, or
                              or _window_in_over(', '.join(select), engine))  # a table calc ordered by a FIXED:
    if lod is None:                                                          # SQL cannot nest windows -> layers
        lod = '' if nested or any('__HAVING' in x and ' OVER (' in x for x in select) else None
    if lod is None:
        return select, T, where
    # C7: FIXED windows in the WHERE's OWN predicates (a filter on a row-level calculation with a FIXED, Clara's
    # 'Date range filter' / Hospitality's SuccessFlag) -- never inside the engine's own filter subqueries
    masked, subqueries = _mask_subqueries(where or '', engine)
    where_windows = bool(engine.scan_row_windows(masked, 0)[1]) or any(
        '__HAVING' in x and engine.scan_row_windows(x, 0)[1] for x in select)
    if not force and not where_windows and not nested and \
            ' '.join((where or '').split()) == ' '.join((lod or '').split()):
        return select, T, where
    # a no-key FIXED inside a context filter itself reads the whole table (context filters run before FIXED)
    ctx_conds = [c.strip() for c in _split_and(re.sub(r'^\s*WHERE\s+', '', lod or '', flags=re.I)) if c.strip()]
    lod = (lod or '').replace('__TBL__', T)
    ctx_table = f'(SELECT * FROM {T} {lod}) __ctx' if lod.strip() else None

    def scoped(text):                         # a no-key FIXED over the context rows -- except inside a context
        if not ctx_table:                     # condition itself, which reads the whole table
            return text
        keep = {}
        for k, c in enumerate(ctx_conds):
            if '__TBL__' in c and c in text:
                keep[f'\u2400CTX{k}\u2400'] = c.replace('__TBL__', T)
                text = text.replace(c, f'\u2400CTX{k}\u2400')
        text = text.replace('__TBL__', ctx_table)
        for tok, c in keep.items():
            text = text.replace(tok, c)
        return text
    sel, layers, n = ', '.join(select), [], 0
    sep = ' \u241e '                          # select list and WHERE predicates scanned together, split after
    text = sel + sep + masked
    for _ in range(4):                        # inner FIXED first; each pass takes one nesting level
        text, found = engine.scan_row_windows(text, n, alias='__F{}__')
        if not found:
            break
        layers.append(found)
        n += len(found)
    else:
        raise PlanError('FIXED LODs nested more than four levels are not supported')
    sel, masked = text.split(sep, 1)
    where = _unmask_subqueries(masked, subqueries)
    sel, where = scoped(sel), scoped(where or '')
    if not layers:
        return [sel], T, where
    src = f'(SELECT *, {", ".join(f"{scoped(e)} AS {a}" for e, a in layers[0])} FROM {T} {lod or ""}) __lod0'
    for k, layer in enumerate(layers[1:], 1):
        src = f'(SELECT *, {", ".join(f"{scoped(e)} AS {a}" for e, a in layer)} FROM {src}) __lod{k}'
    return [sel], src, where


def _having_predicate(f, sheet, engine):
    """C10: a measure filter as a HAVING predicate on the plan's grouped query (the engine's own tolerance: Tableau
    saves the data's min/max as slider bounds, a sum in another order differs in the last digits). None when the
    measure does not translate (the engine's subquery form then applies, or reports it)."""
    wide = dict(f)
    for key, sign in (('min', -1), ('max', 1)):
        v = f.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            wide[key] = v + sign * 1e-12 * max(1.0, abs(v))
    ref = {'caption': f['caption'], 'agg': None if f.get('agg') in (None, 'usr', 'none', 'attr') else f['agg']}
    if f.get('field_id'):
        ref['field_id'] = f['field_id']
    r = engine.resolve_measure(engine.tbl(sheet), sheet.get('name'), ref)
    if not r or r.get('window'):
        return None
    return engine._value_predicate(wide, expr=f"({r['sql']})")


def _table_calc_filters(sheet, engine):
    """C6: the sheet's filters on a TABLE CALCULATION (LOOKUP / RUNNING_* / FIRST / LAST / TOTAL / WINDOW_*) -- Tableau
    applies them last, after aggregation and every table calculation, so they only hide marks. INDEX()/RANK() <= N
    gates keep the engine's own handling. Returns ([(filter, calc sql)], sheet without them)."""
    import calc_translator
    found, rest = [], []
    for f in sheet.get('applied_filters') or []:
        c = engine.CALCS.get(f.get('caption')) or {}
        sql = c.get('sql') or ''
        if calc_translator.is_window_sql(sql) and engine._rank_gate_n(sql) is None:
            found.append((f, sql))
        else:
            rest.append(f)
    return found, (dict(sheet, applied_filters=rest) if found else sheet)


def _table_calc_flag(f, sql, plan, engine, dims):
    """A table-calculation filter as a 1/0 column over the aggregated view (its window filled from the sheet's
    default addressing). None when the filter cannot be expressed."""
    filled = window_sql(sql, plan, {'caption': f.get('caption')}, engine, dims)
    vals = {str(v).lower() for v in f.get('values') or []}
    if f.get('kind') == 'in' and vals and vals <= {'true', 'false'}:
        if vals == {'true', 'false'}:
            return None
        pred = f'({filled})' if vals == {'true'} else f'NOT ({filled})'
    else:
        pred = engine._value_predicate(f, expr=f'({filled})')
    return f'CASE WHEN {pred} THEN 1 ELSE 0 END' if pred else None


def tables_used(sheet, ir):
    """C9: the tables a sheet reads -- 'parent' (the flattened datasource table) and/or the source of each related
    table left out of the flatten -- from every bound field, followed through calculations to their base fields."""
    fields = ir.get('fields') or {}
    did = sheet.get('datasource_id')
    out, seen = set(), set()

    def visit(fid, depth=0):
        if fid in seen or depth > 12:
            return
        seen.add(fid)
        e = fields.get(fid)
        if e is None:
            return
        if e.get('is_calculation'):
            for name in re.findall(r'\[([^\[\]]+)\]', e.get('formula') or ''):
                if name != 'Parameters':
                    visit(f'{did}::{name}', depth + 1)
                    visit(f"{did}::{name.replace(' ', '%20')}", depth + 1)
            return
        out.add((e.get('related') or {}).get('source') or 'parent')
    for fid in (sheet.get('field_bindings') or {}).values():
        visit(fid)
    for f in sheet.get('applied_filters') or []:
        if f.get('field_id'):
            visit(f['field_id'])
    return out


def _single_related(sheet, engine):
    """C9: the one related (left-out) table a sheet reads, when it reads nothing else and that table has data."""
    ir = getattr(engine, 'IR', {}) or {}
    used = tables_used(sheet, ir)
    if len(used) == 1 and 'parent' not in used:
        source = next(iter(used))
        import config
        if source in getattr(config, 'DATASOURCES', {}):
            return source
    return None


def execute(plan, sheet, engine, where_parts=None):
    """Run the plan: the aggregate query, then the table calculations. Returns a DataFrame whose columns
    are col_name(field) for every plan field that carries a value (dims, measures, table calcs)."""
    import pandas as pd
    plan = select_measures(plan, engine)
    if plan.get('unlinked'):
        raise PlanError('Fields come from ' + ', '.join(plan['unlinked']) + ', which has no blend link to this '
                        "sheet's datasource")
    fields_ir = (getattr(engine, 'IR', {}) or {}).get('fields', {})
    for f in plan.get('fields') or []:
        e = fields_ir.get(f.get('field_id') or '') or {}
        # a blended sheet evaluates calculations that span both datasources itself (compile_blend)
        if f.get('kind') == 'dim' and e.get('is_calculation') and not e.get('calculation') and not plan.get('blend'):
            raise PlanError(f"'{f['caption']}' is a calculation that was not translated")
    import v3_relationship_query as RQ
    only = _single_related(sheet, engine)
    if only:
        # C9: every field of the sheet comes from one related table -> answered from that table alone (Tableau
        # queries only the tables a viz uses: no join, nothing multiplied)
        sheet = dict(sheet, datasource=only, _related_only=only)
    related = {} if only else RQ.related_fields(plan, engine)
    uses = {(fields_ir.get(f.get('field_id') or '') or {}).get('uses_related')
            for f in list(plan.get('fields') or []) + list(sheet.get('applied_filters') or [])} - {None}
    if related or (uses and not only):
        # DM4 / C12: a related table's field, or a calculation over it (alone or mixed with the parent's fields)
        return _execute_related(plan, sheet, engine, where_parts, related, fields_ir)
    sheet = bind_fields(plan, sheet, engine)
    with engine.field_scope(sheet):
        tc_filters, filtered = _table_calc_filters(sheet, engine)
        flags = []
        having = measure_having(sheet, engine, where_parts)
        having_sql = []
        if having and plan.get('aggregate', True) and not plan.get('blend'):
            # C10: the plan's query is grouped at the marks, so a measure filter is that query's own HAVING -- exact,
            # before table calculations, and no re-derived mark key (a FIXED dimension broke the IN-subquery form)
            done = []
            for f in having:
                pred = _having_predicate(f, sheet, engine)
                if pred:
                    having_sql.append(pred)
                    done.append(id(f))
            if done:
                filtered = dict(filtered, applied_filters=[f for f in filtered.get('applied_filters') or []
                                                           if id(f) not in done])
        where = sheet_where(filtered, engine, where_parts)
        if plan.get('blend'):
            if having:
                raise PlanError('A measure filter on a blended sheet is not supported')
            if tc_filters:
                raise PlanError('A table-calculation filter on a blended sheet is not supported yet')
            sql, cols = compile_blend(plan, sheet, engine, where, lod_where=lod_where(filtered, engine, where_parts))
        else:
            dims = query_fields(plan)[0]
            for i, (f, csql) in enumerate(tc_filters):
                flag = _table_calc_flag(f, csql, plan, engine, dims)
                if flag:
                    flags.append((flag, f'__TCF{i}'))
            sql, cols = compile_query(plan, sheet, engine, where, lod_where=lod_where(filtered, engine, where_parts),
                                      extra_select=flags, having=having_sql)
        df = engine.q(sql)
    keep = None
    if not plan.get('blend') and flags:
        keep = df[[a for _e, a in flags]].fillna(0).astype(int).min(axis=1).astype(bool).values
    return _finish(plan, cols, df, engine, keep=keep)


def _execute_related(plan, sheet, engine, where_parts, related, fields_ir):
    """DM4: the sheet uses a related table the flatten left out -> per-table relationship query."""
    import sys
    import v3_relationship_query as RQ
    sources = {r['source'] for r in related.values()} | {
        (fields_ir.get(f.get('field_id') or '') or {}).get('uses_related')
        for f in list(plan.get('fields') or []) + list(sheet.get('applied_filters') or [])} - {None}
    if len(sources) > 1:
        raise PlanError('More than one related table on one sheet is not supported yet')
    source = next(iter(sources))
    related, joined = dict(related), {}
    # C12: every field resolves in a scope that reads the related table too; the plan then decides, from the
    # columns each expression uses, which table answers it: the parent, the related table alone, or their join
    sheet = dict(bind_fields(plan, sheet, engine), _related_only=source)
    T = engine.tbl(sheet)
    R = engine.tbl({'datasource': source})
    pcols = {str(c).upper() for c in engine.q(f'SELECT * FROM {T} LIMIT 0').columns}
    rcols = {str(c).upper() for c in engine.q(f'SELECT * FROM {R} LIMIT 0').columns}

    def owner(sql):
        names = set(re.findall(r'\b[A-Za-z_][A-Za-z0-9_]*\b', re.sub(r"'[^']*'", "''", sql or '')))
        names = {n.upper() for n in names}
        p, r = bool(names & (pcols - rcols)), bool(names & (rcols - pcols))
        if p and r and pcols & rcols - {'__K'}:
            raise PlanError('The two related tables share column names; a calculation across them is not '
                            'supported yet')
        return 'J' if p and r else 'R' if r else 'P'
    with engine.field_scope(sheet):
        for f in plan.get('fields') or []:
            e = fields_ir.get(f.get('field_id') or '') or {}
            if id(f) in related or not e.get('uses_related'):
                continue
            dims = f['kind'] == 'dim' and not f.get('aggregate_dimension') and not f.get('table_calc')
            if dims:
                sql = engine.rdim(f['caption'])
            else:
                ref = {'caption': f['caption'], 'agg': None if f['agg'] in ('usr', 'none', 'attr') else f['agg']}
                if f.get('field_id'):
                    ref['field_id'] = f['field_id']
                r = engine.resolve_measure(T, sheet.get('name'), ref)
                if not r or r.get('window'):
                    raise PlanError(f"Measure '{f['caption']}' could not be translated")
                sql = r['sql']
            side = owner(sql)
            if side == 'R':
                related[id(f)] = {'source': source, ('expr' if dims else 'sql'): sql}
            elif side == 'J':
                joined[id(f)] = sql
        if measure_having(sheet, engine, where_parts):
            raise PlanError('A measure filter on a sheet that uses a related table is not supported yet')
        # the sheet's filters by the table their field belongs to: a related field's filter is that table's; a
        # calculation's by the columns it uses (one that mixes both tables refuses)
        rel_filters, mixed_keys, rel_ids_done = [], [], set()
        for flt in sheet.get('applied_filters') or []:
            e = fields_ir.get(flt.get('field_id') or '') or {}
            if e.get('related'):
                rel_filters.append(flt)
            elif e.get('uses_related'):
                with engine.field_scope(sheet):
                    probe = sheet_where(dict(sheet, applied_filters=[flt]), engine, None)
                    base = sheet_where(dict(sheet, applied_filters=[]), engine, None)
                extra = [c for c in _split_and(re.sub(r'^\s*WHERE\s+', '', probe or '', flags=re.I))
                         if c.strip() and c not in _split_and(re.sub(r'^\s*WHERE\s+', '', base or '', flags=re.I))]
                if any(owner(c) == 'J' for c in extra):
                    # a condition over both tables (a FIXED across them included) holds per joined row: it keeps
                    # the relationship keys of the joined rows that pass, and so the rows of each table they relate
                    if lod_where(sheet, engine, where_parts).strip():
                        raise PlanError(f"A filter that mixes two related tables ('{flt.get('caption')}') on a "
                                        'sheet with context filters is not supported yet')
                    mixed_keys.append(' AND '.join(f'({c})' for c in extra))
                    rel_ids_done.add(id(flt))
                elif any(owner(c) == 'R' for c in extra):
                    rel_filters.append(flt)
        rel = {id(f) for f in rel_filters} | rel_ids_done
        where = sheet_where(dict(sheet, applied_filters=[f for f in sheet.get('applied_filters') or []
                                                         if id(f) not in rel]), engine, where_parts)
        rconds = []
        if rel_filters:
            base = set(_split_and(re.sub(r'^\s*WHERE\s+', '', sheet_where(dict(sheet, applied_filters=[]), engine,
                                                                            None) or '', flags=re.I)))
            rw = sheet_where(dict(sheet, applied_filters=rel_filters), engine, None)
            rconds = [c.strip() for c in _split_and(re.sub(r'^\s*WHERE\s+', '', rw or '', flags=re.I))
                      if c.strip() and c not in base]
        if mixed_keys:
            link = (((getattr(engine, 'IR', {}) or {}).get('field_scopes') or {}).get(sheet.get('datasource_id'))
                    or {}).get('related_tables', {}).get(source) or {}
            if not link or link.get('role') == 'fact':
                raise PlanError('A filter that mixes two fact tables is not supported yet')
            pkey, rkey = link['parent_key'], link['key']
            J = f'(SELECT * FROM {T} __p INNER JOIN {R} __r ON __p.{pkey} = __r.{rkey})'
            for k, cond in enumerate(mixed_keys):
                keys = (f'SELECT __PK FROM (SELECT {pkey} AS __PK, CASE WHEN {cond.replace("__TBL__", J)} THEN 1 END '
                        f'AS __C FROM {J} __j) __m{k} WHERE __C = 1')
                rconds.append(f'{rkey} IN ({keys})')
        try:
            sql, cols = RQ.compile_relationship(plan, sheet, engine, where, related, sys.modules[__name__],
                                                rwhere=' AND '.join(rconds), joined=joined)
        except RQ.RelationshipQueryError as exc:
            raise PlanError(str(exc))
        df = engine.q(sql)
    return _finish(plan, cols, df, engine)


def bind_fields(plan, sheet, engine):
    """The sheet with every plan field bound by its occurrence identity, not by caption: the renderer's caption
    map covers only the fields it consumes, and a caption can equal another column's physical name (6 Nov:
    cluster group on Color). A calculation the engine could not translate is left unbound (the plan evaluates it
    itself across a blend, or reports it as not translated). Used by execute and by the engine's measure-filter
    predicate (E06), so both resolve the view's dimensions the same way."""
    fields_ir = (getattr(engine, 'IR', {}) or {}).get('fields', {})
    bound = dict(sheet.get('field_bindings') or {})
    for f in plan.get('fields') or []:
        fid = f.get('field_id')
        entry = fields_ir.get(fid) if fid else None
        if entry and entry.get('datasource_id') == sheet.get('datasource_id') and f.get('caption') not in bound:
            bound[f['caption']] = fid
    sheet = dict(sheet, field_bindings=bound)
    untranslated = {c for c, fid in (sheet.get('field_bindings') or {}).items()
                    if (fields_ir.get(fid) or {}).get('is_calculation') and not (fields_ir.get(fid) or {}).get('calculation')}
    if untranslated:
        sheet = dict(sheet, field_bindings={c: f for c, f in sheet['field_bindings'].items() if c not in untranslated})
    return sheet


def _finish(plan, cols, df, engine, keep=None):
    import pandas as pd
    if len(df) > MAX_ROWS:
        raise PlanError(f'More than {MAX_ROWS:,} rows at the view level of detail')
    out = pd.DataFrame()
    for col, f, is_m in cols:
        values = df[col] if col in df.columns else df[col.lower()]
        out[col_name(f, is_m)] = values.values
    if keep is not None:
        out['__keep'] = keep                  # C6: table-calculation filters hide marks LAST (below)
    if plan.get('geo_points') is not None:
        dims, _m = query_fields(plan)
        keys = out.apply(lambda r: '\u241f'.join(key_text(_label_value(r[col_name(d)], d, engine), d['date_part'])
                                                 for d in dims), axis=1) if len(out) else []
        located = [k in plan['geo_points'] for k in keys]
        out = out[located].reset_index(drop=True) if len(out) else out
        if len(out):
            out['__lat'] = [plan['geo_points'][k][0] for k, ok in zip(keys, located) if ok]
            out['__lon'] = [plan['geo_points'][k][1] for k, ok in zip(keys, located) if ok]
    if plan.get('geo_points') is None and len(out) and any(f.get('kind') == 'geo' for f in plan['fields']):
        import v3_geocode                     # V5: no Tableau coordinates -- countries / US states at their centroids
        out = v3_geocode.place(plan, out)
    for f in plan['fields']:
        if f.get('table_calc') and f['kind'] == 'measure':
            out[col_name(f, True) + '|tc'] = table_calc(out, plan, f)
    if '__keep' in out.columns:               # after every table calculation, as in Tableau
        out = out[out['__keep'].astype(bool)].drop(columns='__keep').reset_index(drop=True)
    if isinstance(plan.get('forecast'), dict):
        import v3_forecast                    # FC: approximated estimates after the view's own rows
        out = v3_forecast.add_estimates(out, plan)
    return out


def _forecast_spec(ws):
    """FC: the sheet's enabled forecast settings (an approximated forecast, v3_forecast), else False."""
    import v3_forecast
    return v3_forecast.spec_from_xml(ws.get('source_xml') or '') or False


def _label_value(v, f, engine):
    try:
        labels = engine.value_labels(f['caption']) or {}
    except Exception:
        labels = {}
    return labels.get(str(v).lower(), labels.get(str(v), v)) if labels else v


# --------------------------------------------------------------------------- table calculations (code)
def _addressing_dims(plan, f):
    """(addressed dims, partition dims) as result columns for a table calc on `f`."""
    along, part = _addressing(plan, f)
    return [col_name(d) for d in along], [col_name(d) for d in part]


def _addressing(plan, f):
    from urllib.parse import unquote       # field ids are url-encoded ('Path%20Frame%20%28bin%29')
    """(addressed dims, partition dims) as dimension fields."""
    dims, _m = query_fields(plan)
    tc = f['table_calc']
    rows = [d for d in dims if d['channel'] == 'rows']
    cols = [d for d in dims if d['channel'] == 'cols']
    marks = [d for d in dims if d['channel'] in MARK_CHANNELS]
    a = tc['addressing']
    if a == 'field' and (tc.get('ordering_fields') or tc.get('ordering_field')):
        along = []
        for token in tc.get('ordering_fields') or [tc['ordering_field']]:      # authored order (E07)
            name = _field_ordering(token)
            along += [d for d in dims if d not in along and
                      (unquote(d.get('field_id') or '').endswith('::' + (name or '\0')) or d['caption'] == name)]
    elif a == 'rows':
        along = rows + marks
    elif a == 'cols':
        along = cols + marks
    elif a == 'pane_rows':
        along = [d for d in rows if d['discrete'] is False] + marks
    elif a == 'pane_cols':
        along = [d for d in cols if d['discrete'] is False] + marks
    else:
        along = dims
    part = [d for d in dims if d not in along]
    return along, part


def table_calc(df, plan, f):
    import pandas as pd
    tc = f['table_calc']
    value = col_name(f, True)
    along, part = _addressing_dims(plan, f)
    frame = df.copy()
    frame['__v'] = pd.to_numeric(frame[value], errors='coerce')
    frame['__i'] = range(len(frame))
    order = [c for c in along if c in frame.columns]
    groups = [c for c in part if c in frame.columns]
    frame = frame.sort_values(groups + order, kind='mergesort') if (groups or order) else frame
    g = frame.groupby(groups, sort=False, dropna=False)['__v'] if groups else None
    kind = tc['type']
    if kind == 'PctTotal':
        total = g.transform('sum') if g is not None else frame['__v'].sum()
        res = frame['__v'] / total.where(total != 0) if g is not None else \
            (frame['__v'] / total if total != 0 else frame['__v'] * float('nan'))
    elif kind == 'CumTotal':
        how = tc.get('aggregation', 'sum')
        fn = {'sum': 'cumsum', 'min': 'cummin', 'max': 'cummax'}.get(how, 'cumsum')
        if how == 'avg':
            # Tableau window aggregates ignore Null: the denominator counts non-null values only (E07)
            present = frame['__v'].notna().astype(float)
            seen = present.groupby([frame[c] for c in groups], sort=False, dropna=False).cumsum() if groups \
                else present.cumsum()
            res = (g.cumsum() if g is not None else frame['__v'].cumsum()) / seen.where(seen > 0)
        else:
            res = getattr(g, fn)() if g is not None else getattr(frame['__v'], fn)()
    elif kind in ('Diff', 'PctDiff'):
        how, offset = tc.get('relative_to') or ('offset', -1)
        src = g if g is not None else frame['__v']
        if how == 'offset':                  # -1 previous, +1 next, any offset along the addressing
            ref = src.shift(-offset)
        elif how in ('first', 'last'):
            ref = (src.transform(how) if g is not None else
                   pd.Series(frame['__v'].iloc[0 if how == 'first' else -1], index=frame.index))
        else:
            raise PlanError(f'Table calculation {kind} relative to {how} is not supported')
        # Tableau: division by zero is Null, never +/-inf (E07)
        res = frame['__v'] - ref if kind == 'Diff' else (frame['__v'] - ref) / ref.abs().where(ref != 0)
    elif kind == 'Rank':
        method = {'competition': 'min', 'modified': 'max', 'dense': 'dense', 'unique': 'first'}[tc.get('rank', 'competition')]
        asc = not tc.get('descending', True)
        res = (g.rank(method=method, ascending=asc) if g is not None
               else frame['__v'].rank(method=method, ascending=asc))
    else:
        raise PlanError(f'Table calculation {kind} is not supported')
    frame['__r'] = res
    return frame.sort_values('__i')['__r'].values


# --------------------------------------------------------------------------- display rows (for comparison)
MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September',
               'October', 'November', 'December']
WEEKDAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']


def key_text(value, part):
    """Canonical key for a dimension value at a date part (both Tableau's text and the app's value map here)."""
    import datetime
    if value is None or (isinstance(value, float) and value != value):
        return 'null'
    t = str(value).strip()
    if part in ('year', 'quarter', 'day', 'week'):
        t = re.sub(r'^FY\s*', '', t, flags=re.I)        # EN17: Tableau's fiscal year header 'FY 2024'
        try:
            return str(int(float(t)))
        except ValueError:
            m = re.match(r'^Q(\d)$', t)
            return m.group(1) if m else t.casefold()
    if part == 'month':
        if t.title() in MONTH_NAMES:
            return str(MONTH_NAMES.index(t.title()) + 1)
        if t.title() in [m[:3] for m in MONTH_NAMES]:
            return str([m[:3] for m in MONTH_NAMES].index(t.title()) + 1)
        try:
            return str(int(float(t)))
        except ValueError:
            return t.casefold()
    if part == 'weekday':
        if t.title() in WEEKDAYS:
            return str(WEEKDAYS.index(t.title()))
        try:
            return str(int(float(t)) % 7)
        except ValueError:
            return t.casefold()
    if part.endswith('_trunc'):
        unit = part.split('_')[0]
        d = _parse_date(t)
        if d is None:
            return t.casefold()
        if unit == 'year':
            return f'{d.year}'
        if unit == 'quarter':
            return f'{d.year}-Q{(d.month - 1) // 3 + 1}'
        if unit == 'month':
            return f'{d.year}-{d.month:02d}'
        if unit == 'week':
            start = d - datetime.timedelta(days=(d.weekday() + 1) % 7)     # Tableau weeks start on Sunday
            return start.isoformat()
        return d.isoformat()
    d = _parse_date(t) if re.match(r'^(\d{4}-\d\d-\d\d|\d{1,2}/\d{1,2}/\d{4}|[A-Z][a-z]+ \d{1,2}, \d{4})', t) else None
    if d is not None:
        return d.isoformat()
    if t.lower() in ('true', 'false'):
        return t.lower()
    try:
        v = float(t.replace(',', ''))
        return str(int(v)) if v.is_integer() else repr(round(v, 9))
    except ValueError:
        return re.sub(r'\s+', ' ', t).casefold()


def _parse_date(t):
    import datetime
    t = re.sub(r'^(week of|week starting)\s+', '', str(t).strip(), flags=re.I)
    m = re.match(r'^Q(\d) (\d{4})$', t)
    if m:
        return datetime.date(int(m.group(2)), 3 * int(m.group(1)) - 2, 1)
    for fmt in ('%Y-%m-%d', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%B %d, %Y', '%b %d, %Y', '%B %Y',
                '%b %Y', '%m/%d/%Y', '%Y', '%d %B %Y'):
        try:
            return datetime.datetime.strptime(t[:len(t)], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.date.fromisoformat(t[:10])
    except ValueError:
        return None


# --------------------------------------------------------------------------- rendering (one generic renderer)
def _vtype(f):
    if f['kind'] == 'measure':
        return 'quantitative'
    if (f['date_part'].endswith('_trunc') or (f['date_part'] == 'none' and f.get('datatype') in ('date', 'datetime'))) \
            and not f['discrete']:
        return 'temporal'
    if f['date_part'] in ('year', 'quarter', 'month', 'day', 'week', 'weekday'):
        return 'ordinal'
    return 'nominal'


_PART_PREFIX = {'month': 'mn', 'year': 'yr', 'quarter': 'qr', 'day': 'dy', 'week': 'wk', 'weekday': 'wd',
                'none': 'none', 'month_trunc': 'tmn', 'year_trunc': 'tyr', 'quarter_trunc': 'tqr',
                'week_trunc': 'twk', 'day_trunc': 'tdy'}
_STYLE_CTX = {}                     # the worksheet's field-scoped formatting while one sheet renders


def field_format(f, key):
    """A field's own formatting from the worksheet XML ('label.text-format', 'header.width', ...)."""
    fields = (_STYLE_CTX.get('style') or {}).get('fields') or {}
    rule = fields.get(f"{_PART_PREFIX.get(f.get('date_part', 'none'), 'none')}:{f.get('caption')}") or {}
    return rule.get(key)


def _labels(df, f, engine):
    """Display values for a dimension column: Tableau's member aliases and date-part names."""
    name = col_name(f)
    if f['date_part'] == 'month':
        fmt = str(field_format(f, 'label.text-format') or '')
        short = fmt.lstrip('i*').upper() in ('LLL', 'MMM')          # Tableau 'Jan' instead of 'January'
        return df[name].map(lambda v: (MONTH_NAMES[int(v) - 1][:3] if short else MONTH_NAMES[int(v) - 1])
                            if v == v and v is not None else v)
    if f['date_part'] == 'quarter':
        return df[name].map(lambda v: f'Q{int(v)}' if v == v and v is not None else v)
    if f['date_part'] == 'year':
        return df[name].map(lambda v: str(int(v)) if v == v and v is not None else v)
    series = df[name]
    try:
        labels = engine.value_labels(f['caption']) or {}
    except Exception:
        labels = {}
    if labels:
        return series.map(lambda v: labels.get(str(v).lower(), labels.get(str(v), v)))
    return series


def display_frame(plan, df, engine):
    """The result with readable column titles (captions) and display values, plus Measure Names/Values long form."""
    import pandas as pd
    out = pd.DataFrame(index=df.index)
    dims, measures = query_fields(plan)
    for d in dims:
        out[_title(d)] = _labels(df, d, engine)
    for m in measures:
        out[_title(m, True)] = df[col_name(m, True)]
    for f in plan['fields']:
        if f.get('table_calc') and f['kind'] == 'measure':
            out[_title(f, True, tc=True)] = df[col_name(f, True) + '|tc']
    return out


def _table_order(plan, df, engine):
    """A text table's rows in Tableau's order: its row headers left to right, each in its own sort (authored
    sort, calendar order for date parts, else ascending), never the database's arbitrary order."""
    try:
        dims = [f for f in plan['fields'] if f['kind'] == 'dim' and f['channel'] == 'rows' and col_name(f) in df.columns]
        if not dims:
            return df
        sort_for = _sort_lookup(plan, engine, {}, df)
        keys, asc = [], []
        out = df.copy()
        for i, f in enumerate(dims):
            order = sort_for(f)
            k = f'__k{i}'
            if isinstance(order, list):
                labels = _labels(df, f, engine).astype(str)
                rank = {v: j for j, v in enumerate(order)}
                out[k] = labels.map(lambda v: rank.get(v, len(rank)))
            else:
                out[k] = df[col_name(f)]
            keys.append(k)
            asc.append(True)
        return out.sort_values(keys, ascending=asc, kind='mergesort', na_position='last').drop(columns=keys)
    except Exception:
        return df


def formatted_frame(plan, df, engine):
    """display_frame with every measure shown in its workbook format (Measure Values in long form become
    one column per measure, as Tableau's text table shows them)."""
    out = display_frame(plan, df, engine)
    _d, measures = query_fields(plan)
    fmt = {}
    for f in list(plan['fields']) + list(plan.get('measure_values') or []):
        if f.get('format'):
            fmt.setdefault(f['caption'], f['format'])
    for m in measures:
        title = _title(m, True)
        if title in out.columns:
            out[title] = out[title].map(lambda v, f=fmt.get(m['caption']): format_value(v, f))
    for c in [c for c in out.columns if c in ('__lat', '__lon')]:
        out = out.drop(columns=c)
    # exact dates the way Tableau shows them (1/5/2021, with the time only when there is one)
    import pandas as pd
    for c in out.columns:
        col = out[c]
        if pd.api.types.is_datetime64_any_dtype(col) or (col.dtype == object and col.notna().any() and
                                                         col.dropna().map(lambda v: hasattr(v, 'year')).all()):
            ts = pd.to_datetime(col, errors='coerce')
            has_time = bool(((ts.dt.hour != 0) | (ts.dt.minute != 0) | (ts.dt.second != 0)).any())
            out[c] = ts.map(lambda v, t=has_time: '' if pd.isna(v) else
                            f"{v.month}/{v.day}/{v.year}" + (v.strftime(' %I:%M:%S %p').replace(' 0', ' ') if t else ''))
    return out


def _title(f, measure=False, tc=False):
    cap = f['caption']
    if f.get('date_part', 'none') != 'none' and not measure:
        part = f['date_part'].replace('_trunc', '').title()
        return f'{part} of {cap}'
    if tc:
        return {'Rank': 'Rank of ', 'PctTotal': '% of Total ', 'CumTotal': 'Running ', 'Diff': 'Difference in ',
                'PctDiff': '% Difference in '}.get(f['table_calc']['type'], '') + cap
    return cap


def render(sheet, where_parts, engine):
    """Draw the sheet from its approved plan (sheet['sheet_plan'])."""
    st, alt = engine.st, engine.alt
    plan = select_measures(sheet['sheet_plan'], engine)     # A4: the measure the selector picks now
    title = engine._layout_text_value(resolve_title(sheet.get('title') or sheet['name'], sheet, where_parts))
    df = execute(plan, sheet, engine, where_parts)      # first: a plan that cannot run leaves the zone untouched
    _STYLE_CTX['style'] = sheet.get('ws_style') or {}
    if (sheet.get('geom') or {}).get('show_title', True):
        runs = sheet.get('title_runs')
        if runs and hasattr(engine, '_runs_html'):
            # the worksheet title as Tableau formats it: its own sizes, weights, colours and lines
            def _keep_edges(raw):
                done = resolve_title(raw, sheet, where_parts) if raw.strip() else raw
                return raw[:len(raw) - len(raw.lstrip())] + str(done).strip() + raw[len(raw.rstrip()):]
            runs = [dict(r, text=_keep_edges(r['text'])) if r.get('text') else r for r in runs]
            if hasattr(engine, '_runs_lines_html'):        # each line at its own height (the space reserved)
                body, align = engine._runs_lines_html(runs, 12)
                st.markdown(f"<div style='text-align:{align};margin:0 0 4px 0'>{body}</div>", unsafe_allow_html=True)
            else:
                body, align = engine._runs_html(runs, 12)
                st.markdown(f"<div style='text-align:{align};line-height:1.2'>{body}</div>", unsafe_allow_html=True)
        else:
            st.markdown(f'**{title}**')
    _LAST_RESULT[sheet.get('name')] = (plan, df)
    evidence = sheet.get('sheet_plan_evidence') or {}
    if evidence.get('status') != 'Verified':
        # stated in the migration notes, never inside the zone (a caption there changes Tableau's layout)
        engine.findings.record('INFO', sheet.get('name'), 'exact-plan-status',
                               (evidence.get('note') or '') if evidence.get('status') == 'Partly verified' else
                               'Exact plan from the workbook shelves; not yet verified against Tableau')
    # zone size from the dashboard layout; a sheet without one (a one-sheet dashboard) uses the page width
    size = {'h': sheet.get('_hpx'), 'w': sheet.get('_wpx') or PAGE_PX, 'fit': sheet.get('_fit'),
            # a legend with its own dashboard zone is drawn there (legend()), never inside the chart
            'legends': [] if sheet.get('_legend_zone') else sheet.get('_legends')}
    with engine.field_scope(_bindable(sheet, engine)):
        if _tiles(plan, df, engine, st):
            return df
        size['style'] = sheet.get('ws_style') or {}
        size['bg'] = effective_background(sheet)
        for spec in plan.get('reflines') or []:
            if spec.get('unsupported'):            # A5: said on the sheet and in the findings, never dropped silently
                try:
                    import findings
                    findings.record('WARNING', sheet.get('name', ''), 'analytics-unsupported', spec['unsupported'])
                except Exception:
                    pass
                st.caption(f"Not shown: {spec['unsupported'].replace(': not drawn', '')}.")
        for el, colour, ratio in low_contrast(size['style'], size['bg']):
            try:
                import findings                # E20: reported, the authored colour is kept (as Tableau draws it)
                findings.record('APPEARANCE', sheet.get('name', ''), 'low-contrast',
                                f'{el} colour {colour} on {size["bg"]}: contrast {ratio}:1 (WCAG minimum 3:1)')
            except Exception:
                pass
        chart = build_chart(plan, df, engine, size)
        scroll = chart is not None and id(chart) in _SCROLL
        strip = _AXIS_STRIP.pop(id(chart), None) if chart is not None else None
        if chart is not None:
            import dashboard_actions as DA        # E19: highlight actions dim the marks that were not picked
            chart = DA.emphasize(chart, sheet, engine)
        if chart is not None:
            chart = apply_ws_style(chart, size['style'], st_alt=engine.alt, bg=size.get('bg'))
        if chart is None:
            kw = {'height': int(size['h'])} if size.get('h') else {}
            import v3_crosstab
            table = None
            if v3_crosstab.applies(plan):
                try:                          # V7: Tableau's crosstab, totals at their own level of detail
                    table = v3_crosstab.crosstab(plan, df, engine,
                                                 run=lambda v: execute(v, sheet, engine, where_parts))
                except Exception:
                    table = None
            if table is None:
                table = formatted_frame(plan, _table_order(plan, df, engine), engine)
            if wrap_labels(plan) and len(table) <= getattr(engine, 'WRAP_TABLE_MAX_ROWS', 0) and \
                    hasattr(engine, '_wrapped_table'):
                engine._wrapped_table(table, height=size.get('h'), style=size.get('style'))   # A10: cells wrap
            else:
                if wrap_labels(plan):
                    try:
                        import findings
                        findings.record('APPEARANCE', sheet.get('name', ''), 'wrap-not-applied',
                                        f'{len(table)} rows: cells do not wrap above '
                                        f'{getattr(engine, "WRAP_TABLE_MAX_ROWS", 0)} rows (the data grid is used)')
                    except Exception:
                        pass
                engine._safe_dataframe(table, use_container_width=True, hide_index=True, **kw)
        elif scroll:
            box_h = int(size.get('h') or 460) - (40 if strip is not None else 0)
            with engine._safe_container(height=max(60, box_h), border=False):
                st.altair_chart(chart, use_container_width=True)
            if strip is not None:
                st.altair_chart(apply_ws_style(strip, size['style'], st_alt=engine.alt, bg=size.get('bg')), use_container_width=True)
        else:
            st.altair_chart(chart, use_container_width=True)
    return df


_TOKEN = re.compile(r'<\s*\[[^\]]+\]\.\[(\w+):([^\]:]+):\w+\]\s*>|\b(\w{2,4}):([^:,<>\[\]]+):(?:ok|nk|qk)\b')
_PART_TEXT = {'qr': lambda v: f'Q{v}', 'tqr': lambda v: f'Q{v}',
              'mn': lambda v: MONTH_NAMES[int(v) - 1] if str(v).isdigit() and 1 <= int(v) <= 12 else str(v)}


def resolve_title(text, sheet, where_parts):
    """A title's field inserts as Tableau shows them: the single value the dashboard control or the sheet's saved
    filter selects for that field (Q4, 2024, Furniture), else 'All'."""
    parts = where_parts if isinstance(where_parts, list) else []

    def value(part, caption):
        part = None if part in ('none', 'usr', 'attr') else part
        for p in parts:
            rf = p.get('runtime_filter') or {}
            if p.get('caption') != caption:
                continue
            if rf.get('kind') == 'date_part' and rf.get('part') == part and rf.get('value') is not None:
                return rf['value']
            if rf.get('kind') == 'categorical' and part is None and rf.get('value') is not None:
                return rf['value']
            if rf.get('kind') in ('date_part', 'categorical') and (rf.get('part') == part or part is None):
                return None                     # the control governs this field and reads All
        for af in sheet.get('applied_filters') or []:
            if af.get('caption') == caption and af.get('kind') == 'in' and (af.get('datepart') or None) == part:
                vals = [v for v in af.get('values') or [] if v not in (None, '%null%')]
                if len(vals) == 1:
                    return vals[0]
        return None

    def sub(m):
        part, caption = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        v = value(part.lower(), caption.strip())
        if v is None:
            return 'All'
        return _PART_TEXT.get(part.lower(), str)(v)
    return _TOKEN.sub(sub, str(text or ''))


_LAST_RESULT = {}      # sheet name -> (plan, rows) of its latest render (its legend zone reuses them)


def legend(sheet, where_parts, engine, legend_type='color', layout='vert', width=None, title=True):
    """Draw the sheet's legend in its own dashboard zone (Tableau places legends as zones, not in the chart)."""
    alt, st = engine.alt, engine.st
    plan = sheet['sheet_plan']
    _STYLE_CTX['style'] = sheet.get('ws_style') or {}
    cached = _LAST_RESULT.get(sheet.get('name'))
    df = cached[1] if cached and cached[0] is plan else execute(plan, sheet, engine, where_parts)
    channel = {'color': 'color', 'size': 'size', 'shape': 'shape'}.get(legend_type or 'color', 'color')
    if any(f.get('measure_values') for f in plan['fields']):
        plan, df = _measure_values_long(plan, df)
        if plan is None:
            return
    f = next((x for x in plan['fields'] if x['channel'] == channel and x['kind'] in ('dim', 'measure')), None)
    if f is None:
        return
    name = col_name(f, True) + ('|tc' if f.get('table_calc') else '') if f['kind'] == 'measure' else col_name(f)
    if name not in df.columns:
        return
    frame = pd_frame = df[[name]].copy()
    safe = re.sub(r'[^A-Za-z0-9_]', '_', name)
    with engine.field_scope(_bindable(sheet, engine)):
        if f['kind'] != 'measure':
            frame[name] = _labels(df, f, engine).astype(str)
        frame = frame.rename(columns={name: safe})
        present, values = None, None
        sort = _sort_lookup(plan, engine)(f)
        if f['kind'] == 'measure':
            import pandas as pd
            values = pd.to_numeric(frame[safe], errors='coerce').dropna()
        else:
            vals = sorted(frame[safe].dropna().astype(str).unique())
            order = sort if isinstance(sort, list) else []
            present = [v for v in order if v in vals] + [v for v in vals if v not in order]
        enc = _enc(alt, {'color': 'Color', 'size': 'Size', 'shape': 'Shape'}[channel], f, safe, sort=sort, plan=plan,
                   present=present, engine=engine, values=values)
    # the legend zone's item layout (leg-item-layout): one column, one row, or a grid that fills the zone width
    lay = {}
    if layout in ('horz', 'grid') and f['kind'] != 'measure':
        n = max(1, len(present or []))
        longest = max([len(str(v)) for v in (present or [])] or [4])
        per_col = max(1, int((width or 200) // (longest * 6.5 + 26)))
        lay = {'direction': 'horizontal', 'columns': n if layout == 'horz' else min(n, per_col)}
    elif f['kind'] == 'measure' and width:
        lay = {'gradientLength': max(40, min(int(width) - 20, 200))}
    enc.legend = alt.Legend(orient='top-left', symbolOpacity=1, title=_title(f, f['kind'] == 'measure') if title else None,
                            labelLimit=160, titleLimit=140, gradientThickness=10, labelOverlap='greedy',
                            titlePadding=3, padding=0, offset=0, symbolSize=60, rowPadding=2,
                            **dict({'gradientLength': 90}, **lay),
                            **({'format': d3_format(f.get('format'))}
                               if f['kind'] == 'measure' and d3_format(f.get('format')) else {}))
    # 'pad' autosize: the chart grows to hold its legend (the app default 'fit' squeezed it to 1 px and clipped it)
    chart = alt.Chart(frame).mark_square(opacity=0, size=1).encode(**{channel: enc}).properties(
        width=1, height=1, autosize=alt.AutoSizeParams(type='pad'), background='transparent',
        padding={'left': 0, 'top': 0, 'right': 4, 'bottom': 0}).configure_view(stroke=None)
    st.altair_chart(chart, use_container_width=False)
    del pd_frame


def _bindable(sheet, engine):
    fields_ir = (getattr(engine, 'IR', {}) or {}).get('fields', {})
    bad = {c for c, fid in (sheet.get('field_bindings') or {}).items()
           if (fields_ir.get(fid) or {}).get('is_calculation') and not (fields_ir.get(fid) or {}).get('calculation')}
    return dict(sheet, field_bindings={c: f for c, f in sheet['field_bindings'].items() if c not in bad}) if bad else sheet


def _tiles(plan, df, engine, st):
    """A text sheet with measures and no dimension (Tableau KPI text): one tile per measure, in workbook order
    and format, like Tableau's text marks. Returns True when drawn."""
    dims, measures = query_fields(plan)
    if dims or plan['mark'] not in ('text', 'square', 'shape') or len(df) != 1 or not measures:
        return False
    fmt = {f['caption']: f.get('format') for f in plan['fields'] if f.get('format')}
    fmt.update({m['caption']: m.get('format') for m in plan.get('measure_values') or [] if m.get('format')})
    cols = st.columns(len(measures))
    for c, m in zip(cols, measures):
        c.metric(mv_name(m), format_value(df[col_name(m, True)].iloc[0], fmt.get(m['caption'])))
    return True


def d3_format(fmt):
    """Tableau number format -> d3 format ('' when it is not a number format)."""
    if not fmt:
        return ''
    t = str(fmt)
    if t.startswith('*') or t.startswith('S') or 'yy' in t or t.startswith('D'):
        return ''
    m = re.match(r'^([CN])(\d+)(%?)$', t)
    if m:                     # Tableau "standard" locale formats (C1033 = en-US currency): two decimals
        return '$,.2f' if m.group(1) == 'C' else ',.2f'
    body = t.split(';')[0]
    decimals = len(re.sub(r'[^0#]', '', body.split('.')[1])) if '.' in body else 0
    comma = ',' if ',' in body else ''
    if body.startswith('p') or body.endswith('%'):
        return f'.{decimals}%'
    if body.startswith('c') or '$' in body:
        return f'${comma}.{decimals}f'
    if body.startswith('n') or re.search(r'[#0]', body):
        return f'{comma}.{decimals}f'
    return ''


def pd_series_format(values, formats):
    """Format each value with its own row's Tableau format (Measure Values mix measures)."""
    return values.combine(formats, lambda v, f: format_value(v, f or None))


def format_value(v, fmt):
    """A value as Tableau displays it with the field's format (tables and labels)."""
    from tableau_format import format_number       # release 27 (F1): the authored pattern itself
    shown = format_number(v, fmt) if fmt else None
    if shown is not None:
        return shown.strip()
    d = d3_format(fmt)
    try:
        x = float(v)
    except (TypeError, ValueError):
        return '' if v is None else str(v)
    if x != x:
        return ''
    if not d:
        return f'{x:,.0f}' if float(x).is_integer() else f'{x:,.2f}'
    m = re.match(r'^(\$?)(,?)\.(\d+)([f%])$', d)
    cur, comma, dec, kind = m.groups()
    if kind == '%':
        return f'{x * 100:{comma}.{dec}f}%'
    text = f'{abs(x):{comma}.{dec}f}'
    if cur:
        return f'(${text})' if x < 0 and '(' in str(fmt) else ('-$' if x < 0 else '$') + text
    return ('-' if x < 0 else '') + text


# Tableau palette name -> (Vega scheme, reversed). Diverging orange-blue: negative orange, positive blue.
PALETTES = {'orange_blue_diverging': ('blueorange', True), 'tableau-orange-blue': ('blueorange', True),
            'red_black': ('redgrey', False), 'red_black_diverging': ('redgrey', False),
            'tableau-red-black': ('redgrey', False), 'red_green_diverging': ('redyellowgreen', False),
            'red_blue_diverging': ('redblue', False), 'red_black_diverging': ('redgrey', False),
            'green_blue_diverging': ('bluegreen', True), 'blue': ('blues', False), 'tableau-blue': ('blues', False),
            'green': ('greens', False), 'orange': ('oranges', False), 'red': ('reds', False),
            'gray': ('greys', False), 'blue_teal': ('tealblues', False), 'purple': ('purples', False),
            'brown': ('browns', False), 'gold': ('goldorange', False)}


def _color_scale(alt, plan, f, values=None):
    cs = (plan.get('color_scales') or {}).get(f['caption']) or plan.get('color_scale') or {}
    if f['kind'] != 'measure':
        return None
    name = re.sub(r'_\d+(_\d+)?$', '', cs.get('palette') or '')
    if not name and values is not None and len(values):
        lo, hi = float(min(values)), float(max(values))
        if lo < 0 < hi:                  # Tableau's automatic palette for a measure crossing zero
            m = max(abs(lo), abs(hi))
            return alt.Scale(scheme='blueorange', reverse=True, domain=[-m, m], domainMid=0)
    scheme, rev = PALETTES.get(name, ('blues', False))
    kw = {'scheme': scheme, 'reverse': rev}
    if 'min' in cs and 'max' in cs:
        kw['domain'] = [cs['min'], cs['max']]
        if cs['min'] < 0 < cs['max']:
            kw['domainMid'] = 0
    return alt.Scale(**kw)


def _enc(alt, channel, f, name, sort=None, title=None, plan=None, present=None, engine=None, values=None):
    t = _vtype(f)
    title = title if title is not None else (f['axis_title'] if 'axis_title' in f and channel in ('X', 'Y')
                                             else _title(f, f['kind'] == 'measure'))
    kw = {'type': t, 'title': title or None}
    if sort is not None and t in ('nominal', 'ordinal'):
        kw['sort'] = sort
    d = d3_format(f.get('format')) if f['kind'] == 'measure' else ''
    if d and channel in ('X', 'Y'):
        kw['axis'] = alt.Axis(format=d)
    if channel in ('X', 'Y') and t in ('nominal', 'ordinal'):
        ax = {'labelOverlap': False, 'labelLimit': 220, 'ticks': False, 'domain': False}
        if channel == 'Y':            # Tableau prints the row field label above its headers, not rotated beside them
            ax.update(titleAngle=0, titleAlign='right', titleAnchor='end', titleBaseline='bottom', titleY=-6, titleX=0)
        elif t == 'ordinal':
            ax.update(labelAngle=0)
        kw['axis'] = alt.Axis(**ax)
    if d and channel in ('Color', 'Size'):
        kw['legend'] = alt.Legend(format=d)
    if channel == 'Shape' and plan is not None:
        smap = (plan.get('shape_maps') or {}).get(f['caption']) or {}
        if smap.get('members'):
            dom = list(present) if present is not None else list(smap['members'])
            dom = [v for v in dom if v in smap['members']] + [v for v in dom if v not in smap['members']]
            rng = [smap['members'].get(v, 'circle') for v in dom]
            kw['scale'] = alt.Scale(domain=dom, range=rng)
    if channel == 'Color' and plan is not None:
        scale = _color_scale(alt, plan, f, values)
        if scale is not None:
            kw['scale'] = scale
        elif present is not None and engine is not None:
            dom, rng = engine.cat_colors(f['caption'], list(present))
            if dom:
                kw['scale'] = alt.Scale(domain=dom, range=rng)
    return getattr(alt, channel)(name, **kw)


def chart_spec(plan):
    """The encodings the renderer will use (also checked against the authored shelves by the visual check)."""
    axis = {'rows': [], 'cols': []}
    for f in plan['fields']:
        if f['channel'] in axis and f['kind'] in ('dim', 'measure') and not f.get('aggregate_dimension'):
            axis[f['channel']].append(f)
    marks = {}
    for f in plan['fields']:
        if f['channel'] in MARK_CHANNELS and f['kind'] in ('dim', 'measure'):
            marks.setdefault(f['channel'], []).append(f)
    return axis, marks


TABLEAU_DEFAULT_MARK = '#4e79a7'      # Tableau's default mark colour (tableau10 blue)
PAGE_PX = 1100                        # usable page width of the wide Streamlit layout (px)
_AXIS_STRIP = {}                      # id(scrolling chart) -> its fixed axis strip (drawn under the scroll area)
ROW_STEP = 22                         # Tableau's row height when a long header list scrolls
AXIS_PX = 44                          # space a continuous axis with its title takes


def _polygon_features(plan, df):
    """V8: one GeoJSON polygon per group (colour / detail dimensions other than the Path field), its points in Path
    order at (Columns measure, Rows measure); marks without a value are skipped."""
    import math
    x = next((f for f in plan['fields'] if f['channel'] == 'cols' and f['kind'] == 'measure'), None)
    y = next((f for f in plan['fields'] if f['channel'] == 'rows' and f['kind'] == 'measure'), None)
    path = next((f for f in plan['fields'] if f['channel'] == 'path' and f['kind'] == 'dim'), None)
    if x is None or y is None:
        return []
    xc, yc = col_name(x, True), col_name(y, True)
    pc = col_name(path) if path is not None else None
    groups = []
    for f in plan['fields']:
        c = col_name(f)
        if f['kind'] == 'dim' and f['channel'] in ('color', 'detail', 'lod') and c != pc and c in df.columns \
                and c not in groups:
            groups.append(c)
    color = next((f for f in plan['fields'] if f['channel'] == 'color' and f['kind'] == 'dim'), None)

    def num(v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return v if math.isfinite(v) else None
    feats = []
    parts = df.groupby([df[g].astype(str) for g in groups], sort=True) if groups else [((), df)]
    for key, g in parts:
        key = key if isinstance(key, tuple) else (key,)
        if pc is not None and pc in g.columns:
            g = g.assign(__o=g[pc].map(lambda v: (0, num(v)) if num(v) is not None else (1, str(v)))).sort_values(
                '__o', kind='stable')
        pts = [[num(a), num(b)] for a, b in zip(g[xc], g[yc]) if num(a) is not None and num(b) is not None]
        pts = [[int(a) if a == int(a) else a, int(b) if b == int(b) else b] for a, b in pts]
        if len(pts) < 3:
            continue
        ring = pts + [pts[0]] if pts[0] != pts[-1] else pts
        props = {'__group': ' · '.join(str(k) for k in key)}
        if color is not None and col_name(color) in g.columns:
            props['__color'] = str(g[col_name(color)].iloc[0])
        feats.append({'type': 'Feature', 'geometry': {'type': 'Polygon', 'coordinates': [ring]}, 'properties': props})
    return feats


def _polygon_chart(plan, df, engine, size=None):
    """V8: the polygons as filled shapes on the measures' own coordinates (identity projection, y up)."""
    alt = engine.alt
    feats = _polygon_features(plan, df)
    if not feats:
        return None
    color = next((f for f in plan['fields'] if f['channel'] == 'color' and f['kind'] == 'dim'), None)
    enc = {'tooltip': [alt.Tooltip('properties.__group:N', title='')]}
    if color is not None:
        present = sorted({f['properties'].get('__color') for f in feats} - {None})
        enc['color'] = _enc(alt, 'Color', color, 'properties.__color', plan=plan, present=present, engine=engine)
    W, H = (size or {}).get('w') or 600, (size or {}).get('h') or 400
    return alt.Chart(alt.Data(values=feats)).mark_geoshape(strokeWidth=0, opacity=0.85).encode(**enc).project(
        type='identity', reflectY=True).properties(width=max(80, W - 160), height=max(80, H - 20))


def _pie_chart(plan, df, engine, size=None):
    """V6: Tableau's Pie mark -- one wedge per colour member, angle = the Angle (wedge-size) measure, Text / Label
    fields as wedge labels. Two dummy measures on Rows (MIN(0) on a dual axis) is Tableau's donut: drawn as a ring.
    None when the sheet has no angle measure (the caller shows the table)."""
    alt = engine.alt
    angle = next((f for f in plan['fields'] if f['channel'] == 'wedge-size' and f['kind'] == 'measure'), None)
    if angle is None or col_name(angle, True) not in df.columns or not len(df):
        return None
    color = next((f for f in plan['fields'] if f['channel'] == 'color' and f['kind'] == 'dim'), None)
    smap = _safe_names(df.columns)
    frame = df.rename(columns=smap).copy()
    theta = smap[col_name(angle, True)]
    import pandas as pd
    frame[theta] = pd.to_numeric(frame[theta], errors='coerce')
    enc = {'theta': alt.Theta(f'{theta}:Q', stack=True)}
    if color is not None and col_name(color) in smap:
        cname = smap[col_name(color)]
        frame[cname] = _labels(df, color, engine).astype(str).values
        present = sorted(frame[cname].dropna().unique())
        enc['color'] = _enc(alt, 'Color', color, cname, plan=plan, present=present, engine=engine)
        enc['order'] = alt.Order(f'{theta}:Q', sort='descending')
    tips = [alt.Tooltip(f'{theta}:Q', title=_title(angle, True), **({'format': d3_format(angle.get('format'))}
                                                                     if d3_format(angle.get('format')) else {}))]
    if color is not None and 'color' in enc:
        tips.insert(0, alt.Tooltip(f'{smap[col_name(color)]}:N', title=color['caption']))
    enc['tooltip'] = tips
    dummies = [f for f in plan['fields'] if f['channel'] == 'rows' and f['kind'] == 'measure']
    donut = len(dummies) >= 2
    W, H = (size or {}).get('w') or 300, (size or {}).get('h') or 300
    outer = max(40, int(min(W, H) / 2) - 30)
    arc = {'outerRadius': outer, **({'innerRadius': int(outer * 0.55)} if donut else {})}
    style = plan.get('mark_style') or {}
    if style.get('stroke'):
        arc.update(stroke=style['stroke'], strokeWidth=1)   # the wedge borders the workbook draws
    if style.get('opacity') is not None:
        arc['opacity'] = style['opacity']
    chart = alt.Chart(frame).mark_arc(**arc).encode(**enc)
    labels = [f for f in plan['fields'] if f['channel'] in ('text', 'label')]
    parts = []
    for f in labels:
        if f['kind'] == 'measure' and col_name(f, True) in df.columns:
            parts.append(df[col_name(f, True)].map(lambda v, fmt=f.get('format'): format_value(v, fmt)))
        elif f['kind'] == 'dim' and col_name(f) in df.columns:
            parts.append(_labels(df, f, engine).astype(str))
    if parts:
        text = parts[0].astype(str)
        for extra in parts[1:]:
            text = text + ' ' + extra.astype(str)
        frame['__label'] = text.values
        chart = chart + alt.Chart(frame).mark_text(radius=outer + 14, fontSize=11, color=_label_style(size, 'color', '#333333')).encode(
            theta=alt.Theta(f'{theta}:Q', stack=True), text='__label:N',
            **({'order': enc['order']} if 'order' in enc else {}))
    return chart.properties(width=W - 40, height=H - 20)


def _safe_names(columns):
    """Result column -> a Vega-safe field name, unique: 'x > PY' and 'x < PY' do not collapse into one field."""
    out, used = {}, set()
    for c in columns:
        base = re.sub(r'[^A-Za-z0-9_]', '_', str(c))
        name, n = base, 1
        while name in used:
            n += 1
            name = f'{base}_{n}'
        used.add(name)
        out[c] = name
    return out


def build_chart(plan, df, engine, size=None):
    """Altair chart following Tableau's shelves: the last continuous field on a shelf is the axis, discrete
    fields before it become headers (facets); mark-card fields become colour/size/text/detail/tooltip.
    Returns None for text tables and shapes the generic renderer does not draw (the caller shows a table)."""
    alt = engine.alt
    mark = plan['mark']
    multi = next((sh for sh, g in (plan.get('measure_axes') or {}).items() if sum(len(x['measures']) for x in g) > 1),
                 None)
    if multi and mark not in ('map', 'text', 'pie'):
        return _measure_panes(plan, df, engine, size, multi)
    if plan.get('authored_mark') == 'Polygon' and not any(f.get('kind') == 'geo' for f in plan['fields']):
        return _polygon_chart(plan, df, engine, size)  # V8: polygons on continuous axes (Sankey ribbons)
    if mark == 'map' or any(f.get('kind') == 'geo' and f['channel'] in ('rows', 'cols') for f in plan['fields']):
        return _map_chart(plan, df, engine, size)      # generated latitude / longitude: a map whatever the mark
    if mark == 'pie':
        return _pie_chart(plan, df, engine, size)
    if mark == 'text':
        return None
    raw = df                                   # before Measure Values become long form (computed sorts need it)
    if any(f.get('measure_values') for f in plan['fields']):
        plan, df = _measure_values_long(plan, df)
        if plan is None:
            return None
    axis, marks = chart_spec(plan)
    frame = df.copy()
    names = {}
    for f in plan['fields']:
        if f['kind'] == 'dim' and not f.get('aggregate_dimension') and not f.get('table_calc'):
            names[f['id']] = col_name(f)
            if _vtype(f) != 'temporal':
                frame[col_name(f)] = _labels(df, f, engine).astype(str)
        elif f['kind'] == 'measure' or f.get('aggregate_dimension'):
            names[f['id']] = col_name(f, True) + ('|tc' if f.get('table_calc') else '')
            if f.get('aggregate_dimension'):
                frame[names[f['id']]] = frame[col_name(f, True)].astype(str)
    smap = _safe_names(frame.columns)        # Vega field names; two captions that differ only in symbols stay apart
    frame.columns = [smap[c] for c in frame.columns]
    safe = {k: smap.get(v, re.sub(r'[^A-Za-z0-9_]', '_', v)) for k, v in names.items()}

    def split(shelf):
        fs = [f for f in axis[shelf] if f['id'] in safe]
        cont = [f for f in fs if f['kind'] == 'measure' or not f['discrete']]
        main = cont[-1] if cont else (fs[-1] if fs else None)
        headers = [f for f in fs if f is not main and f['discrete']]
        return main, headers
    x, col_headers = split('cols')
    y, row_headers = split('rows')
    if x is None and y is None:
        return None
    sort_for = _sort_lookup(plan, engine, safe, raw)
    vlmark = {'bar': 'bar', 'line': 'line', 'area': 'area', 'circle': 'circle', 'square': 'rect',
              'shape': 'point', 'gantt': 'bar', 'point': 'point'}.get(mark, 'bar')
    base = alt.Chart(frame)
    size = size or {}
    mark_kw = {'tooltip': True, 'color': TABLEAU_DEFAULT_MARK}      # a colour encoding overrides it
    if vlmark == 'circle':
        mark_kw['opacity'] = 0.75
    style = plan.get('mark_style') or {}
    if style.get('color'):
        mark_kw['color'] = style['color']      # the worksheet's mark colour (a colour encoding still overrides it)
    if style.get('opacity') is not None:
        mark_kw['opacity'] = style['opacity']
    if vlmark == 'point' and style.get('shape'):
        mark_kw['shape'] = style['shape']
    if style.get('stroke') and vlmark in ('bar', 'rect', 'circle', 'point', 'area'):
        mark_kw.update(stroke=style['stroke'], strokeWidth=1)
    if vlmark == 'line' and style.get('markers') is not None:
        mark_kw['point'] = style['markers']     # Tableau's line markers (all / none)
    if vlmark in ('point', 'circle') and (style.get('open') or
                                          any(m.get('open') for m in (plan.get('shape_maps') or {}).values())):
        mark_kw['filled'] = False              # A10: Tableau's open shapes are hollow marks
    chart = getattr(base, f'mark_{vlmark}')(**mark_kw)
    enc = {}
    hide_axis, hide_labels = set(plan.get('hide_axis') or []), set(plan.get('hide_field_labels') or [])
    for ch, f, scope in (('x', x, 'cols'), ('y', y, 'rows')):
        if f is None:
            continue
        discrete = _vtype(f) in ('nominal', 'ordinal')
        e = _enc(alt, ch.upper(), f, safe[f['id']], sort=sort_for(f),
                 title='' if (discrete and scope in hide_labels) else None)
        if not discrete and scope in hide_axis:
            e.axis = None                      # the workbook hides this axis
        if discrete and f.get('header_hidden'):
            e.axis = None
        if discrete and e.axis is not None and wrap_labels(plan):
            expr = wrap_expr(frame[safe[f['id']]].dropna().astype(str).unique())
            if expr:                           # A10: the workbook wraps header text instead of truncating it
                if isinstance(e.axis, alt.Axis):
                    e.axis.labelExpr = expr
                else:
                    e.axis = alt.Axis(labelExpr=expr)
        enc[ch] = e
    for ch, vl in (('color', 'Color'), ('size', 'Size'), ('shape', 'Shape')):
        f = next((f for f in marks.get(ch, []) if f['id'] in safe), None)
        if f is not None:
            present = None
            if f['kind'] != 'measure':
                values = sorted(frame[safe[f['id']]].dropna().astype(str).unique())
                order = sort_for(f) or []
                present = [v for v in order if v in values] + [v for v in values if v not in order]
            values = None
            if f['kind'] == 'measure':
                import pandas as pd
                values = pd.to_numeric(frame[safe[f['id']]], errors='coerce').dropna()
            enc[ch] = _enc(alt, vl, f, safe[f['id']], sort=sort_for(f), plan=plan, present=present, engine=engine,
                           values=values)
    detail = [f for f in marks.get('detail', []) + marks.get('lod', []) + marks.get('path', []) if f['id'] in safe
              and f['kind'] == 'dim']
    if detail:
        enc['detail'] = [alt.Detail(safe[f['id']], type=_vtype(f)) for f in detail]
    tips = [f for ch in ('tooltip', 'label', 'text', 'color', 'size') for f in marks.get(ch, []) if f['id'] in safe]
    tips += [f for f in (x, y) if f is not None]
    tips += col_headers + row_headers
    seen, tooltip = set(), []
    for f in tips:
        if safe[f['id']] in seen:
            continue
        seen.add(safe[f['id']])
        d = d3_format(f.get('format')) if f['kind'] == 'measure' else ''
        tooltip.append(alt.Tooltip(safe[f['id']], type=_vtype(f), title=_title(f, f['kind'] == 'measure'),
                                   **({'format': d} if d else {})))
    if 'Forecast_Indicator' in frame.columns:
        # FC: approximated forecast estimates are drawn apart from the actuals (dashed line / lighter mark)
        tooltip.append(alt.Tooltip('Forecast_Indicator:N', title='Forecast Indicator'))
        if vlmark == 'line':
            enc['strokeDash'] = alt.StrokeDash('Forecast_Indicator:N', legend=None,
                                               scale=alt.Scale(domain=['Actual', 'Estimate'], range=[[1, 0], [4, 3]]))
        else:
            enc['opacity'] = alt.condition(alt.datum['Forecast_Indicator'] == 'Estimate', alt.value(0.45),
                                           alt.value(1.0))
    enc['tooltip'] = tooltip
    if plan.get('tooltip_lines') and len(frame) == len(df):
        try:                                   # V10: the workbook's own tooltip text, line by line
            import v3_tooltips
            custom = []
            for i, (title, texts) in enumerate(v3_tooltips.columns(plan, df, engine)):
                frame[f'__tip{i}'] = texts
                custom.append(alt.Tooltip(f'__tip{i}:N', title=title))
            if custom:
                enc['tooltip'] = custom
        except Exception:
            pass
    stacked = mark == 'area' or (mark == 'bar' and 'color' in enc)
    stack_ch = 'y' if (y is not None and y['kind'] == 'measure') else 'x'
    color_f = next((f for f in marks.get('color', []) if f['id'] in safe and f['kind'] != 'measure'), None)
    if stacked and stack_ch in enc:
        enc[stack_ch] = enc[stack_ch].stack('zero')
        if color_f is not None:
            order = sort_for(color_f) or sorted(frame[safe[color_f['id']]].dropna().astype(str).unique())
            rank = {v: i for i, v in enumerate(order)}
            frame['__stack'] = frame[safe[color_f['id']]].astype(str).map(lambda v: rank.get(v, len(rank)))
            enc['order'] = alt.Order('__stack:Q', sort='descending')
            base = alt.Chart(frame)
            chart = getattr(base, f'mark_{vlmark}')(**mark_kw)
    size_f = next((f for f in marks.get('size', []) if f['id'] in safe and f['kind'] == 'measure'), None)
    if mark == 'gantt' and x is not None and _vtype(x) == 'temporal' and size_f is not None:
        import pandas as pd
        start = pd.to_datetime(frame[safe[x['id']]])
        frame['__end'] = start + pd.to_timedelta(pd.to_numeric(frame[safe[size_f['id']]], errors='coerce').fillna(0), unit='D')
        base = alt.Chart(frame)
        chart = base.mark_bar(height=10)
        enc['x2'] = alt.X2('__end:T')
        enc.pop('size', None)
    elif mark == 'gantt' and x is not None and _vtype(x) == 'quantitative' and size_f is not None \
            and safe.get(x['id']) in frame.columns:
        # V9: a Gantt bar on a continuous axis starts at the axis value and is Size long (Tableau)
        import pandas as pd
        start = pd.to_numeric(frame[safe[x['id']]], errors='coerce')
        frame['__end'] = start + pd.to_numeric(frame[safe[size_f['id']]], errors='coerce').fillna(0)
        base = alt.Chart(frame)
        chart = base.mark_bar(height=10)
        enc['x2'] = alt.X2('__end:Q')
        enc.pop('size', None)
    labels = [f for f in marks.get('label', []) + marks.get('text', []) if f['id'] in safe]
    if not labels and plan.get('mark_labels'):        # "Show mark labels": Tableau labels the axis measure
        labels = [f for f in (x, y) if f is not None and f['kind'] == 'measure'][:1]
    legends = size.get('legends')
    if legends is not None:                          # Tableau shows a legend only where the dashboard places one
        for ch in ('color', 'size', 'shape'):
            if ch in enc and ch not in legends:
                enc[ch].legend = None
    # a dashboard highlighter on one of this sheet's fields: the selected member stays bright, the rest dim
    hl = getattr(engine, 'highlight_value', None)
    if hl is not None and 'opacity' not in enc:
        for f in plan['fields']:
            if f['kind'] == 'dim' and f['id'] in safe and safe[f['id']] in frame.columns:
                val = hl(f['caption'])
                if val is not None:
                    enc['opacity'] = alt.condition(alt.datum[safe[f['id']]] == str(val), alt.value(1.0), alt.value(0.18))
                    frame[safe[f['id']]] = frame[safe[f['id']]].astype(str)
                    break
    if (x is not None and x['kind'] != 'measure' and _vtype(x) in ('nominal', 'ordinal') and 'x' in enc
            and (y is None or y['kind'] != 'measure')):
        # Tableau prints discrete column headers ABOVE a text table / heatmap (no axis on Rows), not under it
        enc['x'] = _with_axis_top(enc['x'], alt)
    chart = chart.encode(**enc)
    if labels and vlmark in ('bar', 'rect', 'circle', 'point'):
        parts = []
        for f in labels:
            col = frame[safe[f['id']]]
            fmt = f.get('format') or ('p0%' if f.get('table_calc', {}).get('type') == 'PctTotal' else None)
            if f.get('agg') == 'mv' and '__mvfmt' in frame.columns:
                parts.append(pd_series_format(col, frame['__mvfmt']))
                continue
            parts.append(col.map(lambda v, fmt=fmt: format_value(v, fmt)) if f['kind'] == 'measure' else col.astype(str))
        text_col = parts[0]
        for extra in parts[1:]:
            text_col = text_col + ' ' + extra
        frame['__label'] = text_col
        tenc = {k: v for k, v in enc.items() if k in ('x', 'y', 'order')}
        # R46: a bar is one combination of its axis dimension AND its row / column headers (each pane its own bars)
        keys = [safe[f['id']] for f in (x, y) if f is not None and f['kind'] != 'measure']
        keys += [safe[f['id']] for f in row_headers + col_headers
                 if safe.get(f['id']) in frame.columns and safe[f['id']] not in keys]
        # a colour that never splits a bar (Region on Color and on Columns) draws whole bars: labelled at the end
        one_segment = bool(keys) and int(frame.groupby(keys, dropna=False).size().max() or 0) <= 1
        if stacked and color_f is not None and stack_ch in tenc and not one_segment:
            # label at the middle of its own segment of the stack
            meas = safe[(y if stack_ch == 'y' else x)['id']]
            ordered = frame.sort_values('__stack', ascending=False, kind='mergesort')
            cum = ordered.groupby(keys)[meas].cumsum() if keys else ordered[meas].cumsum()
            frame['__mid'] = (cum - ordered[meas] / 2).reindex(frame.index)
            tenc[stack_ch] = (alt.Y if stack_ch == 'y' else alt.X)('__mid:Q')
            tmark = alt.Chart(frame).mark_text(align='center', baseline='middle', color='white')
        elif vlmark in ('rect', 'circle', 'point') or (x is not None and y is not None and x['kind'] != 'measure'
                                                        and y['kind'] != 'measure'):
            # a label on a square / shape / text-table cell sits at the centre of the mark (Tableau)
            tmark = alt.Chart(frame).mark_text(align=_label_style(size, 'align', 'center'), baseline='middle',
                                               fontSize=_label_style(size, 'size', 11),
                                               color=_label_style(size, 'color', '#333333'),
                                               fontWeight=_label_style(size, 'weight', 'normal'),
                                               **_label_font(size))
            heat = next((f for f in marks.get('color', []) if f['id'] in safe and f['kind'] == 'measure'), None)
            if vlmark == 'rect' and heat is not None and safe[heat['id']] in frame.columns:
                # Tableau keeps a label readable on a dark cell: white text on the darkest part of the colour ramp
                import pandas as pd
                vals = pd.to_numeric(frame[safe[heat['id']]], errors='coerce').dropna()
                if len(vals):
                    cut = float(vals.min() + 0.62 * (vals.max() - vals.min()))
                    tenc['color'] = alt.condition(f"datum['{safe[heat['id']]}'] >= {cut}",
                                                  alt.value('#ffffff'), alt.value('#333333'))
        elif vlmark == 'bar' and stack_ch == 'y' and y is not None and y['kind'] == 'measure':
            # a vertical bar's label sits centred just above the bar's end (Tableau's automatic alignment)
            tmark = alt.Chart(frame).mark_text(align='center', baseline='bottom', dy=-3,
                                               fontSize=_label_style(size, 'size', 11),
                                               color=_label_style(size, 'color', '#333333'),
                                               fontWeight=_label_style(size, 'weight', 'normal'), **_label_font(size))
        else:
            tmark = alt.Chart(frame).mark_text(align='left', dx=3, baseline='middle',
                                               fontSize=_label_style(size, 'size', 11),
                                               color=_label_style(size, 'color', '#333333'),
                                               fontWeight=_label_style(size, 'weight', 'normal'), **_label_font(size))
        chart = chart + tmark.encode(**{k: v for k, v in tenc.items() if k != 'order'}, text='__label:N')
    if plan.get('reflines') and not any(f.get('measure_values') for f in plan['fields']):
        import v3_reflines                     # EN15: reference lines, bands and distributions
        params = dict(getattr(engine, 'PARAMS', {}) or {})
        chart = v3_reflines.layers(alt, plan, df, frame, lambda c: smap.get(c, re.sub(r'[^A-Za-z0-9_]', '_', c)), x, y, safe,
                                   chart, params)
    H, W = size.get('h'), size.get('w')
    # the sheet's fit in its dashboard zone (<viewpoint><zoom>): entire view / fit height fill the zone height
    fill_h = size.get('fit') in ('entire-view', 'fit-height')
    legend_px = 120 if any(ch in enc and getattr(enc[ch], 'legend', None) is not None and
                           (legends is None or ch in legends) for ch in ('color', 'size', 'shape')) else 0
    n_y = frame[safe[y['id']]].nunique() if y is not None and _vtype(y) in ('nominal', 'ordinal') else 0
    n_x = frame[safe[x['id']]].nunique() if x is not None and _vtype(x) in ('nominal', 'ordinal') else 0
    if not (row_headers or col_headers):
        # one pane: fill the Tableau zone; a header list longer than the zone scrolls inside it (as in Tableau)
        step = plan.get('row_height') or ROW_STEP
        if n_y and H and n_y * step > H and not fill_h:
            # the list scrolls inside the zone and Tableau keeps the continuous axis fixed under it: the axis is
            # drawn as its own strip below the scrolling rows, on the same scale and the same left offset
            xd = enc['x'].to_dict() if 'x' in enc and hasattr(enc['x'], 'to_dict') else (enc.get('x') or {})
            axis_strip = None
            if x is not None and isinstance(xd, dict) and xd.get('type') in ('quantitative', 'temporal') \
                    and xd.get('field') in frame.columns and (xd.get('axis', {}) is not None):
                import pandas as pd
                xcols = [xd['field']] + ([enc['x2'].to_dict().get('field')] if 'x2' in enc else [])
                if xd['type'] == 'temporal':
                    vals = pd.concat([pd.to_datetime(frame[c], errors='coerce') for c in xcols if c in frame.columns])
                    dom = [vals.min().isoformat(), vals.max().isoformat()]
                else:
                    vals = pd.concat([pd.to_numeric(frame[c], errors='coerce') for c in xcols if c in frame.columns])
                    dom = [float(min(0, vals.min())), float(vals.max())]
                left = _header_px(frame, safe, [], y)
                ydict = enc['y'].to_dict() if hasattr(enc.get('y'), 'to_dict') else {}
                yaxis = dict(ydict.get('axis') or {}, minExtent=left, maxExtent=left)
                # rows keep their vertical gridlines; the labels live in the fixed strip
                xaxis_main = dict(xd, axis={'labels': False, 'ticks': False, 'domain': False, 'title': None, 'grid': True},
                                  scale=dict(xd.get('scale') or {}, domain=dom))
                chart = chart.encode(x=alt.X(**{k: v for k, v in xaxis_main.items()}),
                                     y=alt.Y(**dict(ydict, axis=yaxis)))
                axis_strip = alt.Chart(frame.head(1)).mark_point(opacity=0).encode(
                    x=alt.X(**dict(xd, scale=dict(xd.get('scale') or {}, domain=dom))),
                    y=alt.Y(field=ydict.get('field'), type=ydict.get('type', 'nominal'), title=None,
                            axis=alt.Axis(labels=False, ticks=False, domain=False, minExtent=left, maxExtent=left))
                ).properties(height=1, autosize=alt.AutoSizeParams(type='fit-x', contains='padding'))
            chart = chart.properties(height=n_y * step)
            _SCROLL.add(id(chart))
            if axis_strip is not None:
                _AXIS_STRIP[id(chart)] = axis_strip
        elif n_y > 30 and not H:
            chart = chart.properties(height=n_y * step)
            _SCROLL.add(id(chart))
        elif H:
            # Vega-Lite 'fit': the chart including its axes, titles and legend takes exactly the zone height
            chart = chart.properties(height=max(30, int(H)),
                                     autosize=alt.AutoSizeParams(type='fit', contains='padding'))
        return chart
    # Several discrete headers on one shelf (Year > Month > Segment): Tableau nests them; every combination is
    # one header row/column, in the nested sort order. Combine them into one header, label 'outer · inner'.
    for shelf_headers, key in ((row_headers, '__rows'), (col_headers, '__cols')):
        if len(shelf_headers) > 1:
            cols = [safe[f['id']] for f in shelf_headers]
            ranks = []
            for f in shelf_headers:
                order = sort_for(f)
                vals = list(dict.fromkeys(frame[safe[f['id']]].astype(str)))
                seq = ([v for v in order if v in vals] + sorted(v for v in vals if v not in order)
                       if isinstance(order, list) else sorted(vals))
                ranks.append({v: i for i, v in enumerate(seq)})
            frame[key] = frame[cols].astype(str).agg(' · '.join, axis=1)
            combos = frame[cols].astype(str).drop_duplicates().values.tolist()
            combos.sort(key=lambda c: tuple(r.get(v, 0) for r, v in zip(ranks, c)))
            merged = {'id': key, 'caption': ' / '.join(f['caption'] for f in shelf_headers), 'kind': 'dim',
                      'discrete': True, 'date_part': 'none', 'agg': 'none', '_order': [' · '.join(c) for c in combos],
                      'header_hidden': all(f.get('header_hidden') for f in shelf_headers)}
            safe[key] = key
            shelf_headers[:] = [merged]
    base_sort_for = sort_for
    sort_for = lambda f: f['_order'] if f.get('_order') else base_sort_for(f)
    facet = {}
    n_r = frame[safe[row_headers[-1]['id']]].nunique() if row_headers else 1
    n_c = frame[safe[col_headers[-1]['id']]].nunique() if col_headers else 1
    col_head_px = 0
    col_args = None
    if row_headers:
        f = row_headers[-1]
        no_title = f['caption'] == 'Measure Names' or 'rows' in hide_labels
        facet['row'] = alt.Row(safe[f['id']], type=_vtype(f), sort=sort_for(f),
                               title=None if no_title else _title(f),
                               # Tableau prints a row header at the top of its rows, left aligned
                               header=alt.Header(labelAngle=0, labelAlign='left', labelOrient='left',
                                                 labelAnchor='start', labelBaseline='top',
                                                 titleAngle=0, titleOrient='top', titleAlign='left', labelLimit=160,
                                                 labels=not f.get('header_hidden')))
    if col_headers:
        f = col_headers[-1]
        no_title = f['caption'] == 'Measure Names' or 'cols' in hide_labels
        col_args = {'field': safe[f['id']], 'type': _vtype(f), 'sort': sort_for(f),
                    'title': None if no_title else _title(f), 'labels': not f.get('header_hidden')}
        facet['column'] = alt.Column(col_args['field'], type=col_args['type'], sort=col_args['sort'],
                                     title=col_args['title'], header=alt.Header(labelLimit=200, labels=col_args['labels']))
        col_head_px = (0 if f.get('header_hidden') else 22) + (0 if no_title else 22)
    props = {}
    x_axis_px = 0 if (x is None or (x['kind'] == 'measure' and 'cols' in hide_axis)) else AXIS_PX
    inner_rows = n_y or 0
    nested = False
    if inner_rows and row_headers:
        groups = frame.groupby(safe[row_headers[-1]['id']])[safe[y['id']]].apply(lambda v: frozenset(v.astype(str)))
        nested = groups.nunique() > 1          # members differ per outer header (Category > Sub-Category)
    if nested:
        # Tableau gives every header row the same height and each outer header lists only its own members:
        # one sub-chart per outer member, stacked, one axis at the bottom.
        total = int(frame.groupby(safe[row_headers[-1]['id']])[safe[y['id']]].nunique().sum())
        step = plan.get('row_height') or ROW_STEP
        if fill_h and H:
            # 'Entire view' / 'Fit height': every header row shares the zone height (Tableau squeezes, never scrolls)
            n_outer = frame[safe[row_headers[-1]['id']]].nunique()
            step = max(6, (H - (x_axis_px + 16) - (22 if col_headers else 0) - 4 * n_outer) / max(1, total))
        out = _nested_rows(alt, chart, frame, safe, row_headers[-1], col_args if col_headers else None, sort_for,
                           step, W, n_c, legend_px)
        if H and not fill_h and total * step > H:
            _SCROLL.add(id(out))            # taller than its zone: it scrolls inside the zone, as in Tableau
        return out
    if plan.get('row_height') and inner_rows and not fill_h:   # authored header-row height decides the height
        props['height'] = max(24, inner_rows * plan['row_height'])
    elif H:                         # panels share the zone height (headers, axis, its title and spacing included)
        # a discrete header row (months over a heatmap) is one label line, not an axis with ticks and a title
        x_disc = x is not None and x['kind'] != 'measure' and _vtype(x) in ('nominal', 'ordinal')
        # + the chart's own padding (Vega pads 5 px on each side) so the last row is never clipped
        overhead = (24 if x_disc else x_axis_px + (16 if x_axis_px else 0)) + col_head_px + 22
        props['height'] = max(24, int((H - overhead) / n_r) - 4)
    else:
        props['height'] = max(40, 220 // n_r) if row_headers else 200
    if W:                           # and the zone width (row headers and a placed legend take theirs)
        # a label printed after the end of a bar needs room inside the zone too
        longest = int(frame['__label'].astype(str).str.len().max() or 0) if '__label' in frame.columns else 0
        label_px = max(0, int(longest * 6.2) - 26) if (labels and vlmark == 'bar') else 0
        # a measure axis on Rows takes the width of its tick labels ($50,000) in every pane
        y_axis_px = 0 if (y is None or y['kind'] != 'measure' or 'rows' in hide_axis) else 62
        props['width'] = max(40, int((W - _header_px(frame, safe, row_headers, y if n_y else None) - legend_px) / n_c)
                             - 14 - label_px - y_axis_px)
    chart = chart.properties(**props).facet(**facet, spacing={'row': 4, 'column': 8})
    scroll = bool(H and not fill_h and plan.get('row_height') and inner_rows and props['height'] * n_r > H)
    resolve = {}
    if col_headers and col_headers[-1]['caption'] == 'Measure Names':
        resolve['x'] = 'independent'          # each measure keeps its own axis, as in Tableau
    if row_headers and (row_headers[-1]['caption'] == 'Measure Names' or n_y):
        resolve['y'] = 'independent'          # nested headers: a row header lists only its own members
    if col_headers and n_x:
        resolve['x'] = 'independent'
    if resolve:
        chart = chart.resolve_scale(**resolve)
    chart = chart.properties(title='') if (len(row_headers) > 1 or len(col_headers) > 1) else chart
    if scroll:
        _SCROLL.add(id(chart))              # authored row heights taller than the zone: scrolls inside it
    return chart


def _label_style(size, what, default):
    """Mark-label formatting from the worksheet's 'label' rule (or 'cell' for a text table)."""
    style = (size or {}).get('style') or {}
    rule = dict(style.get('cell') or {}, **(style.get('label') or {}))
    if what == 'size':
        return _pt(rule.get('font-size'), default)
    if what == 'color':
        c = rule.get('color')
        if c:
            return c[:7]
        if is_dark((size or {}).get('bg')) and is_dark(default):
            return DARK_INK['text']               # E20: a generated dark label on a dark background
        return default
    if what == 'weight':
        return 'bold' if rule.get('font-weight') == 'bold' else default
    if what == 'align':
        return rule.get('text-align') if rule.get('text-align') in ('left', 'center', 'right') else default
    return default


def _label_font(size):
    style = (size or {}).get('style') or {}
    font = (style.get('label') or {}).get('font-family') or (style.get('worksheet') or {}).get('font-family')
    return {'font': font} if font else {}


def _pt(v, default=None):
    """Tableau font size (points) -> px as a browser draws it."""
    try:
        return round(float(v) * 4 / 3, 1)
    except (TypeError, ValueError):
        return default


def _off(rule):
    return (rule or {}).get('line-visibility') == 'off' or str((rule or {}).get('stroke-size')) == '0'


def _rgb(color):
    c = str(color or '').strip().lstrip('#')
    if len(c) not in (6, 8) or not re.fullmatch(r'[0-9a-fA-F]+', c):
        return None
    return tuple(int(c[i:i + 2], 16) / 255 for i in (0, 2, 4))


def contrast_ratio(fg, bg):
    """E20: WCAG 2 contrast ratio of two '#rrggbb' colours (1..21); None when either is not a colour."""
    a, b = _rgb(fg), _rgb(bg)
    if a is None or b is None:
        return None

    def lum(c):
        lin = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return round((hi + 0.05) / (lo + 0.05), 2)


def is_dark(bg):
    rgb = _rgb(bg)
    return rgb is not None and contrast_ratio(bg, '#ffffff') > contrast_ratio(bg, '#000000')


DARK_INK = {'text': '#e8e8e8', 'grid': '#444444', 'domain': '#8a8a8a'}


def effective_background(sheet):
    """E20 / X04: the colour a sheet's chart sits on: its own shading (Format > Shading), else the container it
    is drawn in ('_bg' from the snapshot compositor, the live canvas background, or a dark container), else white."""
    import axis_xml
    own = axis_xml.table_background((sheet or {}).get('ws_style'))
    if own not in (None, 'transparent'):
        return own
    if _rgb((sheet or {}).get('_bg')):
        return '#' + str(sheet['_bg']).lstrip('#')[:6]
    if (sheet or {}).get('_dark'):
        return '#1f1f1f'                   # a dark Tableau container (its exact colour is painted by the CSS)
    return '#ffffff'


def low_contrast(style, bg, minimum=3.0):
    """E20: authored text colours drawn on the sheet's background (axis, header, legend title, title) that read
    below the WCAG minimum for large text / graphics -> [(element, colour, ratio)]. Mark labels and cell text are
    left out: Tableau draws them on the marks (a white label on a coloured square), not on the background."""
    style = style or {}
    out = []
    for key, el in (('axis', 'color'), ('header', 'color'), ('legend-title-text', 'color'), ('title', 'color')):
        colour = (style.get(key) or {}).get(el)
        r = contrast_ratio(colour, bg)
        if r is not None and r < minimum:
            out.append((key, colour[:7], r))
    return out


def apply_ws_style(chart, style, st_alt, bg=None):
    """Release 26g: the worksheet's own formatting from its XML style rules, applied to the whole chart:
    default font, axis label font / size / colour, tick colour, axis line, gridlines (on / off / colour / width),
    pane border and background. Anything the sheet does not format keeps Tableau's defaults (light grey grid)."""
    style = style or {}
    try:
        font = (style.get('worksheet') or {}).get('font-family')
        axis = style.get('axis') or {}
        grid = style.get('gridline') or {}
        cfg_axis = {}
        dark = is_dark(bg)
        if dark:                          # E20: the generated defaults stay readable on a dark background
            cfg_axis.update(labelColor=DARK_INK['text'], titleColor=DARK_INK['text'], gridColor=DARK_INK['grid'],
                            domainColor=DARK_INK['domain'], tickColor=DARK_INK['domain'])
        if _off(grid):
            cfg_axis['grid'] = False
        else:
            if grid.get('stroke-color'):
                cfg_axis['gridColor'] = grid['stroke-color'][:7]
            if grid.get('stroke-size'):
                cfg_axis['gridWidth'] = float(grid['stroke-size'])
        if axis.get('tick-color'):
            tc = axis['tick-color']
            cfg_axis['ticks'] = not (len(tc) == 9 and tc.endswith('00'))
            cfg_axis['tickColor'] = tc[:7]
        if _off(axis) and 'line-visibility' in axis:
            cfg_axis['domain'] = False
        if axis.get('font-size'):
            cfg_axis['labelFontSize'] = _pt(axis['font-size'])
        if axis.get('color'):
            cfg_axis['labelColor'] = axis['color']
        if font:
            cfg_axis['labelFont'] = font
            cfg_axis['titleFont'] = font
        if cfg_axis:
            chart = chart.configure_axis(**cfg_axis)
        header = style.get('header') or {}
        cfg_head = {'labelColor': DARK_INK['text'], 'titleColor': DARK_INK['text']} if dark else {}
        if header.get('font-size'):
            cfg_head['labelFontSize'] = _pt(header['font-size'])
        if header.get('color'):
            cfg_head['labelColor'] = header['color']
        if font:
            cfg_head['labelFont'] = font
        if cfg_head:
            chart = chart.configure_header(**cfg_head)
        pane = style.get('pane') or {}
        dividers_off = all(_off(style.get(k)) for k in ('table-div|rows', 'table-div|cols')) or _off(style.get('table-div'))
        if pane.get('border-style') == 'none' or str(pane.get('border-width')) == '0' or dividers_off:
            chart = chart.configure_view(strokeWidth=0)      # V11: pane dividers turned off in the workbook
        bg = (style.get('table') or {}).get('background-color')
        if bg and not (len(bg) == 9 and bg.endswith('00')):
            chart = chart.properties(background=bg[:7])
        leg = style.get('legend-title-text') or {}
        if dark:
            chart = chart.configure_legend(labelColor=DARK_INK['text'], titleColor=DARK_INK['text'])
            chart = chart.configure_title(color=DARK_INK['text'])
        if leg.get('color') or font:
            chart = chart.configure_legend(**({'titleColor': leg['color']} if leg.get('color') else {}),
                                           **({'labelFont': font, 'titleFont': font} if font else {}))
    except Exception:
        return chart                      # formatting never stops a chart from drawing
    return chart


def _with_axis_top(channel, alt):
    d = channel.to_dict()
    if 'axis' in d and d['axis'] is None:          # the author hid this axis
        return channel
    d['axis'] = dict(d.get('axis') or {}, orient='top')
    return type(channel).from_dict(d)


def _header_px(frame, safe, row_headers, y):
    """Width the row headers and the inner discrete axis labels take (from their longest label), so the panes
    get the rest of the zone instead of a fixed guess."""
    px = 0
    for f in list(row_headers[-1:]) + ([y] if y is not None else []):
        if f.get('header_hidden'):
            continue
        authored = field_format(f, 'header.width')          # the header width the author set in Tableau
        if authored and str(authored).isdigit():
            px += int(authored) + 6
            continue
        col = safe.get(f['id'])
        if col in frame.columns:
            longest = frame[col].astype(str).str.len().max() or 0
            px += min(160, int(longest * 6.5)) + 16
    return px or 60


def _nested_rows(alt, chart, frame, safe, outer, column, sort_for, step, W, n_c, legend_px):
    col = safe[outer['id']]
    present = [str(v) for v in frame[col].dropna().astype(str).unique()]
    order = sort_for(outer)
    values = ([v for v in order if v in present] + [v for v in sorted(present) if v not in order]
              if isinstance(order, list) else sorted(present))
    subs = []
    for i, v in enumerate(values):
        sub = chart.transform_filter(alt.datum[col] == v).properties(height=alt.Step(step))
        if W:
            sub = sub.properties(width=max(40, int((W - 260 - legend_px) / max(1, n_c)) - 8))
        if column is not None:                      # Tableau prints the column headers once, above the table
            head = alt.Column(column['field'], type=column['type'], sort=column['sort'],
                              title=None if i else column['title'],
                              header=alt.Header(labels=bool(column['labels']) and not i, labelLimit=200))
            sub = sub.facet(column=head, spacing={'column': 8})
        sub = sub.properties(title=alt.TitleParams(text=v, orient='left', angle=0, anchor='start', align='right',
                                                   fontSize=11, fontWeight='normal', color='#555555', offset=8))
        subs.append(sub)
    out = alt.vconcat(*subs, spacing=4).resolve_scale(y='independent')
    # one x axis, at the bottom (Tableau draws the axis once under the last header)
    spec = out.to_dict()

    def strip_x(node):
        if isinstance(node, dict):
            enc = node.get('encoding')
            if isinstance(enc, dict) and isinstance(enc.get('x'), dict):
                enc['x']['axis'] = None
            for v in node.values():
                strip_x(v)
        elif isinstance(node, list):
            for v in node:
                strip_x(v)
    for part in spec.get('vconcat', [])[:-1]:
        strip_x(part)
    return alt.VConcatChart.from_dict(inline_datasets(spec))


def inline_datasets(spec):
    """A chart spec rebuilt with from_dict keeps its rows in 'datasets'; Streamlit's st.altair_chart replaces that
    key with its own (empty for such a chart), so the chart drew no marks in the app while every image export
    showed them. Every named dataset is written back inline where it is used."""
    named = spec.pop('datasets', None) or {}

    def inline(node):
        if isinstance(node, dict):
            if isinstance(node.get('data'), dict) and node['data'].get('name') in named:
                node['data'] = {'values': named[node['data']['name']]}
            for v in node.values():
                inline(v)
        elif isinstance(node, list):
            for v in node:
                inline(v)
    inline(spec)
    return spec


def _map_chart(plan, df, engine, size=None):
    """A map on generated latitude/longitude, drawn at Tableau's own coordinates for each location (taken from
    Tableau's summary data when the plan was verified), over a bundled background map (countries, US states).
    A filled map (Tableau 'Multipolygon') colours US states by their shapes; other locations are drawn as
    circles at Tableau's coordinates. Without coordinates the caller shows the table."""
    alt = engine.alt
    if '__lat' not in df.columns or not len(df):
        return None
    import pandas as pd
    import v3_basemaps as B
    frame = df.copy()
    dims, _m = query_fields(plan)
    _axis, marks = chart_spec(plan)
    color = next((f for f in marks.get('color', []) if f['kind'] == 'measure'), None)
    cols = {c: re.sub(r'[^A-Za-z0-9_]', '_', c) for c in frame.columns}
    frame = frame.rename(columns=cols)
    tooltip = [alt.Tooltip(cols[col_name(d)], type='nominal', title=d['caption']) for d in dims]
    enc = {'longitude': alt.Longitude('__lon:Q'), 'latitude': alt.Latitude('__lat:Q'), 'tooltip': tooltip}
    color_enc, name = None, None
    if color is not None:
        name = cols[col_name(color, True)]
        color_enc = _enc(alt, 'Color', color, name, plan=plan,
                         values=pd.to_numeric(frame[name], errors='coerce').dropna())
        legends = (size or {}).get('legends')
        if legends is not None and 'color' not in legends:      # the dashboard shows it in its own zone, or not at all
            color_enc.legend = None
        enc['color'] = color_enc
        d = d3_format(color.get('format'))
        tooltip.append(alt.Tooltip(name, type='quantitative', title=color['caption'], **({'format': d} if d else {})))
    # which countries the locations are in (Tableau's geocoding keys: 'state␟country')
    countries = {k.split('\u241f')[-1] for k in (plan.get('geo_points') or {})}
    country_dim = next((d for d in dims if d['caption'].lower().startswith('country')), None)
    if country_dim is not None and cols.get(col_name(country_dim)) in frame.columns:
        countries |= {str(v).lower() for v in frame[cols[col_name(country_dim)]].dropna().unique()}
    world_names = sorted({B.COUNTRY_ALIASES.get(c, c.title()) for c in countries if c})
    us_only = world_names == ['United States of America']
    projection = 'albersUsa' if us_only else 'mercator'
    layers = []
    base = alt.Data(values=B.WORLD_COUNTRIES, format=alt.DataFormat(type='topojson', feature='countries'))
    land = alt.Chart(base).mark_geoshape(fill='#efefef', stroke='#c8c8c8', strokeWidth=0.5, clip=True)
    if world_names and not us_only:
        land = land.transform_filter(alt.FieldOneOfPredicate(field='properties.name', oneOf=world_names))
    if not us_only:
        layers.append(land)
    state_dim = next((d for d in dims if any(w in d['caption'].lower() for w in ('state', 'province'))), None)
    states = alt.Data(values=B.US_STATES, format=alt.DataFormat(type='topojson', feature='states'))
    us_names = {g['properties']['name'] for g in B.US_STATES['objects']['states']['geometries']}
    filled = plan.get('authored_mark') in ('Multipolygon', 'Polygon') and state_dim is not None and color is not None
    if 'United States of America' in world_names or us_only:
        layers.append(alt.Chart(states).mark_geoshape(fill='#efefef', stroke='#ffffff', strokeWidth=0.7, clip=True))
    points = frame
    if filled:
        scol = cols[col_name(state_dim)]
        matched = frame[frame[scol].astype(str).isin(us_names)]
        if len(matched):
            fills = alt.Chart(states).mark_geoshape(stroke='#ffffff', strokeWidth=0.7, clip=True).transform_lookup(
                lookup='properties.name', from_=alt.LookupData(matched, scol, [name, scol])).transform_filter(
                f"isValid(datum['{name}'])").encode(color=color_enc, tooltip=[alt.Tooltip(scol, type='nominal'),
                                                                               tooltip[-1]])
            layers.append(fills)
            points = frame[~frame[scol].astype(str).isin(us_names)]
    if len(points):
        layers.append(alt.Chart(points).mark_circle(size=140, opacity=0.9, stroke='white', strokeWidth=0.5, clip=True)
                      .encode(**enc))
    chart = alt.layer(*layers) if len(layers) > 1 else layers[0]
    if (size or {}).get('w') and (size or {}).get('h'):       # the map fills its dashboard zone
        chart = chart.properties(width=max(80, int(size['w']) - 10), height=max(60, int(size['h']) - 10),
                                 autosize=alt.AutoSizeParams(type='none'))
    # Tableau zooms the map to the marks; fit the projection to the locations (with a margin), not to whole
    # countries (Alaska and the Canadian Arctic would otherwise shrink the map)
    lats = pd.to_numeric(frame['__lat'], errors='coerce').dropna()
    lons = pd.to_numeric(frame['__lon'], errors='coerce').dropna()
    if len(lats) and len(lons):
        pad_y = max(2.0, (lats.max() - lats.min()) * 0.08)
        pad_x = max(2.0, (lons.max() - lons.min()) * 0.05)
        box = [[float(lons.min() - pad_x), float(lats.min() - pad_y)], [float(lons.max() + pad_x), float(lats.max() + pad_y)],
               [float(lons.min() - pad_x), float(lats.max() + pad_y)], [float(lons.max() + pad_x), float(lats.min() - pad_y)]]
        fit = {'type': 'Feature', 'geometry': {'type': 'MultiPoint', 'coordinates': box}, 'properties': {}}
        return chart.project(type=projection, fit=fit)
    return chart.project(type=projection)


MV_NAMES = {'avg': 'Avg. ', 'min': 'Min. ', 'max': 'Max. ', 'cnt': 'Count of ', 'ctd': 'Count Distinct of ',
            'med': 'Median of '}


def mv_name(m):
    """Tableau's Measure Names label for a measure: the author's alias when the workbook gives one
    ('Count of Customers'), else the aggregation prefix as Tableau shows it."""
    import sys
    eng = sys.modules.get('engine')
    aliases = ((getattr(eng, 'IR', None) or {}).get('measure_name_aliases') or {}) if eng is not None else {}
    if aliases:
        internal = (m.get('field_id') or '').split('::')[-1]
        for key in (f"{m.get('agg')}:{m['caption']}", f"{m.get('agg')}:{internal}"):
            if key in aliases:
                return aliases[key]
    return MV_NAMES.get(m.get('agg'), '') + m['caption']


def _measure_values_long(plan, df):
    """Measure Values on Rows/Columns: unpivot the measures into one value column and a Measure Names column,
    so the generic renderer draws them like Tableau (one bar/line per measure). (plan, df) or (None, None)."""
    import pandas as pd
    mv_field = next(f for f in plan['fields'] if f.get('measure_values'))
    if mv_field['channel'] not in ('rows', 'cols') or not plan.get('measure_values'):
        return None, None
    cols = [col_name(m, True) for m in plan['measure_values']]
    ref_cols = {r.get('value_col') for r in plan.get('reflines') or []}     # a reference line computed from one
    keep = [c for c in df.columns if not c.startswith('M|') or c not in cols or c in ref_cols]
    parts = []
    for m, c in zip(plan['measure_values'], cols):
        part = df[keep].copy()
        part['M|mv|Measure Values'] = pd.to_numeric(df[c], errors='coerce')
        part['D|none|Measure Names'] = mv_name(m)
        part['__mvfmt'] = m.get('format') or ''           # each measure keeps its own number format
        parts.append(part)
    long = pd.concat(parts, ignore_index=True)
    order = [mv_name(m) for m in plan['measure_values']]
    plan = deepcopy(plan)
    for f in plan['fields']:
        if f.get('measure_values'):
            f.update(kind='measure', agg='mv', caption='Measure Values', measure_values=False,
                     format=(plan['measure_values'][0].get('format')
                             if len({m.get('format') for m in plan['measure_values']}) == 1 else None))
        elif f['kind'] == 'measure_names':
            f.update(kind='dim', caption='Measure Names', date_part='none', discrete=True)
    # an individual measure pill on Text / Label that is one of the unpivoted measures points at the same value: the
    # Measure Values label already carries it, mark by mark
    mv_keys = {(m['caption'], m.get('agg', 'sum')) for m in plan['measure_values']}
    plan['fields'] = [f for f in plan['fields'] if not (f['channel'] in ('text', 'label') and
                                                        (f['kind'] == 'measure' or f.get('aggregate_dimension'))
                                                        and (f['caption'], f.get('agg')) in mv_keys)]
    plan['measure_values'] = []
    plan['sort'] = [s for s in plan.get('sort') or [] if s.get('field') != 'Measure Names'] + \
                   ([{'field': 'Measure Names', 'manual': order}] if order else [])
    return plan, long


def _sort_lookup(plan, engine=None, columns=None, raw=None):
    sorts = plan.get('sort') or []

    def _computed_order(f, s):
        """Tableau's computed sort ('Region by distinct count of Customer Name, descending') as the explicit member
        order, from the query result itself -- independent of which measure ends up on the axis."""
        if raw is None or not s.get('by'):
            return None
        target = next((g for g in plan['fields'] if g['kind'] == 'measure' and
                       (g['caption'] == s['by'] or g['id'] == 'sort:' + s['by'] or
                        (g.get('field_id') or '').endswith('::' + s['by']))
                       and (not s.get('by_agg') or g.get('agg') == s.get('by_agg'))), None)
        if target is None or col_name(target, True) not in raw.columns or col_name(f) not in raw.columns:
            return None
        import pandas as pd
        vals = pd.to_numeric(raw[col_name(target, True)], errors='coerce')
        frame = pd.DataFrame({'k': _labels(raw, f, engine).astype(str) if engine is not None else raw[col_name(f)].astype(str),
                              'v': vals})
        how = {'sum': 'sum', 'avg': 'mean', 'min': 'min', 'max': 'max', 'cnt': 'sum', 'ctd': 'max'}.get(
            s.get('by_agg') or 'sum', 'sum')
        agg = frame.groupby('k')['v'].agg(how).sort_values(ascending=s.get('direction') != 'desc', kind='mergesort')
        return list(agg.index)

    def sort_for(f):
        found = _authored_sort(f)
        if found is not None:
            return found
        part = f.get('date_part')           # date parts keep calendar order, like Tableau's headers
        if part == 'month':
            short = str(field_format(f, 'label.text-format') or '').lstrip('i*').upper() in ('LLL', 'MMM')
            return [m[:3] for m in MONTH_NAMES] if short else MONTH_NAMES
        if part == 'quarter':
            return ['Q1', 'Q2', 'Q3', 'Q4']
        if part == 'weekday':
            return WEEKDAYS
        return None

    def _authored_sort(f):
        for s in sorts:
            if s.get('field') in (f['caption'], (f.get('field_id') or '').split('::')[-1]):
                if s.get('manual'):
                    return [str(_label_value(v, f, engine)) if engine is not None else v for v in s['manual']]
                order = _computed_order(f, s)
                if order:
                    return order
                if s.get('by') and columns is not None:
                    target = next((g for g in plan['fields'] if g['kind'] == 'measure' and
                                   (g['caption'] == s['by'] or g['id'] == 'sort:' + s['by'] or
                                    (g.get('field_id') or '').endswith('::' + s['by']))), None)
                    if target is not None:
                        name = columns.get(target['id'])
                        if name:
                            return engine.alt.EncodingSortField(field=name, op='min',
                                                                order='descending' if s.get('direction') == 'desc'
                                                                else 'ascending')
                if s.get('by'):
                    return '-x' if s.get('direction') == 'desc' else 'x'
        return None
    return sort_for
