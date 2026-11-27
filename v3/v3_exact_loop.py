"""Release 26: prove a sheet plan against Tableau's own values (the exact loop).

For one sheet: candidate plans (the authored-shelf plan from v3_sheet_plan, its code variants for table-calc
addressing, and Cortex proposals) are executed read-only and compared with the summary data Tableau computed
for that sheet (a tableau-sheet-capture/1 entry). The first candidate whose rows equal Tableau's is
"Verified". Otherwise the sheet keeps its best candidate with the exact difference ("Not matched"), and the
difference is what a Cortex repair is asked to explain. Model output never changes a verdict: only this
comparison does.

Comparison rules (no guessing):
- Tableau's dimension columns map to plan dimensions by caption and date part (YEAR(Order Date) = Order Date
  at year); measure columns map by aggregation and caption (SUM(Sales), AGG(Profit Ratio), CNTD(Customer)).
- Measure Names / Measure Values long rows are pivoted to one column per measure.
- Rows match on all dimension keys; numbers must agree within half a unit of the precision Tableau displays;
  text values (aggregate dimensions such as "Above Target") must be equal.
- Verified needs: every Tableau column accounted for, the same number of rows, no missing, extra or different
  value. A Tableau column the plan does not produce is reported, never ignored.
"""
import itertools
import re

import v3_sheet_plan as P
from v3_sheet_capture import _number as _plain_number

_UNIT = re.compile(r'^(.*?\d)\s*([KMB])(\)?)$')
_SCALE = {'K': 1e3, 'M': 1e6, 'B': 1e9}


def _number(text):
    """Tableau display number -> (value, tolerance), including unit suffixes ($500K, 1.2M, 3B)."""
    v, tol = _plain_number(text)
    if v is not None:
        return v, tol
    m = _UNIT.match(str(text or '').strip())
    if not m:
        return None, None
    v, tol = _plain_number(m.group(1) + (m.group(3) or ''))
    if v is None:
        return None, None
    s = _SCALE[m.group(2)]
    return v * s, tol * s

AGG_TOKENS = {'sum': 'SUM', 'avg': 'AVG', 'min': 'MIN', 'max': 'MAX', 'cnt': 'CNT', 'ctd': 'CNTD',
              'med': 'MEDIAN', 'usr': 'AGG', 'attr': 'ATTR', 'stdev': 'STDEV', 'var': 'VAR', 'none': ''}
PART_TOKENS = {'year': 'YEAR', 'quarter': 'QUARTER', 'month': 'MONTH', 'week': 'WEEK', 'day': 'DAY',
               'weekday': 'WEEKDAY'}
_WRAPPED = re.compile(r'^([A-Z]+)\((.*)\)$', re.S)
GENERATED = re.compile(r'^(Latitude|Longitude|Geometry) \(generated\)$')
MV_PREFIX = re.compile(r'^(avg\.|average of|sum of|count of|count distinct of|min\.|max\.|median of|'
                       r'distinct count of|cntd\.|cnt\.)\s+', re.I)
MAX_DETAIL = 60
REST_PART = re.compile(r'^(year|quarter|month|week|day|weekday) of (.+)$', re.I)


def _canon(text):
    return re.sub(r'\s+', ' ', str(text or '')).strip().casefold()


def _dim_header(f):
    if f.get('header'):
        return f['header']
    part = f['date_part'].replace('_trunc', '')
    token = PART_TOKENS.get(part)
    return f'{token}({f["caption"]})' if token else f['caption']


def _measure_headers(f):
    """Header names Tableau may use for a plan measure (table calcs keep the base measure's header)."""
    token = AGG_TOKENS.get(f['agg'], '')
    out = [f'{token}({f["caption"]})' if token else f['caption']]
    if f['agg'] in ('usr', 'none'):
        out += [f'AGG({f["caption"]})', f['caption']]
    return out


MV_AGG = {'avg.': 'avg', 'average of': 'avg', 'sum of': 'sum', 'count of': 'cnt', 'count distinct of': 'ctd',
          'distinct count of': 'ctd', 'cntd.': 'ctd', 'cnt.': 'cnt', 'min.': 'min', 'max.': 'max',
          'median of': 'med'}


def _mv_key(name):
    return _canon(MV_PREFIX.sub('', str(name or '').strip()))


def _mv_agg(name):
    m = MV_PREFIX.match(str(name or '').strip())
    return MV_AGG.get(m.group(1).lower()) if m else None


