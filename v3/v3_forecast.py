"""FC: an APPROXIMATED Tableau forecast.

Tableau forecasts with exponential smoothing (ETS), but its fitting and initialisation are not published, so a
reproduction cannot equal Tableau to the cent. This module adds a labelled approximation instead of dropping the
forecast: Holt-Winters (level, optional additive trend, optional additive / multiplicative season) fitted per
partition in plain numpy (no optimiser or statistics package in the runtime), driven by the sheet's <forecast-specification>:
model types, ignore-last, forecast range and fill-zero. Estimate rows carry Forecast Indicator = 'Estimate'.
The exact loop compares them to Tableau's estimates and reports the error; they are never 'Verified'."""
import re

import numpy as np

INDICATOR = 'Forecast Indicator'
_SEASON = {'month_trunc': 12, 'quarter_trunc': 4, 'week_trunc': 52, 'day_trunc': 7, 'hour_trunc': 24,
           'year_trunc': 1}
_FREQ = {'month_trunc': 'MS', 'quarter_trunc': 'QS', 'year_trunc': 'YS', 'week_trunc': '7D', 'day_trunc': 'D',
         'hour_trunc': 'h'}
_AUTO_RANGE = {'year_trunc': 5}            # otherwise one season ahead (12 months, 4 quarters, ...)
_GRID = np.linspace(0.01, 0.99, 25)


def _kind(text):
    t = (text or '').lower()
    if 'mult' in t:
        return 'mul'
    if 'add' in t:
        return 'add'
    if t in ('none', 'no-trend', 'no-season'):
        return None
    return 'auto'


def spec_from_xml(xml):
    """The sheet's forecast settings, or None when it has no enabled forecast."""
    m = re.search(r'<forecast-specification\b([^>]*)>', xml or '')
    if not m:
        return None
    attrs = dict(re.findall(r"([\w-]+)=['\"]([^'\"]*)['\"]", m.group(1)))
    if attrs.get('enabled') == 'false':
        return None
    custom = attrs.get('model-type') == 'custom'
    rng = None
    if attrs.get('range-type') not in (None, 'auto'):
        for k in ('range-n', 'range', 'forecast-range'):
            if str(attrs.get(k, '')).isdigit():
                rng = int(attrs[k])
                break
    return {'trend': _kind(attrs.get('trend-type')) if custom else 'auto',
            'season': _kind(attrs.get('season-type')) if custom else 'auto',
            'ignore_last': int(attrs.get('ignore-last') or 0), 'range': rng,
            'fill_zero': attrs.get('fill-type', 'fill-zero') == 'fill-zero',
            'bands': attrs.get('show-prediction-bands') == 'true', 'approximated': True}


def horizon(spec, date_part):
    if spec.get('range'):
        return int(spec['range'])
    return _AUTO_RANGE.get(date_part, _SEASON.get(date_part, 12))


def _fit(y, m, trend, season, ga=_GRID, gb=_GRID, gg=_GRID):
    """Smoothing weights by a grid search on one-step-ahead squared error; returns (sse, state)."""
    n = len(y)
    a, b, g = np.meshgrid(ga, gb if trend else [0.0], gg if season else [0.0], indexing='ij')
    a, b, g = a.ravel(), b.ravel(), g.ravel()
    k = len(a)
    if season:
        # level and slope from the first two seasons; the first season's shape with the slope taken out
        mean0 = y[:m].mean()
        b0 = (y[m:2 * m].mean() - mean0) / m if trend else 0.0
        line = mean0 + b0 * (np.arange(m) - (m - 1) / 2)
        s0 = y[:m] - line if season == 'add' else y[:m] / line
        l0 = line[-1]
        start = m
    else:
        l0, b0, s0 = y[0], (y[1] - y[0]) if trend and n > 1 else 0.0, np.zeros(1)
        start = 1
    lv, bv = np.full(k, float(l0)), np.full(k, float(b0))
    sv = np.tile(np.asarray(s0, float), (k, 1))
    sse = np.zeros(k)
    mm = m if season else 1
    for t in range(start, n):
        si = sv[:, t % mm]
        if season == 'mul':
            f = (lv + bv) * si
            ln = a * (y[t] / si) + (1 - a) * (lv + bv)
        else:
            f = lv + bv + si
            ln = a * (y[t] - si) + (1 - a) * (lv + bv)
        sse += (y[t] - f) ** 2
        bn = b * (ln - lv) + (1 - b) * bv
        if season == 'mul':
            sv[:, t % mm] = g * (y[t] / ln) + (1 - g) * si
        elif season == 'add':
            sv[:, t % mm] = g * (y[t] - ln) + (1 - g) * si
        lv, bv = ln, bn
    i = int(np.nanargmin(sse))
    return float(sse[i]), (float(lv[i]), float(bv[i]), sv[i].copy(), n, mm, season)


