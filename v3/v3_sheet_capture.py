"""Per-sheet Tableau reference values captured with Tableau's Embedding API v3.

Tableau's REST view-data export returns only a dashboard's first sheet. The Embedding
API exposes every worksheet inside a loaded dashboard, and Worksheet.getSummaryDataReaderAsync()
returns the summary data Tableau itself computed for that sheet (its own filters, calculations
and default state). A capture is produced by tools/tableau_sheet_capture.js, run in a browser
session that is signed in to Tableau (today: the operator's session; planned: a Playwright
worker authenticated with a Tableau Connected App).

This module only compares. Rules (same as v3_reference):
- columns are matched by caption after removing Tableau aggregation wrappers and the app's
  aggregation suffixes, plus the sheet's own label->caption aliases from the workbook;
- rows are matched on the dimension keys; values must agree within half a unit of the
  precision Tableau displays;
- nothing is guessed: unmatched columns or keys are reported, never paired by value.
"""
import datetime
import json
import re

from v3_reference import canon, parse_number

SCHEMA = 'tableau-sheet-capture/1'
_WRAP = re.compile(r'^(AGG|SUM|AVG|MIN|MAX|CNT|CNTD|COUNT|COUNTD|ATTR|MEDIAN|USR)\((.*)\)$', re.I)
_DATE = re.compile(r'^(YEAR|QUARTER|MONTH|WEEK|DAY|MDY)\((.*)\)$', re.I)
_SUFFIX = re.compile(r'\s*\((SUM|AVG|MIN|MAX|CNT|CNTD|COUNT|USR|AGG|MEDIAN|ATTR)\)\s*$', re.I)
_PREFIX = re.compile(r'^(avg\.|average of|sum of|count of|min\.|max\.|median of)\s+', re.I)
MONTHS = {m: i for i, m in enumerate(['january', 'february', 'march', 'april', 'may', 'june', 'july',
                                       'august', 'september', 'october', 'november', 'december'], 1)}