# --------------------------------------------------------------------------- Tableau side
def tableau_table(entry):
    """(columns, rows) from a capture entry with Measure Names/Values pivoted to 'MV:<name>' columns and
    duplicate rows (dual axis) removed. Duplicate column names are kept with a '#n' suffix."""
    cols = list(entry.get('columns') or [])
    named, counts = [], {}
    for c in cols:
        counts[c] = counts.get(c, 0) + 1
        named.append(c if counts[c] == 1 else f'{c}#{counts[c]}')
    rows = [dict(zip(named, r)) for r in entry.get('rows') or []]
    if 'Measure Names' in named and 'Measure Values' in named:
        keep = [c for c in named if c not in ('Measure Names', 'Measure Values')]
        wide = {}
        for r in rows:
            k = tuple(r[c] for c in keep)
            row = wide.setdefault(k, {c: r[c] for c in keep})
            row['MV:' + str(r['Measure Names'])] = r['Measure Values']
        rows = list(wide.values())
        named = keep + sorted({c for r in rows for c in r if c.startswith('MV:')})
    seen, out = set(), []
    for r in rows:
        sig = tuple(sorted(r.items()))
        if sig not in seen:
            seen.add(sig)
            out.append(r)
    return named, out


# --------------------------------------------------------------------------- mapping columns
def map_columns(plan, t_cols):
    """{tableau column: ('dim', field) | ('measure', field, variant) | ('mv', measure)} and unmapped columns.
    `variant` is 'base' or 'tc' (the table-calculation result). Duplicate headers are resolved later."""
    dims, measures = P.query_fields(plan)
    mapping, unmapped = {}, []
    dim_by = {_canon(_dim_header(d)): d for d in dims}
    meas_by = {}
    for m in measures:
        for h in _measure_headers(m):
            meas_by.setdefault(_canon(h), []).append(m)
    tc_fields = [f for f in plan['fields'] if f.get('table_calc') and f['kind'] == 'measure']
    agg_dims = [f for f in plan['fields'] if f.get('aggregate_dimension')]
    mv = {}
    for m in plan.get('measure_values') or []:
        mv.setdefault(_mv_key(m['caption']), []).append(m)
    for c in t_cols:
        base = c.split('#')[0]
        if GENERATED.match(base):
            mapping[c] = ('ignored', None)
            continue
        if c.startswith('MV:'):
            options = mv.get(_mv_key(c[3:])) or []
            agg = _mv_agg(c[3:])
            exact = [m for m in options if (agg and m.get('agg') == agg) or
                     (not agg and m.get('agg') in ('sum', 'usr', 'none', None))]
            m = (exact or (options if len(options) == 1 else [None]))[0]
            mapping[c] = ('mv', m) if m else None
            if not m:
                unmapped.append(c)
            continue
        key = _canon(base)
        dp = REST_PART.match(base)                 # REST Query View Data: 'Month of Order Date' = MONTH(Order Date)
        if dp and key not in dim_by and _canon(f'{dp.group(1).upper()}({dp.group(2)})') in dim_by:
            mapping[c] = ('dim', dim_by[_canon(f'{dp.group(1).upper()}({dp.group(2)})')])
            continue
        if key in dim_by and not _WRAPPED.match(base) or (key in dim_by and base.split('(')[0] in PART_TOKENS.values()):
            mapping[c] = ('dim', dim_by[key])
            continue
        cands = meas_by.get(key, [])
        tcs = [f for f in tc_fields if any(_canon(h) == key for h in _measure_headers(f))]
        ads = [f for f in agg_dims if any(_canon(h) == key for h in _measure_headers(f))]
        if cands or tcs or ads:
            mapping[c] = ('measure', cands, tcs + ads)
            continue
        # REST Query View Data spellings: 'Distinct count of X' / 'Sum of X' (prefix names the aggregation) and a
        # bare caption 'X' for SUM / AGG pills -- accepted only when exactly one plan measure fits (never a guess)
        agg, cap = _mv_agg(base), _mv_key(base)
        fits = lambda f: _canon(f['caption']) == cap and (
            agg is None or f['agg'] == agg or (agg == 'sum' and f['agg'] in ('usr', 'none')))
        same = [m for m in measures if fits(m)]
        extra = [f for f in tc_fields + agg_dims if fits(f)]
        if len(same) == 1 or (not same and len(extra) == 1):
            mapping[c] = ('measure', same, extra)
            continue
        unmapped.append(c)
    return mapping, unmapped


