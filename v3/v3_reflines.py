"""EN15: Tableau reference lines, bands and distributions for the sheet plan (v3_sheet_plan).

parse()  reads the worksheet's <reference-line> elements (formula, scope, value field / parameter / constant, label,
         percentage distribution, quantiles, fills).
bind()   ties each line to the plan's columns: the axis it is drawn on and the field it is computed from; a value
         field that is not on the view joins the query as a 'refline' field (computed, not drawn).
values() computes the lines over the view's marks, per Tableau's scope: per-table = all marks, per-pane = marks sharing
         every discrete header but the innermost on Rows and on Columns, per-cell = marks sharing every discrete header.
layers() draws them on the Altair chart: a rule across the pane, a tick on each band for per-cell lines on a discrete
         axis, a shaded bar for a distribution band filled below.
"""
import re

FORMULAS = ('average', 'median', 'min', 'max', 'sum', 'total', 'constant', 'quantiles')
SCOPES = ('per-table', 'per-pane', 'per-cell')
_TOKEN = re.compile(r'^\[([^\]]+)\]\.\[(?:(?P<pre>[a-z]+):)?(?:(?P<agg>[a-z]+):)?(?P<name>[^:\]]+)(?::[a-z]+)*(?::\d+)?\]$')
LINE_COLOR = '#333333'
# every <reference-line> attribute the corpus uses (58 lines in 35 workbooks); anything else -- a box plot, a newer
# analytics object -- is reported and left undrawn, never silently approximated
KNOWN_ATTRS = {'axis-column', 'enable-instant-analytics', 'formula', 'id', 'label-type', 'scope', 'value-column',
               'z-order', 'probability', 'label', 'percentage', 'fill-above', 'fill-below', 'symmetric',
               'percentage-bands', 'tooltip-type', 'tile-count', 'tooltip', 'value'}
_BOX_TAG = re.compile(r'<([\w:-]*box[\w:-]*)\b', re.I)
BAND_COLOR = '#c8c8c8'


def _attrs(text):
    return {k: v.replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"').replace('&amp;', '&')
            for k, v in re.findall(r"([\w:-]+)=['\"]([^'\"]*)['\"]", text)}


def _field(token):
    """A pill token -> {'ds', 'agg', 'name', 'tc'} or {'param': name}."""
    token = (token or '').strip()
    if token.startswith('[Parameters].'):
        return {'param': token.split('].[', 1)[1].rstrip(']')}
    parts = token[1:-1].split('].[')
    if len(parts) < 2:
        return None
    ds, pill = parts[0], parts[-1]
    bits = pill.split(':')
    if len(bits) >= 3 and bits[0] in ('sum', 'avg', 'min', 'max', 'cnt', 'ctd', 'med', 'usr', 'none', 'attr'):
        return {'ds': ds, 'agg': bits[0], 'name': bits[1], 'tc': None}
    if len(bits) >= 4 and bits[1] in ('sum', 'avg', 'min', 'max', 'cnt', 'ctd', 'med', 'usr'):
        return {'ds': ds, 'agg': bits[1], 'name': bits[2], 'tc': bits[0]}     # a table calculation on the pill
    return {'ds': ds, 'agg': 'none', 'name': bits[0] if len(bits) < 3 else bits[1], 'tc': None}


def _line_styles(xml):
    """V11: <style-rule element='refline'> formats: sheet-wide (no id) and per line (id), as {id or '': {attr: v}}."""
    out = {}
    for m in re.finditer(r"<style-rule element=['\"]refline['\"]>(.*?)</style-rule>", xml or '', re.S):
        for f in re.findall(r'<format\b([^>]*)/?>', m.group(1)):
            a = _attrs(f)
            if a.get('attr'):
                out.setdefault(a.get('id', ''), {})[a['attr']] = a.get('value')
    return out


