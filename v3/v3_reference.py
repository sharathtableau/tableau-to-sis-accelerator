"""Live Tableau reference comparison for the Validation page.

Tableau's REST API returns the data behind each published view (Tableau computes it,
so it is independent of this app). For each view we:
  1. find the dashboard (or sheet) the view publishes, by Tableau's own view name;
  2. find the single sheet whose rendered columns map to exactly the view's column
     captions (via validation_adapter.resolve_chart_columns) - none or several means
     the view is reported as not comparable, never guessed;
  3. compare every row on the dimension keys, allowing only the rounding implied by
     the number of decimals Tableau displays.
Both sides use the published default state (default parameters, no selections).
"""
import csv
import io
import math
import re

_NUM = re.compile(r'^\s*\(?[-+]?[$€£¥]?\s*[-+]?[\d,]*\.?\d+\s*%?\)?\s*$')


def canon(text):
    return re.sub(r'\s+', ' ', str(text or '')).strip().casefold()


def parse_number(text):
    """(value, tolerance) from a Tableau-formatted cell, or (None, None)."""
    raw = str(text or '').strip()
    if not raw or not _NUM.match(raw):
        return None, None
    negative = raw.startswith('(') and raw.endswith(')') or raw.lstrip('($€£¥ ').startswith('-')
    pct = raw.rstrip(')').endswith('%')
    digits = re.sub(r'[^\d.]', '', raw)
    if not digits or digits == '.':
        return None, None
    decimals = len(digits.split('.', 1)[1]) if '.' in digits else 0
    value = float(digits) * (-1 if negative else 1)
    tolerance = 0.5 * 10 ** (-decimals)
    if pct:
        value, tolerance = value / 100, tolerance / 100
    return value, tolerance


def read_view_csv(text):
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return [], []
    header, body = rows[0], [r for r in rows[1:] if any(c.strip() for c in r)]
    return header, [dict(zip(header, r)) for r in body]


def _sheets_for_view(ir, view_name):
    name = canon(view_name)
    for dash in ir.get('dashboards', []):
        if canon(dash.get('name')) == name or canon(dash.get('title')) == name:
            return dash, list(dash.get('sheets', []))
    for dash in ir.get('dashboards', []):
        for sheet in dash.get('sheets', []):
            if canon(sheet.get('name')) == name:
                return dash, [sheet]
    return None, []