def _alias_measure_values(mapping, unmapped, t_rows):
    """A Measure Names member renamed by the author ('Count of Customers') is identified by evidence only:
    the pivoted column whose value in every row equals one wide measure column that is already mapped."""
    left = []
    for c in unmapped:
        if not c.startswith('MV:') or not t_rows:
            left.append(c)
            continue
        hits = []
        for w, m in mapping.items():
            if not m or m[0] != 'measure' or not m[1]:
                continue
            if all(_number(r.get(c))[0] is not None and _number(r.get(w))[0] is not None and
                   abs(_number(r.get(c))[0] - _number(r.get(w))[0]) <= _number(r.get(c))[1] + _number(r.get(w))[1]
                   for r in t_rows):
                hits.append(m[1][0])
        if len(hits) == 1:
            mapping[c] = ('mv', hits[0])
        else:
            left.append(c)
    return left


# --------------------------------------------------------------------------- app side
def app_rows(plan, df, engine):
    """Rows keyed like the comparison: {'D|part|caption': canonical key, 'M|agg|caption': value, ...}."""
    dims, measures = P.query_fields(plan)
    labelled = {P.col_name(d): P._labels(df, d, engine) for d in dims}
    out = []
    for i in range(len(df)):
        r = {}
        for d in dims:
            r[P.col_name(d)] = P.key_text(labelled[P.col_name(d)].iloc[i], d['date_part'])
        for c in df.columns:
            if c.startswith('M|'):
                r[c] = df[c].iloc[i]
        out.append(r)
    return out


def _num(v):
    try:
        if v is None:
            return None
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def _same(t_text, app_value):
    """(comparable, equal) for one Tableau display text and one app value."""
    if t_text is None:
        return False, False                    # cell absent from a truncated capture: nothing to compare
    tv, tol = _number(t_text)
    if tv is None:
        t = str(t_text or '').strip()
        if t in ('Null', ''):
            return True, app_value is None or _num(app_value) is None and str(app_value) in ('None', 'nan', '')
        a = str(app_value if app_value is not None else '').strip()
        return True, _canon(t) == _canon(a)
    av = _num(app_value)
    if av is None:
        return True, False
    return True, abs(tv - av) <= tol + 1e-9 * max(1.0, abs(tv))