def _line(styles, rid):
    """{'visible', 'width'?, 'color'?} for one line: its own formats over the sheet-wide ones."""
    merged = dict(styles.get('', {}), **styles.get(rid or '', {}))
    out = {'visible': not (merged.get('line-visibility') == 'off' or
                           (str(merged.get('stroke-size')) == '0' and merged.get('line-visibility') != 'on'))}
    if merged.get('stroke-size') not in (None, '', '0'):
        try:
            out['width'] = float(merged['stroke-size'])
        except ValueError:
            pass
    if merged.get('stroke-color'):
        out['color'] = merged['stroke-color'][:7]
    return out


def parse(xml):
    """Reference lines of a worksheet XML, in authored order."""
    out = []
    styles = _line_styles(xml)
    for m in re.finditer(r'<reference-line\b([^>]*?)(/>|>(.*?)</reference-line>)', xml or '', re.S):
        a = _attrs(m.group(1))
        body = m.group(3) or ''
        formula = (a.get('formula') or ('constant' if a.get('value') else 'average')).lower()
        spec = {'id': a.get('id'), 'formula': formula, 'scope': a.get('scope') or 'per-table',
                'axis': _field(a.get('axis-column')), 'value': _field(a.get('value-column')) if a.get('value-column')
                else None, 'label_type': a.get('label-type') or 'automatic', 'label': a.get('label'),
                'percentages': [float(p) for p in re.findall(r"<reference-line-value [^>]*percentage=['\"]([\d.]+)",
                                                              body)],
                'fill_below': a.get('fill-below') == 'true', 'fill_above': a.get('fill-above') == 'true',
                'tiles': int(a['tile-count']) if str(a.get('tile-count', '')).isdigit() else None,
                'line': _line(styles, a.get('id'))}
        if formula == 'constant':
            try:
                spec['constant'] = float(a.get('value'))
            except (TypeError, ValueError):
                spec['constant'] = None
        odd = sorted(k for k in a if k not in KNOWN_ATTRS)
        if formula not in FORMULAS or any('box' in k or 'whisker' in k for k in odd):
            kind = 'box plot' if any('box' in k or 'whisker' in k for k in odd) or 'box' in formula \
                else f'reference line formula {formula!r}'
            spec['unsupported'] = f'{kind} is not supported yet: not drawn'
        out.append(spec)
    for tag in sorted(set(_BOX_TAG.findall(xml or ''))):
        out.append({'id': tag, 'formula': 'box', 'scope': 'per-pane', 'axis': None, 'value': None,
                    'label_type': 'none', 'label': None, 'percentages': [], 'fill_below': False,
                    'fill_above': False, 'tiles': None, 'line': {'visible': False},
                    'unsupported': f'box plot (<{tag}>) is not supported yet: not drawn'})
    return out


def box_stats(values, whiskers='iqr'):
    """A5: one box of a Tableau box plot over the values it summarises (Tableau builds it over the disaggregated
    marks). Quartiles by R type 7 -- the method Tableau documents for its distributions and percentiles -- i.e.
    linear interpolation. whiskers 'iqr' = 'Data within 1.5 times the IQR' (to the furthest value inside the
    fences), 'extent' = 'Maximum extent of the data'. Values outside the whiskers are the outliers.
    {'q1', 'median', 'q3', 'lower', 'upper', 'outliers'}; None for no numeric values."""
    xs = sorted(float(v) for v in values if v is not None and v == v)
    if not xs:
        return None

    def q(p):
        h = (len(xs) - 1) * p
        lo = int(h)
        hi = min(lo + 1, len(xs) - 1)
        return xs[lo] + (h - lo) * (xs[hi] - xs[lo])
    q1, med, q3 = q(0.25), q(0.5), q(0.75)
    if whiskers == 'extent':
        lower, upper = xs[0], xs[-1]
    else:
        iqr = q3 - q1
        lo_f, hi_f = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        lower = min(x for x in xs if x >= lo_f)
        upper = max(x for x in xs if x <= hi_f)
    return {'q1': q1, 'median': med, 'q3': q3, 'lower': lower, 'upper': upper,
            'outliers': [x for x in xs if x < lower or x > upper]}