MAX_DETAIL = 400
_DATE_FORMATS = ('%B %d, %Y', '%b %d, %Y', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d', '%m/%d/%Y')
_GLYPHS = re.compile(r'[\u25b2\u25bc\u25b3\u25bd]')
_UNIT = re.compile(r'\s+(days?|hrs?|hours?)$', re.I)
_SIGN_COL = re.compile(r'^sign\(\[(.*)\]\)$')
_ENGINE_LABEL = re.compile(r'^(?:c|cp)\.\s*')     # engine display rule: rank_card_plan strips C./CP.
TEXT_ONLY = 'The app sheet shows text/HTML only (no tabular values to compare)'


def _number(text):
    """parse_number after removing Tableau/app decorations: arrows and unit suffixes."""
    t = _UNIT.sub('', _GLYPHS.sub('', str(text or '')).strip()).strip()
    return parse_number(t)


def _as_date(text):
    t = str(text or '').strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.datetime.strptime(t, fmt).date().isoformat()
        except ValueError:
            continue
    return None


BUNDLE = 'tableau-capture-bundle/1'


def load(data):
    """One capture, or a bundle of captures (one per dashboard of a workbook, release 26). A bundle is returned
    as one capture whose 'sheets' merge every dashboard's sheets (first dashboard wins on a repeated sheet
    name) and whose 'bundle' lists the dashboard captures."""
    capture = json.loads(data.decode('utf-8') if isinstance(data, (bytes, bytearray)) else data)
    if isinstance(capture, dict) and capture.get('schema') == BUNDLE and isinstance(capture.get('captures'), dict):
        parts = [c for c in capture['captures'].values()]
        if not parts or any(not isinstance(c, dict) or c.get('schema') != SCHEMA or
                            not isinstance(c.get('sheets'), dict) for c in parts):
            raise ValueError('Not a Tableau capture bundle (expected schema %s)' % BUNDLE)
        merged = {}
        for c in parts:
            for name, entry in c['sheets'].items():
                merged.setdefault(name, entry)
        first = parts[0]
        return {'schema': SCHEMA, 'method': first.get('method'), 'view': first.get('view'),
                'dashboard': first.get('dashboard'), 'captured_at': first.get('captured_at'),
                'sheets': merged, 'bundle': parts}
    if not isinstance(capture, dict) or capture.get('schema') != SCHEMA or not isinstance(capture.get('sheets'), dict):
        raise ValueError('Not a Tableau sheet capture (expected schema %s)' % SCHEMA)
    return capture


def dashboard_captures(capture):
    """[capture per dashboard] for a single capture or a bundle."""
    return list(capture.get('bundle') or [capture])


def _norm(name):
    text = str(name or '').strip()
    for _ in range(3):
        m = _WRAP.match(text) or _DATE.match(text)
        if not m:
            break
        text = m.group(2)
    text = _PREFIX.sub('', _SUFFIX.sub('', text))
    return canon(text)


def _datepart(name):
    m = _DATE.match(str(name or '').strip())
    return m.group(1).upper() if m else None


def _aliases(sheet):
    """label -> caption pairs the workbook itself declares on the sheet's shelves."""
    out = {}

    def visit(node):
        if isinstance(node, dict):
            if node.get('caption') and node.get('label') and isinstance(node['label'], str):
                out[_norm(node['label'])] = _norm(node['caption'])
            for v in node.values():
                visit(v)
        elif isinstance(node, list):
            for v in node:
                visit(v)
    visit(sheet or {})
    return out


def _tableau_rows(entry):
    """Wide rows {normalized column: displayed text}, Measure Names/Values pivoted, duplicates removed."""
    cols = entry.get('columns') or []
    rows = [dict(zip(cols, r)) for r in entry.get('rows') or []]
    if 'Measure Names' in cols and 'Measure Values' in cols:
        keys = [c for c in cols if c not in ('Measure Names', 'Measure Values')]
        wide = {}
        for r in rows:
            k = tuple(r[c] for c in keys)
            wide.setdefault(k, {c: r[c] for c in keys})[r['Measure Names']] = r['Measure Values']
        rows, cols = list(wide.values()), keys + sorted({r['Measure Names'] for r in rows})
        dims = {_norm(c) for c in keys}
    else:                                # Tableau wraps measures (AGG(...), SUM(...)); dimensions are bare
        dims = {_norm(c) for c in cols if not _WRAP.match(str(c).strip())}
    parts = {}
    for c in cols:                       # first occurrence wins (ATTR(x) must not erase MONTH(x))
        parts.setdefault(_norm(c), _datepart(c))
    seen, out = set(), []
    for r in rows:
        nr = {}
        for c in cols:
            n = _norm(c)
            if n not in nr:              # ATTR(x) beside x: same column, keep the first
                nr[n] = r.get(c, '')
        for col in list(nr):            # Tableau's own SIGN([x]) restores a sign hidden by an arrow format
            m = _SIGN_COL.match(col)
            if m and m.group(1) in nr and str(nr[col]).strip() == '-1':
                v = str(nr[m.group(1)]).strip()
                if v and not v.startswith('-') and _number(v)[0] is not None:
                    nr[m.group(1)] = '-' + v
        sig = tuple(sorted(nr.items()))
        if sig not in seen:              # dual-axis panes repeat identical rows
            seen.add(sig)
            out.append(nr)
    return out, parts, dims


def _key_value(text, part):
    t = str(text or '').strip()
    if part == 'MONTH' and canon(t) in MONTHS:
        return str(MONTHS[canon(t)])
    d = _as_date(t)
    if d:
        return d
    v, _ = parse_number(t)
    if v is not None and float(v).is_integer():
        return str(int(v))
    return canon(t)


def _app_columns(app, t_cols, sheet):
    """Map app column names onto Tableau's: workbook label aliases, the engine's own display rule
    for calculation prefixes (C./CP.), and the rank-table change column (the sheet's second text field)."""
    alias = _aliases(sheet)
    stripped = {}
    for c in t_cols:
        stripped.setdefault(_ENGINE_LABEL.sub('', c), c)
    texts = [_norm(t) for t in ((sheet or {}).get('text_fields') or [])]
    out = []
    for r in app:
        nr = {}
        for k, v in r.items():
            n = alias.get(_norm(k), _norm(k))
            if n not in t_cols:
                if n in stripped:
                    n = stripped[n]
                elif n in ('\u03b4%', 'δ%', 'chg') and len(texts) >= 2 and texts[1] in t_cols:
                    n = texts[1]
            nr[n] = v
        out.append(nr)
    return out


def _kpi_compare(t_row, app_rows):
    """KPI tiles: the app lists (caption, value) pairs; Tableau returns one row of measures."""
    app = {}
    for r in app_rows:
        name = _norm(r.get('KPI'))
        app.setdefault(name, r.get('Value'))
        app.setdefault(_ENGINE_LABEL.sub('', name), r.get('Value'))
    detail, matched, mismatched = [], 0, 0
    for col, text in t_row.items():
        if col not in app:
            continue
        tv, tol = _number(text)
        av, _ = _number(app[col])
        if tv is None or av is None:
            continue
        ok = abs(tv - av) <= tol + 1e-9 * max(1.0, abs(tv))
        matched += ok
        mismatched += not ok
        detail.append({'Key': 'KPI', 'Measure': col, 'Tableau': text, 'App': app[col], 'Match': ok})
    status = 'Match' if matched and not mismatched else 'Difference found' if mismatched else 'Not comparable'
    return {'Status': status, 'Matched values': matched, 'Mismatched values': mismatched, 'Rows missing in app': 0,
            'Note': f'KPI tile: {matched + mismatched} value(s) compared'}, detail


def compare_sheet(entry, app_rows, sheet=None):
    """(summary dict, detail rows) for one captured sheet against the app's displayed rows."""
    if entry.get('error'):
        return {'Status': 'Tableau returned no data', 'Note': entry['error'][:200]}, []
    t_rows, parts, dims = _tableau_rows(entry)
    if not t_rows:
        return {'Status': 'Tableau returned no data', 'Note': 'Empty summary data'}, []
    if not app_rows:
        return {'Status': 'Not comparable', 'Note': TEXT_ONLY}, []
    if set(app_rows[0]) == {'KPI', 'Value'} and len(t_rows) == 1:
        return _kpi_compare(t_rows[0], app_rows)
    app = _app_columns(app_rows, list(t_rows[0]), sheet)
    shared = [c for c in t_rows[0] if c in app[0]]
    keys = [c for c in shared if c in dims]
    measures = [c for c in shared if c not in keys]
    only_tableau = [c for c in t_rows[0] if c not in app[0]]
    if not keys or not measures:
        return {'Status': 'Not comparable',
                'Note': 'No shared key/measure columns. Tableau: ' + ', '.join(t_rows[0]) +
                        '; app: ' + ', '.join(app[0])}, []
    index = {}
    for r in app:
        index.setdefault(tuple(_key_value(r.get(k), parts.get(k)) for k in keys), r)
    detail, matched, mismatched, missing, totals = [], 0, 0, 0, 0
    for r in t_rows:
        if all(str(r.get(c, '')).strip() in ('Null', '') for c in keys):
            totals += 1                  # Tableau grand-total / null-key row, not a data row
            continue
        k = tuple(_key_value(r.get(c), parts.get(c)) for c in keys)
        a = index.get(k)
        label = ' / '.join(str(r.get(c)) for c in keys)
        if a is None:
            if all(_number(r.get(m))[0] is None for m in measures):
                continue                 # Tableau row with no values (e.g. Measure Names pane with Null)
            missing += 1
            detail.append({'Key': label, 'Measure': '—', 'Tableau': '', 'App': 'row not in app', 'Match': False})
            continue
        for m in measures:
            tv, tol = _number(r.get(m))
            av, _ = _number(a.get(m))
            if tv is None or av is None:
                continue
            ok = abs(tv - av) <= tol + 1e-9 * max(1.0, abs(tv))
            matched += ok
            mismatched += not ok
            detail.append({'Key': label, 'Measure': m, 'Tableau': r.get(m), 'App': a.get(m), 'Match': ok})
    status = ('Match' if matched and not mismatched and not missing else
              'Difference found' if mismatched or missing else 'Not comparable')
    note = f"{len(t_rows) - totals} Tableau rows on {', '.join(keys)}; {len(measures)} measure(s)"
    if totals:
        note += f'; {totals} Tableau total row(s) not compared'
    if entry.get('note'):
        note += '; ' + entry['note']
    if only_tableau:
        note += '; not in app capture: ' + ', '.join(only_tableau)
    return {'Status': status, 'Matched values': matched, 'Mismatched values': mismatched,
            'Rows missing in app': missing, 'Note': note}, detail[:MAX_DETAIL]


def compare_capture(capture, ir, sheet_values):
    """{sheet name: (summary, detail)} for every captured sheet that exists in the migrated dashboard."""
    if capture.get('bundle'):
        results = {}
        for part in capture['bundle']:
            for name, value in _compare_one(part, ir, sheet_values).items():
                if name not in results or results[name][0].get('Status') == 'Not comparable':
                    results[name] = value
        return results
    return _compare_one(capture, ir, sheet_values)


def _compare_one(capture, ir, sheet_values):
    dash = next((d for d in ir.get('dashboards', [])
                 if canon(d.get('name')) == canon(capture.get('dashboard'))
                 or canon(d.get('title')) == canon(capture.get('dashboard'))), None)
    results = {}
    for name, entry in capture['sheets'].items():
        sheet = next((s for s in (dash or {}).get('sheets', []) if s.get('name') == name), None)
        if dash is None or sheet is None:
            results[name] = ({'Status': 'Not comparable', 'Note': 'Sheet not in the migrated dashboard'}, [])
            continue
        values = (sheet_values or {}).get((dash.get('name'), name)) or {}
        if entry.get('error') and '[0..0)' in entry['error'] and values and not values.get('total'):
            results[name] = ({'Status': 'Match (empty in both)', 'Matched values': 0, 'Mismatched values': 0,
                              'Rows missing in app': 0, 'Note': 'Tableau and the app both return no rows'}, [])
            continue
        results[name] = compare_sheet(entry, values.get('all') or values.get('rows'), sheet)
    return results