# --------------------------------------------------------------------------- comparison
def compare(plan, df, entry, engine):
    """Compare one executed plan with one Tableau capture entry. Returns a result dict:
    {'status': 'Verified'|'Not matched'|'Not comparable', 'reasons': [...], counts..., 'details': [...]}"""
    res = {'status': 'Not matched', 'reasons': [], 'rows_tableau': 0, 'rows_app': len(df), 'matched': 0,
           'different': 0, 'missing_in_app': 0, 'extra_in_app': 0, 'details': [], 'columns': {}}
    if entry.get('error'):
        if '[0..0)' in entry['error'] and len(df) == 0:
            res.update(status='Verified', reasons=['Tableau and the app both return no rows'])
            return res
        res.update(status='Not comparable', reasons=['Tableau returned no data: ' + entry['error'][:120]])
        return res
    t_cols, t_rows = tableau_table(entry)
    app_estimates = None
    if 'Forecast Indicator' in getattr(df, 'columns', []):
        # FC: the app's approximated forecast rows are compared apart from the view's actual rows
        app_estimates = df[df['Forecast Indicator'] == 'Estimate'].drop(columns='Forecast Indicator')
        df = df[df['Forecast Indicator'] != 'Estimate'].drop(columns='Forecast Indicator').reset_index(drop=True)
        res['rows_app'] = len(df)
    forecast_rows = 0
    estimate_rows = []
    gen = [f['caption'] for f in plan['fields'] if f['kind'] == 'generated']
    if 'Forecast Indicator' in gen and 'Forecast Indicator' in t_cols:
        # Tableau's forecast model adds 'Estimate' rows; only the 'Actual' rows are data the app can equal
        forecast_rows = sum(1 for r in t_rows if str(r.get('Forecast Indicator')) != 'Actual')
        estimate_rows = [r for r in t_rows if str(r.get('Forecast Indicator')) != 'Actual']
        t_rows = [r for r in t_rows if str(r.get('Forecast Indicator')) == 'Actual']
        t_cols = [c for c in t_cols if c != 'Forecast Indicator']
        entry = dict(entry, total_rows=int(entry.get('total_rows') or 0) - forecast_rows)
    pivoted = any(c.startswith('MV:') for c in t_cols)
    n_mv = sum(1 for c in t_cols if c.startswith('MV:'))
    long_total = int(entry.get('total_rows') or len(entry.get('rows') or []))
    truncated_long = long_total > len(entry.get('rows') or [])
    if pivoted and not truncated_long:
        res['rows_tableau'] = len(t_rows)
    elif pivoted:
        res['rows_tableau'] = long_total            # long rows; compared as app rows x measures below
    else:
        res['rows_tableau'] = long_total
    mapping, unmapped = map_columns(plan, t_cols)
    unmapped = _alias_measure_values(mapping, unmapped, t_rows)
    if unmapped:
        res['reasons'].append('Tableau columns the plan does not produce: ' + ', '.join(unmapped))
    dims_t = [c for c, m in mapping.items() if m and m[0] == 'dim']
    dims_plan, _ = P.query_fields(plan)
    missing_dims = [d for d in dims_plan if all(mapping[c][1] is not d for c in dims_t)]
    if missing_dims:
        res['reasons'].append('Plan dimensions Tableau does not show: ' + ', '.join(_dim_header(d) for d in missing_dims))
    rows = app_rows(plan, df, engine)
    key_cols = [(c, mapping[c][1]) for c in dims_t]
    index = {}
    for r in rows:
        index.setdefault(tuple(r[P.col_name(d)] for _c, d in key_cols), []).append(r)
    # measure columns: choose, per Tableau column, the app column that agrees on the most rows
    measure_cols = [c for c, m in mapping.items() if m and m[0] in ('measure', 'mv')]
    choice = _choose_columns(mapping, measure_cols, t_rows, key_cols, index)
    res['columns'] = {c: v for c, v in choice.items()}
    for c in measure_cols:
        if choice.get(c) is None:
            res['reasons'].append(f'No app value matches Tableau column {c}')
    seen_keys = set()
    truncated = bool(entry.get('note')) or truncated_long or (not pivoted and len(t_rows) < res['rows_tableau'])
    for tr in t_rows:
        k = tuple(P.key_text(tr.get(c), d['date_part']) for c, d in key_cols)
        cand = index.get(k) or []
        label = ' / '.join(str(tr.get(c)) for c, _d in key_cols) or '(single row)'
        if len(cand) > 1 and not plan.get('aggregate', True):
            # disaggregated view: several data rows share the keys; match each Tableau row to one app row
            pick = next((a for a in cand if not a.get('__used') and all(
                _same(tr.get(c), a.get(choice.get(c)))[1] for c in measure_cols if choice.get(c))), None)
            pick = pick or next((a for a in cand if not a.get('__used')), None)
            if pick is not None:
                pick['__used'] = True
                cand = [pick]
        if len(cand) != 1:
            if all(str(tr.get(c, '')).strip() in ('', 'Null') for c in measure_cols):
                continue
            res['missing_in_app'] += 1
            if len(res['details']) < MAX_DETAIL:
                res['details'].append({'Key': label, 'Column': '—', 'Tableau': '',
                                       'App': 'row not in app' if not cand else 'duplicate key in app'})
            continue
        seen_keys.add(k)
        a = cand[0]
        if not plan.get('aggregate', True) and len(index.get(k) or []) > 1 and \
                any(not r.get('__used') for r in index[k]):
            seen_keys.discard(k)               # some data rows with these keys are still unmatched
        for c in measure_cols:
            col = choice.get(c)
            if col is None:
                continue
            ok_cmp, ok = _same(tr.get(c), a.get(col))
            if not ok_cmp:
                continue
            if ok:
                res['matched'] += 1
            else:
                res['different'] += 1
                if len(res['details']) < MAX_DETAIL:
                    res['details'].append({'Key': label, 'Column': c, 'Tableau': tr.get(c), 'App': a.get(col)})
    extra = [k for k in index if k not in seen_keys]
    replaced = 0
    if estimate_rows:
        # Tableau's forecast option "ignore last period" shows the last actual period as an estimate:
        # an app row whose keys Tableau lists only as an estimate is that period, not an extra row
        est_keys = {tuple(P.key_text(r.get(c), d['date_part']) for c, d in key_cols) for r in estimate_rows}
        replaced = sum(1 for k in extra if k in est_keys)
        extra = [k for k in extra if k not in est_keys]
    if not plan.get('aggregate', True):
        extra = [k for k, rs in index.items() for r in rs if not r.get('__used') and k not in seen_keys]
    res['extra_in_app'] = 0 if truncated else len(extra)
    if not truncated:
        for k in extra[:max(0, MAX_DETAIL - len(res['details']))]:
            res['details'].append({'Key': ' / '.join(k), 'Column': '—', 'Tableau': 'row not in Tableau', 'App': ''})
    app_count = len(rows) * n_mv if (pivoted and truncated_long) else len(rows)
    if truncated and app_count != res['rows_tableau']:
        res['reasons'].append(f"Row count differs: Tableau {res['rows_tableau']:,}, app {app_count:,}"
                              + (' (measure rows)' if pivoted and truncated_long else ''))
    if not truncated and res['extra_in_app']:
        res['reasons'].append(f"{res['extra_in_app']} row(s) in the app that Tableau does not show")
    if res['missing_in_app']:
        res['reasons'].append(f"{res['missing_in_app']} Tableau row(s) not in the app")
    if res['different']:
        res['reasons'].append(f"{res['different']} value(s) differ")
    if not res['reasons'] and (res['matched'] or not measure_cols):
        res['status'] = 'Verified'
        if forecast_rows:
            res['status'] = 'Partly verified'
            approx = _estimate_error(estimate_rows, app_estimates, key_cols, choice, measure_cols, engine, plan)
            res['forecast'] = approx
            if approx:
                res['reasons'].append(f'All {len(t_rows)} actual rows equal Tableau; {forecast_rows} forecast estimate '
                                      f"row(s) are an approximated forecast ({approx['matched']} compared, mean "
                                      f"difference {approx['mean_abs_pct']:.1%} from Tableau's model)")
            else:
                res['reasons'].append(f'All {len(t_rows)} actual rows equal Tableau; {forecast_rows} forecast '
                                      "estimate row(s) come from Tableau's forecast model and are not reproduced"
                                      + (f' ({replaced} of them is the last actual period, which Tableau shows as '
                                         'an estimate)' if replaced else ''))
    return res