def bind(plan, by_token=None, captions=None, primary=None):
    """Ties every line to result columns: spec['axis_col'] (drawn on) and spec['value_col'] (computed from). A value
    field off the view joins the query as a 'refline' field when it belongs to the primary datasource; otherwise the
    line is marked unbound (spec['unbound'] = reason) and not drawn."""
    import v3_sheet_plan as P
    by_token = by_token or {}
    captions = captions or {}

    def find(ref):
        if not ref or ref.get('param'):
            return None
        tok = next((t for t in by_token if t and t.endswith(f"{ref['agg']}:{ref['name']}:qk]")
                    and (ref.get('tc') is None) == (by_token[t].get('table_calc') is None)), None)
        if tok:
            return by_token[tok]
        cap = captions.get(ref['name'], ref['name'])
        pool = list(plan['fields']) + list(plan.get('measure_values') or [])   # a measure inside Measure Values too
        return next((f for f in pool if f.get('kind', 'measure') == 'measure' and f['caption'] in (cap, ref['name'])
                     and f.get('agg') == ref['agg'] and bool(f.get('table_calc')) == bool(ref.get('tc'))), None)

    def col(f):
        return P.col_name(f, True) + ('|tc' if f.get('table_calc') else '')
    mv_axis = next((f for f in plan['fields'] if f.get('measure_values') and f.get('channel') in ('rows', 'cols')), None)
    text_only = not any(f.get('channel') in ('rows', 'cols') and (f.get('kind') == 'measure' or not f.get('discrete'))
                        and not f.get('measure_values') for f in plan['fields']) and mv_axis is None

    def date_axis(ref):
        """A continuous date on Rows / Columns (a line at the latest / earliest / average date)."""
        if not ref or ref.get('agg') not in ('none', None):
            return None
        cap = captions.get(ref['name'], ref['name'])
        return next((f for f in plan['fields'] if f.get('channel') in ('rows', 'cols') and f.get('kind') == 'dim'
                     and not f.get('discrete') and f['caption'] in (cap, ref['name'])), None)
    for spec in plan.get('reflines') or []:
        if spec.get('unsupported'):
            spec['unbound'] = spec['unsupported']
            continue
        if (spec['axis'] or {}).get('name') == 'Multiple Values':
            if mv_axis is None:
                spec['unbound'] = 'Measure Values are not on an axis (text table: Tableau draws no line)'
                continue
            spec['axis_col'], spec['axis_caption'] = 'M|mv|Measure Values', 'Measure Values'  # the unpivoted axis
            spec['format'] = None
            if (spec.get('value') or {}).get('name') == 'Multiple Values':
                spec['value'] = None                        # computed over the measure values themselves
            axis = mv_axis
        else:
            axis = find(spec['axis'])
        if axis is None and date_axis(spec['axis']) is not None:
            d = date_axis(spec['axis'])
            spec.update(axis_col=P.col_name(d), axis_caption=d['caption'], format=None, temporal=True, value=None)
            continue
        if axis is None:
            spec['unbound'] = 'text table: Tableau draws no line' if text_only else 'axis field not on the view'
            continue
        if axis is not mv_axis:
            spec['axis_col'] = col(axis)
            spec['axis_caption'] = axis['caption']
            spec['format'] = axis.get('format')
        val = spec.get('value')
        if val and val.get('param'):
            val['param'] = captions.get(val['param'], val['param'])    # [Parameters].[Parameter 2] -> its caption
        if not val or val.get('param') or spec['formula'] == 'constant':
            continue
        f = find(val)
        if f is None:
            if val.get('tc') or (primary and val.get('ds') not in (primary, None)):
                spec['unbound'] = 'value field is not on the view'
                continue
            f = {'id': 'refline:' + spec['id'], 'caption': captions.get(val['name'], val['name']), 'channel': 'refline',
                 'kind': 'measure', 'agg': val['agg'], 'discrete': False, 'date_part': 'none'}
            if not any(x.get('channel') == 'refline' and x['caption'] == f['caption'] and x['agg'] == f['agg']
                       for x in plan['fields']):
                plan['fields'].append(f)
        spec['value_col'] = col(f)
    return plan