_DATEPART_PREFIX = re.compile(r'^(day|week|month|quarter|year) of ', re.I)
_DATE_FORMATS = ('%B %d, %Y', '%b %d, %Y', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d', '%m/%d/%Y')


def col_canon(name):
    """Column identity: Tableau CSV headers say 'Day of Event Date' where the app says 'Event Date'."""
    return canon(_DATEPART_PREFIX.sub('', str(name or '').strip()))


def _key_canon(value):
    import datetime
    t = str(value if value is not None else '').strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.datetime.strptime(t, fmt).date().isoformat()
        except ValueError:
            continue
    return canon(t)


def compare_rows(header, tableau_rows, app_rows, keys):
    """Row-level comparison on the given dimension (key) columns; every other column is a measure."""
    keys = [c for c in header if col_canon(c) in {col_canon(k) for k in keys}]
    numeric = [c for c in header if c not in keys]
    def key_of(row):
        return tuple(_key_canon(row.get(k)) for k in keys)
    app_by_key = {}
    for row in app_rows:
        app_by_key.setdefault(key_of(row), []).append(row)
    details, matched, mismatched, missing = [], 0, 0, 0
    seen = set()
    for row in tableau_rows:
        k = key_of(row)
        candidates = app_by_key.get(k, [])
        if len(candidates) != 1:
            missing += 1
            details.append({'Key': ' / '.join(row.get(c, '') for c in keys) or '(total)', 'Measure': '—',
                            'Tableau': '—', 'App': 'missing' if not candidates else 'duplicate key', 'Match': False})
            continue
        seen.add(k)
        app = candidates[0]
        for col in numeric:
            tab_value, tol = parse_number(row.get(col))
            app_value = app.get(col)
            try:
                app_value = float(app_value)
            except (TypeError, ValueError):
                app_value = None
            ok = (tab_value is None and app_value is None) or (
                tab_value is not None and app_value is not None and not math.isnan(app_value)
                and abs(app_value - tab_value) <= tol + 1e-9 * max(1.0, abs(tab_value)))
            matched += ok
            mismatched += not ok
            details.append({'Key': ' / '.join(row.get(c, '') for c in keys) or '(total)', 'Measure': col,
                            'Tableau': row.get(col, ''), 'App': '—' if app_value is None else f'{app_value:,.4f}'.rstrip('0').rstrip('.'),
                            'Match': ok})
    extra = sum(1 for k in app_by_key if k not in seen)
    return {'keys': keys, 'measures': numeric, 'matched': matched, 'mismatched': mismatched,
            'missing_in_app': missing, 'extra_in_app': extra, 'details': details}


def compare_view(ir, view, capture):
    """Compare one pulled view. `capture(sheet, dashboard) -> chart or None` renders a sheet."""
    import validation_adapter as V
    record = {'View': view.get('view'), 'Sheet': '—', 'Status': '', 'Matched values': 0,
              'Mismatched values': 0, 'Rows missing in app': 0, 'Note': ''}
    if view.get('error') or not view.get('csv'):
        reason = view.get('error') or 'Tableau returned no data'
        if 'DataSourcesNotConnected' in reason or '400081' in reason:
            reason = 'Tableau cannot query this view: its data source is not connected in Tableau Cloud (400081)'
        return dict(record, Status='Tableau reference unavailable', Note=reason[:300]), []
    header, rows = read_view_csv(view['csv'])
    wanted = {col_canon(c) for c in header}
    dash, sheets = _sheets_for_view(ir, view.get('view'))
    candidates = []
    for sheet in sheets:
        chart = capture(sheet, dash)
        if chart is None:
            continue
        try:
            dims, _measures, rename = V.resolve_chart_columns(sheet, chart)
        except Exception:
            continue
        if isinstance(rename, dict) and rename and {col_canon(c) for c in rename.values()} == wanted:
            candidates.append((sheet, chart, rename, dims))
    if len(candidates) != 1:
        note = ('no sheet produces exactly these columns: ' if not candidates else
                f'{len(candidates)} sheets produce these columns; not guessing: ') + ', '.join(header)
        return dict(record, Status='Not comparable', Note=note[:300]), []
    sheet, chart, rename, dims = candidates[0]
    app_rows = V.streamlit_rows(chart, rename) or []
    by_caption = {col_canon(c): c for c in header}
    app_rows = [{by_caption.get(col_canon(k), k): v for k, v in row.items()} for row in app_rows]
    result = compare_rows(header, rows, app_rows, dims)
    status = ('Match' if result['matched'] and not result['mismatched'] and not result['missing_in_app']
              else 'Difference found')
    note = f"{len(rows)} Tableau rows; keys: {', '.join(result['keys']) or 'none'}"
    if result['extra_in_app']:
        note += f"; {result['extra_in_app']} extra row(s) in app"
    return dict(record, Sheet=sheet.get('name'), Status=status, **{
        'Matched values': result['matched'], 'Mismatched values': result['mismatched'],
        'Rows missing in app': result['missing_in_app'], 'Note': note}), result['details']


def headless_capture(sheet, dashboard):
    """Render one sheet without drawing it (original headless capture), native presentation."""
    import headless_render as H
    cap = H._capture_all(dict(sheet, _presentation='native'), None, (dashboard or {}).get('name'))
    if cap.get('error') or not cap.get('altair'):
        return None
    return H._arrange_altair(cap['altair'], cap.get('altair_cols'))