def _estimate_error(estimate_rows, app_estimates, key_cols, choice, measure_cols, engine, plan):
    """FC: how far the app's approximated forecast is from Tableau's estimates (never a match claim)."""
    if app_estimates is None or not len(app_estimates) or not estimate_rows:
        return None
    index = {}
    for r in app_rows(plan, app_estimates.reset_index(drop=True), engine):
        index.setdefault(tuple(r[P.col_name(d)] for _c, d in key_cols), r)
    errs = []
    for tr in estimate_rows:
        a = index.get(tuple(P.key_text(tr.get(c), d['date_part']) for c, d in key_cols))
        for c in measure_cols:
            col = choice.get(c)
            tv, av = _number(tr.get(c))[0], (_num(a.get(col)) if a and col else None)
            if tv not in (None, 0) and av is not None:
                errs.append(abs(av - tv) / abs(tv))
    if not errs:
        return None
    return {'matched': len(errs), 'mean_abs_pct': sum(errs) / len(errs), 'approximated': True}


def _choose_columns(mapping, measure_cols, t_rows, key_cols, index):
    """Tableau measure column -> app column. Several candidates (base value vs table-calc result, duplicated
    headers) are scored on a sample of rows; each app column is used at most once per header group."""
    sample = t_rows[:300]
    pairs = []
    for tr in sample:
        k = tuple(P.key_text(tr.get(c), d['date_part']) for c, d in key_cols)
        cand = index.get(k) or []
        if len(cand) == 1:
            pairs.append((tr, cand[0]))
    choice = {}
    groups = {}
    for c in measure_cols:
        groups.setdefault(c.split('#')[0], []).append(c)
    for base, cols in groups.items():
        options = []
        for c in cols:
            m = mapping[c]
            if m[0] == 'mv':
                options.append([P.col_name(m[1], True)])
            else:
                opts = [P.col_name(f, True) for f in m[1]]
                opts += [P.col_name(f, True) + '|tc' for f in m[2] if f.get('table_calc')]
                opts += [P.col_name(f, True) for f in m[2] if f.get('aggregate_dimension')]
                options.append(list(dict.fromkeys(opts)))
        all_opts = list(dict.fromkeys(o for opts in options for o in opts))

        def score(c, o):
            return sum(1 for tr, a in pairs if _same(tr.get(c), a.get(o))[1])
        best, best_score = None, -1
        for perm in itertools.permutations(all_opts, len(cols)) if len(all_opts) >= len(cols) else []:
            if any(o not in options[i] for i, o in enumerate(perm)):
                continue
            s = sum(score(c, o) for c, o in zip(cols, perm))
            if s > best_score:
                best, best_score = perm, s
        if best is None:
            for c, opts in zip(cols, options):
                choice[c] = opts[0] if opts else None
        else:
            choice.update(dict(zip(cols, best)))
    return choice