def _scope_keys(plan, scope):
    rows = [P_col(f) for f in plan['fields'] if f.get('channel') == 'rows' and f.get('kind') == 'dim'
            and f.get('discrete') and not f.get('aggregate_dimension')]
    cols = [P_col(f) for f in plan['fields'] if f.get('channel') == 'cols' and f.get('kind') == 'dim'
            and f.get('discrete') and not f.get('aggregate_dimension')]
    if scope == 'per-cell':
        return rows + cols
    if scope == 'per-pane':
        return rows[:-1] + cols[:-1]
    return []


def P_col(f):
    import v3_sheet_plan as P
    return P.col_name(f)


def values(plan, df, params=None):
    """[{'spec', 'axis', 'lines': [per-row values for each line], 'fill'}] for every bound line (in authored order)."""
    import pandas as pd
    params = params or {}
    out = []
    for spec in plan.get('reflines') or []:
        if spec.get('unbound') or spec.get('axis_col') not in df.columns:
            continue
        n = len(df)
        val = spec.get('value') or {}
        if spec['formula'] == 'constant' or val.get('param'):
            raw = spec.get('constant') if spec['formula'] == 'constant' else params.get(val['param'])
            try:
                base = [pd.Series([float(str(raw).strip('"'))] * n, index=df.index)]
            except (TypeError, ValueError):
                continue
        elif spec.get('temporal'):
            series = pd.to_datetime(df[spec['axis_col']], errors='coerce')
            keys = [k for k in _scope_keys(plan, spec['scope']) if k in df.columns]
            fn = {'average': 'mean', 'median': 'median', 'min': 'min', 'max': 'max'}.get(spec['formula'])
            if fn is None or series.isna().all():
                continue
            v = (series.groupby([df[k].astype(str) for k in keys]).transform(fn) if keys
                 else pd.Series([getattr(series, fn)()] * n, index=df.index))
            out.append({'spec': spec, 'axis': spec['axis_col'], 'fill': None, 'temporal': True,
                        'lines': [[None if x is pd.NaT or x != x else x.isoformat() for x in v.tolist()]]})
            continue
        else:
            vc = spec.get('value_col') or spec['axis_col']
            if vc not in df.columns:
                continue
            series = pd.to_numeric(df[vc], errors='coerce')
            keys = [k for k in _scope_keys(plan, spec['scope']) if k in df.columns]
            grouped = series.groupby([df[k].astype(str) for k in keys]) if keys else None
            if spec['formula'] == 'quantiles':
                tiles = spec.get('tiles') or 4
                qs = [k / tiles for k in range(1, tiles)]
                base = [(grouped.transform(lambda s, q=q: s.quantile(q)) if grouped is not None
                         else pd.Series([series.quantile(q)] * n, index=df.index)) for q in qs]
            else:
                fn = {'average': 'mean', 'median': 'median', 'min': 'min', 'max': 'max', 'sum': 'sum',
                      'total': 'sum'}.get(spec['formula'])
                if fn is None:
                    continue
                base = [grouped.transform(fn) if grouped is not None
                        else pd.Series([getattr(series, fn)()] * n, index=df.index)]
        if spec['percentages']:
            base = [b * (p / 100.0) for b in base[:1] for p in spec['percentages']]
        fill = ('both' if spec['fill_below'] and spec['fill_above'] else 'below' if spec['fill_below']
                else 'above' if spec['fill_above'] else None)
        out.append({'spec': spec, 'axis': spec['axis_col'], 'fill': fill,
                    'lines': [[None if v != v else float(v) for v in b.tolist()] for b in base]})
    return out