def _project(state, steps):
    lv, bv, s, n, mm, season = state
    out = []
    for h in range(1, steps + 1):
        si = s[(n + h - 1) % mm]
        out.append((lv + h * bv) * si if season == 'mul' else lv + h * bv + (si if season else 0.0))
    return out


def holt_winters(y, period, trend, season, steps):
    """Forecast `steps` periods after y. trend / season: 'add', 'mul' (season only), None, or 'auto' (the
    candidate with the lowest AIC, as Tableau's automatic model picks by information criterion)."""
    y = np.asarray([float(v) for v in y], float)
    if steps <= 0:
        return []
    if len(y) == 0:
        return [0.0] * steps
    if len(y) < 3:
        return [float(y[-1])] * steps
    can_season = period > 1 and len(y) >= 2 * period
    trends = ['add', None] if trend == 'auto' else [trend if trend in ('add', None) else 'add']
    seasons = ['add', 'mul', None] if season == 'auto' else [season]
    seasons = [s if can_season else None for s in seasons]
    best = None
    for tr in trends:
        for se in dict.fromkeys(seasons):
            if se == 'mul' and (y <= 0).any():
                se = 'add'
            sse, state = _fit(y, period, tr, se)
            k = 1 + (1 if tr else 0) + (period if se else 0)
            aic = len(y) * np.log(max(sse, 1e-12) / len(y)) + 2 * k
            if best is None or aic < best[0]:
                best = (aic, state)
    return [float(v) for v in _project(best[1], steps)]


def add_estimates(df, plan):
    """The view's rows plus forecast Estimate rows per partition (every other dimension on the view)."""
    import pandas as pd
    import v3_sheet_plan as P
    spec = plan.get('forecast')
    if not isinstance(spec, dict) or not len(df):
        return df
    dims = [f for f in plan.get('fields') or [] if f.get('kind') == 'dim']
    time = next((d for d in dims if d.get('date_part') in _FREQ), None)
    if time is None:
        return df
    tcol = P.col_name(time)
    parts = [P.col_name(d) for d in dims if d is not time and P.col_name(d) in df.columns]
    measures = [P.col_name(f, True) for f in plan['fields'] if f.get('kind') == 'measure'
                and not f.get('table_calc') and P.col_name(f, True) in df.columns]
    if tcol not in df.columns or not measures:
        return df
    part_ = time['date_part']
    freq, period = _FREQ[part_], _SEASON[part_]
    steps = int(spec.get('ignore_last') or 0) + horizon(spec, part_)
    data = df.copy()
    data[tcol] = pd.to_datetime(data[tcol])
    last = data[tcol].max()
    periods = sorted(data[tcol].dropna().unique())
    ignored = set(periods[len(periods) - int(spec.get('ignore_last') or 0):]) if spec.get('ignore_last') else set()
    actual = data[~data[tcol].isin(ignored)].copy()
    actual[INDICATOR] = 'Actual'
    estimates = []
    groups = actual.groupby(parts, dropna=False, sort=False) if parts else [((), actual)]
    for key, g in groups:
        key = key if isinstance(key, tuple) else (key,)
        g = g.sort_values(tcol)
        idx = pd.date_range(g[tcol].min(), max(g[tcol].max(), max(set(periods) - ignored)), freq=freq)
        future = pd.date_range(idx[-1], periods=steps + 1, freq=freq)[1:]
        rows = {c: [v] * steps for c, v in zip(parts, key)}
        rows[tcol] = list(future)
        for mcol in measures:
            s = g.groupby(tcol)[mcol].sum(min_count=1).reindex(idx)
            s = s.fillna(0.0) if spec.get('fill_zero', True) else s.interpolate().bfill().ffill()
            rows[mcol] = holt_winters(s.values, period, spec.get('trend', 'auto'), spec.get('season', 'auto'), steps)
        estimates.append(pd.DataFrame(rows))
    est = pd.concat(estimates, ignore_index=True) if estimates else pd.DataFrame(columns=actual.columns)
    est[INDICATOR] = 'Estimate'
    out = pd.concat([actual, est], ignore_index=True)
    if not pd.api.types.is_datetime64_any_dtype(df[tcol]):
        out[tcol] = out[tcol].dt.date if last is not None else out[tcol]
    return out.sort_values(parts + [tcol], kind='stable').reset_index(drop=True)