# --------------------------------------------------------------------------- candidates and the loop
def variants(plan):
    """Code variants of a plan that differ only where the workbook XML is ambiguous for code: the
    addressing of each table calculation. The authored reading comes first."""
    slots = [(i, k) for i, f in enumerate(plan['fields']) for k in ('table_calc', 'window') if f.get(k)]
    if not slots:
        return [plan]
    out = [plan]
    for i, k in slots:
        for a in ('rows', 'cols', 'table'):
            if plan['fields'][i][k]['addressing'] == a:
                continue
            v = P.deepcopy(plan)
            v['fields'][i][k]['addressing'] = a
            cap = plan['fields'][i]['caption']
            # the same calculated field on several shelves is one calculation: change it everywhere
            for f in v['fields']:
                if f['caption'] == cap and f.get(k):
                    f[k]['addressing'] = a
            v['source'] = plan.get('source', 'authored') + f'+addressing:{a}'
            out.append(v)
    return out


def with_geocoding(plan, entry):
    """For a map on generated latitude/longitude, keep Tableau's own coordinates per location member (from its
    summary data). Members Tableau could not place are not marks in Tableau; the plan drops them too."""
    if not entry or not any(f['kind'] == 'geo' for f in plan['fields']):
        return plan
    cols = entry.get('columns') or []
    if 'Latitude (generated)' not in cols or 'Longitude (generated)' not in cols:
        return plan
    dims, _m = P.query_fields(plan)
    keyed = [(cols.index(P.col_name(d).split('|')[2]) if P.col_name(d).split('|')[2] in cols else None, d)
             for d in dims]
    if any(i is None for i, _d in keyed):
        return plan
    lat, lon = cols.index('Latitude (generated)'), cols.index('Longitude (generated)')
    points = {}
    for r in entry.get('rows') or []:
        k = '\u241f'.join(P.key_text(r[i], d['date_part']) for i, d in keyed)
        points[k] = [_number(r[lat])[0], _number(r[lon])[0]]
    if entry.get('note'):
        return plan                           # a truncated capture cannot list every location
    out = P.deepcopy(plan)
    out['geo_points'] = points
    out['source'] = plan.get('source', 'authored') + '+tableau-geocoding'
    return out


def prove(sheet, engine, entry, extra_candidates=(), where_parts=None, ir=None):
    """Run every candidate plan for one sheet and compare with Tableau. Returns (best plan or None, evidence)."""
    tried = []
    candidates = []
    try:
        candidates += variants(with_geocoding(P.from_sheet(sheet, ir), entry))
    except P.PlanError as exc:
        tried.append({'source': 'authored', 'status': 'Not planned', 'reasons': [str(exc)]})
    for c in extra_candidates:
        candidates += variants(c)
    best = None
    for plan in candidates:
        problems = P.validate(plan)
        if problems:
            tried.append({'source': plan.get('source'), 'status': 'Invalid plan', 'reasons': problems[:5]})
            continue
        try:
            df = P.execute(plan, sheet, engine, where_parts)
        except Exception as exc:          # a plan that cannot run is evidence, not a crash
            tried.append({'source': plan.get('source'), 'status': 'Did not run',
                          'reasons': [f'{type(exc).__name__}: {str(exc)[:200]}']})
            continue
        result = compare(plan, df, entry, engine) if entry is not None else {
            'status': 'No Tableau reference', 'reasons': ['No Tableau values captured for this sheet']}
        result['source'] = plan.get('source')
        result['fingerprint'] = P.fingerprint(plan)
        tried.append(result)
        if best is None or _rank(result) > _rank(best[1]):
            best = (plan, result)
        if result['status'] == 'Verified':
            break
    evidence = {'status': best[1]['status'] if best else 'Not planned', 'tried': tried,
                'best': best[1] if best else None}
    return (best[0] if best else None), evidence