def label_text(spec, value, fmt):
    """The text Tableau prints next to the line."""
    import v3_sheet_plan as P
    kind = spec.get('label_type') or 'automatic'
    if kind == 'none' or value is None:
        return ''
    if isinstance(value, str):                     # a date line: Tableau prints the date
        shown = value[:10]
        return (spec.get('label') or '<Value>').replace('<Value>', shown) if kind == 'custom' else shown
    shown = P.format_value(value, fmt) if fmt else (f'{value:,.0f}' if abs(value) >= 1000 else f'{value:g}')
    if kind == 'custom':
        return (spec.get('label') or '<Value>').replace('<Value>', shown).replace(
            '<FieldName>', spec.get('axis_caption') or '').replace('<Computation>', spec.get('formula', '').title())
    return shown


def layers(alt, plan, df, frame, safe_col, x, y, safe, chart, params=None):
    """The chart with its reference lines layered on (fills behind the marks, lines in front)."""
    lines = values(plan, df, params)
    if not lines:
        return chart
    behind, front = [], []
    for k, item in enumerate(lines):
        spec = item['spec']
        ch = 'x' if x is not None and safe.get(x['id']) == safe_col(item['axis']) else \
            'y' if y is not None and safe.get(y['id']) == safe_col(item['axis']) else None
        if ch is None:
            continue
        other = y if ch == 'x' else x
        band = other if (other is not None and other['kind'] != 'measure' and other.get('discrete')
                         and spec['scope'] == 'per-cell') else None
        enc_v = alt.X if ch == 'x' else alt.Y
        enc_o = alt.Y if ch == 'x' else alt.X
        for j, vals in enumerate(item['lines']):
            name = f'__ref{k}_{j}'
            frame[name] = vals
            frame[name + '_lbl'] = [label_text(spec, v, spec.get('format')) for v in vals]
            keys = [safe_col(c) for c in _scope_keys(plan, spec['scope']) if safe_col(c) in frame.columns]
            # one label per line: the first mark of each scope carries it (the chart may add a row encoding later)
            frame[name + '_first'] = (~frame.duplicated(subset=keys)).astype(int) if keys else \
                [1] + [0] * (len(frame) - 1)
            agg = enc_v(f'min({name}):T') if item.get('temporal') else enc_v(f'mean({name}):Q')
            if band is not None:
                key = {ch: agg, ('y' if ch == 'x' else 'x'): enc_o(safe[band['id']], type='nominal')}
                if item['fill'] in ('below', 'both'):
                    behind.append(alt.Chart(frame).mark_bar(color=BAND_COLOR, opacity=0.6).encode(**key))
                line = spec.get('line') or {'visible': True}
                if line.get('visible', True):
                    front.append(alt.Chart(frame).mark_tick(color=line.get('color', '#000000'),
                                                            thickness=line.get('width', 2)).encode(**key))
            else:
                line = spec.get('line') or {'visible': True}
                if line.get('visible', True):
                    front.append(alt.Chart(frame).mark_rule(color=line.get('color', LINE_COLOR),
                                                            strokeWidth=line.get('width', 1)).encode(**{ch: agg}))
                if spec.get('label_type') != 'none':
                    front.append(alt.Chart(frame).transform_filter(f'datum.{name}_first == 1').mark_text(
                        align='left', baseline='bottom', dx=3, dy=-2, fontSize=10, color=LINE_COLOR).encode(
                        **{ch: agg, 'text': alt.Text(f'min({name}_lbl):N')}))
    for layer in behind:
        chart = layer + chart
    for layer in front:
        chart = chart + layer
    return chart


def validate(reflines):
    p = []
    for i, r in enumerate(reflines or []):
        if isinstance(r, dict) and r.get('unsupported'):
            continue                                   # reported and not drawn; the rest of the sheet still draws
        if not isinstance(r, dict) or r.get('formula') not in FORMULAS or r.get('scope') not in SCOPES:
            p.append(f'reflines[{i}] formula / scope not in the closed list')
    return p