def _rank(r):
    order = {'Verified': 4, 'Partly verified': 3, 'Not matched': 2, 'No Tableau reference': 1, 'Not comparable': 1}
    return (order.get(r.get('status'), 0), r.get('matched', 0) - 5 * r.get('different', 0)
            - r.get('missing_in_app', 0) - r.get('extra_in_app', 0) - 50 * len(r.get('reasons') or []))


def visual_check(plan, sheet):
    """Code check of the drawn spec against the authored shelves: mark and every axis / marks-card field."""
    authored = P.from_sheet(sheet, plan.get('_ir'))
    issues = []
    if plan['mark'] != authored['mark']:
        issues.append(f"mark {plan['mark']} differs from authored {authored['mark']}")
    have = {(f['channel'], f['caption']) for f in plan['fields']}
    for f in authored['fields']:
        if f['kind'] in ('dim', 'measure') and (f['channel'], f['caption']) not in have:
            issues.append(f"{f['caption']} on {f['channel']} is not drawn")
    not_drawn = []
    if P.build_chart is not None:
        if plan['mark'] in ('text', 'map', 'pie') or any(f.get('measure_values') for f in plan['fields']):
            not_drawn.append(f"{plan['mark']} drawn as a table")
    return {'status': 'Matches authored shelves' if not issues else 'Differs', 'issues': issues,
            'notes': not_drawn}


# --------------------------------------------------------------------------- pipeline integration (release 26)
PLAN_NOTE = 'Exact plan from the workbook shelves; not yet verified against Tableau'
APPLICABLE = ('Verified', 'Partly verified')


def attach_plans(ir, path=None):
    """At intake: every sheet the V3 adapters refuse gets its exact plan from the authored shelves (kind
    'planned', state PARTIAL until a person applies Tableau-verified evidence). The original-engine
    approximation is kept on the sheet as the stand-in when the plan cannot run on the bound data.
    Returns [(dashboard, sheet)] planned. No data is read and no model is called."""
    import v3_fallback as F
    targets = [(d, s) for d in ir.get('dashboards') or [] for s in d.get('sheets') or []
               if F.refused(s) and s.get('worksheet_semantics')]
    if not targets:
        return []
    lookup = F.legacy_lookup(path) if path else None
    done = []
    for dash, sheet in targets:
        try:
            plan = P.from_sheet(sheet, ir)
        except P.PlanError:
            continue
        if P.validate(plan):
            continue
        reason = (sheet.get('support') or {}).get('reason') or 'no exact adapter'
        approx = F.approximation(dict(sheet, v3_refusal=reason), (lookup or {}).get((dash.get('name'), sheet.get('name'))))
        sheet['v3_refusal'] = reason
        sheet['sheet_plan'] = plan
        sheet['engine_kind'] = sheet.get('kind')
        sheet['kind'] = 'planned'
        sheet['support'] = {'status': 'PARTIAL', 'reason': PLAN_NOTE}
        _refresh_projection(sheet)
        if approx is not None:
            sheet['approximation'] = approx
        from ir_contract import sheet_issues
        if sheet_issues(sheet):                   # never ship a sheet the contract rejects: restore the refusal
            _restore(sheet, reason)
            continue
        done.append((dash.get('name'), sheet.get('name')))
    if done:
        ir['planned_sheets'] = [{'dashboard': d, 'sheet': s} for d, s in done]
    return done


def _refresh_projection(sheet):
    from worksheet_semantics import projection
    model = sheet['worksheet_semantics']
    model['projection'] = projection(sheet, model)


def _restore(sheet, reason):
    for k in ('sheet_plan', 'approximation', 'v3_refusal', 'sheet_plan_evidence'):
        sheet.pop(k, None)
    sheet['kind'] = sheet.pop('engine_kind', None) or 'unsupported'
    sheet['support'] = {'status': 'UNSUPPORTED', 'reason': reason}
    _refresh_projection(sheet)


def _captures_by_dashboard(capture):
    import v3_sheet_capture as SC
    return {SC.canon(c.get('dashboard')): c for c in SC.dashboard_captures(capture)}


def prove_run(run, snowflake):
    """Prove every sheet that has scoped semantics against the loaded Tableau reference (read-only queries
    on the bound data). Stores run['exact'] = {(dashboard, sheet): {...}} and returns the summary rows."""
    import engine
    import parameter_actions
    import v3_evidence as EV
    import v3_sheet_capture as SC
    from v3_local import preview_context
    capture = SC.load(run['capture_bytes'])
    by_dash = _captures_by_dashboard(capture)
    ir = run['result']['ir']
    out = {}
    with preview_context(ir, run.get('bindings') or {}, snowflake=snowflake), EV.offscreen():
        parameter_actions.reset(parameter_actions.context(engine))
        for dash in ir.get('dashboards') or []:
            cap = by_dash.get(SC.canon(dash.get('name'))) or by_dash.get(SC.canon(dash.get('title')))
            for sheet in dash.get('sheets') or []:
                if not sheet.get('worksheet_semantics'):
                    continue
                entry = ((cap or {}).get('sheets') or {}).get(sheet.get('name'))
                if entry is None:
                    continue
                plan, ev = prove(sheet, engine, entry, ir=ir)
                best = ev.get('best') or {}
                out[(dash.get('name'), sheet.get('name'))] = {
                    'plan': plan, 'status': ev['status'], 'best': best, 'tried': ev['tried'],
                    'visual': visual_check(plan, sheet) if plan else None,
                    'current': 'exact plan' if sheet.get('sheet_plan') else (sheet.get('kind') or '—'),
                    'applied': bool((sheet.get('sheet_plan_evidence') or {}).get('fingerprint') ==
                                    (best.get('fingerprint') if best else None) and plan is not None)}
    run['exact'] = out
    run['exact_meta'] = f"{capture.get('method')} · {len(SC.dashboard_captures(capture))} dashboard capture(s)"
    return summary_rows(run)


def summary_rows(run):
    rows = []
    for (dash, name), r in (run.get('exact') or {}).items():
        b = r.get('best') or {}
        reasons = b.get('reasons') or [x for t in r.get('tried') or [] for x in t.get('reasons') or []][:2]
        rows.append({'Dashboard': dash, 'Sheet': name, 'Result': r['status'],
                     'Values equal': b.get('matched'), 'Different': b.get('different'),
                     'Tableau rows': b.get('rows_tableau'), 'App rows': b.get('rows_app'),
                     'Missing': b.get('missing_in_app'), 'Extra': b.get('extra_in_app'),
                     'Plan': (b.get('source') or '—').replace('authored', 'workbook shelves'),
                     'Drawn as': (r.get('visual') or {}).get('status', '—'),
                     'Applied': 'Yes' if r.get('applied') else 'No',
                     'Note': '; '.join(str(x) for x in reasons)[:300]})
    return rows


def applicable(run):
    """Results a person can apply: a Tableau-verified plan that is not yet the sheet's applied plan."""
    return [(k, r) for k, r in (run.get('exact') or {}).items() if r['status'] in APPLICABLE and r.get('plan')
            and not r.get('applied')]


def apply_verified(run, approved_by, capture_label, at):
    """Apply every verified plan (a person's decision, recorded by the caller): the sheet is drawn from the plan
    that equalled Tableau's values, with that evidence on the sheet. Returns the applied (dashboard, sheet)."""
    ir = run['result']['ir']
    done = []
    for (dash_name, name), r in applicable(run):
        sheet = next((s for d in ir.get('dashboards') or [] if d.get('name') == dash_name
                      for s in d.get('sheets') or [] if s.get('name') == name), None)
        if sheet is None:
            continue
        b = r['best']
        if 'engine_kind' not in sheet:
            sheet['engine_kind'] = sheet.get('kind')
        sheet['sheet_plan'] = r['plan']
        sheet['kind'] = 'planned'
        _refresh_projection(sheet)
        sheet['sheet_plan_evidence'] = {
            'status': r['status'], 'fingerprint': b.get('fingerprint'), 'values_equal': b.get('matched'),
            'tableau_rows': b.get('rows_tableau'), 'note': '; '.join(b.get('reasons') or [])[:300],
            'reference': capture_label, 'approved_by': approved_by, 'approved_at': at}
        sheet['support'] = ({'status': 'UNVERIFIED', 'reason': ''} if r['status'] == 'Verified' else
                            {'status': 'PARTIAL', 'reason': '; '.join(b.get('reasons') or [])[:300]})
        r['applied'] = True
        done.append((dash_name, name))
    return done
