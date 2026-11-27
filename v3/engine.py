# =============================================================================
# engine.py  --  runtime interpreter for the generated dashboards.
# The accelerator's Stage-3 output (app.py) imports this and feeds it the IR.
# Renders ANY parsed sheet spec: kpi / mbar / bar / line / area / scatter /
# heatmap / circle / map / table / pctbar / dots. Same code runs locally
# (DuckDB) and in SiS (Snowpark). Multi-datasource: each sheet queries the
# table config maps for its Tableau datasource.
#
# TRANSPARENCY RULE: the engine never guesses. Anything it cannot resolve
# (unknown measure, missing column, unsupported construct) is recorded in
# findings.py and surfaced in-app + in the compatibility report.
# =============================================================================
import streamlit as st
import altair as alt
import pandas as pd
import html as _html
import hashlib
from contextlib import contextmanager
from threading import RLock

import findings
import ir_contract
from backend import run_sql
import config
from config import ORDERS, table_for
import calc_translator
from calc_translator import (to_phys, measure_sql as _lib_measure,
                             MEASURE_LIBRARY, CAPTION_ALIASES, WIN_ORDER,
                             param_token, param_sql_literal)

CALCS = {}          # caption -> {"sql":..., "agg_ready":bool, "window":bool}
ALIASES = {}        # caption -> {raw value: display label}   (from the workbook)
COLMAP = {}         # caption -> SOURCE column name (workbook-renamed fields)
_AGGMAP = {"sum": "SUM", "avg": "AVG", "min": "MIN", "max": "MAX",
           "cnt": "COUNT", "ctd": "COUNT", "med": "MEDIAN"}

# ---------------------------------------------------------------------------
# Validation-evidence capture -- OFF by default. A deployed app never turns
# this on, so this section changes NOTHING about what any app renders; it
# exists purely so a validation run can record the REAL final dataframe for
# chart kinds that never produce an Altair object (map/treemap render via
# Plotly; plain tables render via st.dataframe; rank-lists render as
# hand-built HTML) -- kinds the existing Altair-encoding-based capture
# (validation_adapter.resolve_chart_columns) cannot see at all. A validation
# pass sets EVIDENCE_CAPTURE=True and _EVIDENCE_DASHBOARD for the duration of
# one headless render_sheet() call (see
# headless_render.capture_sheet_chart(..., dashboard_name=...)); every other
# caller (a deployed app, a normal preview) leaves EVIDENCE_CAPTURE False, so
# _record_chart_evidence below short-circuits on its first line and never
# imports anything. Lazy-imported and fully exception-guarded: recording
# evidence must never be able to break a chart's actual rendering.
# ---------------------------------------------------------------------------
EVIDENCE_CAPTURE = False
_EVIDENCE_DASHBOARD = None
# Detail/list-table sheets (r_table) cap evidence to their top N displayed
# rows, not the full (up to 200) displayed set -- see r_table's own comment.
_EVIDENCE_TABLE_ROW_CAP = 30


def _record_chart_evidence(sheet, chart_type, grain, measure_names, df,
                           query=None, sort=None):
    """Record ONE chart's real, post-filter/sort/top-N/rank dataframe into
    validation_evidence_bridge.REGISTRY, keyed by (dashboard, sheet) so the
    validation pack builder prefers this exact evidence over any reverse-
    engineered guess. `grain`/`measure_names` must already be the sheet's
    real Tableau captions (never an internal engine alias) -- the caller
    (each r_* renderer) is the one place that still has that mapping.

    IDEMPOTENT per (dashboard, sheet): a sheet with no Altair chart (every
    kind this function actually instruments -- map/treemap/table/rank-table)
    gets render_sheet() called on it a SECOND time by
    headless_render.capture_sheet_kpis' own KPI-tile probe, which would
    otherwise re-enter this function and hit REGISTRY.record_chart's
    duplicate-key guard. Checking REGISTRY.get() first makes the first
    (highest-fidelity) recording win outright rather than depending on every
    caller keeping EVIDENCE_DASHBOARD correctly tagged across every possible
    second pass."""
    if not EVIDENCE_CAPTURE or df is None:
        return
    try:
        import validation_evidence_bridge as _veb
    except Exception:
        return
    sheet_name = sheet.get("title") or sheet.get("name") or ""
    try:
        if _veb.REGISTRY.get(_EVIDENCE_DASHBOARD or "", sheet_name) is not None:
            return
        _veb.REGISTRY.record_chart(
            dashboard=_EVIDENCE_DASHBOARD or "",
            sheet=sheet_name,
            chart_type=chart_type,
            grain=list(grain),
            measures=[{"name": m, "field": m} for m in measure_names],
            rows=df,
            query=query,
            sort=list(sort or ()),
        )
    except Exception:
        pass


def set_calcs(c):
    global CALCS
    CALCS = c or {}


def set_aliases(a):
    global ALIASES
    ALIASES = a or {}


def px(caption):
    """Caption -> physical column, resolving workbook RENAMES first
    (caption 'Year' over source column [Date] must query DATE)."""
    _require_field_scope(caption)
    return to_phys(COLMAP.get(caption, caption))


def _require_field_scope(caption):
    if not _ACTIVE_DATASOURCE_ID and caption in IR.get('ambiguous_fields', []):
        raise ValueError(f'A datasource-scoped reference is required for {caption!r}')
    if _ACTIVE_DATASOURCE_ID and caption not in CALCS and caption not in COLMAP:
        scope = IR.get('field_scopes', {}).get(_ACTIVE_DATASOURCE_ID, {})
        if caption in scope.get('ambiguous_captions', []):
            raise ValueError(f'A field ID is required for ambiguous caption {caption!r}')


def _leading_agg_fn(sql):
    """The SQL aggregation function a curated MEASURE_LIBRARY entry's formula
    LEADS with (e.g. 'AVG' from 'AVG(DISCOUNT)'), or None if it isn't a bare
    single-function wrapper. A compound/derived formula (Profit Ratio's
    division) never reaches this in practice -- its caption has no matching
    raw column, so D2's re-check never fires for it -- but this stays honest
    (None, not a wrong guess) rather than assume the leading token means
    anything for a formula shape it wasn't written to parse."""
    m = _re2.match(r"\s*([A-Za-z]+)\(", sql or "")
    return m.group(1).upper() if m else None


def _agg_expr(col, agg):
    a = (agg or "sum").lower()
    if a == "avg":  return f"AVG({col})"
    if a == "min":  return f"MIN({col})"
    if a == "max":  return f"MAX({col})"
    if a == "med":  return f"MEDIAN({col})"
    if a == "cnt":  return f"COUNT({col})"
    if a == "ctd":  return f"COUNT(DISTINCT {col})"
    if a == "stdev": return f"STDDEV({col})"
    if a == "var":  return f"VARIANCE({col})"
    return f"SUM({col})"


PARAM_DEFS = {}     # caption -> default value (from the workbook)
PARAMS = {}         # caption -> CURRENT value (updated by sidebar widgets)
PARAM_DOMAIN = {}   # caption -> allowed values (list-domain params = dropdown)
_ACTIVE_RELATED_ONLY = None   # C9: the one related table the sheet being resolved reads (field_scope)
IR = {}             # the configured IR (dashboards consulted for param usage)


CMAPS = {}          # caption -> {value: #hex} -- Tableau's EXACT mark colors
PALETTE_REFS = {}   # caption -> named palette (no explicit maps)
_FIELD_LOCK = RLock()
_ACTIVE_DATASOURCE_ID = None


@contextmanager
def field_scope(sheet):
    """Bridge legacy caption renderers to one validated datasource catalog.

    Restores all maps even when SQL or chart preparation fails. This adapter
    is transitional: renderers should ultimately consume field IDs directly.
    """
    global CALCS, COLMAP, ALIASES, CMAPS, PALETTE_REFS, _ACTIVE_DATASOURCE_ID, _ACTIVE_RELATED_ONLY
    did = sheet.get('datasource_id')
    # C9: a nested scope (a measure resolved inside its sheet) inherits the sheet's single-related-table reading
    related_only = sheet.get('_related_only') or _ACTIVE_RELATED_ONLY
    if not did or 'field_scopes' not in IR:
        yield
        return
    with _FIELD_LOCK:
        scope = IR['field_scopes'].get(did)
        if scope is None or scope.get('binding_error'):
            raise ValueError((scope or {}).get('binding_error', f'Unknown datasource ID {did}'))
        previous = CALCS, COLMAP, ALIASES, CMAPS, PALETTE_REFS, _ACTIVE_DATASOURCE_ID, _ACTIVE_RELATED_ONLY
        try:
            _ACTIVE_RELATED_ONLY = related_only
            CALCS, COLMAP = dict(scope['calcs']), dict(scope['colmap'])
            ALIASES = scope.get('aliases', {})
            CMAPS = dict(scope.get('color_maps', {}))
            CMAPS.update(sheet.get('color_maps', {}))
            PALETTE_REFS = dict(scope.get('palette_refs', {}))
            PALETTE_REFS.update(sheet.get('palette_refs', {}))
            _ACTIVE_DATASOURCE_ID = did
            for caption, fid in sheet.get('field_bindings', {}).items():
                entry = IR['fields'].get(fid)
                if not entry or entry['datasource_id'] != did:
                    raise ValueError(f'Invalid field reference {fid}')
                if entry.get('unavailable') and not (
                        related_only and related_only in (
                            (entry.get('related') or {}).get('source'), entry.get('uses_related'))):
                    # E04: from a relationship table left out of the data -- unless the sheet reads ONLY that table
                    # (C9: answered from it alone, as Tableau queries only the tables a sheet uses)
                    raise ValueError(entry['unavailable'])
                CALCS.pop(caption, None)
                COLMAP.pop(caption, None)
                if entry.get('calculation'):
                    CALCS[caption] = entry['calculation']
                elif not entry.get('is_calculation'):
                    # a related table's field reads that table's own column (C9 / C12: only when the sheet reads it)
                    COLMAP[caption] = (entry.get('related') or {}).get('column') or entry['physical']
                else:
                    why = entry.get('refusal')
                    raise ValueError(f'Untranslated field {caption}' + (f' ({why})' if why and why != 'not translated'
                                                                         else ''))
            yield
        finally:
            CALCS, COLMAP, ALIASES, CMAPS, PALETTE_REFS, _ACTIVE_DATASOURCE_ID, _ACTIVE_RELATED_ONLY = previous


def configure(ir):
    """One-stop engine configuration from an IR (calcs, aliases, params, colmap)."""
    global PARAM_DEFS, PARAMS, COLMAP, CMAPS, PARAM_DOMAIN, IR
    IR = ir or {}
    # PER-WORKBOOK CLIENT PROFILE (2026-07-21 MVP fix): resolve config.PROFILE
    # for THIS workbook before anything reads MEASURE_LIBRARY/DIM_VALUE_*, so a
    # foreign workbook never silently inherits another client's curated
    # measure SQL/formats/colors (see config.py's profile registry).
    # Scoped migration semantics must never depend on a client filename profile.
    # Retain the inherited unscoped path for its separately tested legacy callers.
    if "field_scopes" in IR:
        import profile_default
        config.PROFILE = profile_default
        calc_translator.set_profile(profile_default)
    else:
        calc_translator.set_profile(config.set_profile(ir.get("source_file")))
    set_calcs(ir.get("calcs", {}))
    set_aliases(ir.get("aliases", {}))
    COLMAP = ir.get("colmap", {}) or {}
    CMAPS = ir.get("color_maps", {}) or {}
    global PALETTE_REFS
    PALETTE_REFS = ir.get("palette_refs", {}) or {}
    PARAM_DEFS = ir.get("params", {}) or {}
    PARAM_DOMAIN = ir.get("param_domains", {}) or {}
    PARAMS = dict(PARAM_DEFS)
    if 'field_scopes' in IR:
        import parameter_actions,sys
        parameter_actions.apply(sys.modules[__name__])


def cat_colors(caption, present):
    """(domain, range) of the exact colors for the values actually present:
    the WORKBOOK's own color map, overridden by any client-profile colors.
    Display aliases applied. (None, None) when unmapped."""
    cmap = dict(CMAPS.get(caption or "", {}))
    cmap.update(getattr(config.PROFILE, "DIM_VALUE_COLORS", {}).get(caption or "", {}))
    if not cmap and caption in PALETTE_REFS:
        # named palette without explicit maps: assign in legend (sorted) order
        pal = TABLEAU_PALETTES.get(PALETTE_REFS[caption],
                                   TABLEAU_PALETTES["tableau-10"])
        vals = sorted(str(v) for v in present)
        cmap = {v: pal[i % len(pal)] for i, v in enumerate(vals)}
    if not cmap:
        return None, None
    labels = value_labels(caption) or {}
    by_label = {}
    for raw, colr in cmap.items():
        by_label[str(_label_of(labels, raw))] = colr
        by_label[str(raw)] = colr
    dom = [v for v in present if str(v) in by_label]
    if not dom:
        return None, None
    return dom, [by_label[str(v)] for v in dom]


def param_value(caption):
    v = PARAMS.get(caption)
    try:
        return float(str(v).strip('"'))
    except (TypeError, ValueError):
        return v


def _param_display_value(v):
    """Human-readable Tableau parameter value (hashes are syntax, not UI)."""
    s = str(v if v is not None else "").strip().strip('"')
    if s.startswith("#") and s.endswith("#") and len(s) > 1:
        s = s[1:-1]
    return s


def sub_params(sql):
    """Replace __PARAM_X__ tokens with the CURRENT parameter values."""
    for cap, v in PARAMS.items():
        tok = param_token(cap)
        if tok in sql:
            sql = sql.replace(tok, param_sql_literal(v))
    return sql


def value_labels(caption):
    """Display labels for a dimension's raw values: the WORKBOOK's own aliases
    first, then any client-profile override. None when no mapping exists."""
    m = dict(ALIASES.get(caption or "", {}))
    m.update(config.PROFILE.DIM_VALUE_LABELS.get(caption or "", {}))
    return m or None


def tbl(s):
    """The physical table this sheet queries (datasource captured by the parser).
    DM2 (E02): in V3's scoped mode a datasource that is not bound refuses -- config.table_for's built-in default
    table is for the inherited unscoped apps only, never a silent stand-in for another workbook's data."""
    import config as _config
    ds = s.get("datasource")
    if ds and "field_scopes" in IR and ds not in _config.DATASOURCES:
        raise ValueError(f"Datasource '{ds}' is not bound to data; this sheet cannot be queried")
    if not ds and "field_scopes" in IR:
        # never the built-in default table (another workbook's data) for a sheet that names no datasource
        raise ValueError("The sheet names no datasource; it cannot be queried")
    return table_for(ds)


def _fmt_guess(cap):
    l = cap.lower()
    if "ratio" in l or "discount" in l or l.endswith("%"):
        return "pct"
    return "float"


def _measure_display_label(cap, fallback=None):
    """A parameter-swap CASE calc's own caption (e.g. a "Metric Selection"
    calc that CASEs on a parameter to let the user pick which measure shows)
    is the WIDGET's name, not something a viewer wants to read as an axis
    title -- Tableau shows the same generic caption by default too, which is
    exactly why real dashboards rename the axis to reflect whatever measure
    is currently plotted. Detect this generically for ANY calc that embeds a
    __PARAM_x__ token (that's what marks a calc as parameter-driven, however
    it's named) and resolve to the CURRENTLY SELECTED parameter value's
    display label instead. Falls back to `fallback` (or the raw caption)
    when the calc isn't parameter-driven or nothing resolves -- never raises,
    never hides a real caption behind a blank label."""
    entry = CALCS.get(cap)
    sql = entry.get("sql", "") if entry else ""
    if sql:
        for pcap in PARAMS:
            if param_token(pcap) in sql:
                raw = str(PARAMS.get(pcap, "")).strip().strip('"')
                labels = value_labels(pcap)
                if labels and raw in labels:
                    return labels[raw]
                if raw:
                    return raw
                break
    return fallback if fallback is not None else cap


def resolve_measure(T, sheet, m):
    if m.get('field_id') and 'fields' in IR:
        entry = IR['fields'].get(m['field_id'])
        if not entry:
            raise ValueError(f"Unknown field ID {m['field_id']}")
        if _ACTIVE_DATASOURCE_ID and entry['datasource_id'] != _ACTIVE_DATASOURCE_ID:
            raise ValueError('Cross-datasource measure requires an explicit relationship plan')
        with field_scope({'datasource_id': entry['datasource_id'],
                          'field_bindings': {m['caption']: m['field_id']}}):
            r = _resolve_measure(T, sheet, m)
    else:
        r = _resolve_measure(T, sheet, m)
    if r and m.get("fmt"):
        r["fmt"] = m["fmt"]        # workbook default-format wins
    return r


def _resolve_measure(T, sheet, m):
    """Resolve a measure ref {caption, agg, count_records?} to {sql, fmt[, window]}.
    Resolution order: count-of-records -> workbook calc -> profile library ->
    physical column. NO silent fallback: unresolvable -> finding + None."""
    cap = m["caption"]
    _require_field_scope(cap)
    agg = m.get("agg")
    if m.get("count_records"):
        return {"sql": "COUNT(*)", "fmt": "num0"}
    if cap in CALCS:
        c = CALCS[cap]
        if c.get("window"):
            return {"sql": c["sql"], "fmt": _fmt_guess(cap), "window": True}
        if c["agg_ready"]:
            return {"sql": c["sql"], "fmt": _fmt_guess(cap)}
        a = _AGGMAP.get((agg or "sum").lower(), "SUM")
        inner = ("DISTINCT " + c["sql"]) if (agg or "").lower() == "ctd" else c["sql"]
        return {"sql": f"{a}({inner})", "fmt": _fmt_guess(cap)}
    key = CAPTION_ALIASES.get(cap, cap)
    if key in MEASURE_LIBRARY:
        entry = MEASURE_LIBRARY[key]
        col = px(cap)
        # SHARATH'S RULING (D2, 2026-08-13): a curated profile entry applies
        # only where the workbook itself declares that SAME aggregation --
        # otherwise the workbook's declared agg wins. Found live: Discount is
        # a RAW COLUMN curated here as AVG(DISCOUNT) (correct on most sheets),
        # but Product Detail Sheet declares agg=sum for it -- the curated
        # entry silently overrode that, so the shipped app showed
        # 0.1/0.2/0.1 while Tableau AND the backend both showed 0.2/0.8/0.4.
        # Only checked when the caption maps onto a REAL physical column --
        # a compound/derived curated measure like "Profit Ratio" has no raw
        # column at all (px("Profit Ratio") matches nothing in the table), so
        # this never second-guesses it; it always uses its own formula.
        if agg and _col_known(T, col):
            curated_fn = _leading_agg_fn(entry["sql"])
            declared_fn = _AGGMAP.get(agg.lower(), "SUM")
            if curated_fn and curated_fn != declared_fn:
                return {"sql": _agg_expr(col, agg),
                        "fmt": entry.get("fmt") or _fmt_guess(cap)}
        return dict(entry)
    col = px(cap)
    if _col_known(T, col):
        fmt = "pct" if ((agg or "").lower() == "avg" and "ratio" in cap.lower()) else _fmt_guess(cap)
        return {"sql": _agg_expr(col, agg), "fmt": fmt}
    findings.record("BLOCKER", sheet, "measure-unresolved",
                    f"Measure '{cap}' is not a workbook calc, not in the client "
                    f"profile, and column {col} does not exist in {T}.")
    return None


def _axis_fmt(fmt):
    """Vega-Lite axis format for a measure format token."""
    if fmt == "pct":
        return "%"
    if fmt in ("cur0", "cur2", "money0", "money2"):
        return "$~s"
    return "~s"


def _axis_obj(fmt):
    """Axis object for a measure format. Money axes read $600B like Tableau,
    not $600G (D3's SI suffix for giga)."""
    f = _axis_fmt(fmt)
    if f == "$~s":
        return alt.Axis(format="$~s", labelExpr='replace(datum.label, "G", "B")')
    return alt.Axis(format=f)


def rdim(caption):
    """Resolve a dimension expression (row-level calc or physical column)."""
    if caption in CALCS and not CALCS[caption]["agg_ready"]:
        return CALCS[caption]["sql"]
    return px(caption)


def flag_dim(s, T, color):
    """Resolve a color DIMENSION to (expr, mode) for the grouped query.
    mode: 'group'  -- plain column / row-level calc: SELECT + GROUP BY
          'agg'    -- aggregate-grain calc (table calc like WINDOW_MAX
                      comparison): SELECT-only, NEVER in GROUP BY
          'window' -- row-grain window (FIXED LOD): base-CTE precompute
          None     -- unresolvable (finding recorded)"""
    if not color or color.get("kind") != "dimension":
        return None, None
    cap = color["caption"]
    cspec = CALCS.get(cap)
    if cspec and cspec.get("agg_ready"):
        return cspec["sql"], "agg"
    expr = rdim(cap)
    if cap not in CALCS and not _col_known(T, expr):
        findings.record("WARNING", s["name"], "color-unresolved",
                        f"Color field '{cap}' could not be resolved "
                        f"(untranslated calc or blend field); rendered uncolored.")
        return None, None
    if " OVER (" in expr:
        return expr, "window"
    return expr, "group"


# Standard named Tableau palettes -- used when a workbook colors a dimension
# by palette NAME without explicit per-value maps (assigned in legend order).
TABLEAU_PALETTES = {
    "tableau-10": ["#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14a",
                   "#edc949", "#af7aa1", "#ff9da7", "#9c755f", "#bab0ac"],
    "tableau-20": ["#4e79a7", "#a0cbe8", "#f28e2b", "#ffbe7d", "#59a14a",
                   "#8cd17d", "#b6992d", "#f1ce63", "#499894", "#86bcb6",
                   "#e15759", "#ff9d9a", "#79706e", "#bab0ac", "#d37295",
                   "#fabfd2", "#b07aa1", "#d4a6c8", "#9d7660", "#d7b5a6"],
    "color-blind-10": ["#1170aa", "#fc7d0b", "#a3acb9", "#57606c", "#5fa2ce",
                       "#c85200", "#7b848f", "#a3cce9", "#ffbc79", "#c8d0d9"],
    "tableau-classic-10": ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
                           "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"],
}

PROFIT_COLORS = ["#4575b4", "#f58518"]
SLATE = "#4a76a1"          # Tableau "Sales bar" slate-blue (from mark-color)
DIVERGING = [[0.0, "#b35806"], [0.25, "#e08214"], [0.5, "#f7f7f7"],
             [0.75, "#4393c3"], [1.0, "#2166ac"]]

STATE_ABBR = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI",
    "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI",
    "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX",
    "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}


def viewer_key():
    """E10: who the cached result belongs to. Hosted: the viewer (row access policies on CURRENT_USER() give different
    rows to different people for the same SQL); local preview: None (one person, local files)."""
    import backend as _backend
    if _backend._active_session() is None:
        return None
    try:
        user = st.user
        for key in ('email', 'user_name', 'login_name', 'name'):      # as v3_store.viewer() reads it
            value = user.get(key) if hasattr(user, 'get') else getattr(user, key, None)
            if value:
                return str(value)
    except Exception:
        pass
    return 'anonymous'


@st.cache_data(ttl=600)
def _q_cached(sql, viewer):
    return run_sql(sql)


def _q_exec(sql):
    return _q_cached(sql, viewer_key())


_q_exec.clear = _q_cached.clear


import re as _re2
import sys

# Lab: a repair proven against Tableau's values and approved by a reviewer is recorded on ONE sheet
# as sheet['query_fix'] (a typed id). While that sheet renders, its aggregate-of-FIXED queries are
# rewritten by the same deterministic transform the proof ran. Every other sheet is unaffected.
# The fix runs the SAME mechanism the cause proof ran: every query the sheet sends goes through
# _run_query; the engine's own query runs first (errors -> hoist and retry as always), then the
# typed transform is tried on it and, when applicable, its result is used. What was proven ships.
QUERY_FIXES = ('lod_key_grain',)
ACTIVE_QUERY_FIX = None
ACTIVE_FIX_RECORD = None      # counters for the sheet being rendered with a fix
ACTIVE_FIX_KEY = None         # the LOD key a proven, Cortex-proposed fix is limited to (None = every key)
LAST_FIX_RENDER = {}          # sheet name -> what the fix did during its most recent render


def _run_query(sql):
    result = _q_exec(sql)
    fix, rec = ACTIVE_QUERY_FIX, ACTIVE_FIX_RECORD
    if fix not in QUERY_FIXES:
        return result
    from query_fixes import transform_for, read_only
    new = transform_for(fix, ACTIVE_FIX_KEY)(sql)
    if not new or new == sql or not read_only(new):
        if rec is not None:
            rec['Queries not applicable'] += 1
        return result
    try:
        out = _q_exec(new)
    except Exception as exc:
        if rec is not None:
            rec['Rewrites failed'] += 1
            rec['Last error'] = f"{type(exc).__name__}: {str(exc)[:200]}"
        findings.record("WARNING", "query-fix", "fix-not-applied", f"{fix}: {type(exc).__name__}")
        return result
    if rec is not None:
        rec['Queries rewritten'] += 1
    return out


_exec_fixed = _run_query      # earlier name, kept for callers and tests


# EXTRACT(MONTH FROM x) / TRIM(BOTH ' ' FROM x) / SUBSTRING(x FROM n): a FROM inside a call, not a FROM clause
_FROM_IN_CALL = _re2.compile(r"\b(?:EXTRACT|TRIM|SUBSTRING|OVERLAY|POSITION)\s*\([^()]*$", _re2.I)


def q(sql):
    # substitute parameter tokens BEFORE the cache key is formed, so changing
    # a what-if parameter in the sidebar re-queries instead of hitting stale cache
    sql = sub_params(sql)
    if "__TBL__" in sql:
        # table-scoped scalar LODs reference the query's own table: resolve
        # the placeholder to the first REAL FROM target in this statement
        # (skipping the placeholder occurrences themselves)
        for m in _re2.finditer(r'\bFROM\s+([A-Za-z0-9_."]+)', sql):
            # C7: never the column of EXTRACT(<part> FROM <column>) / TRIM(.. FROM ..) -- a FROM clause only
            if m.group(1) != "__TBL__" and not _FROM_IN_CALL.search(sql[:m.start()]):
                sql = sql.replace("__TBL__", m.group(1))
                break
    if WIN_ORDER in sql:
        # __WIN_ORDER__ (INDEX() -> ROW_NUMBER() OVER (__WIN_ORDER__), see
        # calc_translator.py) is normally filled in by _windowize() with the
        # sheet's real ORDER BY -- but that is an OPT-IN helper only some
        # chart-kind renderers call. A table-kind sheet using INDEX() never
        # goes through it, so the raw, unfillable placeholder reached
        # Snowflake as literal SQL text: "syntax error ... unexpected
        # '__WIN_ORDER__'". Found live 2026-09-10 (real "GOLD KR Dashboard"
        # "[Weekly] Raw Data Extract"). Fall back to a safe, always-valid
        # order here -- the correct ordering for THAT sheet is a real
        # improvement _windowize's caller list should eventually cover, but
        # sending Snowflake a token it can never resolve is strictly worse
        # than an arbitrary-but-valid row order.
        sql = sql.replace(WIN_ORDER, "ORDER BY 1")
    if "__TC_ORDER" in sql:
        # C6: LOOKUP / RUNNING_* / FIRST / LAST need the sheet's addressing, which only the exact plan supplies
        # (v3_sheet_plan.window_sql). Never a guessed order: refuse with the reason.
        raise ValueError("A table calculation (LOOKUP / RUNNING_* / FIRST / LAST) needs the sheet's "
                         "addressing; it is computed on the exact plan path only")
    try:
        return _run_query(sql)
    except Exception as e:
        # A live-connected table this pipeline does not own (and so never
        # normalized) can have QUOTED, case-preserving columns -- "Country",
        # not COUNTRY. EVERY column reference this app builds -- rdim/px,
        # a baked-in calc_translator.py CASE (which has no live-session
        # access at all, so it can never check this itself), a measure
        # library entry -- assumes plain unquoted UPPER_SNAKE, true for every
        # table this pipeline creates but not one someone else built. Found
        # live 2026-09-10 (real "Sales Dashboard Ver 01" workbook, table
        # SANDBOX.BI.VIZLEAP_SALES): every KPI tile failed with
        # SnowparkSQLException "invalid identifier 'COUNTRY'" etc. Fixing
        # this in rdim()/px() alone would still miss the calc_translator-baked
        # SQL (SUM(QUANTITY), SUM(TOTALSALES), ... inside the workbook's own
        # "Metric Selection" swap calc) -- so this is fixed ONCE, here, the
        # single chokepoint every query this app ever runs passes through,
        # regardless of which function built it.
        if _is_invalid_identifier_error(e):
            cur = sql
            for _ in range(20):     # one bad identifier fixed per pass
                fixed = _case_correct_identifier(cur, e)
                if not fixed or fixed == cur:
                    break
                cur = fixed
                try:
                    return _run_query(cur)
                except Exception as e2:
                    if not _is_invalid_identifier_error(e2):
                        raise
                    e = e2
            raise
        # aggregate-of-FIXED: AGG( ... win OVER (PARTITION BY ..) ... ) is
        # illegal inline -- hoist row-grain windows into base subqueries and
        # retry. Innermost-first, one layer per pass (window-in-window chains
        # need several). Deterministic rewrite of our own generated shape.
        if not _is_win_in_agg_error(e):
            raise
        cur = sql
        for _ in range(3):
            cur = _hoist_windows(cur, _LOD_WHERE.get())
            if not cur:
                raise
            try:
                return _run_query(cur)
            except Exception as e2:
                if not _is_win_in_agg_error(e2):
                    raise
        raise


def _is_win_in_agg_error(e):
    """True for the 'window function inside an aggregate' compile error --
    DuckDB and Snowflake word it differently."""
    m = str(e).lower()
    return ("cannot contain window function" in m               # DuckDB
            or "may not appear inside an aggregate" in m)       # Snowflake


_INVALID_IDENT_RE = _re2.compile(r"invalid identifier ['\"]([^'\"]+)['\"]", _re2.I)
_FROM_JOIN_TABLE_RE = _re2.compile(r'\b(?:FROM|JOIN)\s+([A-Za-z0-9_."]+)', _re2.I)


def _is_invalid_identifier_error(e):
    """True for Snowflake's/DuckDB's 'invalid identifier' compile error --
    the signature of a column reference this app built (uppercase, unquoted)
    not matching a live table's real (possibly quoted, case-preserving)
    column."""
    return bool(_INVALID_IDENT_RE.search(str(e)))


def _case_correct_identifier(sql, e):
    """A column reference that failed to resolve, case-corrected against
    every table this statement's FROM/JOIN clauses actually name -- or None
    if no case-insensitive match exists anywhere (a genuinely missing
    column; the caller re-raises the original error unchanged).

    Deliberately narrow: only ever substitutes the ONE identifier Snowflake/
    DuckDB just named as invalid, never guesses at others pre-emptively --
    a query with several mismatched columns gets fixed one real compile
    error at a time, each one independently confirmed bad before being
    touched."""
    m = _INVALID_IDENT_RE.search(str(e))
    if not m:
        return None
    bad = m.group(1).split(".")[-1].strip('"').upper()    # drop a table alias
    real_cols = {}                                          # UPPER -> real case
    for tm in _FROM_JOIN_TABLE_RE.finditer(sql):
        t = tm.group(1)
        if t.upper() == "__TBL__":
            continue
        for c in table_columns(t):
            real_cols.setdefault(c.upper(), c)
    real = real_cols.get(bad)
    if not real or real == bad:      # no match, or already correctly-cased
        return None
    return _quote_replace_identifier(sql, bad, real)


def _quote_replace_identifier(sql, bad_upper, real_name):
    """Replace every bare, unquoted, word-boundary occurrence of bad_upper
    with the quoted, case-correct real_name -- skipping single- and double-
    quoted spans entirely, so a STRING LITERAL that happens to spell the
    same word (a filter value, a CASE branch's comparison target) is never
    touched, only an actual column reference."""
    pat = _re2.compile(r"\b" + _re2.escape(bad_upper) + r"\b")
    out, i, n, quote_char = [], 0, len(sql), None
    while i < n:
        c = sql[i]
        if quote_char is not None:
            out.append(c)
            if c == quote_char:
                if i + 1 < n and sql[i + 1] == quote_char:  # doubled = escaped
                    out.append(sql[i + 1])
                    i += 2
                    continue
                quote_char = None
            i += 1
            continue
        if c in "'\"":
            quote_char = c
            out.append(c)
            i += 1
            continue
        m = pat.match(sql, i)
        if m:
            out.append(f'"{real_name}"')
            i = m.end()
            continue
        out.append(c)
        i += 1
    return "".join(out)


_WIN_EXPR = _re2.compile(
    r"\b(SUM|AVG|MIN|MAX|COUNT|MEDIAN|STDEV|VAR)\s*\(", _re2.I)


def scan_row_windows(s, start=0, alias="__W{}__"):
    """Every ROW-GRAIN window call in `s` -- AGG(row expression) OVER (...), the SQL of a FIXED LOD -- replaced by
    an alias. Returns (text, [(window expression, alias)]); aliases are numbered from `start`. Agg-grain windows
    (MAX(SUM(x)) OVER (), table calculations) stay inline; of nested windows only the innermost is taken."""
    hoisted, i = [], start
    out, pos = s, 0
    while True:
        w = _WIN_EXPR.search(out, pos)
        if not w:
            return out, hoisted
        op = out.index("(", w.end() - 1)
        end = _match_paren_engine(out, op)
        if end < 0:
            return out, hoisted
        rest = out[end:]
        om = _re2.match(r"\s*OVER\s*\(", rest, _re2.I)
        if not om:
            pos = w.end()       # step INTO this call: nested windows
            continue            # (AVG(.. MIN(x) OVER (..) ..)) must match
        oend = _match_paren_engine(out, end + om.end() - 1)
        if oend < 0:
            return out, hoisted
        body = out[op + 1:end - 1]
        over = out[end + om.end():oend - 1]     # a FIXED keyed on a FIXED calc: window in the PARTITION BY
        if _re2.search(r"\bOVER\s*\(", body + " " + over, _re2.I):
            pos = w.end()       # window-in-window: hoist the INNER one
            continue            # this pass; outer goes on the next layer
        if _WIN_EXPR.search(body):
            pos = oend          # agg-grain window (MAX(SUM(x)) OVER ()):
            continue            # stays inline, must NOT move to row grain
        expr = out[w.start():oend]
        name = alias.format(i)
        i += 1
        hoisted.append((expr, name))
        out = out[:w.start()] + name + out[oend:]
        pos = w.start() + len(name)


# C17: the WHERE the sheet being rendered computes its FIXED LODs under (its context / data source conditions,
# Tableau's order) -- set by _render_sheet_impl around the handler call; None = no sheet (the old hoist).
# A ContextVar, so concurrent viewer sessions (threads) never see each other's scope.
import contextvars as _cv
_LOD_WHERE = _cv.ContextVar("v3_lod_where", default=None)


def lod_where_now():
    return _LOD_WHERE.get()


@contextmanager
def lod_scope(where):
    token = _LOD_WHERE.set(where)
    try:
        yield
    finally:
        _LOD_WHERE.reset(token)


def _hoist_windows(sql, lod_where=None):
    """C17: with lod_where (a sheet's context WHERE, '' for none) the windows are computed under it and the
    statement's own WHERE applies outside them -- FIXED before dimension filters. None = the old shape.
    Rewrite `SELECT ..., AGG(..win OVER (PARTITION BY..)..) .. FROM T
    [WHERE w] GROUP BY ..` so each row-grain window computes in a base
    subquery: FROM (SELECT *, win AS __W<i>__ FROM T WHERE w) t. Only
    handles the single-table shape the engine generates; returns None when
    the SQL doesn't match (caller re-raises the original error)."""
    # split on TOP-LEVEL (paren-depth-0) FROM / WHERE / GROUP BY only --
    # naive regex grabbed the GROUP BY inside a top-N IN-subquery and
    # truncated the WHERE mid-subquery (unbalanced parens, syntax error)
    kws = _tl_keywords(sql)
    from_kw = next((k for k in kws if k[0] == "FROM"), None)
    if not from_kw:
        return None
    fpos = from_kw[1]
    head = sql[:fpos]
    rest_start = fpos + 4
    rm = _re2.match(r"\s*", sql[rest_start:])
    tstart = rest_start + rm.end()
    if sql[tstart] == "(":
        close = _match_paren_engine(sql, tstart)
        if close < 0:
            return None
        am = _re2.match(r"\s*(\w+)", sql[close:])
        tend = close + (am.end() if am else 0)
    else:
        tm = _re2.match(r"[A-Za-z0-9_.\"]+", sql[tstart:])
        if not tm:
            return None
        tend = tstart + tm.end()
    T = sql[tstart:tend]
    where_kw = next((k for k in kws if k[0] == "WHERE" and k[1] >= tend), None)
    # tail = first top-level clause AFTER the where (GROUP BY / ORDER BY /
    # LIMIT) -- must stay OUTSIDE the row subquery (LIMIT inside would cap
    # raw rows, not result rows)
    tail_kw = next((k for k in kws
                    if k[0] in ("GROUP BY", "ORDER BY", "LIMIT")
                    and k[1] >= (where_kw[1] if where_kw else tend)), None)
    tpos = tail_kw[1] if tail_kw else len(sql)
    where = sql[where_kw[1]:tpos].strip() if where_kw else ""
    tail = sql[tpos:].strip()
    # find each window call FN(body) OVER (spec) and alias it out.
    # Alias numbering CONTINUES past any __W<i>__ already present from a
    # previous hoist layer -- reusing __W0__ made DATEDIFF(__W0__, __W0__)
    # silently return 0 (the wrong-number class, caught by user screenshot).
    taken = [int(n) for n in _re2.findall(r"__W(\d+)__", sql)]
    hoisted = []
    def _scan(s):
        out, found = scan_row_windows(s, (max(taken) + 1 if taken else 0) + len(hoisted))
        hoisted.extend(found)
        return out
    new_head = _scan(head)
    if not hoisted:
        return None
    inner_sel = ", ".join(f"{e} AS {a}" for e, a in hoisted)
    if lod_where is not None and " ".join(where.split()) != " ".join(lod_where.split()):
        return (f"{new_head} FROM (SELECT *, {inner_sel} FROM {T} {lod_where}) t {where} "
                f"{tail}").strip()
    return (f"{new_head} FROM (SELECT *, {inner_sel} FROM {T} {where}) t "
            f"{tail}").strip()


_TL_KW = _re2.compile(r"\b(FROM|WHERE|GROUP BY|ORDER BY|LIMIT)\b", _re2.I)


def _tl_keywords(sql):
    """[(KEYWORD, pos)] for SQL keywords at paren-depth 0 (outside every
    subquery), in order. String literals are rare in our generated SQL and
    never contain these keywords uppercase-standalone."""
    out, depth, i = [], 0, 0
    while i < len(sql):
        c = sql[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif depth == 0:
            m = _TL_KW.match(sql, i)
            if m and (i == 0 or not (sql[i-1].isalnum() or sql[i-1] == "_")):
                out.append((m.group(1).upper(), i))
                i = m.end()
                continue
        i += 1
    return out


def _match_paren_engine(s, i):
    depth = 0
    for j in range(i, len(s)):
        if s[j] == "(":
            depth += 1
        elif s[j] == ")":
            depth -= 1
            if depth == 0:
                return j + 1
    return -1


@st.cache_data(ttl=600)
def table_columns(T=ORDERS):
    try:
        return list(q(f"SELECT * FROM {T} LIMIT 0").columns)
    except Exception:
        return []


def _col_known(T, col):
    """True if `col` (an UPPER_SNAKE guess from px()/to_phys()) names a REAL
    column of T -- checked case-INSENSITIVELY, not just via `col in
    table_columns(T)`.

    THE GAP this closes: q()'s self-heal (_case_correct_identifier) only
    ever runs AFTER a query is actually sent and Snowflake rejects it -- but
    every one of these 7 call sites is a PRE-FLIGHT existence check that
    decides whether to build a query AT ALL. A live-connected table's real,
    case-preserving column ("EmpID", not EMPID) fails the case-SENSITIVE
    `in table_columns(T)` test here and gets refused before a query is ever
    attempted, so q() never gets the chance to fix it. Found live
    2026-09-10 sweeping the real corpus: dozens of measures/dimensions
    across Human Resource Dashboard, GOLD KR Dashboard, Customer Opt-In/Out
    Analysis and others reported "column X does not exist" / "dimension
    skipped" even though the column genuinely exists, just under different
    case.

    Deliberately returns only True/False, not the corrected name: the
    caller still builds SQL with the plain uppercase guess, and q()'s
    self-heal corrects it at execution time -- one mechanism, not two."""
    cols = table_columns(T)
    if col in cols:
        return True
    upper = col.upper()
    return any(c.upper() == upper for c in cols)


def fmt_val(v, fmt):
    try:
        if v is None or pd.isna(v):   # NaN rendered as 'nan%' looked broken --
            return "–"                # Tableau hides the empty-direction delta
    except (TypeError, ValueError):
        pass
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)                 # text column in a detail list -> as-is
                                      # (was ValueError: float('Social'))
    if fmt == "pct":
        return f"{v:.1%}"
    if fmt == "cur0":
        return ("-" if v < 0 else "") + f"${abs(v):,.0f}"
    if fmt == "cur2":
        return ("-" if v < 0 else "") + f"${abs(v):,.2f}"
    if fmt == "num0":
        return f"{v:,.0f}"
    if fmt == "num2":
        return f"{v:,.2f}"
    if fmt == "money2":
        return ("-" if v < 0 else "") + f"${abs(v):,.2f}"
    if fmt in ("money0", "int"):
        sign = "-" if v < 0 else ""
        a = abs(v)
        s = f"{a/1_000_000:.2f}M" if a >= 1_000_000 else (f"{a/1_000:.1f}K" if a >= 10_000 else f"{a:,.0f}")
        return (sign + "$" + s) if fmt == "money0" else (sign + s)
    return f"{v:,.2f}"


def fmt_tableau(v, measure, resolved=None):
    """Format a KPI label value using Tableau's raw authored pattern.

    The ordinary renderer uses compact format tokens. Customized KPI labels
    need the actual prefix, scaling and arrow convention (for example
    ``PHP 2,603.1K`` and ``▼13.11%``), all of which are present in the XML.
    """
    pattern = str((measure or {}).get("tableau_format") or "")
    try:
        number = float(v)
    except (TypeError, ValueError):
        return fmt_val(v, (measure or {}).get("fmt")
                       or (resolved or {}).get("fmt") or "num2")
    if pd.isna(number):
        return "—"
    # Release 27 (F1): the authored pattern itself (sections, literals, glyphs, display units, rounding)
    from tableau_format import format_number
    shown = format_number(number, pattern) if pattern else None
    if shown is not None:
        return shown.strip()
    if "%" in pattern:
        decimals = 2 if "0.00%" in pattern else (1 if "0.0%" in pattern else 0)
        arrow = ""
        if "▲" in pattern and "▼" in pattern:
            arrow = "▲" if number >= 0 else "▼"
        elif "▲" in pattern:
            arrow = "▲"
        elif "▼" in pattern:
            arrow = "▼"
        shown = abs(number) if arrow else number
        return f"{arrow}{shown:.{decimals}%}"
    prefix_match = _re2.match(r'\s*[cC]\"([^\"]*)\"', pattern)
    prefix = prefix_match.group(1) if prefix_match else ""
    suffix, scale = "", 1.0
    if "K" in pattern:
        suffix, scale = "K", 1_000.0
    elif "M" in pattern:
        suffix, scale = "M", 1_000_000.0
    decimals = (2 if ".00" in pattern else 1 if ".0" in pattern else 0)
    if pattern:
        sign = "-" if number < 0 else ""
        return f"{sign}{prefix}{abs(number) / scale:,.{decimals}f}{suffix}"
    return fmt_val(number, (measure or {}).get("fmt")
                   or (resolved or {}).get("fmt") or "num2")


def _alt_color_scale(scale, fmt=None):
    """Translate a parsed color scale (palette + domain + custom colors) to Altair."""
    if not scale:
        return alt.Scale(scheme="redblue")
    dom = [scale["min"], scale["max"]] if ("min" in scale and "max" in scale) else None
    if scale.get("colors"):
        rng = scale["colors"]
        return alt.Scale(domain=dom, range=rng, clamp=True) if dom else alt.Scale(range=rng)
    pal = (scale.get("palette") or "").lower()
    scheme = "redblue"
    if "red_black" in pal or "red-black" in pal:
        scheme = "redgrey"
    elif "orange" in pal and "blue" in pal:
        scheme = "orangeblue"
    elif "green" in pal:
        scheme = "greens"
    if dom:
        return alt.Scale(scheme=scheme, domain=dom, clamp=True)
    return alt.Scale(scheme=scheme, domainMid=0)


_EXTRACT_PART = {"yr": "YEAR", "tyr": "YEAR", "qr": "QUARTER", "tqr": "QUARTER",
                 "tdy": "DAY",
                 "mn": "MONTH", "tmn": "MONTH", "wk": "WEEK", "twk": "WEEK",
                 "dy": "DAY", "mdy": "DAY"}


def _part_num(v, part):
    """A date-part filter member is stored EITHER as the part number ('2000')
    OR as a full date ('2000-01-01') -- Tableau writes whichever the source
    column holds. Return the integer part value for the EXTRACT comparison, or
    None if it cannot be resolved (skip rather than crash). Fixes the
    'could not convert string to float: 2000-01-01' render break on any
    date-part member filter whose members are full dates (field-found on World
    Indicators Year filter)."""
    s = str(v).strip().strip("#").strip()     # C10: Tableau's own date literal '#2023-07-28 00:00:00#'
    if _re2.fullmatch(r"-?\d+(\.\d+)?", s):
        return int(float(s))
    d = pd.to_datetime(s, errors="coerce")
    if pd.isna(d):
        return None
    return {"YEAR": d.year, "QUARTER": (d.month - 1) // 3 + 1, "MONTH": d.month,
            "WEEK": int(d.isocalendar()[1]), "DAY": d.day,
            "YEARMONTH": d.year * 100 + d.month}.get(part)


def _part_sql(part, pc):
    """SQL for a filter's date part. YEARMONTH is Tableau's discrete Month/Year ('my:', members yyyymm)."""
    if part == "YEARMONTH":
        return f"(EXTRACT(YEAR FROM {pc}) * 100 + EXTRACT(MONTH FROM {pc}))"
    return f"EXTRACT({part} FROM {pc})"


_RANK_GATE_RE = _re2.compile(
    r"^\s*(?:ROW_NUMBER|RANK|DENSE_RANK)\s*\(\s*\)\s*OVER\s*\(\s*"
    + _re2.escape(WIN_ORDER) + r"\s*\)\s*<=\s*(\d+)\s*$", _re2.I)


def _rank_gate_n(sql):
    """N when `sql` is a view-order rank gate -- Tableau's INDEX()<=N (and the
    RANK()<=N family), which the translator emits with an UNRESOLVED view
    order placeholder. None for anything else (never guess at a window)."""
    m = _RANK_GATE_RE.match(str(sql or ""))
    return int(m.group(1)) if m else None


def _first_dim_caption(s):
    """The sheet's ranking dimension when no sort names one."""
    for k in ("dim", "y", "x", "panel"):
        v = s.get(k)
        if isinstance(v, dict) and v.get("caption") and not v.get("agg"):
            return v["caption"]
        if isinstance(v, str) and v:
            return v
    for k in ("y_dims", "x_dims", "dims"):
        v = s.get(k) or []
        if v:
            d = v[-1]
            return d["caption"] if isinstance(d, dict) else d
    return None


def measure_sql_for(T, caption, agg):
    """Aggregated SQL for a sort/rank measure, via the normal resolution chain
    (workbook calc -> profile library -> physical column). None if unresolvable
    -- the caller reports and refuses rather than inventing an ordering."""
    # pre-check so an unresolvable sort key does NOT record a measure BLOCKER
    # against a phantom "(sort)" sheet; the caller reports it in context
    if (caption not in CALCS
            and CAPTION_ALIASES.get(caption, caption) not in MEASURE_LIBRARY
            and not _col_known(T, px(caption))):
        return None
    r = _resolve_measure(T, "(sort)", {"caption": caption, "agg": agg})
    if not r or r.get("window"):
        return None                 # a windowed sort key is not a rankable order
    return r["sql"]


_REL_PERIODS = {"year", "quarter", "month", "week", "day"}


def _relative_date_predicate(f, week_start="sunday"):
    """C3: Tableau relative-date filter -> date window. Whole periods [first, last] around the anchor (anchor-date,
    else CURRENT_DATE): >= start of (anchor's period + first), < start of (anchor's period + last + 1); range-type
    'todate' = from the start of the anchor's period through the anchor day. include-null keeps NULL dates. None when
    the filter cannot be expressed (unknown period, no offsets) -- the caller reports it."""
    period = (f.get("period") or "").lower()
    if period not in _REL_PERIODS:
        return None
    col = px(f["caption"])
    anchor = f"DATE '{f['anchor']}'" if f.get("anchor") and _re2.fullmatch(r"\d{4}-\d{2}-\d{2}", f["anchor"]) \
        else "CURRENT_DATE"
    start = f"DATE_TRUNC('{period}', {anchor})"
    if period == "week":                  # SQL weeks start on Monday; Tableau's on the datasource's first day of week
        import v3_sheet_plan as _P
        start = _P._date_sql(anchor, "week_trunc", week_start)
    rng = (f.get("range") or "").lower()
    if rng == "todate":
        cond = f"{col} >= {start} AND {col} < DATEADD(day, 1, {anchor})"
    else:
        first, last = f.get("first"), f.get("last")
        if first is None or last is None:
            n = f.get("n")
            try:
                n = int(n) if n is not None else None
            except (TypeError, ValueError):
                n = None
            if rng in ("current", "this"):
                first = last = 0
            elif rng in ("last", "previous"):
                first = last = -1
            elif rng == "next":
                first = last = 1
            elif rng == "lastn" and n:
                first, last = -(n - 1), 0
            elif rng == "nextn" and n:
                first, last = 0, n - 1
            else:
                return None
        cond = (f"{col} >= DATEADD({period}, {int(first)}, {start}) AND "
                f"{col} < DATEADD({period}, {int(last) + 1}, {start})")
    return f"(({cond}) OR {col} IS NULL)" if f.get("include_null") else f"({cond})"


def is_measure_filter(f):
    """E06: a quantitative filter on an AGGREGATED measure (sum:/avg:/... or an aggregate calculation) is a measure
    filter -- Tableau applies it after aggregation at the view's level of detail (HAVING), never as a row WHERE."""
    if f.get("kind") != "range":
        return False
    if f.get("measure"):
        return True
    return f.get("agg") == "usr" and bool((CALCS.get(f.get("caption")) or {}).get("agg_ready"))


_MF_NULL, _MF_SEP = "'~^NULL^~'", "'~^~'"


def _measure_filter_predicate(s, f, T, sub_where):
    """(predicate, reason) for one measure filter. The view's level of detail comes from the sheet's plan
    (v3_sheet_plan.from_sheet): one null-safe text key per mark; the subquery applies the same filters the view
    does (sub_where) and keeps the keys whose aggregate passes. No dimensions = one mark (EXISTS). A
    disaggregated view tests each row's own value. Numeric bounds use a 1e-12 relative tolerance: Tableau saves
    the data's own min/max as slider bounds, and a sum in another order differs in the last float digits."""
    import sys
    import v3_sheet_plan as P
    me = sys.modules[__name__]
    try:
        plan = P.from_sheet(s, IR)
        scope = P.bind_fields(plan, s, me)
    except Exception:
        return None, "the sheet's level of detail could not be planned"
    with field_scope(scope):
        return _measure_filter_sql(s, f, T, sub_where, plan, P)


def _measure_filter_sql(s, f, T, sub_where, plan, P):
    wide = dict(f)
    for key, sign in (("min", -1), ("max", 1)):
        v = f.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            wide[key] = v + sign * 1e-12 * max(1.0, abs(v))
    if not plan.get("aggregate", True):
        if f.get("agg") == "usr":
            return None, "an aggregate calculation in a disaggregated view"
        return _value_predicate(f, expr=rdim(f["caption"])), ""
    ref = {"caption": f["caption"], "agg": None if f.get("agg") in (None, "usr", "none", "attr") else f["agg"]}
    if f.get("field_id"):
        ref["field_id"] = f["field_id"]
    r = resolve_measure(T, s.get("name"), ref)
    if not r or r.get("window"):
        return None, "the measure did not translate"
    pred = _value_predicate(wide, expr=f"({r['sql']})")
    if not pred:
        return None, ""
    dims, _m = P.query_fields(plan)
    if not dims:
        return f"EXISTS (SELECT 1 FROM {T} {sub_where} HAVING {pred})", ""
    keys = [f"COALESCE(CAST({P._date_sql(rdim(d['caption']), d['date_part'], plan.get('week_start', 'sunday'), d.get('fiscal', 1))}"
            f" AS VARCHAR), {_MF_NULL})" for d in dims]
    key = f" || {_MF_SEP} || ".join(keys)
    return f"{key} IN (SELECT {key} FROM {T} {sub_where} GROUP BY 1 HAVING {pred})", ""


def _member_literal(v):
    """A filter member as SQL. Tableau's own date literal '#2023-07-28 00:00:00#' is a TIMESTAMP (C10; compared as
    text it never matched a date column -- or failed the cast); every other member is a quoted string."""
    m = _re2.fullmatch(r"#(\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2}:\d{2}(?:\.\d+)?)?)#", str(v).strip())
    if m:
        return f"CAST('{m.group(1)}' AS TIMESTAMP)"
    return "'" + _q1(str(v)) + "'"


def _value_predicate(f, expr=None):
    """SQL predicate for a FIXED-VALUE filter -- categorical IN / EXCLUDE,
    quantitative range, date-part member/range -- date-part aware. None if the
    filter carries no constrainable value or isn't a value filter (calc /
    top-N / relative-date are handled elsewhere).

    SINGLE SOURCE OF TRUTH: used both by _apply_sheet_filters' main loop (the
    outer WHERE) and by _context_cond (a context filter injected into the top-N
    ranking subquery), so the two can never drift on how a range / date-part /
    exclude filter becomes SQL."""
    pc = expr or px(f["caption"])
    k = f.get("kind")
    part = _EXTRACT_PART.get(f.get("datepart") or "") or ("YEARMONTH" if f.get("datepart") == "my" else None)
    if k == "range":
        if f.get("nulls") == "only":
            return f"{pc} IS NULL"
        conds = []
        if part:                              # date-part range: bounds are part numbers
            for key, op in (("min", ">="), ("max", "<=")):
                v = f.get(key)
                pv = _part_num(v, part) if v is not None else None
                if pv is not None:
                    conds.append(f"{_part_sql(part, pc)} {op} {pv}")
        else:
            lit = lambda v: (f"'{v}'" if isinstance(v, str) else f"{v}")
            if f.get("min") is not None:
                conds.append(f"{pc} >= {lit(f['min'])}")
            if f.get("max") is not None:
                conds.append(f"{pc} <= {lit(f['max'])}")
        body = " AND ".join(conds)
        if f.get("nulls") == "include":          # in-range-or-null (E06)
            return f"({body} OR {pc} IS NULL)" if body else None
        if f.get("nulls") == "exclude" and not body:   # non-null
            return f"{pc} IS NOT NULL"
        return body or None
    if k == "in" and f.get("values"):
        if part:                              # date-part members: numbers, not raw dates
            pvs = [_part_num(v, part) for v in f["values"]]
            nums = ", ".join(str(x) for x in pvs if x is not None)
            return f"{_part_sql(part, pc)} IN ({nums})" if nums else None
        keep = [v for v in f["values"] if str(v).lower() != "%null%"]
        clause = []
        if len(keep) != len(f["values"]):
            clause.append(f"{pc} IS NULL")
        if keep:
            vals = ", ".join(_member_literal(v) for v in keep)
            clause.append(f"{pc} IN ({vals})")
        return ("(" + " OR ".join(clause) + ")") if clause else None
    if k == "not_in" and f.get("values"):
        drop = [v for v in f["values"] if str(v).lower() != "%null%"]
        conds = []
        if len(drop) != len(f["values"]):
            conds.append(f"{pc} IS NOT NULL")
        if drop and part:                     # date-part members: numbers, not raw dates (DSF)
            nums = ", ".join(str(x) for x in (_part_num(v, part) for v in drop) if x is not None)
            if nums:
                conds.append(f"{_part_sql(part, pc)} NOT IN ({nums})")
        elif drop:
            vals = ", ".join(_member_literal(v) for v in drop)
            conds.append(f"{pc} NOT IN ({vals})")
        return " AND ".join(conds) or None
    if k == "ord_range" and f.get("datepart"):
        p2 = _EXTRACT_PART.get(f["datepart"])
        conds = []
        if p2 and f.get("from"):
            conds.append(f"EXTRACT({p2} FROM {pc}) >= {f['from']}")
        if p2 and f.get("to"):
            conds.append(f"EXTRACT({p2} FROM {pc}) <= {f['to']}")
        return " AND ".join(conds) or None
    return None


def _context_cond(f):
    """A context filter's own captured value as a SQL predicate for injecting
    into a top-N ranking subquery (and, C5, the rows a FIXED LOD is computed
    over). Covers ranges + date-parts via the shared _value_predicate. A filter
    on a ROW-LEVEL calculation is its predicate, exactly as in the sheet's own
    filter loop: a boolean gate is the calculation itself (NOT for 'false'),
    IN / NOT IN compares the calculation's value -- never the caption as a
    column name. An aggregate / table-calc filter cannot be a row context: None."""
    cspec = CALCS.get(f.get("caption"))
    if cspec:
        if cspec.get("agg_ready") or cspec.get("window"):
            return None
        vals = {str(v).lower() for v in f.get("values", [])}
        if f.get("kind") == "in" and vals and vals <= {"true", "false"}:
            return {frozenset({"true"}): f"({cspec['sql']})",
                    frozenset({"false"}): f"NOT ({cspec['sql']})"}.get(frozenset(vals))
        return _value_predicate(f, expr=f"({cspec['sql']})")
    return _value_predicate(f)


def context_conditions(s, governed=None, dash_parts=None):
    """The sheet's CONTEXT predicates (context filters, data source filters, and the dashboard value of a
    context column a control governs) -- the rows Tableau computes top N and FIXED LODs over (C5)."""
    governed = governed or set()
    # CONTEXT filters (Tableau order of ops: applied BEFORE top-N). Build their
    # effective predicate so the top-N ranking subquery ranks WITHIN the
    # context -- 'top 10 customers in Central', not 'the global top 10 shown
    # for Central only'. A context col governed by a dashboard widget takes the
    # widget's live value (dash_parts clause); an ungoverned one uses its own
    # captured members.
    context_cols = {px(f["caption"]) for f in s.get("applied_filters", [])
                    if f.get("context")}
    # context filters that enumerate ALL members are skipped by applied_filters
    # (their live value comes from a dashboard widget); context_fields lists
    # those columns so the dashboard's value still reaches the ranking subquery.
    context_cols |= {px(c) for c in s.get("context_fields", [])}
    context_conds = []
    for p in (dash_parts or []):
        if isinstance(p, dict) and p.get("col") in context_cols and p.get("clause"):
            context_conds.append(p["clause"])
    for f in s.get("applied_filters", []):
        # a DATA SOURCE filter is never replaced by a dashboard control (Tableau applies it first, always)
        if f.get("context") and (px(f["caption"]) not in governed or f.get("scope") == "datasource"):
            c = _context_cond(f)
            if c:
                context_conds.append(c)
    return context_conds


def _apply_sheet_filters(s, where, T, governed=None, dash_parts=None):
    if s.get("worksheet_semantics", {}).get("projection", {}).get("renderer") in {"scatter", "dual", "crosstab"}:
        from visual_plan_common import apply_filters
        import sys
        return apply_filters(sys.modules[__name__], s, where, governed)
    """AND a sheet's captured fixed filter VALUES (range / IN / ordinal) onto the
    dashboard WHERE. Date-PART member filters (MONTH(x) IN (4)) use EXTRACT --
    never compared against the raw column. Skipped filters are recorded.

    `governed` = physical columns already controlled by a DASHBOARD filter
    widget (build_where). A sheet's own fixed filter on such a column is
    SUPPRESSED: in Tableau a dashboard quick-filter and a worksheet/context
    filter on the SAME field are ONE filter surfaced as a control, not two
    that intersect. Applying both AND'd them -- e.g. a 'Region Context Filter'
    sheet carrying a saved `REGION IN ('Central')` plus the dashboard's own
    Region widget set to East yielded `REGION='East' AND REGION IN ('Central')`
    = always empty, so the sheet rendered blank for every region except the
    one the workbook happened to be saved on (Central). The dashboard control
    is the single source of truth for its column.

    `dash_parts` = the dashboard filter parts that apply to this sheet. Used to
    resolve the effective value of a CONTEXT filter that a dashboard widget
    governs, so it can be injected into the top-N ranking subquery (Tableau
    applies context filters BEFORE top-N)."""
    avail = table_columns(T)
    governed = governed or set()
    context_conds = context_conditions(s, governed, dash_parts)
    context_where = ("WHERE " + " AND ".join(context_conds)) if context_conds else ""
    cons = []
    view_order_filters = []          # INDEX()/RANK() gates -- applied LAST (below)
    measure_filters = []             # aggregate filters -- after dimension filters, before table calcs (E06)
    for f in s.get("applied_filters", []):
        pc = px(f["caption"])
        # A dashboard filter widget already governs this column -- do NOT also
        # apply the sheet's saved value for it (Tableau treats them as one).
        # A DATA SOURCE filter is the exception: it always applies (DSF).
        if pc in governed and f.get("scope") != "datasource":
            findings.record("INFO", s["name"], "filter-governed-by-dashboard",
                            f"Filter on '{f['caption']}' is controlled by the "
                            "dashboard's own filter widget; the sheet's saved "
                            "value is not additionally applied (matches Tableau).")
            continue
        # boolean filter with BOTH values selected = no-op; skip silently
        if f.get("kind") == "in" and {str(v).lower() for v in f.get("values", [])} >= {"true", "false"}:
            continue
        if is_measure_filter(f):
            # E06: never a plain row WHERE -- applied after this loop (after the dimension / top-N filters), see
            # _measure_filter_predicate
            measure_filters.append(f)
            continue
        if pc not in avail:
            cspec = CALCS.get(f["caption"])
            vals = {str(v).lower() for v in f.get("values", [])}
            if (cspec and cspec.get("window") and f.get("kind") == "in"
                    and vals == {"true"} and _rank_gate_n(cspec.get("sql")) is not None):
                # VIEW-ORDER TABLE-CALC FILTER (INDEX()<=5 / RANK()<=10). Held
                # back: Tableau evaluates table calcs AFTER dimension filters
                # and aggregation, so the ranking must see the SAME filtered
                # rows this sheet does -- ranking the unfiltered table would
                # pick a different top 5. Handled after the loop.
                view_order_filters.append((f, cspec))
                continue
            if (cspec and not cspec.get("agg_ready")
                    and not cspec.get("window")
                    and f.get("kind") == "in" and vals <= {"true", "false"}):
                # row-level BOOLEAN calc filter (period gates like 'Current
                # Period'): push the predicate itself -- skipping it plotted
                # the FULL date range on sheets whose measure isn't gated
                if vals == {"true"}:
                    cons.append(f"({cspec['sql']})")
                elif vals == {"false"}:
                    cons.append(f"NOT ({cspec['sql']})")
                continue                     # both values = no-op
            if (cspec and not cspec.get("agg_ready") and not cspec.get("window")
                    and f.get("kind") in ("in", "not_in") and f.get("values")):
                # E06c: IN / NOT IN on a ROW-LEVEL calculated dimension (group, cluster, CASE) is a dimension
                # filter on the calculation's own value -- the same predicate builder as a column filter
                pred = _value_predicate(f, expr=f"({cspec['sql']})")
                if pred:
                    cons.append(pred)
                    continue
            # A dropped filter that changes the ROW SET is never INFO. The old
            # code graded every calc filter INFO with the boilerplate "Tableau
            # default covers the full range" -- true for an untouched
            # quantitative range, a LIE for a gate like INDEX()<=5, which is
            # how Fil Test's Sheet 1 shipped 17 bars against Tableau's 5 while
            # the report said 99% and "no visual-risk sheets".
            if f["caption"] in CALCS:
                findings.record("WARNING", s["name"], "filter-not-pushed",
                                f"Filter on '{f['caption']}' NOT pushed to SQL "
                                "(aggregate-level calc filter): every member is "
                                "shown -- Tableau restricts this view, so rows "
                                "and totals will differ.")
            else:
                findings.record("WARNING", s["name"], "filter-not-pushed",
                                f"Filter on '{f['caption']}' not pushed "
                                f"(no column {pc} in {T}).")
            continue
        k = f.get("kind")
        if k == "relative_date":
            import v3_sheet_plan as _P
            pred = _relative_date_predicate(f, _P._week_start(s.get("worksheet_semantics") or {}))
            if pred:
                cons.append(pred)
                continue
            findings.record("WARNING", s["name"], "relative-date-not-pushed",
                            "Relative-date filter on '%s' (%s %s %s) not translated; "
                            "ALL dates shown -- totals will differ from Tableau."
                            % (f["caption"], f.get("range") or "last",
                               f.get("n") or "?", f.get("period") or "period"))
            continue
        if k in ("range", "in", "not_in", "ord_range"):
            # date-part / range / member / exclude value filters all go through
            # the SAME predicate builder the context-into-top-N path uses.
            pred = _value_predicate(f)
            if pred:
                cons.append(pred)
        elif k == "top_n":
            # Tableau order of operations: top-N runs BEFORE dimension/measure
            # filters, so those are NOT AND'd into the ranking subquery -- BUT
            # CONTEXT filters run before top-N, so context_where IS applied
            # inside the subquery ('top 10 customers in Central').
            from calc_translator import translate_formula
            expr_sql = None
            if f.get("order_expr"):
                # nested calc refs resolve to their ALREADY-TRANSLATED SQL --
                # CALCS holds {sql,...} entries, not formulas, so it must not
                # be passed as calc_defs (dict-for-formula crash, 11 sheets)
                pre = _re2.sub(r"\[([^\]]+)\]",
                               lambda m: "(" + CALCS[m.group(1)]["sql"] + ")"
                               if m.group(1) in CALCS else m.group(0),
                               f["order_expr"])
                expr_sql, _ = translate_formula(pre, colmap=COLMAP)
            if not expr_sql:
                findings.record("WARNING", s["name"], "topn-not-pushed",
                                f"Top-{f.get('n')} filter on '{f['caption']}': ranking "
                                f"expression {f.get('order_expr')!r} not translatable; "
                                "filter NOT applied (all members shown).")
                continue
            # A ranking column absent from THIS sheet's table degrades to a
            # WARNING -- never emit SQL that raises a BinderException and takes
            # the whole sheet down. This bites relationship extracts that could
            # not flatten (a dim table's columns, e.g. a Customers
            # 'acquisition_channel', never made it into the fact table), and any
            # ranking that references a field this sheet's datasource lacks.
            missing = sorted({px(nm)
                              for nm in _re2.findall(r"\[([^\]]+)\]", f["order_expr"])
                              if nm not in CALCS and px(nm) not in avail})
            if missing:
                findings.record("WARNING", s["name"], "topn-column-missing",
                                f"Top-{f.get('n') or f.get('n_param')} filter on "
                                f"'{f['caption']}' ranks by {f['order_expr']}, but "
                                f"column(s) {', '.join(missing)} are not in this "
                                f"sheet's table ({T.split('.')[-1]}); filter NOT "
                                "applied (all members shown). Usually a relationship "
                                "extract that did not flatten -- re-onboard the "
                                "workbook so the joined columns are present.")
                continue
            direc = "DESC" if (f.get("dir") or "top") == "top" else "ASC"
            if " OVER (" in expr_sql:
                # ranking expression carries row-grain windows (Avg-Days
                # chains): hoist them INSIDE the subquery scope now -- the
                # q() retry only rewrites the outer statement
                sub = f"SELECT {pc} AS D, ({expr_sql}) AS ORD FROM {T} {context_where} GROUP BY 1"
                for _ in range(3):          # window-in-window needs layers
                    nxt = _hoist_windows(sub)
                    if not nxt:
                        break
                    sub = nxt
                if sub:
                    n_lit = (param_token(f["n_param"]) if f.get("n_param")
                             else str(int(f["n"])))
                    cons.append(
                        f"{pc} IN (SELECT D FROM ({sub}) s WHERE ORD IS NOT "
                        f"NULL ORDER BY ORD {direc} LIMIT {n_lit})")
                    findings.record("INFO", s["name"], "topn-order-of-ops",
                                    f"Top-{f.get('n') or f.get('n_param')} on "
                                    f"'{f['caption']}' by a windowed expression; "
                                    "ranking computed over the unfiltered table.")
                    continue
            if f.get("n_param"):
                # N driven by a parameter: __PARAM__ token substituted live by
                # sub_params. ROW_NUMBER + `rn <= value`, NOT LIMIT -- numeric
                # params substitute as float literals (5.0) which LIMIT rejects
                # in Snowflake, while a numeric compare tolerates them in both
                # engines.
                tok = param_token(f["n_param"])
                cons.append(
                    f"{pc} IN (SELECT {pc} FROM (SELECT {pc}, ROW_NUMBER() "
                    f"OVER (ORDER BY {expr_sql} {direc}) AS rn FROM {T} {context_where} "
                    f"GROUP BY {pc}) WHERE rn <= {tok})")
            else:
                cons.append(f"{pc} IN (SELECT {pc} FROM {T} {context_where} GROUP BY {pc} "
                            f"ORDER BY {expr_sql} {direc} LIMIT {int(f['n'])})")
            n_desc = f.get("n") or f"[{f.get('n_param')}]"
            findings.record("INFO", s["name"], "topn-order-of-ops",
                            f"Top-{n_desc} on '{f['caption']}' by {f.get('order_expr')} "
                            "ranked within any context filter" +
                            (f" ({context_where[6:]})" if context_where else
                             " (none); dimension filters correctly excluded from the ranking"))
    # ---- MEASURE FILTERS (E06) -------------------------------------------------
    # Tableau order of operations: after dimension and top-N filters, before table calculations. A measure
    # filter keeps the view's marks (its level-of-detail dimensions) whose aggregate passes, so every renderer
    # gets it through this WHERE; other measures still aggregate every row of a kept mark (= HAVING).
    if measure_filters:
        inner = " AND ".join(cons)
        w = where.strip()
        sub_where = (w + " AND " + inner) if (w and inner) else (("WHERE " + inner) if inner else w)
        for f in measure_filters:
            pred, why = _measure_filter_predicate(s, f, T, sub_where)
            if pred:
                cons.append(pred)
            else:
                findings.record("WARNING", s["name"], "measure-filter-not-applied",
                                f"Measure filter on '{f['caption']}' not applied ({why}); marks outside the "
                                "range may show.")
    # ---- VIEW-ORDER TABLE-CALC FILTERS (INDEX()<=N / RANK()<=N) -------------
    # Tableau order of operations: these run AFTER dimension filters and
    # aggregation, over the sheet's own view order. So rank inside the SAME
    # filtered set (dashboard WHERE + this sheet's pushed filters).
    for f, cspec in view_order_filters:
        n = _rank_gate_n(cspec["sql"])
        srt = s.get("sort")
        dim_cap = (srt or {}).get("on") or _first_dim_caption(s)
        if not srt or not srt.get("field") or not dim_cap:
            findings.record("WARNING", s["name"], "view-order-filter-not-pushed",
                            f"'{f['caption']}' restricts this view to the first "
                            f"{n} rows of Tableau's VIEW ORDER, but this sheet "
                            "declares no sort to define that order; filter NOT "
                            "applied -- every member is shown.")
            continue
        dcol = px(dim_cap)
        if dcol not in avail:
            findings.record("WARNING", s["name"], "view-order-filter-not-pushed",
                            f"'{f['caption']}' ranks by '{dim_cap}', which has no "
                            f"column in {T}; filter NOT applied.")
            continue
        order_sql = measure_sql_for(T, srt["field"], srt.get("agg") or "sum")
        if not order_sql:
            findings.record("WARNING", s["name"], "view-order-filter-not-pushed",
                            f"'{f['caption']}' sorts by '{srt['field']}', which did "
                            "not translate to SQL; filter NOT applied.")
            continue
        direc = "DESC" if (srt.get("dir") or "desc").lower().startswith("desc") else "ASC"
        inner = " AND ".join(cons)
        w = where.strip()
        if w and inner:
            sub_where = w + " AND " + inner
        elif inner:
            sub_where = "WHERE " + inner
        else:
            sub_where = w
        cons.append(f"{dcol} IN (SELECT {dcol} FROM {T} {sub_where} GROUP BY {dcol} "
                    f"ORDER BY {order_sql} {direc} LIMIT {n})")
        findings.record("INFO", s["name"], "view-order-filter-pushed",
                        f"'{f['caption']}' applied as the first {n} of "
                        f"'{dim_cap}' by {srt.get('agg', 'sum').upper()}"
                        f"({srt['field']}) {direc} -- the sheet's own sort, "
                        "ranked after this view's filters (Tableau evaluates "
                        "table calcs last).")
    if not cons:
        return where
    clause = " AND ".join(cons)
    return (where + " AND " + clause) if where.strip() else ("WHERE " + clause)


def _no_data(s, df):
    """0 rows for a sheet is almost always a captured-filter bug, not a fact
    about the data -- record it so the report surfaces it pre-delivery."""
    if df is None or len(df) == 0:
        findings.record("WARNING", s["name"], "empty-result",
                        "Query returned 0 rows -- check captured filters.")
        st.info(f"{s['name']}: no data (see migration notes)")
        return True
    return False


def _manual_order(s, dim, present):
    """Manual (drag) sort order for `dim`, limited to values actually present."""
    ms = s.get("manual_sort")
    if ms and ms.get("on") == dim and ms.get("order"):
        keep = [v for v in ms["order"] if v in set(present)]
        extra = [v for v in present if v not in set(keep)]
        return keep + extra
    return None


def _refline_rule(s, df, value_col, axis="x", only_axis=None):
    """Best-effort reference line: a rule at an aggregate of the data. Returns an
    Altair layer or None (param-based / unknown formulas are skipped).
    only_axis: match reflines whose 'axis' caption equals it (scatter has
    reflines on BOTH axes; drawing all of them on X doubled the vertical)."""
    try:
        out = []
        for rl in s.get("reflines", []):
            if value_col not in df.columns:
                continue
            if only_axis is not None and rl.get("axis") \
               and rl["axis"].strip().lower() != str(only_axis).strip().lower():
                continue
            v = None
            if rl.get("is_param"):
                # live parameter value (sidebar) first, workbook default second
                if rl.get("value_param"):
                    pv = param_value(rl["value_param"])
                    v = pv if isinstance(pv, float) else None
                if v is None:
                    v = rl.get("value_literal")
            else:
                agg = (rl.get("formula") or "").lower()
                col = df[value_col].dropna()
                if col.empty:
                    continue
                v = {"average": col.mean(), "median": col.median(),
                     "min": col.min(), "max": col.max(), "sum": col.sum(),
                     "total": col.sum()}.get(agg)
            if v is None:
                continue
            # Reuse the plotted field name so the rule shares the series'
            # scale AND axis. A synthetic field named V created a third
            # visible axis on dual-axis charts and, under independent scales,
            # could position the rule on a one-value scale instead of the
            # series scale. Do NOT set axis=None here: Vega-Lite merges axis
            # definitions across layers that share a scale, and an explicit
            # null wins the merge -- it silently removed the bar series' whole
            # left '$ Sales' axis on the real Sales Dashboard Monthly chart
            # (found live 2026-09-10; proven with vl-convert in the gate).
            rule_data = pd.DataFrame({value_col: [float(v)]})
            rule_enc = (alt.X(f"{value_col}:Q") if axis == "x"
                        else alt.Y(f"{value_col}:Q"))
            rule = alt.Chart(rule_data).mark_rule(
                color="#888", strokeDash=[4, 4]).encode(**{axis: rule_enc})
            out.append(rule)
        return out
    except Exception:
        return []


def _q1(s):
    return s.replace("'", "''")


def _label_of(labels, v):
    """Alias lookup tolerant of case (SQL booleans stringify as True/False,
    Tableau alias keys are lowercase true/false)."""
    if v in labels:
        return labels[v]
    lower = {k.lower(): lab for k, lab in labels.items()}
    return lower.get(str(v).lower(), v)


def _apply_labels(df, col, caption):
    """Map a result column's raw values to the workbook's display aliases."""
    labels = value_labels(caption)
    if labels is not None and col in df.columns:
        df[col] = df[col].astype(str).map(lambda v: _label_of(labels, v))
    return df


def _windowize(inner_sql, win_items, order_alias):
    """Wrap a grouped subquery so window (table-calc) measures compute over its
    rows. win_items: [(expr_with_WIN_ORDER, alias)]. Ordering is best-effort."""
    order = "ORDER BY " + order_alias
    outer = ", ".join(e.replace(WIN_ORDER, order) + f" AS {a}" for e, a in win_items)
    return f"SELECT t.*, {outer} FROM ({inner_sql}) t"


_PART_LABEL = {"yr": "Year", "qr": "Quarter", "mn": "Month", "dy": "Day",
               "wk": "Week", "hr": "Hour"}


def _in_state(key):
    """True when Streamlit already holds a value for this widget (then it must not get a default index)."""
    try:
        return key in st.session_state
    except Exception:
        return False


class _NullBlock:
    """A no-op Streamlit element: usable as a context manager and callable."""
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __call__(self, *a, **k):
        return _NullBlock()

    def __getattr__(self, name):
        return _NullBlock()


class _StateOnlyST:
    """Streamlit stand-in that draws nothing and returns what each widget WILL show: its value in session state,
    else the default it is created with. Release 26c: controls are drawn in their Tableau zones, which can come
    after the charts they filter; their values are needed first."""
    def __init__(self, real):
        self._real = real

    @property
    def session_state(self):
        return self._real.session_state

    def _state(self, key, default):
        try:
            if key is not None and key in self._real.session_state:
                return self._real.session_state[key]
        except Exception:
            pass
        return default

    def columns(self, spec, *a, **k):
        n = spec if isinstance(spec, int) else len(spec)
        return [_NullBlock() for _ in range(n)]

    def selectbox(self, label, options, index=0, key=None, **k):
        opts = list(options)
        return self._state(key, opts[index] if (opts and index is not None) else (opts[0] if opts else None))

    radio = selectbox

    def multiselect(self, label, options, default=None, key=None, **k):
        return self._state(key, list(default or []))

    def date_input(self, label, value=None, key=None, **k):
        return self._state(key, value)

    def number_input(self, label, min_value=None, max_value=None, value=None, key=None, **k):
        return self._state(key, value)

    def text_input(self, label, value="", key=None, **k):
        return self._state(key, value)

    def checkbox(self, label, value=False, key=None, **k):
        return bool(self._state(key, value))

    def slider(self, label, min_value=None, max_value=None, value=None, key=None, **k):
        return self._state(key, value)

    def __getattr__(self, name):
        return _NullBlock()


@contextmanager
def _state_only_widgets():
    global st
    real = st
    st = _StateOnlyST(real)
    try:
        yield
    finally:
        st = real


def _has_leaf(node, key):
    if not isinstance(node, dict):
        return False
    if key in node:
        return True
    return any(_has_leaf(k, key) for k in node.get("children") or [])


def _leaves(node, key):
    if not isinstance(node, dict):
        return []
    if key in node:
        return [node]
    return [x for k in node.get("children") or [] for x in _leaves(k, key)]


def _filter_for_zone(dash, zone):
    """The placed filter (dash['filters']) a filter zone shows."""
    for f in dash.get("filters") or []:
        if (f.get("caption") == zone.get("caption") and (f.get("datepart") or None) == (zone.get("datepart") or None)
                and (not zone.get("field_id") or f.get("field_id") in (None, zone.get("field_id")))):
            return f
    return None


def _saved_selection(dash, f, part=None):
    """The selection Tableau opens a placed quick filter with: the saved IN filter on the same field (and date
    part) of the worksheet the filter zone is bound to, else of any sheet on the dashboard. [] = All."""
    sheets = dash.get("sheets") or []
    bound = [s for s in sheets if s.get("name") == f.get("scope_sheet")] or sheets
    for sheet in bound:
        for af in sheet.get("applied_filters") or []:
            if af.get("caption") != f.get("caption") or af.get("kind") != "in":
                continue
            if (af.get("datepart") or None) != (part or None):
                continue
            values = [str(v) for v in af.get("values") or [] if v not in (None, "%null%")]
            if values:
                return values
    return []


MULTI_MODES = ("checkdropdown", "checklist")


def _tableau_member(v):
    """How Tableau shows a quick-filter member ('All' is '(All)')."""
    return "(All)" if v == "All" else v     # Tableau quick filters that select several values


def _checklist_widget(label, options, saved, key, parameter_actions):
    """Tableau's 'Multiple Values (list)' quick filter -- its default: a checkbox for (All) and one per member.
    Returns the selected members ([] = All)."""
    start = set(v for v in saved if v in options) if 0 < len(saved) < len(options) else set(options)
    all_key, item_key = key + "::(All)", (lambda v: f"{key}::{v}")
    parameter_actions.register_default(IR, st, all_key, len(start) == len(options))

    def _all_changed():
        on = bool(st.session_state.get(all_key))
        for v in options:
            st.session_state[item_key(v)] = on

    def _item_changed():
        st.session_state[all_key] = all(st.session_state.get(item_key(v), True) for v in options)
    if label:
        st.markdown(f"<div data-testid='stWidgetLabel'><p style='margin:0'>{label}</p></div>", unsafe_allow_html=True)
    kw = {} if _in_state(all_key) else {"value": len(start) == len(options)}
    st.checkbox("(All)", key=all_key, on_change=_all_changed, **kw)
    picked = []
    for v in options:
        k = item_key(v)
        kw = {} if _in_state(k) else {"value": v in start}
        if st.checkbox(str(v), key=k, on_change=_item_changed, **kw):
            picked.append(v)
    return [] if len(picked) == len(options) else picked


def _multi_widget(label, options, saved, key, parameter_actions, mode=None):
    """A multiple-values quick filter. Empty selection = All (Tableau's '(All)'); opens at the saved values."""
    if mode == "checklist":
        return _checklist_widget(label, options, saved, key, parameter_actions)
    default = [v for v in saved if v in options] if 0 < len(saved) < len(options) else []
    parameter_actions.register_default(IR, st, key, default)
    kw = {} if _in_state(key) else {"default": default}
    try:
        picked = st.multiselect(label, options, key=key, placeholder="(All)", **kw)
    except TypeError:                     # older Streamlit (no placeholder)
        picked = st.multiselect(label, options, key=key, **kw)
    return [v for v in (picked or []) if v in options]


def build_where(dash):
    """Render this dashboard's PLACED controls as a row; return [{col, clause}].

    'Placed' means the .twb has a <zone type='filter'|'paramctrl'> for it --
    Tableau's own control surface. Sheet-level filters still apply to their
    sheets (via _apply_sheet_filters); they are simply not widgets, exactly
    as in Tableau. Each sheet keeps only the parts whose column exists in ITS
    table."""
    # A workbook/dashboard may span several datasources. Resolve each placed
    # filter against the datasource of the worksheet Tableau bound its zone to
    # instead of the process-wide default ORDERS table. The old global lookup
    # made controls on every secondary datasource disappear and populated some
    # same-named fields from the wrong table.
    by_name = {s.get("name"): s for s in (dash.get("sheets") or [])}
    usable = []
    for raw in (dash.get("filters") or []):
        if raw.get("kind") not in ("categorical", "date", "date_part"):
            continue
        f = dict(raw)
        source = by_name.get(f.get("scope_sheet"))
        if source is None:
            source = next((s for s in (dash.get("sheets") or [])
                           if any(x.get("caption") == f.get("caption")
                                  for x in (s.get("filters") or []))), None)
        filter_scope = dict(source or {})
        if f.get('datasource_id'):
            filter_scope['datasource_id'] = f['datasource_id']
        if f.get('field_id'):
            filter_scope['field_bindings'] = {f['caption']: f['field_id']}
        try:
            # DM2: the filter's own datasource, else its source sheet's; never the built-in default table in the
            # scoped (V3) mode -- an unbound or sourceless filter is reported, not populated from other data
            if f.get('datasource_id'):
                declared = IR.get('field_scopes', {}).get(f['datasource_id'])
                if not declared:
                    raise ValueError('Unknown filter datasource')
                T = tbl({"datasource": declared['caption']})
            elif source:
                T = tbl(source)
            elif "field_scopes" in IR:
                raise ValueError('Filter has no source worksheet')
            else:
                T = ORDERS
            with field_scope(filter_scope):
                expr = rdim(f["caption"])
                cspec = CALCS.get(f["caption"])
        except Exception as exc:
            findings.record('BLOCKER', dash['name'], 'filter-binding-failed', str(exc))
            st.warning(f"Filter {f['caption']}: could not resolve ({exc})")
            continue
        valid = bool(cspec and not cspec.get("agg_ready")
                     and not cspec.get("window")) or _col_known(T, expr)
        if not valid:
            findings.record(
                "WARNING", dash.get("name", "(dashboard)"),
                "placed-filter-unresolved",
                f"Placed filter '{f['caption']}' could not be resolved against "
                f"its source worksheet table {T}; the control is not shown.")
            continue
        f["_table"], f["_expr"] = T, expr
        usable.append(f)
    placed_params = [p for p in (dash.get("params") or []) if p in PARAM_DEFS]
    parts = []
    if not usable and not placed_params:
        return parts
    # one control per placed item, left to right, like Tableau's control row.
    # zip (not indexing) -- headless probes return a non-list from columns().
    items = [("filter", f) for f in usable] + [("param", p) for p in placed_params]
    for w, (kind, item) in zip(st.columns(len(items)), items):
        if kind == "param":
            with w:
                _param_widget(item, PARAM_DEFS[item], dash["name"])
            continue
        f = item
        cap, pc, T = f["caption"], f["_expr"], f["_table"]
        part = f.get("datepart")
        scope = f.get("scope_sheet")     # worksheet this filter zone is bound to
        key = dash["name"] + "::" + (f.get('field_id') or cap) + "::" + str(part) + "::" + T
        with w, _control_boundary(dash['name'], cap):
            if f["kind"] == "date_part" and part not in _EXTRACT_PART:
                findings.record("WARNING", dash["name"], "filter-widget-unmapped",
                                f"Dashboard filter '{cap}' uses date part '{part}' "
                                "which has no SQL mapping; showing a date range "
                                "instead -- selections will not match Tableau.")
                f = dict(f, kind="date")          # degrade LOUDLY, never vanish
            if f["kind"] == "date_part":
                # Tableau labels a date-part widget "Year of Order Date" and
                # filters on the PART, never the raw timestamp.
                expr = f"EXTRACT({_EXTRACT_PART[part]} FROM {pc})"
                vals = q(f"SELECT DISTINCT {expr} AS V FROM {T} "
                         f"WHERE {pc} IS NOT NULL ORDER BY 1")["V"].tolist()
                lbl = f"{_PART_LABEL.get(part, part.title())} of {cap}"
                import parameter_actions
                options = ["All"] + [str(int(v)) for v in vals]
                saved = [v for v in _saved_selection(dash, f, part) if v in options]
                if f.get('mode') in MULTI_MODES:
                    # Tableau's checkbox list / multiple-values dropdown: several values at once (none = All)
                    picked = _multi_widget(lbl, options[1:], saved, key, parameter_actions, f.get('mode'))
                    nums = [int(v) for v in picked]
                    parts.append({"col": pc, "clause": (f"{expr} IN ({', '.join(str(n) for n in nums)})"
                                                        if nums else None),
                                  "caption": cap, "scope": scope, "table": T,
                                  "governs": True, 'datasource_id': f.get('datasource_id'),
                                  'field_id': f.get('field_id'),
                                  'runtime_filter': {'kind': 'date_part', 'part': part,
                                                     'value': nums[0] if len(nums) == 1 else None,
                                                     **({'values': nums} if len(nums) > 1 else {})}})
                    continue
                if f.get('show_all') is False and len(options) > 1:
                    options = options[1:]                  # the author removed '(All)' from this quick filter
                default = saved[0] if len(saved) == 1 else options[0]     # Tableau opens at the saved selection
                parameter_actions.register_default(IR,st,key,default)
                fmt = (lambda v, _p=part: _tableau_member(v) if v == "All" else
                       (f"Q{v}" if _p in ("qr", "tqr") else v))
                sel = (st.radio if f.get('mode') == 'radiolist' else st.selectbox)(lbl, options, key=key, **({} if _in_state(key) else {'index': options.index(default)}), format_func=fmt)
                # Always emit a part so the widget GOVERNS its column even at
                # "All" (clause=None). "All" means no restriction -- and must
                # still OVERRIDE a sheet's own saved filter on that column, or a
                # context filter's stale saved value (e.g. Region='Central')
                # silently re-narrows the sheet when the widget reads "All".
                parts.append({"col": pc, "clause": (f"{expr} = {int(sel)}" if sel != "All" else None),
                              "caption": cap, "scope": scope, "table": T,
                              "governs": True, 'datasource_id': f.get('datasource_id'),
                              'field_id': f.get('field_id'),
                              'runtime_filter': {'kind': 'date_part', 'part': part,
                                                 'value': None if sel == 'All' else int(sel)}})
            elif f["kind"] == "categorical":
                vals = q(f"SELECT DISTINCT {pc} AS V FROM {T} WHERE {pc} IS NOT NULL ORDER BY 1")["V"].tolist()
                import parameter_actions
                options = ["All"] + [str(v) for v in vals]
                saved = [v for v in _saved_selection(dash, f) if v in options]
                if f.get('mode') in MULTI_MODES:
                    picked = _multi_widget(cap, options[1:], saved, key, parameter_actions, f.get('mode'))
                    chosen = [v for v in vals if str(v) in picked]
                    parts.append({"col": pc, "clause": (f"{pc} IN ({', '.join(chr(39) + _q1(str(v)) + chr(39) for v in picked)})"
                                                        if picked else None),
                                  "caption": cap, "scope": scope, "table": T,
                                  "governs": True, 'datasource_id': f.get('datasource_id'),
                                  'field_id': f.get('field_id'),
                                  'runtime_filter': {'kind': 'categorical',
                                                     'value': chosen[0] if len(chosen) == 1 else None,
                                                     **({'values': chosen} if len(chosen) > 1 else {})}})
                    continue
                if f.get('show_all') is False and len(options) > 1:
                    options = options[1:]
                default = saved[0] if len(saved) == 1 else options[0]     # Tableau opens at the saved selection
                parameter_actions.register_default(IR,st,key,default)
                sel = (st.radio if f.get('mode') == 'radiolist' else st.selectbox)(cap, options, key=key, **({} if _in_state(key) else {'index': options.index(default)}), format_func=_tableau_member)
                selected_value = next((v for v in vals if str(v) == sel), None)
                parts.append({"col": pc, "clause": (f"{pc} = '{_q1(sel)}'" if sel != "All" else None),
                              "caption": cap, "scope": scope, "table": T,
                              "governs": True, 'datasource_id': f.get('datasource_id'),
                              'field_id': f.get('field_id'),
                              'runtime_filter': {'kind': 'categorical',
                                                 'value': None if sel == 'All' else selected_value}})
            else:
                # positional access, NOT b["lo"]/b["hi"]: DuckDB keeps unquoted
                # aliases lowercase but Snowflake folds them to UPPERCASE, so a
                # by-name lowercase key raised KeyError('lo') ONLY in the
                # deployed SiS app (field-found; every local test is DuckDB and
                # so never hit it). iloc is dialect-agnostic.
                b = q(f"SELECT MIN({pc}) AS LO, MAX({pc}) AS HI FROM {T}")
                lo = pd.to_datetime(b.iloc[0, 0]).date()
                hi = pd.to_datetime(b.iloc[0, 1]).date()
                import parameter_actions
                parameter_actions.register_default(IR,st,key,(lo,hi))
                r = st.date_input(cap, value=(lo, hi), min_value=lo, max_value=hi, key=key)
                if isinstance(r, (list, tuple)) and len(r) == 2:
                    parts.append({"col": pc, "clause": f"{pc} BETWEEN '{r[0]}' AND '{r[1]}'",
                                  "caption": cap, "scope": scope, "table": T,
                                  "governs": True, 'datasource_id': f.get('datasource_id'),
                                  'field_id': f.get('field_id'),
                                  'runtime_filter': {'kind': 'date_range',
                                                     'start': r[0].isoformat(),
                                                     'end': r[1].isoformat()}})
    return parts


@contextmanager
def _control_boundary(dashboard, caption):
    try:
        yield
    except Exception as exc:
        findings.record('BLOCKER', dashboard, 'filter-query-failed', f'{caption}: {exc}')
        st.warning(f'Filter {caption}: could not load ({exc})')


def _parts_for_sheet(where_parts, s):
    """The dashboard-filter clauses that actually apply to sheet `s`.

    Tableau binds a placed quick-filter to a source worksheet (the filter
    zone's `name`) and applies it to THAT sheet plus any worksheet that carries
    the same field in its own filters (Tableau writes a multi-worksheet filter
    into each affected worksheet's XML) -- NOT to every sheet that merely has
    the column. Applying a dashboard filter everywhere made a Region
    quick-filter (bound to one chart) AND itself onto a PARAMETER-driven chart
    on the same datasource: Region='West' AND (param) Region='South' = blank,
    and a KPI tile that Tableau leaves at the grand total silently narrowed to
    one region. A part with no scope (older XML / standalone-tab filter with no
    bound zone) stays global, so nothing regresses."""
    own_filters = list(s.get("filters", [])) + list(s.get("applied_filters", []))
    own_filter_cols = {px(f["caption"]) for f in own_filters}
    own_filter_caps = {f["caption"] for f in own_filters}
    own_filter_ids = {f.get('field_id') for f in own_filters if f.get('field_id')}
    return [p for p in where_parts
            if not isinstance(p, dict)
            or ((not p.get('datasource_id') or p['datasource_id'] == s.get('datasource_id'))
            and (not p.get("scope")
            or p.get("scope") == s.get("name")
            or (p.get('field_id') in own_filter_ids if p.get('field_id') else
                (p.get("col") in own_filter_cols or p.get("caption") in own_filter_caps))))]


def _where_for(T, parts):
    """Assemble the WHERE clause for one sheet's table from dashboard parts.
    A part with clause=None is an 'All' selection -- it governs its column (see
    _parts_for_sheet) but adds no restriction, so it contributes no SQL."""
    avail = table_columns(T)
    keep = [p["clause"] for p in parts
            if p.get("clause")
            and (p.get("table") == T or p["col"] in avail)]
    return ("WHERE " + " AND ".join(keep)) if keep else ""


# --------------------------------------------------------------------------- #
# per-kind renderers
# --------------------------------------------------------------------------- #
def r_kpi(s, where):
    T = tbl(s)
    ms = s["measures"][:7]
    resolved = []
    for m in ms:
        spec = resolve_measure(T, s["name"], m)
        if spec and spec.get("window"):
            findings.record("WARNING", s["name"], "window-in-kpi",
                            f"Measure '{m['caption']}' is a table calc; not "
                            f"meaningful as a single KPI value. Skipped.")
            spec = None
        resolved.append((m, spec))
    ok = [(i, m, sp) for i, (m, sp) in enumerate(resolved) if sp]
    def query_measure(selected):
        if s.get('worksheet_semantics', {}).get('projection', {}).get('renderer') == 'kpi-fixed':
            from kpi_fixed_plan import build, compile_sql
            plan = build(s, s['worksheet_semantics'])
            if plan != s['worksheet_semantics']['projection'] or plan['status'] != 'MAPPED':
                raise ValueError('FIXED KPI source plan changed')
            for ref, sp in resolved:
                if sp and not any(m['field_id'] == ref.get('field_id') and m['sql'] == sp['sql'] for m in plan['measures']):
                    raise ValueError('FIXED KPI translation does not match execution')
            return q(compile_sql(selected, T, where, sys.modules[__name__]))
        return q(f"SELECT {selected} FROM {T} {where}")
    row = {}
    if ok:
        sel = ", ".join(sp["sql"] + f" AS M{i}" for i, m, sp in ok)
        try:
            row = query_measure(sel).to_dict("records")[0]
        except Exception:
            # one bad measure poisons the combined query -> resolve per measure
            for i, m, sp in ok:
                try:
                    row[f"M{i}"] = query_measure(sp['sql'] + ' AS V')["V"][0]
                except Exception as e:
                    row[f"M{i}"] = None
                    findings.record("BLOCKER", s["name"], "measure-query-failed",
                                    f"Measure '{m['caption']}': {type(e).__name__}: {e}")
    # A Tableau customized-label is the authoritative composition of a KPI
    # text mark. Render its literal/field runs as one tile instead of exposing
    # every underlying current/previous/positive/negative helper calculation
    # as a separate st.metric card.
    native_card = (s.get("_presentation") == "native"
                   and len([m for m, sp in resolved if sp and "%" not in m["caption"]]) == 1
                   and any(sp and "%" in m["caption"] for m, sp in resolved))
    if s.get("label_runs") and not native_card:
        by_caption = {}
        for i, (m, sp) in enumerate(resolved):
            by_caption.setdefault(m["caption"], (i, m, sp))
        pieces, align = [], None
        for run in s["label_runs"]:
            if run.get("field"):
                entry = by_caption.get(run["field"])
                if not entry:
                    parameters = [o['parameter'] for o in s.get('worksheet_semantics', {}).get('occurrences', [])
                                  if o.get('parameter') and o['caption'] == run['field']]
                    if not parameters or any(p != parameters[0] for p in parameters):
                        if s.get('worksheet_semantics'):
                            findings.record('BLOCKER', s['name'], 'kpi-label-unbound',
                                            'Customized label field has no exact measure or parameter binding')
                        continue
                    parameter = parameters[0]
                    if parameter['caption'] not in PARAMS:
                        raise ValueError('Missing runtime parameter: ' + parameter['caption'])
                    raw = PARAMS[parameter['caption']]
                    content = parameter['aliases'].get(str(raw), _param_display_value(raw))
                else:
                    i, m, sp = entry
                    value = row.get(f"M{i}")
                    if value is None or pd.isna(value):
                        continue                    # inactive direction-gated delta
                    content = fmt_tableau(value, m, sp)
            else:
                content = run.get("text", "")
            content = _html.escape(str(content)).replace("\n", "<br>")
            # Release 27 (F2): authored sizes are points (Tableau pt), fonts and alignment come from the run
            pt = run.get("fontsize") or _LABEL_DEFAULT_PT
            style = [f"font-size:{pt}pt"]
            if run.get("fontcolor"):
                style.append(f"color:{run['fontcolor']}")
            if str(run.get("bold", "")).lower() == "true":
                style.append("font-weight:700")
            if str(run.get("italic", "")).lower() == "true":
                style.append("font-style:italic")
            if run.get("fontname"):
                style.append(f"font-family:{_font_stack(run['fontname'])}")
            if run.get("fontalignment") is not None and align is None:
                align = {"0": "left", "1": "center", "2": "right"}.get(str(run["fontalignment"]), None)
            if run.get('field') and entry and content and '<br>' not in content:
                style.append('white-space:nowrap')
                style.append(f'font-size:min({pt}pt, calc(100cqi / {max(1,len(content))*.65:.2f}))')
            pieces.append(f"<span style=\"{';'.join(style)}\">{content}</span>")
        if pieces:
            # One <div> per authored line, blank lines kept at the height of their own run (Tableau keeps them),
            # stacked with the sheet's cell vertical alignment (Tableau draws a KPI label top / middle / bottom).
            lines = [[]]
            for piece in pieces:
                match = _re2.fullmatch(r'<span style="([^"]*)">(.*)</span>', piece, _re2.S)
                if not match:
                    raise ValueError('Unbalanced authored KPI label run')
                style, content = match.groups()
                for i, fragment in enumerate(content.split('<br>')):
                    if i:
                        lines.append([])
                    lines[-1].append(f'<span style="{style}">{fragment or "&#8203;"}</span>')
            while lines and not _html.unescape(_re2.sub(r'<[^>]*>', '', ''.join(lines[-1]))).replace('​', '').strip():
                lines.pop()                              # trailing paragraph marks draw nothing
            # the line pitch sits on each line: Streamlit's markdown CSS gives inner <div>s its own 1.6 line height
            body = ''.join('<div style="line-height:1.2;margin:0">' + ''.join(line) + '</div>' for line in lines)
            cell = (s.get('ws_style') or {}).get('cell') or {}
            align = align or cell.get('text-align') or 'center'
            vertical = {'top': 'flex-start', 'bottom': 'flex-end'}.get(cell.get('vertical-align'), 'center')
            sizing = (f'display:flex;flex-direction:column;justify-content:{vertical};'
                      + (f'min-height:{s["_hpx"]}px;' if s.get('_hpx') else ''))
            family = next((r['fontname'] for r in s['label_runs'] if r.get('fontname')), None) \
                or ((s.get('ws_style') or {}).get('worksheet') or {}).get('font-family')
            st.markdown(
                f"<div class='tableau-kpi-label' style='text-align:{align};font-size:{_LABEL_DEFAULT_PT}pt;"
                + (f"font-family:{_html.escape(_font_stack(family), quote=True)};" if family else "")
                # Tableau's text line pitch is ~1.2 x the font size (Tableau Cloud: title to 14pt value 38px, value to
                # 10pt delta 21px); a looser pitch pushes the last authored line out of a short KPI zone
                + "container-type:inline-size;line-height:1.2;padding:2px 4px 0;" + sizing + "'>" + body + "</div>",
                unsafe_allow_html=True)
            return
    # Tableau KPI-card pattern: one MAIN measure + direction-gated %deltas
    # (CP.+% / CP.-% -- only one is non-null). Render as ONE metric with a
    # signed delta, the way the Tableau card shows it, instead of a flat row
    # of raw measures where the empty direction reads as a broken value.
    mains = [i for i, (m, sp) in enumerate(resolved)
             if sp and "%" not in m["caption"]]
    deltas = [i for i, (m, sp) in enumerate(resolved)
              if sp and "%" in m["caption"]]
    if len(mains) == 1 and deltas:
        i = mains[0]
        m, sp = resolved[i]
        dval, dsign = None, 1
        for j in deltas:
            v = row.get(f"M{j}")
            if v is not None and not pd.isna(v):
                dval = float(v)
                pattern = str(resolved[j][0].get("tableau_format") or "")
                dsign = (-1 if ("▼" in pattern and "▲" not in pattern)
                         or _re2.search(r"-\s*%|\B-%", resolved[j][0]["caption"])
                         else (-1 if dval < 0 else 1))
                break
        delta = (f"{dsign * abs(dval):+.1%}" if dval is not None else None)
        st.metric(m.get("label", m["caption"]),
                  fmt_val(row.get(f"M{i}"), m.get("fmt") or sp["fmt"]),
                  delta=delta)
        return
    cols = st.columns(len(ms))
    for i, (c, (m, sp)) in enumerate(zip(cols, resolved)):
        if sp is None or row.get(f"M{i}") is None:
            c.metric(m.get("label", m["caption"]), "n/a",
                     help="Not convertible -- see migration notes")
            continue
        fmt = m.get("fmt") or sp["fmt"]
        c.metric(m.get("label", m["caption"]), fmt_val(row[f"M{i}"], fmt))


_PLACEHOLDER_MEAS = _re2.compile(r"^(MIN|MAX|AVG|SUM)\(\s*-?\d+(\.\d+)?\s*\)$", _re2.I)


def _is_placeholder_only(s):
    """All measures are constant placeholders (AVG(0)/MIN(0)) -> the sheet is
    a Tableau member LIST/legend positioned by a dummy axis, not a chart."""
    ms = s.get("measures") or []
    return bool(ms) and all(
        _PLACEHOLDER_MEAS.match(str(m.get("caption", "")).strip()) for m in ms)


def _placeholder_list(s, where):
    """Render a placeholder-measure sheet as the member list it is: its
    dimension + text columns as a table (e.g. Select Products -> product name
    + release date; Product img -> product name + category). Generic: any
    Tableau list/legend sheet with a dummy measure axis."""
    T = tbl(s)
    order = []
    if s.get("dim"):
        order.append(s["dim"])
    for c in s.get("text_fields", []):
        cc = c.strip().strip("'\"")
        if cc and cc not in order and not _PLACEHOLDER_MEAS.match(cc):
            order.append(cc)
    avail = table_columns(T)
    sel, labels = [], []
    for c in order:
        if px(c) in avail:
            sel.append(f"{px(c)} AS C{len(sel)}")
            labels.append(c)
        elif c in CALCS and not CALCS[c].get("agg_ready") and " OVER (" not in CALCS[c]["sql"]:
            sel.append(f"({CALCS[c]['sql']}) AS C{len(sel)}")
            labels.append(c)
    if not sel:
        return False
    df = q(f"SELECT DISTINCT {', '.join(sel)} FROM {T} {where} LIMIT 200")
    df.columns = labels
    if s.get("mark") in ("Shape",) and "img" in s["name"].lower():
        findings.record("APPEARANCE", s["name"], "image-marks",
                        "Tableau shows image marks; rendered as the member "
                        "list (image thumbnails not reproduced).")
    _safe_dataframe(df, use_container_width=True, hide_index=True)
    return True


def _select_param_measure(ms, sel_cap):
    """Narrow an mbar measure list down to the ONE the current parameter value
    selects -- Tableau's native Measure Names filter set to Condition -> By
    formula (no calculated field), as opposed to the calc-based CASE-on-
    parameter swap, which needs no special-casing since it just resolves like
    any other calc measure. Falls back to the full list (unfiltered, same as
    before this existed) if the parameter's current value matches nothing --
    never crashes, never silently hides every measure."""
    if not sel_cap:
        return ms
    cur = str(PARAMS.get(sel_cap, "")).strip().strip('"').lower()
    picked = [m for m in ms if m["caption"].strip().lower() == cur]
    return picked or ms


def r_mbar(s, where):
    """Multi-measure bar panel: one horizontal bar small-multiple per measure,
    sharing a single row dimension (e.g. Region), value-labelled per measure.
    Narrowed to a single measure when the sheet's Measure Names filter is
    parameter-driven (s['param_select'])."""
    # Scoped occurrence adapters establish real field roles. A display caption
    # that resembles AVG(0) must not redirect those fields to a member list.
    if not s.get('worksheet_semantics') and _is_placeholder_only(s) and _placeholder_list(s, where):
        return
    T = tbl(s)
    dim = s.get("dim")
    ms = _select_param_measure(s.get("measures", []), s.get("param_select"))
    if not ms:
        st.info(f"{s['name']}: nothing to show"); return
    # Tableau uses Measure Names as an implicit dimension when several
    # measures are placed on one shelf.  It does not appear as an ordinary
    # workbook column, so the parser correctly has no physical `dim`.  The
    # old renderer treated that valid measure-only view as malformed and
    # emitted "nothing to show" (funnels and compact KPI comparison bars were
    # the common casualties).  Materialize Measure Names/Values here.
    if not dim:
        keep, exprs = [], []
        for m in ms:
            sp = resolve_measure(T, s["name"], m)
            if sp and not sp.get("window"):
                exprs.append(sp["sql"] + f" AS M{len(keep)}")
                keep.append((m, sp))
        if not keep:
            st.warning(f"{s['name']}: no convertible measures -- see migration notes")
            return
        row = q(f"SELECT {', '.join(exprs)} FROM {T} {where}")
        if row.empty:
            st.info(f"{s['name']}: no data for current filters")
            return
        data = []
        for j, (m, sp) in enumerate(keep):
            value = row.iloc[0, j]
            if value is None or pd.isna(value):
                continue
            data.append({"MEASURE": m.get("label") or m["caption"],
                         "VAL": float(value),
                         "LBL": fmt_tableau(value, m, sp)})
        if not data:
            st.info(f"{s['name']}: no data for current filters")
            return
        df = pd.DataFrame(data)
        order = df["MEASURE"].tolist()
        vmax = max(float(df["VAL"].abs().max()), 1.0)
        bar = alt.Chart(df).mark_bar(
            color=s.get("mark_color") or "#f28e2b", size=24).encode(
            y=alt.Y("MEASURE:N", sort=order, title=None,
                    axis=alt.Axis(labelLimit=260, labelOverlap=False)),
            x=alt.X("VAL:Q", title=None, axis=None,
                    scale=alt.Scale(domain=[0, vmax * 1.25])),
            tooltip=[alt.Tooltip("MEASURE:N", title="Measure"),
                     alt.Tooltip("VAL:Q", title="Value")])
        text = alt.Chart(df).mark_text(
            align="left", dx=4, color="#3a3a3a", clip=False).encode(
            y=alt.Y("MEASURE:N", sort=order), x="VAL:Q", text="LBL:N")
        st.altair_chart((bar + text).properties(height=max(120, len(data) * 38)),
                        use_container_width=True)
        return
    dphys = rdim(dim)
    keep, sel = [], [f"{dphys} AS DIM"]
    for m in ms:
        sp = resolve_measure(T, s["name"], m)
        if sp and not sp.get("window"):
            sel.append(sp["sql"] + f" AS M{len(keep)}")
            keep.append(m)
    if not keep:
        st.warning(f"{s['name']}: no convertible measures -- see migration notes"); return
    # optional bar color: an aggregate-level bucket calc (e.g. Above Threshold?)
    cdim = s.get("color") if (isinstance(s.get("color"), dict)
                              and s["color"].get("kind") == "dimension") else None
    if cdim:
        ccap = cdim["caption"]
        if ccap in CALCS and CALCS[ccap]["agg_ready"]:
            sel.append(f"{CALCS[ccap]['sql']} AS C")
        elif _col_known(T, px(ccap)) or ccap in CALCS:
            sel.append(f"{rdim(ccap)} AS C")
        else:
            cdim = None
    df = q(f"SELECT {', '.join(sel)} FROM {T} {where} GROUP BY 1"
           + (", C" if (cdim and not (cdim['caption'] in CALCS and CALCS[cdim['caption']]['agg_ready'])) else ""))
    if _no_data(s, df):
        return
    df["DIM"] = df["DIM"].astype(str)
    morder = _manual_order(s, dim, df["DIM"].tolist())
    order = s.get("sort")
    if morder:
        row_order = morder
    elif order and order.get("field"):
        om = resolve_measure(T, s["name"], {"caption": order["field"], "agg": order.get("agg")})
        if om:
            d2 = "DESC" if str(order.get("dir", "desc")).lower().startswith("d") else "ASC"
            # tie-break by the dimension itself (Tableau "sort by Names" -> constant)
            od = q(f"SELECT {dphys} AS DIM, {om['sql']} AS SS FROM {T} {where} GROUP BY 1 ORDER BY SS {d2}, DIM ASC")
            row_order = od["DIM"].astype(str).tolist()
        else:
            row_order = df["DIM"].tolist()
    else:
        row_order = df["DIM"].tolist()
    # Tableau shows long category lists in a SCROLLABLE pane -- mirror that:
    # many rows render full-height inside a fixed-height scrolling container.
    wrap = st.container(height=560) if len(row_order) > 25 else st.container()
    with wrap:
        cols = st.columns(len(keep))
    for col, (j, m) in zip(cols, enumerate(keep)):
        fmt = m.get("fmt") or "num0"
        lbl = m.get("label", m["caption"])
        cols_keep = ["DIM", f"M{j}"] + (["C"] if "C" in df.columns else [])
        sub = df[cols_keep].rename(columns={f"M{j}": "VAL"}).copy()
        sub["LBL"] = sub["VAL"].map(lambda v: fmt_val(v, fmt))
        # Two real bugs found live 2026-08-06 on Superstore's CustomerOverview
        # (6-panel mbar, 4 regions): (1) Vega's default axis labelOverlap
        # heuristic hid 2 of 4 row labels even with ample vertical room per
        # band (30px for an 11px font) -- its overlap estimate is conservative
        # regardless of actual space, so it must be disabled explicitly, not
        # just given more room. (2) the end-of-bar value label for whichever
        # row had this panel's largest value ran past the plot area's own
        # width and got clipped by the SVG's default overflow:hidden -- the
        # x-domain was sized to fit only the BARS, reserving zero space for
        # the trailing label text. Padding the domain by an amount derived
        # from the widest formatted label (not a flat percentage -- "15.0%"
        # and "$739,814" need very different headroom) fixes both the
        # clipping and (paired with clip=False) removes Vega's OWN internal
        # clip-path so a label allowed to overflow isn't hard-cut mid-glyph.
        # Include negative and zero-only aggregates; a [0, max] domain hides
        # negative measures and reverses the axis for an all-negative panel.
        vmin = min(0.0, float(sub["VAL"].min()))
        vmax = max(0.0, float(sub["VAL"].max()))
        span = max(vmax - vmin, 1.0)
        longest_lbl = sub["LBL"].astype(str).map(len).max()
        pad_frac = min(0.05 * longest_lbl + 0.05, 0.55)
        xscale = alt.Scale(domain=[vmin, vmax + span * pad_frac / (1 - pad_frac)])
        base = alt.Chart(sub)
        if "C" in sub.columns:
            _apply_labels(sub, "C", (s.get("color") or {}).get("caption"))
            cdom_, crng_ = cat_colors((s.get("color") or {}).get("caption"),
                                      sorted(sub["C"].dropna().astype(str).unique().tolist()))
            color_enc = alt.Color("C:N", title=(s.get("color") or {}).get("caption", ""),
                                  scale=alt.Scale(domain=cdom_, range=crng_) if cdom_ else alt.Undefined,
                                  legend=alt.Legend() if j == 0 else None)
            bar = base.mark_bar().encode(
                y=alt.Y("DIM:N", sort=row_order, title=None,
                       axis=alt.Axis(labelFontSize=11, labelOverlap=False)),
                x=alt.X("VAL:Q", title=None, axis=None, scale=xscale),
                color=color_enc,
                tooltip=[alt.Tooltip("DIM:N", title=dim), alt.Tooltip("VAL:Q", title=lbl), "C:N"])
        else:
            bar = base.mark_bar(color=m.get("color") or s.get("mark_color") or SLATE).encode(
                y=alt.Y("DIM:N", sort=row_order, title=None,
                       axis=alt.Axis(labelFontSize=11, labelOverlap=False)),
                x=alt.X("VAL:Q", title=None, axis=None, scale=xscale),
                tooltip=[alt.Tooltip("DIM:N", title=dim), alt.Tooltip("VAL:Q", title=lbl)])
        txt = base.mark_text(align="left", dx=3, fontSize=11, color="#3a3a3a", clip=False).encode(
            y=alt.Y("DIM:N", sort=row_order),
            x=alt.X("VAL:Q", scale=xscale), text="LBL:N")
        ch = (bar + txt).properties(height=len(row_order) * 30 + 12, title=lbl)
        with col:
            st.altair_chart(ch, use_container_width=True)


def r_bar(s, where):
    T = tbl(s)
    y = s.get("y"); x = s.get("x"); color = s.get("color")
    horiz = s.get("orient") == "h"
    mref = x if horiz else y
    dcap = (y if horiz else x)["caption"]
    meas = resolve_measure(T, s["name"], mref)
    if meas is None:
        st.warning(f"{s['name']}: measure '{mref['caption']}' not convertible -- see migration notes")
        return
    dim = rdim(dcap)
    cmeas = None
    cdim = None
    if isinstance(color, dict) and color.get("kind") == "dimension":
        cdim = color                       # categorical color (e.g. quota buckets)
    if meas.get("window"):
        inner = f"SELECT {dim} AS DIM FROM {T} {where} GROUP BY 1"
        sql = _windowize(inner, [(meas["sql"], "VAL")], "DIM") + " ORDER BY DIM LIMIT 30"
        findings.record("WARNING", s["name"], "window-ordering",
                        f"Table-calc measure '{mref['caption']}' computed with "
                        f"best-effort ordering (by {dcap}).")
    else:
        sel = [f"{dim} AS DIM", f"{meas['sql']} AS VAL"]
        if isinstance(color, dict) and color.get("kind") == "measure":
            cmeas = resolve_measure(T, s["name"], color)
            if cmeas:
                sel.append(f"{cmeas['sql']} AS C")
        c_group = False
        if cdim:
            ccap = cdim["caption"]
            if ccap in CALCS and CALCS[ccap]["agg_ready"]:
                # aggregate-level bucket calc (CASE over SUM): SELECT-only
                sel.append(f"{CALCS[ccap]['sql']} AS C")
            else:
                sel.append(f"{rdim(ccap)} AS C"); c_group = True
        grp = "GROUP BY 1" + (", 3" if c_group else "")
        sql = f"SELECT {', '.join(sel)} FROM {T} {where} {grp} ORDER BY VAL DESC LIMIT 30"
    df = q(sql)
    df["__ACTION_VALUE"] = df["DIM"]
    df["DIM"] = df["DIM"].astype(str)
    _apply_labels(df, "DIM", dcap)
    if cdim:
        _apply_labels(df, "C", cdim["caption"])
    ranked = any(tc.get("type") == "Rank" for tc in s.get("table_calcs", []))
    if ranked:
        df = df.reset_index(drop=True); df["RNK"] = (df.index + 1).astype(str)
    # sort: manual order if captured, else by value
    morder = _manual_order(s, dcap, df["DIM"].tolist())
    # Release 27 (F6): no authored sort = Tableau's natural (ascending) member order; an authored sort keeps its
    # direction. Only the legacy path without semantics keeps the old by-value default.
    authored_sort = s.get("sort")
    if morder:
        dim_sort = morder
    elif s.get("worksheet_semantics") and not authored_sort:
        dim_sort = "ascending"
    elif authored_sort and str(authored_sort.get("dir", "")).lower() == "asc":
        dim_sort = "x" if horiz else "y"
    else:
        dim_sort = "-x" if horiz else "-y"
    if dim_sort in ("x", "y", "-x", "-y"):
        # an explicit member list: an encoding-based sort breaks layers that do not encode the value (labels)
        totals = df.groupby("DIM", sort=False)["VAL"].sum()
        dim_sort = totals.sort_values(ascending=not str(dim_sort).startswith("-"), kind="mergesort").index.astype(str).tolist()
    compact = bool(s.get("_compact"))
    # Tableau shelf scopes: a horizontal bar has its dimension on Rows and
    # its measure on Columns (vertical: the reverse). Honor what the author
    # hid -- the field name over the row labels (display-field-labels) and
    # the whole value axis (axis display=false): a KPI tile shows five
    # labeled bars and nothing else, not a '0M 4M' scale under them.
    dim_scope, meas_scope = ("rows", "cols") if horiz else ("cols", "rows")
    dim_title = None if dim_scope in (s.get("hide_field_labels") or []) else dcap
    hide_val_axis = meas_scope in (s.get("hide_axis") or [])
    # Release 27 (F9/F2): the dimension's own label colour / size from the worksheet XML (e.g. orange stars)
    dim_label = {}
    try:
        import xml.etree.ElementTree as _ET
        shelf = _ET.fromstring(s["worksheet_semantics"]["source_xml"]).findtext(".//table/" + dim_scope) or ""
        key = shelf.strip().rsplit("[", 1)[-1].rstrip("]").rsplit(":", 1)[0] if "[" in shelf else ""
        dim_label = ((s.get("ws_style") or {}).get("fields") or {}).get(key) or {}
    except Exception:
        dim_label = {}
    lab_kw = {}
    if dim_label.get("label.color"):
        lab_kw["labelColor"] = dim_label["label.color"]
    try:
        lab_kw["labelFontSize"] = float(dim_label["label.font-size"]) * 4 / 3
    except (KeyError, TypeError, ValueError):
        lab_kw["labelFontSize"] = 10 if compact else 11
    enc_dim = (alt.Y("DIM:N", sort=dim_sort, title=dim_title,
                     axis=alt.Axis(labelOverlap=False,
                                   labelLimit=88 if compact else 180, **lab_kw)) if horiz
               else alt.X("DIM:N", sort=dim_sort, title=dim_title))
    val_axis = alt.Axis(format=_axis_fmt(meas["fmt"]) if meas["fmt"] != "float" else ("~s" if "field_scopes" in IR else "$~s"))
    if hide_val_axis:
        val_axis = None
    val_scale = alt.Undefined
    if s.get("worksheet_semantics", {}).get("source_xml"):
        # Release 27 (F3): domain-expand='false' on the value shelf = the axis fits the data (no forced zero)
        import axis_xml
        if not axis_xml.shelf_zero(axis_xml.parse(s["worksheet_semantics"]["source_xml"]), meas_scope):
            lo, hi = float(df["VAL"].min()), float(df["VAL"].max())
            pad = (hi - lo) * 0.08 or abs(hi) * 0.02 or 1.0
            val_scale = alt.Scale(zero=False, domain=[lo - pad, hi + pad * 0.5], clamp=True)
    enc_val = (alt.X("VAL:Q", title=None, axis=val_axis, scale=val_scale) if horiz
               else alt.Y("VAL:Q", title=None, axis=val_axis, scale=val_scale))
    tip = [alt.Tooltip("DIM:N", title=dcap), alt.Tooltip("VAL:Q", title=mref["caption"], format=",.0f")]
    if ranked:                                   # rank is a tooltip field in Tableau
        tip.insert(1, alt.Tooltip("RNK:N", title="Rank"))
    enc = {"tooltip": tip}
    enc["y" if horiz else "x"] = enc_dim
    enc["x" if horiz else "y"] = enc_val
    if "C" in df.columns:
        if cmeas is not None and pd.api.types.is_numeric_dtype(df["C"]):
            enc["color"] = alt.Color("C:Q", title=color["caption"],
                                     scale=_alt_color_scale(color.get("scale"), cmeas["fmt"]))
        else:                       # categorical color (dim or bucket-label calc)
            ms_c = s.get("manual_sort") or {}
            dom = (ms_c.get("order") if ms_c.get("on") == color.get("caption") else None)
            dom = [v.replace("\\%", "%") for v in dom] if dom else None
            if dom is None:
                dom = sorted(df["C"].dropna().astype(str).unique().tolist())
            cdom, crng = cat_colors(color.get("caption"), dom)
            scale = (alt.Scale(domain=cdom, range=crng) if cdom
                     else (alt.Scale(domain=dom) if dom else alt.Undefined))
            enc["color"] = alt.Color("C:N", title=color["caption"], scale=scale, sort=dom)
        enc["tooltip"] = tip + ["C:N" if not (cmeas and pd.api.types.is_numeric_dtype(df["C"])) else "C:Q"]
        # A grouped bar OFFSET only makes sense when the color/group field is a
        # DIFFERENT dimension from the axis dimension. When color == the axis
        # dim (a bar simply colored by its own category), an offset reserves one
        # slot per color value inside EVERY axis band but fills only the single
        # matching one -- so each bar shrinks to 1/N of its band with the rest
        # blank (the "inconsistent gaps" between the Category bars). Treat that
        # as a plain colored bar, no offset.
        same_axis_field = (color.get("caption") == dcap)
        grouped_distinct = s.get("grouped") and not same_axis_field
        stacked = not grouped_distinct
        if grouped_distinct:
            # nested shelf dims (Category / Region): side-by-side groups
            if horiz:
                enc["yOffset"] = alt.YOffset("C:N")
            else:
                enc["xOffset"] = alt.XOffset("C:N")
        else:
            # force a deterministic stack order so label midpoints line up
            c_order = sorted(df["C"].dropna().astype(str).unique().tolist())
            df["_o"] = df["C"].astype(str).map({v: i for i, v in enumerate(c_order)})
            enc["order"] = alt.Order("_o:Q")
        bars = alt.Chart(df).mark_bar().encode(**enc)
    else:
        stacked = False
        bars = alt.Chart(df).mark_bar(color=s.get("mark_color") or PROFIT_COLORS[0]).encode(**enc)
    layers = [bars]
    if s.get("labels"):
        lfmt = meas["fmt"] if "field_scopes" in IR or meas["fmt"] != "float" else "money0"
        # Release 27 (F1): the value's own Tableau format (e.g. 'n#,##0.0' -> 4,393,084.1)
        from tableau_format import format_number
        tpat = (IR.get("fields") or {}).get(mref.get("field_id"), {}).get("format") if mref.get("field_id") else None
        df["LBLTXT"] = df["VAL"].map(lambda v: (format_number(v, tpat) or "").strip() or fmt_val(v, lfmt))
        projection = s.get('worksheet_semantics', {}).get('projection', {})
        if any(role in ('x:label', 'y:label') and oid in {
                o['id'] for o in s['worksheet_semantics']['occurrences'] if o['level'] == 'row'
                } for oid, role in projection.get('bindings', {}).items()):
            # Only validated same-grain label references enter this path.
            def label(row):
                # CX2 rule (F1): a label run that inserts the measure shows it in the field's Tableau format
                # (the same LBLTXT as a plain label: '4,351,309.5', not fmt_val's rounded '4,351,310')
                values = {dcap: row['DIM'], mref['caption']: row['LBLTXT']}
                return ''.join(str(values[r['field']]) if r.get('field') else r.get('text', '')
                               for r in s['label_runs'])
            df['LBLTXT'] = df.apply(label, axis=1)
        if "C" in df.columns and stacked:
            # STACKED bars: labels sit at each segment's midpoint, white,
            # and segments too small to fit a label stay unlabeled
            df = df.sort_values(["DIM", "_o"])
            df["_cum"] = df.groupby("DIM")["VAL"].cumsum()
            df["MIDV"] = df["_cum"] - df["VAL"] / 2
            span = float(df.groupby("DIM")["VAL"].sum().max() or 1)
            df.loc[df["VAL"] < 0.06 * span, "LBLTXT"] = ""
            tenc = {("x" if horiz else "y"): alt.X("MIDV:Q", title=None) if horiz
                    else alt.Y("MIDV:Q", title=None),
                    ("y" if horiz else "x"): enc["y" if horiz else "x"]}
            layers.append(alt.Chart(df).mark_text(fontSize=10, color="white")
                          .encode(text="LBLTXT:N", **tenc))
        else:
            # Dense KPI-card rows have no safe space beyond the bar end. Keep
            # the value inside the bar so it stays aligned and never clips at
            # the card boundary. Full-size charts retain the outside label.
            tkw = ({"align": "left", "x": 2, "color": "#333333"}
                   if horiz and s.get("_canvas") and val_scale is not alt.Undefined else
                   {"align": "right", "dx": -4, "color": "white"}
                   if horiz and compact else
                   ({"align": "left", "dx": 3, "color": "#3a3a3a"}
                    if horiz else
                    {"baseline": "bottom", "dy": -3, "color": "#3a3a3a"}))
            tenc = {k: v for k, v in enc.items() if k in ("x", "y", "xOffset", "yOffset")}
            if "x" in tkw:
                tenc.pop("x", None)          # pinned at the bar start (mark x), not at the value
            layers.append(alt.Chart(df).mark_text(fontSize=10, **tkw)
                          .encode(text="LBLTXT:N", **tenc))
    layers += _refline_rule(s, df, "VAL", axis="x" if horiz else "y")
    chart = alt.layer(*layers) if len(layers) > 1 else bars
    h = min(360, s["_hpx"]) if s.get("_hpx") else 360
    # Keep enough vertical room for every categorical bar. This prevents
    # Vega's responsive resize from visually dropping alternate categories
    # when a Tableau KPI tile is short but contains a top-5 list.
    if horiz and not s.get("_canvas"):
        h = max(h, 28 * max(1, df["DIM"].nunique()) + 35)
    from dashboard_actions import chart as action_chart
    import sys
    # Release 27 (F5): in a canvas the zone holds every row (Tableau's fit squeezes them), axes included
    props = {"height": h}
    if s.get("_canvas"):
        props["autosize"] = alt.AutoSizeParams(type="fit", contains="padding")
    action_chart(chart.properties(**props), s, '__ACTION_VALUE', sys.modules[__name__], use_container_width=True)


def r_timeseries(s, where, area):
    # Tableau compound axis ([avg:A + avg:B] on one shelf) = one stacked
    # pane per measure -- render each measure as its own chart.
    ys = s.get("ys")
    if ys and len(ys) > 1:
        for ym in ys:
            st.caption(ym.get("label", ym["caption"]))
            _timeseries_one(dict(s, y=ym, ys=None), where, area)
        return
    _timeseries_one(s, where, area)


# Release 27 (F4): Tableau's default tick labels on a CONTINUOUS date axis, by the authored grain
_TABLEAU_TIME_FMT = {"DAY": "%b %-d", "WEEK": "%b %-d", "MONTH": "%b %Y", "QUARTER": "Q%q %Y", "YEAR": "%Y"}


def _timeseries_one(s, where, area, bar=False):
    T = tbl(s)
    x, y = s["x"], s["y"]
    xp = px(x["caption"])
    part = {"yr": "YEAR", "tyr": "YEAR", "qr": "QUARTER", "tqr": "QUARTER", "mn": "MONTH", "tmn": "MONTH",
            "twk": "WEEK", "wk": "WEEK", "tdy": "DAY", "dy": "DAY",
            "mdy": "DAY"}.get(x.get("datepart") or "mn", "MONTH")
    # Scoped V3 compiles date semantics from the authored occurrence. Legacy
    # callers retain their existing path; a discrete date part must not be
    # grouped by truncated full dates or coerced to a Unix timestamp.
    date_plan = (s.get('worksheet_semantics', {}).get('projection', {}).get('date_axis'))
    discrete_date = bool(date_plan and date_plan['discrete'])
    date_function = 'DATE_PART' if date_plan and date_plan['mode'] == 'date-part' else 'DATE_TRUNC'
    if date_plan:
        part = date_plan['part']
    date_expression = f"{date_function}('{part}', {xp})"
    meas = resolve_measure(T, s["name"], y)
    if meas is None:
        st.warning(f"{s['name']}: measure '{y['caption']}' not convertible -- see migration notes")
        return
    color = s.get("color")
    panel = s.get("panel")
    # A color dimension that is a row-level WINDOW calc (e.g. {FIXED Order ID:
    # SUM(Profit)}>0) must be computed in a base CTE, not in GROUP BY directly.
    maximum_color = s.get('worksheet_semantics', {}).get('projection', {}).get('maximum_color')
    flag_expr, flag_mode = (None, None) if maximum_color else flag_dim(s, T, color)
    flag_windowed = flag_mode == "window"
    sel = [f"{date_expression} AS T", f"{meas['sql']} AS VAL"]
    grp = ["1"]
    if panel:
        sel.append(f"{px(panel)} AS PANEL"); grp.append(str(len(sel)))
    if flag_mode == "agg":
        sel.append(f"{flag_expr} AS FLAG")           # SELECT-only (agg grain)
    elif flag_mode == "group":
        sel.append(f"{flag_expr} AS FLAG"); grp.append(str(len(sel)))
    sql = f"SELECT {', '.join(sel)} FROM {T} {where} GROUP BY {', '.join(grp)}"
    if flag_windowed:
        sql = (f"WITH base AS (SELECT *, {flag_expr} AS FLAG FROM {T} {where}) "
               f"SELECT {date_expression} AS T, "
               + (f"{px(panel)} AS PANEL, " if panel else "")
               + "FLAG, " + f"{meas['sql']} AS VAL FROM base GROUP BY 1"
               + (", 2, 3" if panel else ", 2"))
    fixed_measure = s.get('worksheet_semantics', {}).get('projection', {}).get('fixed_measure')
    if fixed_measure:
        if fixed_measure['field_id'] != y.get('field_id') or fixed_measure['sql'] != meas['sql']:
            raise ValueError('Calendar FIXED measure translation changed')
        if panel or flag_mode:
            raise ValueError('Calendar FIXED composition is unplanned')
        from kpi_fixed_plan import compile_sql
        sql = compile_sql(', '.join(sel), T, where, sys.modules[__name__], group_by='1')
    df = q(sql)
    if maximum_color:
        maximum = df['VAL'].max()
        df['FLAG'] = df['VAL'].map(lambda v: maximum_color['maximum_label']
                                 if pd.notna(v) and pd.notna(maximum) and v == maximum
                                 else maximum_color['other_label'])
    if bar and df['T'].isna().any():
        raise ValueError('Null date-bar groups require an explicit null-display contract')
    if discrete_date:
        if df['T'].isna().any():
            raise ValueError('Null discrete-date headers require an explicit null-display contract')
        if date_plan['mode'] == 'date-part':
            df['T'] = df['T'].astype('int64')
        else:
            df['T'] = pd.to_datetime(df['T']).dt.strftime('%Y-%m-%d')
    else:
        df["T"] = pd.to_datetime(df["T"])
    # friendly labels: workbook aliases first, profile override second
    labels = value_labels((color or {}).get("caption", ""))
    if labels is not None and "FLAG" in df.columns:
        df["FLAG"] = df["FLAG"].astype(str).map(lambda v: _label_of(labels, v))
    val_fmt = {"pct": ".1%", "num0": ",.0f", "num2": ",.2f", "int": ",.0f"}.get(meas["fmt"], "$,.0f")
    y_axis = (None if 'rows' in s.get('hide_axis', []) and s.get("_presentation") != "native" else
              _axis_obj(meas["fmt"] if meas["fmt"] != "float" else "money0"))

    def one(d, title=None):
        # stack areas; NEVER stack lines (Vega-Lite happily stacks lines too,
        # which silently turns a multi-line chart into cumulative offsets)
        time_type = 'T:O' if discrete_date else 'T:T'
        x_axis = None if 'cols' in s.get('hide_axis', []) else alt.Undefined
        if x_axis is not None and not discrete_date:
            # Release 27 (F4): a continuous date axis labels its ticks the way Tableau does for the authored
            # grain ("Oct 4" for days, "Jan 2024" for months), never Vega's mixed "October / Fri 03 / Oct 05".
            x_axis = alt.Axis(format=_TABLEAU_TIME_FMT.get(part, '%b %-d, %Y'), labelOverlap=True)
        enc = dict(x=alt.X(time_type, title=None, sort='ascending', axis=x_axis) if discrete_date else alt.X(time_type, title=None, axis=x_axis),
                   y=alt.Y("VAL:Q", title=None, stack="zero" if area else None,
                           axis=y_axis),
                   tooltip=[time_type, alt.Tooltip("VAL:Q", format=val_fmt)])
        if "FLAG" in d.columns:
            dom = sorted(d["FLAG"].dropna().unique().tolist())
            cdom, crng = cat_colors((s.get("color") or {}).get("caption"), dom)
            if cdom:
                dom, rng = cdom, crng
            else:
                rng = PROFIT_COLORS if len(dom) == 2 else None
            enc["color"] = alt.Color("FLAG:N", title=(s.get("color") or {}).get("caption", ""),
                                     scale=alt.Scale(domain=dom, range=rng) if rng else alt.Undefined,
                                     legend=None if s.get('_legends') == [] else alt.Undefined)
            enc["tooltip"] = [time_type, "FLAG:N", alt.Tooltip("VAL:Q", format=val_fmt)]
        mc = s.get("mark_color")
        c = alt.Chart(d)
        bar_kw = {"color": mc} if mc else {}
        if bar and not discrete_date and s.get("_wpx"):
            # CX2 rule (mark-sizing automatic + continuous date): Tableau fills one date unit per bar
            import axis_xml
            src = (s.get('worksheet_semantics') or {}).get('source_xml')
            if not src or axis_xml.automatic_mark_size(src):
                bpx = axis_xml.date_bar_px(d["T"], part, int(s["_wpx"]) - (0 if y_axis is None else 50))
                if bpx:
                    bar_kw["size"] = bpx
                    enc["x"] = alt.X(time_type, title=None, axis=x_axis, scale=alt.Scale(padding=bpx / 2 + 2))
        c = (c.mark_bar(**bar_kw) if bar else
             c.mark_area(opacity=0.85, **({"color": mc} if mc and "FLAG" not in d.columns else {}))
             if area else c.mark_line(point=bool(s.get("labels")),
                                      **({"color": mc} if mc and "FLAG" not in d.columns else {})))
        base_height = 150 if title or s.get('_compact') else 200
        pane_height = min(base_height, s['_hpx']) if s.get('_hpx') else base_height
        if s.get('_presentation') == 'native' and s.get('_compact'):
            pane_height = 110
        c = c.encode(**enc).properties(height=pane_height, title=title or "")
        if s.get("labels") and not area:
            lfmt = meas["fmt"] if "field_scopes" in IR or meas["fmt"] != "float" else "money0"
            d = d.copy().sort_values("T").reset_index(drop=True)
            d["LBLTXT"] = d["VAL"].map(lambda v: fmt_val(v, lfmt))
            if len(d) > 16:              # dense series: label every k-th + last
                k = max(1, round(len(d) / 12))
                keep = set(range(0, len(d), k)) | {len(d) - 1}
                d.loc[~d.index.isin(keep), "LBLTXT"] = ""
            txt = alt.Chart(d).mark_text(fontSize=10, dy=-10, color="#3a3a3a").encode(
                x=enc["x"], y=enc["y"], text="LBLTXT:N")
            c = c + txt
        from dashboard_actions import chart as action_chart
        action_chart(c, s, 'T', sys.modules[__name__], use_container_width=True)

    if panel and "PANEL" in df.columns:
        vals = sorted(df["PANEL"].dropna().unique())
        if len(vals) > 8 and "FLAG" not in df.columns:
            # high-cardinality panel (e.g. one line per country): a wall of
            # small multiples is unreadable -- draw ONE multi-line chart.
            findings.record("INFO", s["name"], "panel-as-color",
                            f"{len(vals)} '{panel}' panels drawn as one "
                            f"multi-line chart (Tableau shows a scrollable wall).")
            d2 = df.rename(columns={"PANEL": "FLAG"})
            one(d2.groupby(["T", "FLAG"], as_index=False)["VAL"].sum())
        elif len(vals) > 8:
            findings.record("WARNING", s["name"], "panel-capped",
                            f"{len(vals)} '{panel}' panels; showing first 8.")
            for pv in vals[:8]:
                one(df[df["PANEL"] == pv], str(pv))
        else:
            for pv in vals:
                one(df[df["PANEL"] == pv], str(pv))
    else:
        one(df)


def r_scatter(s, where):
    if s.get('worksheet_semantics',{}).get('projection',{}).get('renderer')=='logical-scatter':
        import logical_scatter,sys
        return logical_scatter.render(s,where,sys.modules[__name__])
    if 'worksheet_semantics' in s:
        import sys
        from scatter_plan import render
        return render(s, where, sys.modules[__name__])
    T = tbl(s)
    x, y = s["x"], s["y"]
    # Dot / strip plot: y is a dimension (one measure across a category axis).
    if s.get("ydim"):
        ydim = rdim(y["caption"])
        xs = resolve_measure(T, s["name"], x)
        if xs is None:
            st.warning(f"{s['name']}: measure '{x['caption']}' not convertible"); return
        color = s.get("color")
        sel = [f"{ydim} AS Y", f"{xs['sql']} AS X"]
        if isinstance(color, dict) and color.get("kind") == "measure":
            cm = resolve_measure(T, s["name"], color)
            if cm:
                sel.append(f"{cm['sql']} AS C")
        df = q(f"SELECT {', '.join(sel)} FROM {T} {where} GROUP BY 1 ORDER BY X DESC LIMIT 50")
        enc = dict(x=alt.X("X:Q", title=_measure_display_label(x["caption"]), axis=alt.Axis(format=_axis_fmt(xs["fmt"]) if xs["fmt"] != "float" else "$~s")),
                   y=alt.Y("Y:N", sort="-x", title=y["caption"]),
                   tooltip=list(df.columns))
        if "C" in df.columns:
            enc["color"] = alt.Color("C:Q", title=(color or {}).get("caption", ""),
                                     scale=_alt_color_scale((color or {}).get("scale")))
        st.altair_chart(alt.Chart(df).mark_circle(size=90, opacity=0.8).encode(**enc)
                        .properties(height=380), use_container_width=True)
        return
    detail = s.get("detail")
    grain = detail.get("caption") if isinstance(detail, dict) else detail
    color = s.get("color")
    cdim_is_grain = False
    if not grain and isinstance(color, dict) and color.get("kind") == "dimension":
        # No explicit Detail field -- Tableau's mark still has a real level of
        # detail from whatever dimension drives Color/Label (a common, valid
        # authoring pattern: color-by-category scatter with nothing on
        # Detail). Without this, x/y aggregate across the WHOLE table with no
        # GROUP BY at all, collapsing every mark into a single point. Found
        # live 2026-09-10 on the real 'Sales Dashboard Ver 01' Scater sheet
        # (color/text = "Type Selected", detail = None): rendered as one
        # dot instead of one per type.
        grain = color.get("caption")
        cdim_is_grain = True
    xs = resolve_measure(T, s["name"], x)
    ys = resolve_measure(T, s["name"], y)
    if xs is None and ys is None:
        st.warning(f"{s['name']}: measures not convertible -- see migration notes"); return
    # grain may itself be a row-level CALC (e.g. a parameter-swap dimension
    # like "Type Selected" -- a CASE on a parameter, not a physical column),
    # not just a plain field: resolve it the same way rdim()/flag_dim() do
    # elsewhere, CALCS first, physical column second, instead of only ever
    # checking _col_known against px(), which a calc caption never matches.
    has_grain = bool(grain) and (grain in CALCS or _col_known(T, px(grain)))
    cdim_is_grain = cdim_is_grain and has_grain
    sel = []
    grp_pos = []
    if has_grain:
        sel.append(f"{rdim(grain)} AS G")
        grp_pos.append(str(len(sel)))
    shape = s.get("shape")
    shape_cap = shape.get("caption") if isinstance(shape, dict) else None
    shape_ok = bool(shape_cap) and (shape_cap in CALCS or _col_known(T, px(shape_cap)))
    shape_is_agg = bool(shape_cap in CALCS and CALCS[shape_cap].get("agg_ready"))
    shape_alias = None
    if shape_ok:
        if has_grain and shape_cap == grain:
            shape_alias = "G"
        elif shape_is_agg:
            # Aggregate calculations on Shape belong in SELECT, never GROUP BY.
            # Tableau commonly reuses the same sign/bucket calculation on Color
            # and Shape, so defer it until the measure encodings are resolved.
            shape_alias = "S"
        else:
            sel.append(f"{rdim(shape_cap)} AS S")
            grp_pos.append(str(len(sel)))
            shape_alias = "S"
    win_items = []
    if xs and xs.get("window"):
        win_items.append((xs["sql"], "X"))
    elif xs:
        sel.append(f"{xs['sql']} AS X")
    if ys and ys.get("window"):
        win_items.append((ys["sql"], "Y"))
    elif ys:
        sel.append(f"{ys['sql']} AS Y")
    cmeas = None
    if isinstance(color, dict) and color.get("kind") == "measure":
        cmeas = resolve_measure(T, s["name"], color)
        if cmeas and not cmeas.get("window"):
            sel.append(f"{cmeas['sql']} AS C")
    if shape_is_agg:
        if cmeas and shape_cap == color.get("caption"):
            shape_alias = "C"
        else:
            sel.append(f"{CALCS[shape_cap]['sql']} AS S")
    sz = s.get("size")
    if isinstance(sz, dict) and sz.get("kind") == "measure" and has_grain:
        smeas = resolve_measure(T, s["name"], sz)
        if smeas and not smeas.get("window"):
            sel.append(f"{smeas['sql']} AS SZ")
    grp = " GROUP BY " + ", ".join(grp_pos) if grp_pos else ""
    sql = f"SELECT {', '.join(sel)} FROM {T} {where}{grp}"
    if win_items:
        order_alias = "X" if (xs and not xs.get("window")) else ("Y" if (ys and not ys.get("window")) else "1")
        if order_alias in ("X", "Y"):
            order_alias += " DESC"
        sql = _windowize(sql, win_items, order_alias)
        findings.record("WARNING", s["name"], "window-ordering",
                        "Table-calc measure computed with best-effort ordering.")
    df = q(sql)
    if "X" not in df.columns or "Y" not in df.columns:
        st.warning(f"{s['name']}: axis measure missing -- see migration notes"); return
    enc = dict(x=alt.X("X:Q", title=_measure_display_label(x["caption"]),
                       scale=alt.Scale(zero=False),
                       axis=alt.Axis(format=_axis_fmt(xs["fmt"]) if (xs and xs["fmt"] != "float") else "$~s")),
               y=alt.Y("Y:Q", title=_measure_display_label(y["caption"]),
                       scale=alt.Scale(zero=False),
                       axis=alt.Axis(format=_axis_fmt(ys["fmt"]) if (ys and ys["fmt"] != "float") else "$~s")),
               tooltip=list(df.columns))
    shown = s.get("_legends")          # None: not a dashboard -> keep legends
    clegend = alt.Undefined if (shown is None or "color" in shown) else None
    slegend = alt.Undefined if (shown is None or "size" in shown) else None
    if "C" in df.columns:
        if pd.api.types.is_numeric_dtype(df["C"]):
            enc["color"] = alt.Color("C:Q", title=(color or {}).get("caption", ""),
                                     scale=_alt_color_scale((color or {}).get("scale")),
                                     legend=clegend)
        else:                       # calc measure returning labels (e.g. quota buckets)
            enc["color"] = alt.Color("C:N", title=(color or {}).get("caption", ""),
                                     legend=clegend)
    elif cdim_is_grain and "G" in df.columns:
        enc["color"] = alt.Color("G:N", title=color.get("caption", ""), legend=clegend)
    else:
        enc["color"] = alt.value(s.get("mark_color") or PROFIT_COLORS[0])
    if "SZ" in df.columns:
        enc["size"] = alt.Size("SZ:Q", title=_measure_display_label(
            (sz or {}).get("caption", ""), (sz or {}).get("caption", "")),
            legend=slegend)
    if shape_alias:
        enc["shape"] = alt.Shape(f"{shape_alias}:N", title=shape_cap)
    mark_kw = dict(opacity=0.6) if "SZ" in df.columns else dict(size=60, opacity=0.6)
    pts = (alt.Chart(df).mark_point(filled=True, **mark_kw).encode(**enc)
           if shape_alias else alt.Chart(df).mark_circle(**mark_kw).encode(**enc))
    layers = [pts]
    # A Tableau Text shelf contributes to the mark's visual identity.  For
    # the common scatter shape where the same categorical field supplies the
    # fallback grain/color, label the points with that resolved grain rather
    # than silently discarding the Text encoding.
    if s.get("text_fields") and "G" in df.columns:
        layers.append(alt.Chart(df).mark_text(
            align="left", dx=8, dy=6, fontSize=10, color="#3a3a3a"
        ).encode(x=enc["x"], y=enc["y"], text="G:N"))
    if s.get("trendline"):                       # captured trend line -> regression
        layers.append(alt.Chart(df).transform_regression("X", "Y")
                       .mark_line(color="#b0b0b0", strokeDash=[6, 4]).encode(x="X:Q", y="Y:Q"))
    layers += _refline_rule(s, df, "X", axis="x", only_axis=x["caption"])
    layers += _refline_rule(s, df, "Y", axis="y", only_axis=y["caption"])
    chart = alt.layer(*layers) if len(layers) > 1 else pts
    h = min(380, s["_hpx"]) if s.get("_hpx") else 380
    st.altair_chart(chart.properties(height=h), use_container_width=True)


def _dim_expr(d):
    """Resolve a (possibly date-part) dimension to a SQL expression."""
    col = rdim(d["caption"])
    dp = d.get("datepart")
    if dp in ("yr", "tyr"):
        return f"EXTRACT(YEAR FROM {col})"
    if dp in ("mn", "tmn"):
        return f"EXTRACT(MONTH FROM {col})"
    if dp in ("qr", "tqr"):
        return f"EXTRACT(QUARTER FROM {col})"
    return col


def r_heatmap(s, where):
    """Highlight table: compound row label (e.g. Category + Year) x a col dim,
    cells shaded + labeled by a measure. Single chart (no fragile faceting)."""
    T = tbl(s)
    ydims = s.get("y_dims") or [s.get("y", {"caption": "Category"})]
    cm = s["color_measure"]
    meas = resolve_measure(T, s["name"], cm)
    if meas is None:
        st.warning(f"{s['name']}: measure '{cm['caption']}' not convertible"); return
    xexpr = _dim_expr(s["x"])
    ysel = [f"{_dim_expr(d)} AS Y{i}" for i, d in enumerate(ydims)]
    sel = ysel + [f"{xexpr} AS XV", f"{meas['sql']} AS VAL"]
    grp = ", ".join(str(i + 1) for i in range(len(ysel) + 1))
    df = q(f"SELECT {', '.join(sel)} FROM {T} {where} GROUP BY {grp}")
    if _no_data(s, df):
        return
    ycols = [f"Y{i}" for i in range(len(ydims))]
    for c in ycols:
        df[c] = df[c].apply(lambda v: str(int(v)) if isinstance(v, float) and v == int(v) else str(v))
    df["ROWLAB"] = df[ycols].agg("  ".join, axis=1)
    roworder = (df[ycols + ["ROWLAB"]].drop_duplicates().sort_values(ycols)["ROWLAB"].tolist())
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    if s["x"].get("datepart") in ("mn", "tmn"):
        df["XLAB"] = df["XV"].astype(int).map(lambda m: months[m - 1]); xsort = months
    else:
        df["XLAB"] = df["XV"].astype(str); xsort = sorted(df["XLAB"].unique())
    h = max(380, 36 * len(roworder))
    y_enc = alt.Y("ROWLAB:O", sort=roworder, title=None,
                  axis=alt.Axis(labelOverlap=False, labelLimit=260, labelFontSize=12))
    rect = alt.Chart(df).mark_rect(stroke="white", strokeWidth=1).encode(
        x=alt.X("XLAB:O", sort=xsort, title=None,
                axis=alt.Axis(labelAngle=0, labelFontSize=12)),
        y=y_enc,
        color=alt.Color("VAL:Q", scale=alt.Scale(scheme="blues"), title=cm["caption"]))
    layers = [rect]
    if s.get("text"):
        mx = float(df["VAL"].max() or 0)
        layers.append(alt.Chart(df).mark_text(fontSize=11).encode(
            x=alt.X("XLAB:O", sort=xsort), y=y_enc,
            text=alt.Text("VAL:Q", format="$,.0f"),
            color=alt.condition(f"datum.VAL > {mx * 0.55}", alt.value("white"), alt.value("black"))))
    st.altair_chart(alt.layer(*layers).properties(height=h), use_container_width=True)


def _rank_html(disp, dim, numeric):
    """Render a rank list as a hand-built HTML table so four fit a row
    without wrapping mid-value (st.table/st.dataframe wrap in a narrow
    column: '$697,68'/'2', 'Quanti'/'ty', '13.8%'/'▲'). Tight Tableau-like
    styling: rank grey, dimension left, numbers right, everything nowrap."""
    def esc(x):
        return (str(x).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))
    cols = list(disp.columns)

    def align(c):
        return "right" if numeric.get(c) else "left"
    head = ("<th class='rkn'>#</th>"
            + "".join(f"<th style='text-align:{align(c)}'>{esc(c)}</th>"
                      for c in cols))
    body = []
    for idx, row in disp.iterrows():
        cells = f"<td class='rkn'>{idx}</td>" + "".join(
            f"<td style='text-align:{align(c)}'>{esc(row[c])}</td>"
            for c in cols)
        body.append(f"<tr>{cells}</tr>")
    # box-sizing:border-box + width:100% keeps padding INSIDE the column so
    # the last (Δ%) cell can't bleed into the neighbour table (the SiS
    # hairline overlap). auto layout + nowrap = no truncation; small
    # font/padding so content fits the column at SiS width.
    html = (
        "<style>"
        "div.rkwrap{overflow:hidden}"
        "table.rk{border-collapse:collapse;width:100%;box-sizing:border-box;"
        "font-size:0.74rem;font-family:inherit;margin:2px 0}"
        "table.rk th{color:#5a5a5a;font-weight:600;padding:3px 6px;"
        "border-bottom:1px solid #dcdcdc;white-space:nowrap}"
        "table.rk td{padding:3px 6px;border-bottom:1px solid #f0f0f0;"
        "white-space:nowrap;color:#1a1a1a}"
        "table.rk td.rkn,table.rk th.rkn{color:#9a9a9a;text-align:right;"
        "width:18px;padding-right:4px}"
        "table.rk th:last-child,table.rk td:last-child{padding-right:2px}"
        "</style>"
        f"<div class='rkwrap'><table class='rk'>"
        f"<thead><tr>{head}</tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table></div>")
    st.markdown(html, unsafe_allow_html=True)


def _rank_table(s, where):
    """Tableau rank-list sheets (MIN(0) placeholder axis + measures as TEXT
    marks, usually behind a top-N filter): render the way Tableau shows them
    -- rank, member, value, %delta with direction arrow. Returns False when
    the shape doesn't fit so the caller falls back to circles."""
    T = tbl(s)
    dims = [d["caption"] for d in (s.get("y_dims") or [])]
    dims = [d for d in dims if not (CALCS.get(d) or {}).get("window")]
    if len(dims) != 1:
        return False
    dim = dims[0]
    avail = table_columns(T)
    exprs = []                      # (caption, sql) -- calc OR raw measure
    for c in s.get("text_fields", []):
        if c == dim:
            continue                # the dim itself repeated as a label
        csp = CALCS.get(c)
        if csp and csp.get("agg_ready"):
            exprs.append((c, csp["sql"]))
        elif csp and not csp.get("window") and " OVER (" not in csp["sql"]:
            exprs.append((c, f"SUM(({csp['sql']}))"))   # row-level calc -> SUM
        elif px(c) in avail:        # raw field shown as text -> SUM
            exprs.append((c, f"SUM({px(c)})"))
    if not exprs:
        return False
    tf = [c for c, _ in exprs]
    cols = ", ".join(f"({sql}) AS V{i}" for i, (_, sql) in enumerate(exprs))
    # DISPLAY order the way Tableau shows the list: the sheet's computed
    # sort first, then the top-N filter's ranking expression, then V0.
    # The order expression is SELECTed as VORD (not put raw in ORDER BY) so
    # the window hoist in q() can rewrite it -- ORDER BY lives in the tail
    # the hoist never scans.
    ord_expr, direc = None, "DESC"
    srt = s.get("sort") or {}
    sfield = srt.get("field")
    if sfield and (CALCS.get(sfield) or {}).get("agg_ready"):
        ord_expr = CALCS[sfield]["sql"]
        direc = "DESC" if (srt.get("dir") or "desc") == "desc" else "ASC"
    else:
        tn = next((f for f in s.get("applied_filters", [])
                   if f.get("kind") == "top_n" and f.get("order_expr")), None)
        if tn:
            from calc_translator import translate_formula
            pre = _re2.sub(r"\[([^\]]+)\]",
                           lambda m: "(" + CALCS[m.group(1)]["sql"] + ")"
                           if m.group(1) in CALCS else m.group(0),
                           tn["order_expr"])
            osql, _ = translate_formula(pre, colmap=COLMAP)
            if osql:
                ord_expr = osql
                direc = "DESC" if (tn.get("dir") or "top") == "top" else "ASC"
    ord_sel = f", ({ord_expr}) AS VORD" if ord_expr else ""
    order_by = f"VORD {direc}" if ord_expr else "V0 DESC"
    try:
        df = q(f"SELECT {rdim(dim)} AS D, {cols}{ord_sel} FROM {T} {where} "
               f"GROUP BY 1 ORDER BY {order_by} NULLS LAST")
    except Exception as e:
        if "sum(VARCHAR" in str(e):
            # text detail fields (Customer Details cards): display value via
            # MIN -- SUM on a string column can never work
            cols2 = ", ".join(f"(MIN({px(c)})) AS V{i}" if f"SUM({px(c)})" == sql
                              else f"({sql}) AS V{i}"
                              for i, (c, sql) in enumerate(exprs))
            try:
                df = q(f"SELECT {rdim(dim)} AS D, {cols2}{ord_sel} FROM {T} "
                       f"{where} GROUP BY 1 ORDER BY {order_by} NULLS LAST")
            except Exception as e2:
                findings.record("WARNING", s["name"], "rank-table-fallback",
                                f"Text-list sheet not convertible: {str(e2)[:150]}")
                return False
        else:
            findings.record("WARNING", s["name"], "rank-table-fallback",
                            f"Text-list sheet not convertible: {str(e)[:150]}")
            return False
    if df.empty:
        return False
    _apply_labels(df, "D", dim)
    if EVIDENCE_CAPTURE:
        _record_chart_evidence(
            s, "rank_table", [dim], tf,
            df.rename(columns={"D": dim, **{f"V{i}": c for i, c in enumerate(tf)}}),
            query=f"SELECT {rdim(dim)} AS D, {cols}{ord_sel} FROM {T} {where} "
                  f"GROUP BY 1 ORDER BY {order_by} NULLS LAST")
    disp = pd.DataFrame()
    disp[dim] = df["D"].values
    numeric = {}                           # header -> is-number (right-align)
    for i, c in enumerate(tf):
        v = df[f"V{i}"]
        if "%" in c:                       # delta column: signed % + arrow
            hdr = "Δ%"
            # non-breaking space keeps '13.8% ▲' on ONE line
            disp[hdr] = [("–" if pd.isna(x) else
                          f"{abs(x):.1%} " + ("▲" if x >= 0 else "▼"))
                         for x in v]
            numeric[hdr] = True
        else:
            fmt = _fmt_guess(c)
            fmt = "cur0" if fmt == "float" and any(
                k in c.lower() for k in ("revenue", "aov", "sales")) else fmt
            hdr = _re2.sub(r"^C\.\s*", "", c).strip() or c
            hdr = {"Total Revenue": "Revenue", "Quantity": "Qty"}.get(hdr, hdr)
            disp[hdr] = [fmt_val(x, "num0" if fmt == "float" else fmt)
                         for x in v]
            numeric[hdr] = True
    disp.index = range(1, len(df) + 1)
    disp.index.name = "#"
    _rank_html(disp, dim, numeric)
    return True


def r_circle(s, where):
    if s.get('worksheet_semantics', {}).get('projection', {}).get('renderer') == 'percent-summary':
        import percent_summary
        return percent_summary.render(s, where, sys.modules[__name__])
    if s.get("worksheet_semantics", {}).get("projection", {}).get("renderer") == "rank-card":
        import rank_card_plan
        return rank_card_plan.render(s, where, sys.modules[__name__])
    """Per-product circles. If faceted by a column dim (e.g. Segment), render one
    sub-chart per value side by side via st.columns (robust, matches Tableau)."""
    T = tbl(s)
    # constant-placeholder axis (MIN(0), AVG(-0.5), ...) = Tableau's trick
    # for text-list sheets ("Top 3 X", value legends) -- render as a table
    _PLACEHOLDER = r"(MIN|AVG|MAX)\(\s*-?\d+(\.\d+)?\s*\)"
    xcap = str((s.get("x") or {}).get("caption", ""))
    xsql = str((CALCS.get(xcap) or {}).get("sql", ""))
    if (_re2.fullmatch(_PLACEHOLDER, xsql.strip(), _re2.I)
            or _re2.fullmatch(_PLACEHOLDER, xcap.strip(), _re2.I)):
        if _rank_table(s, where):
            return
    facet = s.get("facet_col")
    ydims = s.get("y_dims") or []
    xs = resolve_measure(T, s["name"], s["x"])
    if xs is None:
        st.warning(f"{s['name']}: measure '{s['x']['caption']}' not convertible"); return
    cm = s.get("color_measure")
    sel, grp = [], []
    if facet:
        sel.append(f"{rdim(facet)} AS FACET"); grp.append(str(len(sel)))
    for i, d in enumerate(ydims):
        sel.append(f"{rdim(d['caption'])} AS Y{i}"); grp.append(str(len(sel)))
    if s.get("detail"):
        sel.append(f"{rdim(s['detail'])} AS DET"); grp.append(str(len(sel)))
    shape = s.get("shape")
    shape_cap = shape.get("caption") if isinstance(shape, dict) else None
    shape_ok = bool(shape_cap) and (shape_cap in CALCS or _col_known(T, px(shape_cap)))
    shape_is_agg = bool(shape_cap in CALCS and CALCS[shape_cap].get("agg_ready"))
    if shape_ok and not shape_is_agg:
        sel.append(f"{rdim(shape_cap)} AS SHAPE"); grp.append(str(len(sel)))
    sel.append(f"{xs['sql']} AS X")
    cmeas = None
    if cm:
        cmeas = resolve_measure(T, s["name"], cm)
        if cmeas:
            sel.append(f"{cmeas['sql']} AS C")
    if shape_is_agg:
        if cmeas and shape_cap == cm.get("caption"):
            # Alias the already-selected calculation; the chart may use one
            # result column for quantitative color and nominal mark shape.
            shape_alias = "C"
        else:
            sel.append(f"{CALCS[shape_cap]['sql']} AS SHAPE")
            shape_alias = "SHAPE"
    else:
        shape_alias = "SHAPE" if shape_ok else None
    group_sql = f" GROUP BY {', '.join(grp)}" if grp else ""
    df = q(f"SELECT {', '.join(sel)} FROM {T} {where}{group_sql}")
    if _no_data(s, df):
        return
    ycols = [f"Y{i}" for i in range(len(ydims))] or None
    if ycols:
        df["ROWLAB"] = df[ycols].astype(str).agg(" - ".join, axis=1)
        yorder = sorted(df["ROWLAB"].unique())
    _cfmt = cmeas["fmt"] if cmeas else None
    _cscale = (_alt_color_scale(cm.get("scale"), _cfmt) if cm and cm.get("scale")
               else (alt.Scale(scheme="redgrey", domain=[-0.5, 0.5], clamp=True)
                     if _cfmt == "pct" else alt.Scale(scheme="redgrey", domainMid=0)))
    color_enc = (alt.Color("C:Q", title=(cm or {}).get("caption", ""), scale=_cscale)
                 if "C" in df.columns else alt.value(PROFIT_COLORS[0]))

    def one(d, title=None):
        enc = dict(x=alt.X("X:Q", title=s["x"]["caption"], axis=alt.Axis(format="$~s")),
                   tooltip=list(d.columns))
        if ycols:
            enc["y"] = alt.Y("ROWLAB:O", sort=yorder, title=None)
        enc["color"] = color_enc
        if shape_alias and shape_alias in d.columns:
            enc["shape"] = alt.Shape(f"{shape_alias}:N", title=shape_cap)
            ch = alt.Chart(d).mark_point(
                filled=True, size=70, opacity=0.7).encode(**enc)
        else:
            ch = alt.Chart(d).mark_circle(size=45, opacity=0.6).encode(**enc)
        return ch.properties(height=max(260, 22 * (d["ROWLAB"].nunique() if ycols else 8)),
                             title=title or "")

    if facet:
        vals = sorted(df["FACET"].dropna().unique())
        for c, v in zip(st.columns(len(vals)), vals):
            with c:
                st.altair_chart(one(df[df["FACET"] == v], str(v)), use_container_width=True)
    else:
        st.altair_chart(one(df), use_container_width=True)


def r_pctbar(s, where):
    """Percent-of-total strip: share of row count per segment (e.g. Ship Status),
    rendered as a single 100%-stacked horizontal bar with % labels."""
    T = tbl(s)
    seg = s.get("segment")
    if not seg:
        st.info(f"{s['name']}: no segment dimension"); return
    df = q(f"SELECT {rdim(seg)} AS SEG, COUNT(*) AS N FROM {T} {where} GROUP BY 1")
    if _no_data(s, df):
        return
    df["SEG"] = df["SEG"].astype(str)
    _apply_labels(df, "SEG", seg)
    # explicit segment layout (x/x2) so the PERCENT LABELS sit at midpoints,
    # printed on the segments the way Tableau shows them
    df = df.sort_values("N", ascending=False).reset_index(drop=True)
    total = float(df["N"].sum() or 1)
    df["PCT"] = df["N"] / total
    df["X1"] = df["PCT"].cumsum()
    df["X0"] = df["X1"] - df["PCT"]
    df["MID"] = (df["X0"] + df["X1"]) / 2
    df["LBL"] = df["PCT"].map(lambda v: f"{v:.1%}")
    order = df["SEG"].tolist()
    base = alt.Chart(df)
    cdom, crng = cat_colors(seg, order)
    seg_scale = (alt.Scale(domain=cdom, range=crng) if cdom
                 else alt.Scale(range=PROFIT_COLORS + ["#72b7b2", "#e45756"]))
    bar = base.mark_bar(height=52).encode(
        x=alt.X("X0:Q", title=None, axis=alt.Axis(format="%"),
                scale=alt.Scale(domain=[0, 1])),
        x2="X1:Q",
        color=alt.Color("SEG:N", sort=order, title=seg, scale=seg_scale),
        tooltip=["SEG:N", alt.Tooltip("N:Q", format=",.0f"),
                 alt.Tooltip("PCT:Q", format=".1%")])
    txt = base.mark_text(fontSize=13, fontWeight="bold", color="white").encode(
        x=alt.X("MID:Q"), text="LBL:N")
    st.altair_chart((bar + txt).properties(height=95), use_container_width=True)


def r_dots(s, where):
    """Dot timeline: category dim (y) x date (x), dot size/color encoded
    (e.g. Days to Ship by Product). Aggregated per (category, date[, color])."""
    T = tbl(s)
    x, y = s["x"], s["y"]
    xcol = px(x["caption"])
    part = _EXTRACT_PART.get(x.get("datepart") or "", None)
    xexpr = f"DATE_TRUNC('{part}', {xcol})" if part else xcol
    sel = [f"{rdim(y['caption'])} AS Y", f"{xexpr} AS X"]
    grp = ["1", "2"]
    color = s.get("color")
    if color and color.get("kind") == "dimension":
        sel.append(f"{rdim(color['caption'])} AS C"); grp.append(str(len(sel)))
    sz = s.get("size")
    smeas = resolve_measure(T, s["name"], sz) if (sz and sz.get("kind") == "measure") else None
    if smeas and not smeas.get("window"):
        sel.append(f"{smeas['sql']} AS SZ")
    df = q(f"SELECT {', '.join(sel)} FROM {T} {where} GROUP BY {', '.join(grp)} LIMIT 5000")
    if _no_data(s, df):
        return
    if len(df) == 5000:
        findings.record("INFO", s["name"], "row-cap",
                        "Dot timeline capped at 5000 points for rendering.")
    # keep the category axis readable: top 40 categories by point count
    ncat = df["Y"].nunique()
    if ncat > 40:
        top = df["Y"].value_counts().head(40).index
        df = df[df["Y"].isin(top)]
        findings.record("INFO", s["name"], "category-cap",
                        f"Showing top 40 of {ncat} categories (Tableau scrolls; "
                        f"a fixed chart cannot).")
    df["X"] = pd.to_datetime(df["X"])
    if color:
        _apply_labels(df, "C", color.get("caption"))
    enc = dict(x=alt.X("X:T", title=None),
               y=alt.Y("Y:N", title=y["caption"], sort="ascending",
                       axis=alt.Axis(labelLimit=220)),
               tooltip=list(df.columns))
    if "C" in df.columns:
        cdom, crng = cat_colors((color or {}).get("caption"),
                                sorted(df["C"].dropna().astype(str).unique().tolist()))
        enc["color"] = alt.Color("C:N", title=(color or {}).get("caption", ""),
                                 scale=alt.Scale(domain=cdom, range=crng) if cdom else alt.Undefined)
    if "SZ" in df.columns:
        enc["size"] = alt.Size("SZ:Q", title=_measure_display_label(
            (sz or {}).get("caption", ""), (sz or {}).get("caption", "")))
    h = max(300, 18 * df["Y"].nunique())
    st.altair_chart(alt.Chart(df).mark_circle(opacity=0.7).encode(**enc)
                    .properties(height=min(h, 700)), use_container_width=True)


def r_dual(s, where):
    if s.get("worksheet_semantics"):
        import dual_plan, sys
        return dual_plan.render(s, where, sys.modules[__name__])
    """TRUE dual-axis / combo chart: two measures overlaid on one x axis with
    INDEPENDENT y scales (left/right), each drawn with its own mark (bar+line,
    line+line, ...) exactly as the workbook's panes declare."""
    T = tbl(s)
    x = s["x"]
    ys = s.get("ys") or []
    if len(ys) != 2:
        return _timeseries_one(s, where, area=False)
    xp = px(x["caption"])
    dp = (x.get("datepart") or "mn").lower()
    yearly = dp in ("yr", "tyr")
    xexpr = (f"EXTRACT(YEAR FROM {xp})" if yearly
             else f"DATE_TRUNC('{_EXTRACT_PART.get(dp, 'MONTH')}', {xp})")
    sel, ok = [f"{xexpr} AS T"], []
    for i, ym in enumerate(ys):
        sp = resolve_measure(T, s["name"], ym)
        if sp and not sp.get("window"):
            sel.append(f"{sp['sql']} AS V{len(ok)}")
            ok.append((ym, sp))
    if len(ok) < 2:
        return _timeseries_one(dict(s, y=(ok[0][0] if ok else ys[0]), ys=None),
                               where, area=False)
    df = q(f"SELECT {', '.join(sel)} FROM {T} {where} GROUP BY 1")
    if _no_data(s, df):
        return
    if yearly:
        df["T"] = df["T"].astype(int).astype(str)
        x_enc = alt.X("T:O", title=None,
                      axis=None if "cols" in s.get("hide_axis", [])
                      else alt.Axis(labelAngle=0))
    else:
        df["T"] = pd.to_datetime(df["T"])
        x_enc = alt.X("T:T", title=None,
                      axis=None if "cols" in s.get("hide_axis", [])
                      else alt.Undefined)
    default_cols = ["#4e79a7", "#f28e2b"]
    series_layers = []
    for i, (ym, sp) in enumerate(ok):
        col = ym.get("color") or default_cols[i]
        lbl = ym.get("label") or _measure_display_label(ym["caption"])
        if sp["fmt"] != "float":
            fmt = _axis_fmt(sp["fmt"])
        elif float(df[f"V{i}"].abs().max() or 0) <= 1.5:
            fmt = "%"          # 0-1 rate scale: 0.4 reads as 40%, never "400m"
        else:
            fmt = "$~s"
        y_axis = (None if "rows" in s.get("hide_axis", []) else
                  alt.Axis(title=lbl, titleColor=col,
                           orient="left" if i == 0 else "right",
                           format=fmt,
                           labelExpr='replace(datum.label, "G", "B")'
                           if "$" in fmt else alt.Undefined))
        y_enc = alt.Y(f"V{i}:Q", axis=y_axis)
        base = alt.Chart(df).encode(x=x_enc, y=y_enc,
                                    tooltip=["T:O" if yearly else "T:T",
                                             alt.Tooltip(f"V{i}:Q", title=lbl, format=",.1f")])
        mk = (ym.get("mark") or "line").lower()
        if mk == "bar":
            mark = base.mark_bar(color=col, opacity=0.75, size=14)
        elif mk == "area":
            mark = base.mark_area(color=col, opacity=0.5)
        else:
            mark = base.mark_line(
                color=col,
                point=alt.OverlayMarkDef(filled=True, color=col, size=35),
                strokeWidth=2.5)
        refs = _refline_rule(s, df, f"V{i}", axis="y",
                             only_axis=ym.get("caption"))
        # Keep each reference line nested with its owning series so it shares
        # that series' scale; only the two series groups are independent.
        series = alt.layer(mark, *refs) if refs else mark
        series_layers.append((0 if mk == "bar" else 1, series))
    # bars behind lines
    series_layers.sort(key=lambda pair: pair[0])
    ch = alt.layer(*(series for _, series in series_layers)).resolve_scale(y="independent")
    h = 170 if s.get("_compact") else 320
    if s.get("_hpx"):
        h = min(h, s["_hpx"])
    if s.get("_dark"):        # inside a dark Tableau container (Gross vs Net)
        ch = (ch.properties(height=h, background="transparent")
                .configure_axis(labelColor="#e8e8e8", titleColor="#e8e8e8",
                                gridColor="#444444", domainColor="#888888"))
    else:
        ch = ch.properties(height=h)
    st.altair_chart(ch, use_container_width=True)


def r_pie(s, where):
    if s.get('worksheet_semantics',{}).get('projection',{}).get('renderer')=='composed-donut':
        from composed_pie import build
        plan=build(s,s['worksheet_semantics'])
        if plan!=s['worksheet_semantics']['projection'] or plan['status']!='MAPPED':raise ValueError('Composed donut plan is stale or incomplete')
        findings.record('APPEARANCE',s['name'],'composed-donut',plan['appearance_adaptation'])
    """Pie chart (explicit Tableau Pie mark): wedges = the measure per segment,
    workbook colors, value labels outside the arc."""
    T = tbl(s)
    seg = s.get("segment")
    m = s.get("measure")
    if not seg or not m:
        st.info(f"{s['name']}: nothing to show"); return
    meas = resolve_measure(T, s["name"], m)
    if meas is None:
        st.warning(f"{s['name']}: measure '{m['caption']}' not convertible"); return
    df = q(f"SELECT {rdim(seg)} AS SEG, {meas['sql']} AS VAL FROM {T} {where} GROUP BY 1")
    df = df.dropna(subset=["VAL"])
    if _no_data(s, df):
        return
    df["SEG"] = df["SEG"].astype(str)
    _apply_labels(df, "SEG", seg)
    if s.get('worksheet_semantics',{}).get('projection',{}).get('renderer')=='composed-donut':
        order=s['worksheet_semantics']['projection']['order']
        if not set(df['SEG']).issubset(set(order)):raise ValueError('Donut has categories absent from its authored sort contract')
        df=df.set_index('SEG').reindex([v for v in order if v in set(df['SEG'])]).reset_index()
    else:
        df = df.sort_values("VAL", ascending=False).reset_index(drop=True)
    fmt = m.get("fmt") or meas["fmt"]
    df["LBL"] = df["VAL"].map(lambda v: fmt_val(v, fmt if fmt != "float" else "money0"))
    order = df["SEG"].tolist()
    cdom, crng = cat_colors(seg, order)
    if crng:  # white wedges vanish on the white canvas (Tableau uses white
        crng = [("#d3d3d3" if str(c).lower() in ("#ffffff", "#fff", "white")
                 else c) for c in crng]      # as the wedge BORDER, not fill)
    scale = alt.Scale(domain=cdom, range=crng) if cdom else alt.Undefined
    base = alt.Chart(df).encode(
        # title=m["caption"] on theta (and on the tooltip below) -- without
        # it this was the ONLY channel on the chart with no real Tableau
        # caption anywhere, so the validation pack's column mapper refused
        # to compare the chart at all ("channel 'theta' ... no resolvable
        # Tableau caption"), found live 2026-08-11. `color` already titled
        # itself from `seg`; `theta` never did.
        theta=alt.Theta("VAL:Q", stack=True, title=m["caption"]),
        color=alt.Color("SEG:N", title=seg, scale=scale, sort=order,
                        # Release 27 (F14): in a canvas Tableau's legends are their own dashboard zones
                        legend=None if (s.get("_compact") or s.get("_canvas")) else alt.Undefined),
        order=alt.Order("VAL:Q", sort="descending"),
        tooltip=["SEG:N", alt.Tooltip("VAL:Q", format=",.0f", title=m["caption"])])
    # radius must fit the geometry column: 3-across rows give ~200px --
    # a fixed 130px arc CLIPS into a pac-man (caught by browser screenshot)
    r_out, r_in, tsize = (80, 50, 12) if s.get("_compact") else (130, 80, 16)
    total = fmt_val(df["VAL"].sum(), fmt if fmt != "float" else "money0")
    center_lines = [[(f"Total {total}", tsize, "#000000", True, None)]]
    if s.get("_canvas"):
        # Release 27 (F14): the ring fills its zone; a composed donut's hole is the inner pie pane's authored
        # size relative to the outer one; the centre text is the inner pane's authored label runs.
        avail_h = int(s.get("_hpx") or 220); avail_w = int(s.get("_wpx") or avail_h)
        r_out = max(20, min(avail_w, avail_h) // 2 - 6)
        ratio = 0.62
        try:
            import axis_xml
            panes = axis_xml.parse(s["worksheet_semantics"]["source_xml"])["marks"]
            explicit = {k: v for k, v in panes.items() if k != "0"} or panes   # the authored pie panes, not the automatic one
            sizes = sorted(float(v.get("size")) for v in explicit.values() if v.get("size"))
            if len(sizes) >= 2 and sizes[-1] > 0:
                ratio = sizes[-2] / sizes[-1]
        except Exception:
            pass
        r_in = int(r_out * min(max(ratio, 0.3), 0.95))
        if s.get("label_runs"):
            lines = [[]]
            for run in s["label_runs"]:
                text = total if run.get("field") else str(run.get("text", ""))
                for i, part in enumerate(text.split("\n")):
                    if i:
                        lines.append([])
                    if part.strip():
                        lines[-1].append((part, float(run.get("fontsize") or 9) * 4 / 3, run.get("fontcolor") or "#333333",
                                          str(run.get("bold", "")).lower() == "true", run.get("fontname")))
            center_lines = [ln for ln in lines if ln] or center_lines
    pie = base.mark_arc(outerRadius=r_out, innerRadius=r_in)  # donut, as Tableau
    layers = [pie]
    pitch = [max(p[1] for p in ln) * 1.25 for ln in center_lines]
    top = -sum(pitch) / 2
    for ln, h in zip(center_lines, pitch):
        text = "".join(p[0] for p in ln); size, color, bold, font = ln[0][1], ln[0][2], any(p[3] for p in ln), ln[0][4]
        layers.append(alt.Chart(pd.DataFrame({"t": [text]})).mark_text(
            size=size, color=color, fontWeight="bold" if bold else "normal", baseline="middle",
            dy=top + h / 2, **({"font": _font_stack(font)} if font else {})).encode(text="t:N"))
        top += h
    if s.get("labels") and not s.get("worksheet_semantics"):
        layers.append(base.mark_text(radius=r_out + 24, fontSize=11 if s.get("_compact") else 12, color="#333333")
                      .encode(text="LBL:N", color=alt.value("#333333")))
    hpie = 230 if s.get("_compact") else 360
    if s.get("_hpx"):
        hpie = min(hpie, s["_hpx"])
    st.altair_chart(alt.layer(*layers).properties(height=hpie, **({"autosize": alt.AutoSizeParams(type="fit", contains="padding")}
                                                               if s.get("_canvas") else {})),
                    use_container_width=True)
    if s.get("labels") and s.get("worksheet_semantics") and not (s.get("_canvas") and s.get("label_runs")):
        entries=[]
        palette=dict(zip(cdom,crng)) if cdom else {}
        for row in df.itertuples(index=False):
            swatch=_html.escape(str(palette.get(row.SEG,"#888888")),quote=True)
            entries.append('<div style="display:flex;gap:6px;justify-content:space-between;flex-wrap:wrap"><span><span style="display:inline-block;width:9px;height:9px;background:'+swatch+';margin-right:5px"></span>'+_html.escape(str(row.SEG))+'</span><strong>'+_html.escape(str(row.LBL))+'</strong></div>')
        st.markdown('<div class="v3-donut-labels" style="color:#333333;font-size:12px;padding:2px 4px">'+''.join(entries)+'</div>',unsafe_allow_html=True)


def r_dtbar(s, where):
    """Stacked bars per period (Tableau Bar mark / Automatic over a DISCRETE
    date pill, e.g. Tourism Income by Region: one bar per year, stacked by
    Region in the workbook's colors)."""
    if s.get('worksheet_semantics', {}).get('projection', {}).get('date_axis'):
        # Scoped date bars share the validated calendar grain and axis contract.
        # Stacking/labels are refused by that contract before this renderer runs.
        return _timeseries_one(s, where, area=False, bar=True)
    ys = s.get("ys")
    if ys and len(ys) > 1:
        for ym in ys:
            st.caption(ym.get("label", ym["caption"]))
            _dtbar_one(dict(s, y=ym, ys=None), where)
        return
    _dtbar_one(s, where)


def _dtbar_one(s, where):
    T = tbl(s)
    x, y = s["x"], s["y"]
    xp = px(x["caption"])
    dp = (x.get("datepart") or "yr").lower()
    yearly = dp in ("yr", "tyr")
    xexpr = (f"EXTRACT(YEAR FROM {xp})" if yearly
             else f"DATE_TRUNC('{_EXTRACT_PART.get(dp, 'MONTH')}', {xp})")
    meas = resolve_measure(T, s["name"], y)
    if meas is None:
        st.warning(f"{s['name']}: measure '{y['caption']}' not convertible -- see migration notes")
        return
    color = s.get("color")
    flag_expr, flag_mode = flag_dim(s, T, color)
    sel = [f"{xexpr} AS X", f"{meas['sql']} AS VAL"]
    grp = ["1"]
    if flag_mode == "agg":
        sel.append(f"{flag_expr} AS FLAG")           # SELECT-only (agg grain)
    elif flag_mode == "window":
        # row-grain window can't sit in a grouped SELECT; precompute in a CTE
        pass
    elif flag_mode == "group":
        sel.append(f"{flag_expr} AS FLAG"); grp.append(str(len(sel)))
    sql = f"SELECT {', '.join(sel)} FROM {T} {where} GROUP BY {', '.join(grp)}"
    if flag_mode == "window":
        sql = (f"WITH base AS (SELECT *, {flag_expr} AS FLAG FROM {T} {where}) "
               f"SELECT {xexpr} AS X, {meas['sql']} AS VAL, FLAG "
               f"FROM base GROUP BY 1, 3")
    df = q(sql)
    if _no_data(s, df):
        return
    if yearly:
        df["X"] = df["X"].astype(int).astype(str)
        x_enc = alt.X("X:O", title=None, axis=alt.Axis(labelAngle=0))
    else:
        df["X"] = pd.to_datetime(df["X"])
        x_enc = alt.X("X:T", title=None)
    enc = dict(x=x_enc,
               y=alt.Y("VAL:Q", title=None, stack="zero", axis=_axis_obj(meas["fmt"])),
               tooltip=[alt.Tooltip("X:O" if yearly else "X:T"),
                        alt.Tooltip("VAL:Q", format=",.0f")])
    if "FLAG" in df.columns:
        _apply_labels(df, "FLAG", color.get("caption"))
        dom = sorted(df["FLAG"].dropna().astype(str).unique().tolist())
        cdom, crng = cat_colors(color.get("caption"), dom)
        enc["color"] = alt.Color("FLAG:N", title=color.get("caption", ""),
                                 scale=alt.Scale(domain=cdom, range=crng) if cdom
                                 else alt.Undefined,
                                 legend=None if s.get("_compact") else alt.Undefined)
        enc["tooltip"].insert(1, "FLAG:N")
    ch = alt.Chart(df).mark_bar().encode(**enc)
    h = 150 if s.get("_compact") else 320   # sparkline bands: 3+ per row
    if s.get("_hpx"):
        h = min(h, s["_hpx"])
    st.altair_chart(ch.properties(height=h), use_container_width=True)


def r_treemap(s, where):
    """Treemap (Tableau Automatic mark with size+text, no axes): tiles sized
    by the measure, grouped/colored by the color dimension, $B value labels."""
    T = tbl(s)
    lbl = s.get("label")
    sz = s.get("size")
    if not lbl or not sz:
        st.info(f"{s['name']}: nothing to show"); return
    smeas = resolve_measure(T, s["name"], sz)
    if smeas is None:
        st.warning(f"{s['name']}: size measure not convertible -- see migration notes")
        return
    color = s.get("color")
    sel = [f"{rdim(lbl)} AS L", f"{smeas['sql']} AS V"]
    grp = ["1"]
    if color:
        sel.append(f"{rdim(color['caption'])} AS C"); grp.append("3")
    df = q(f"SELECT {', '.join(sel)} FROM {T} {where} GROUP BY {', '.join(grp)}")
    df = df.dropna(subset=["V"])
    df = df[df["V"] > 0]
    if _no_data(s, df):
        return
    mx = float(df["V"].max())
    scale, suffix = ((1e9, "B") if mx >= 2e9 else (1e6, "M") if mx >= 2e6 else (1, ""))
    df["VS"] = df["V"] / scale
    money = smeas["fmt"] in ("cur0", "cur2", "money0", "money2", "float")
    pre = "$" if money else ""
    try:
        import plotly.express as pex
        kw = {}
        if "C" in df.columns:
            _apply_labels(df, "C", color.get("caption"))
            cdom, crng = cat_colors(color.get("caption"),
                                    sorted(df["C"].dropna().astype(str).unique().tolist()))
            if cdom:
                kw["color_discrete_map"] = dict(zip(cdom, crng))
            fig = pex.treemap(df, path=["C", "L"], values="VS", color="C", **kw)
        else:
            fig = pex.treemap(df, path=["L"], values="VS")
        fig.update_traces(
            texttemplate="%{label}<br>" + pre + "%{value:,.0f}" + suffix,
            hovertemplate="%{label}<br>" + pre + "%{value:,.1f}" + suffix + "<extra></extra>")
        fig.update_layout(margin=dict(l=0, r=0, t=0, b=0), height=460)
        if EVIDENCE_CAPTURE:
            rename = {"L": lbl, "V": sz["caption"]}
            grain = [lbl]
            if "C" in df.columns:
                rename["C"] = color.get("caption")
                grain = [color.get("caption"), lbl]
            _record_chart_evidence(
                s, "treemap", grain, [sz["caption"]], df.rename(columns=rename),
                query=f"SELECT {', '.join(sel)} FROM {T} {where} "
                      f"GROUP BY {', '.join(grp)}")
        st.plotly_chart(fig, use_container_width=True)
    except Exception as e:
        findings.record("WARNING", s["name"], "treemap-fallback",
                        f"Treemap failed ({type(e).__name__}); bar fallback shown.")
        st.bar_chart(df.set_index("L")["V"])


def _pack_circles(radii):
    """Greedy circle packing on a golden-angle spiral (largest first).
    Returns [(x, y)] -- good-enough approximation of Tableau packed bubbles."""
    import math
    pos = []
    ga = math.pi * (3 - math.sqrt(5))
    for i, r in enumerate(radii):
        if i == 0:
            pos.append((0.0, 0.0))
            continue
        t = i * ga
        rad = 0.0
        step = max(r * 0.25, 0.4)
        while True:
            xx, yy = rad * math.cos(t), rad * math.sin(t)
            if all((xx - px_) ** 2 + (yy - py_) ** 2 >= (r + radii[j]) ** 2 * 0.96
                   for j, (px_, py_) in enumerate(pos)):
                pos.append((xx, yy))
                break
            rad += step
    return pos


def r_bubbles(s, where):
    """Packed bubbles (e.g. TourismByCountry): one circle per label dim,
    area = size measure, colored by a dimension, biggest labeled."""
    T = tbl(s)
    lbl = s.get("label")
    sz = s.get("size")
    if not lbl or not sz:
        st.info(f"{s['name']}: nothing to show"); return
    smeas = resolve_measure(T, s["name"], sz)
    if smeas is None:
        st.warning(f"{s['name']}: size measure not convertible -- see migration notes")
        return
    color = s.get("color")
    sel = [f"{rdim(lbl)} AS L", f"{smeas['sql']} AS V"]
    grp = ["1"]
    if color:
        sel.append(f"{rdim(color['caption'])} AS C"); grp.append("3")
    df = q(f"SELECT {', '.join(sel)} FROM {T} {where} GROUP BY {', '.join(grp)}")
    df = df.dropna(subset=["V"])
    if _no_data(s, df):
        return
    n0 = len(df)
    df = df.sort_values("V", ascending=False).head(40).reset_index(drop=True)
    if n0 > 40:
        findings.record("INFO", s["name"], "category-cap",
                        f"Showing top 40 of {n0} bubbles.")
    import numpy as np
    df["R"] = np.sqrt(df["V"].astype(float).clip(lower=0))
    df["R"] = df["R"] / df["R"].max() * 10
    xy = _pack_circles(df["R"].tolist())
    df["X"] = [p[0] for p in xy]
    df["Y"] = [p[1] for p in xy]
    # Altair size = mark AREA in px^2; scale to a readable range
    df["SIZE"] = (df["R"] ** 2) * 55
    df["TXT"] = df["L"].astype(str).where(df["R"] > df["R"].max() * 0.35, "")
    enc = dict(x=alt.X("X:Q", axis=None), y=alt.Y("Y:Q", axis=None),
               size=alt.Size("SIZE:Q", legend=None, scale=alt.Scale(range=[30, 5200])),
               tooltip=[alt.Tooltip("L:N", title=lbl),
                        alt.Tooltip("V:Q", title=sz["caption"], format=",.0f")]
                       + (["C:N"] if "C" in df.columns else []))
    if "C" in df.columns:
        _apply_labels(df, "C", color.get("caption"))
        cdom, crng = cat_colors(color.get("caption"),
                                sorted(df["C"].dropna().astype(str).unique().tolist()))
        enc["color"] = alt.Color("C:N", title=color.get("caption", ""),
                                 scale=alt.Scale(domain=cdom, range=crng) if cdom else alt.Undefined)
    else:
        enc["color"] = alt.value(PROFIT_COLORS[0])
    pts = alt.Chart(df).mark_circle(opacity=0.85).encode(**enc)
    txt = alt.Chart(df).mark_text(fontSize=10, color="white").encode(
        x="X:Q", y="Y:Q", text="TXT:N")
    st.altair_chart((pts + txt).properties(height=420), use_container_width=True)


def r_strips(s, where):
    """One dot-strip panel per measure (Tableau Circle mark with a compound
    measure shelf, e.g. Ease of Biz Factors: 3 measures x countries)."""
    if _is_placeholder_only(s) and _placeholder_list(s, where):
        return
    T = tbl(s)
    ms = s.get("measures", [])
    shape = s.get("shape")
    shape_cap = shape.get("caption") if isinstance(shape, dict) else None
    # Shape and Text shelves contribute to Tableau's mark grain just like
    # Detail.  Falling back to either prevents shape-based selector/image
    # sheets from becoming a blank "nothing to show" panel.
    det = s.get("detail") or shape_cap or next(iter(s.get("text_fields") or []), None)
    cm = s.get("color_measure")
    if not ms or not det:
        st.info(f"{s['name']}: nothing to show"); return
    cmeas = resolve_measure(T, s["name"], cm) if cm else None
    cols = st.columns(len(ms))
    for col, m in zip(cols, ms):
        sp = resolve_measure(T, s["name"], m)
        if sp is None or sp.get("window"):
            with col:
                st.warning(f"{m['caption']}: not convertible")
            continue
        sel = [f"{rdim(det)} AS D", f"{sp['sql']} AS V"]
        if cmeas and not cmeas.get("window"):
            sel.append(f"{cmeas['sql']} AS C")
        df = q(f"SELECT {', '.join(sel)} FROM {T} {where} GROUP BY 1")
        df = df.dropna(subset=["V"])
        enc = dict(x=alt.X("V:Q", title=m["caption"],
                           axis=alt.Axis(format=_axis_fmt(sp["fmt"]) if sp["fmt"] != "float" else "~s")),
                   tooltip=[alt.Tooltip("D:N", title=det),
                            alt.Tooltip("V:Q", format=",.1f")])
        if "C" in df.columns:
            enc["color"] = alt.Color("C:Q", title=(cm or {}).get("caption", ""),
                                     scale=_alt_color_scale((cm or {}).get("scale")))
            enc["tooltip"].append(alt.Tooltip("C:Q", format=",.1f"))
        else:
            enc["color"] = alt.value(PROFIT_COLORS[0])
        if shape_cap and det == shape_cap:
            enc["shape"] = alt.Shape("D:N", title=shape_cap)
            ch = alt.Chart(df).mark_point(
                filled=True, size=90, opacity=0.75).encode(**enc)
        else:
            ch = alt.Chart(df).mark_circle(size=70, opacity=0.7).encode(**enc)
        with col:
            st.altair_chart(ch.properties(height=340), use_container_width=True)


def r_gantt(s, where):
    """Gantt bars: each mark starts at the date and its LENGTH is the size
    measure in days (Tableau Gantt, e.g. Days to Ship by Product)."""
    T = tbl(s)
    x, y = s["x"], s["y"]
    xcol = px(x["caption"])
    # "START" is a RESERVED word in Snowflake (fine as an unquoted alias in
    # DuckDB, rejected by Snowpark) -> quote it. Quoting preserves the exact
    # case in BOTH engines, so df["START"] below still matches. Field-found on
    # the deployed 'Days to Ship by Product' gantt sheet.
    sel = [f"{rdim(y['caption'])} AS Y", f'{xcol} AS "START"']
    grp = ["1", "2"]
    color = s.get("color")
    if color and color.get("kind") == "dimension":
        sel.append(f"{rdim(color['caption'])} AS C"); grp.append(str(len(sel)))
    sz = s.get("size")
    smeas = resolve_measure(T, s["name"], sz) if (sz and sz.get("kind") == "measure") else None
    if smeas is None or smeas.get("window"):
        st.warning(f"{s['name']}: duration measure not convertible -- see migration notes")
        return
    sel.append(f"{smeas['sql']} AS DUR")
    df = q(f"SELECT {', '.join(sel)} FROM {T} {where} GROUP BY {', '.join(grp)} LIMIT 5000")
    if _no_data(s, df):
        return
    ncat = df["Y"].nunique()
    if ncat > 40:
        top = df["Y"].value_counts().head(40).index
        df = df[df["Y"].isin(top)].copy()
        findings.record("INFO", s["name"], "category-cap",
                        f"Showing top 40 of {ncat} categories (Tableau scrolls; "
                        f"a fixed chart cannot).")
    df["START"] = pd.to_datetime(df["START"])
    # zero-duration marks still need a visible tick (~0.35 day)
    df["END"] = df["START"] + pd.to_timedelta(
        df["DUR"].astype(float).clip(lower=0.35), unit="D")
    if "C" in df.columns:
        _apply_labels(df, "C", color.get("caption"))
    enc = dict(x=alt.X("START:T", title=None),
               x2="END:T",
               y=alt.Y("Y:N", title=y["caption"], sort="ascending",
                       axis=alt.Axis(labelLimit=220)),
               tooltip=[alt.Tooltip("Y:N", title=y["caption"]),
                        alt.Tooltip("START:T"),
                        alt.Tooltip("DUR:Q", title=(sz or {}).get("caption", "Duration"),
                                    format=",.0f")] + (["C:N"] if "C" in df.columns else []))
    if "C" in df.columns:
        cdom, crng = cat_colors(color.get("caption"),
                                sorted(df["C"].dropna().astype(str).unique().tolist()))
        enc["color"] = alt.Color("C:N", title=(color or {}).get("caption", ""),
                                 scale=alt.Scale(domain=cdom, range=crng) if cdom else alt.Undefined)
    h = max(300, 16 * df["Y"].nunique())
    ch = alt.Chart(df).mark_bar(height=5).encode(**enc)
    st.altair_chart(ch.properties(height=min(h, 700)), use_container_width=True)


def r_table(s, where):
    if s.get('worksheet_semantics',{}).get('projection',{}).get('renderer')=='static-tooltip':
        import decoration_plan,sys
        return decoration_plan.render(s,sys.modules[__name__])
    if s.get("worksheet_semantics"):
        import crosstab_plan, sys
        return crosstab_plan.render(s, where, sys.modules[__name__])
    T = tbl(s)
    cols_avail = table_columns(T)
    # a dim converts if it is a physical column, a row-level calc/GROUP, or an
    # aggregate-level bucket calc (conditional SET / Achieved-Quota style):
    # the last kind is SELECT-only -- it cannot be part of GROUP BY.
    def _dim_kind(d):
        if _col_known(T, px(d)):
            return "row"
        if d in CALCS:
            return "agg" if CALCS[d]["agg_ready"] else "row"
        return None
    dims = [d for d in s.get("dims", []) if _dim_kind(d) == "row"]
    agg_dims = [d for d in s.get("dims", []) if _dim_kind(d) == "agg"]
    for d in s.get("dims", []):
        if _dim_kind(d) is None:
            findings.record("WARNING", s["name"], "dim-skipped",
                            f"Table dimension '{d}' skipped: no column {px(d)} in {T}.")
    ms = s.get("measures", [])
    # B2 (Global Sales Dashboard's "Details Standard Class", a 3-datasource
    # BLEND): the same caption can land in BOTH the sheet's dims list and its
    # measures list -- found live, not hypothetical (SUM_OF_SALES and 'SALES '
    # each classified BOTH ways). Selecting a caption twice produces a
    # DataFrame with duplicate column names, which Arrow/Streamlit's own
    # dataframe serialization refuses outright ("Duplicate column names
    # found") -- the sheet rendered an inline error card instead of the
    # table. The measure's own aggregated SQL is the correct read (a raw dim
    # read via rdim() would repeat it unaggregated, once per row), so a
    # measure caption always wins; drop it from dims/agg_dims here, before
    # GROUP BY's own row count is derived from len(dims), rather than
    # deduplicating sel/labels after the fact.
    measure_caps = {m.get("label", m["caption"]) for m in ms}
    dims = [d for d in dims if d not in measure_caps]
    agg_dims = [d for d in agg_dims if d not in measure_caps]
    # row-level dims whose SQL is a WINDOW (conditional sets) cannot appear in
    # GROUP BY directly -> precompute them in a subquery source
    win_dims = [d for d in dims if " OVER (" in rdim(d)]
    if win_dims:
        pre = ", ".join(f"{rdim(d)} AS W{k}" for k, d in enumerate(win_dims))
        src = f"(SELECT *, {pre} FROM {T} {where}) t"
        where_out = ""
        wmap = {d: f"W{k}" for k, d in enumerate(win_dims)}
    else:
        src, where_out, wmap = f"{T} {where}", "", {}
    sel, labels = [], []
    for i, d in enumerate(dims):
        sel.append(f"{wmap.get(d, rdim(d))} AS C{i}"); labels.append(d)
    for j, d in enumerate(agg_dims):
        sel.append(f"{CALCS[d]['sql']} AS A{j}"); labels.append(d)
    for m in ms:
        sp = resolve_measure(T, s["name"], m)
        if sp and not sp.get("window"):
            sel.append(sp["sql"] + f" AS M{len(labels)}")
            labels.append(m.get("label", m["caption"]))
    if not sel:
        st.warning(f"{s['name']}: nothing convertible to show -- see migration notes"); return
    grp = ("GROUP BY " + ", ".join(str(i + 1) for i in range(len(dims)))) if dims else ""
    df = q(f"SELECT {', '.join(sel)} FROM {src} {where_out} {grp} LIMIT 200")
    df.columns = labels
    if len(df) == 0:
        findings.record("WARNING", s["name"], "empty-result",
                        "Query returned 0 rows -- check captured filters.")
    meas_labels = labels[len(dims) + len(agg_dims):]
    # Tableau detail tables sort by the measure (the workbook's sort field if
    # captured, else the first measure) descending
    srt = s.get("sort") or {}
    sort_col = (srt.get("field") if srt.get("field") in df.columns
                else (meas_labels[0] if meas_labels else None))
    if sort_col and pd.api.types.is_numeric_dtype(df[sort_col]):
        df = df.sort_values(sort_col, ascending=(srt.get("dir") == "asc"),
                            na_position="last").reset_index(drop=True)
    # in-cell bars: a Bar/Gantt mark on a table = Tableau's bar-in-table; show
    # each measure column as a ProgressColumn (bar sized by value)
    colcfg = {}
    if s.get("mark") in ("Bar", "Gantt") and hasattr(st, "column_config"):
        for c, m in zip(meas_labels, ms):
            if pd.api.types.is_numeric_dtype(df[c]) and len(df):
                fmt = m.get("fmt") or "num0"
                pfmt = ("$%.0f" if fmt.startswith("cur") else
                        "%.1f%%" if fmt == "pct" else "%.0f")
                mx = float(df[c].max()) or 1.0
                try:
                    colcfg[c] = st.column_config.ProgressColumn(
                        c, min_value=min(0.0, float(df[c].min())),
                        max_value=mx, format=pfmt)
                except Exception:
                    pass
    if EVIDENCE_CAPTURE:
        # Detail/list tables are Tableau's row-level views, not aggregated
        # charts -- their own displayed grain can legitimately be hundreds
        # of raw records (this sheet's own query already caps at 200). A
        # reviewer validates a detail table by spot-checking the top rows in
        # their displayed sort order, not by diffing all 200 -- so evidence
        # is capped to the same TOP N a reviewer would actually look at
        # (2026-08 explicit user decision), not the full displayed set.
        _record_chart_evidence(
            s, "table", dims + agg_dims, meas_labels,
            df.head(_EVIDENCE_TABLE_ROW_CAP),
            query=f"SELECT {', '.join(sel)} FROM {src} "
                  f"{where_out} {grp} LIMIT {_EVIDENCE_TABLE_ROW_CAP}")
    _safe_dataframe(df, use_container_width=True, hide_index=True,
                    column_config=colcfg or None)


def r_map(s, where):
    """Choropleth. Two geo scopes (US states / world countries, from the geo
    field name) x two color modes (continuous measure / categorical bucket calc,
    e.g. Birth Rate Bin -- aggregated per region with MAX as an ATTR stand-in)."""
    T = tbl(s)
    geo_cap = s["geo"]
    geo = px(geo_cap)
    if not _col_known(T, geo):
        findings.record("BLOCKER", s["name"], "geo-missing",
                        f"Geo column {geo} not found in {T}.")
        st.warning(f"{s['name']}: geo column not found"); return
    cm = s["color_measure"]
    cap = cm["caption"]
    categorical = (cap in CALCS and not CALCS[cap]["agg_ready"]
                   and " OVER (" not in CALCS[cap]["sql"])
    if categorical:
        expr = f"MAX({CALCS[cap]['sql']})"     # one bucket per region
    else:
        meas = resolve_measure(T, s["name"], cm)
        if meas is None:
            st.warning(f"{s['name']}: measure '{cap}' not convertible"); return
        expr = meas["sql"]
    df = q(f"SELECT {geo} AS GEO_NAME, {expr} AS C FROM {T} {where} GROUP BY 1")
    world = "country" in geo_cap.lower() or "nation" in geo_cap.lower()
    if world:
        loc_kw = dict(locations="GEO_NAME", locationmode="country names")
    else:
        df["CODE"] = df["GEO_NAME"].map(STATE_ABBR)
        unmapped = df["CODE"].isna().sum()
        if unmapped:
            findings.record("INFO", s["name"], "geo-unmapped",
                            f"{unmapped} region name(s) not in the US-state map and omitted.")
        df = df.dropna(subset=["CODE"])
        loc_kw = dict(locations="CODE", locationmode="USA-states", scope="usa")
    try:
        import plotly.express as pex
        if categorical or not pd.api.types.is_numeric_dtype(df["C"]):
            df = _apply_labels(df, "C", cap)
            vals = sorted(df["C"].dropna().unique())
            cdom, crng = cat_colors(cap, vals)
            kw = (dict(color_discrete_map=dict(zip(cdom, crng))) if cdom else
                  dict(color_discrete_sequence=["#4575b4", "#f28e2b", "#72b7b2",
                                                "#e45756", "#54a24b", "#b279a2"]))
            fig = pex.choropleth(df, color="C", hover_name="GEO_NAME",
                                 category_orders={"C": vals},
                                 labels={"C": cap}, **kw, **loc_kw)
        else:
            fig = pex.choropleth(df, color="C", hover_name="GEO_NAME",
                                 color_continuous_scale=DIVERGING,
                                 color_continuous_midpoint=0,
                                 labels={"C": cap}, **loc_kw)
        fig.update_layout(margin=dict(l=0, r=0, t=0, b=0), height=430)
        if EVIDENCE_CAPTURE:
            _record_chart_evidence(
                s, "map", [geo_cap], [cap],
                df.rename(columns={"GEO_NAME": geo_cap, "C": cap}),
                query=f"SELECT {geo} AS GEO_NAME, {expr} AS C FROM {T} "
                      f"{where} GROUP BY 1")
        st.plotly_chart(fig, use_container_width=True)
    except Exception as e:
        findings.record("WARNING", s["name"], "map-fallback",
                        f"Choropleth failed ({type(e).__name__}); bar fallback shown.")
        st.bar_chart(df.set_index("GEO_NAME")["C"])


def _apply_drill(s):
    """Hierarchy drill-down as a LEVEL SELECTOR (Streamlit has no
    click-to-drill): swap the sheet's hierarchy dimension for the chosen
    level. Headless probes' fake selectbox returns None -> keep current."""
    d = s.get("drill")
    if not d:
        return s
    try:
        lvl = st.selectbox("Drill: %s" % d["name"], d["levels"],
                           index=d["levels"].index(d["current"]),
                           key="drill_%s" % s["name"])
    except Exception:
        lvl = None
    if not lvl or lvl == d["current"] or lvl not in d["levels"]:
        return s
    import copy
    s2 = copy.deepcopy(s)
    # avoid duplicating a dim that is ALREADY on the sheet at another slot
    existing = {f.get("caption") for k in ("x", "y", "dim", "panel", "color")
                for f in [s2.get(k)] if isinstance(f, dict)}
    for lk in ("y_dims", "x_dims"):
        existing |= {f.get("caption") for f in (s2.get(lk) or [])
                     if isinstance(f, dict)}
    if lvl in existing:
        return s
    if d.get("slot_idx") is not None:          # dim inside a list slot
        lst = s2.get(d["slot"]) or []
        if d["slot_idx"] < len(lst) and isinstance(lst[d["slot_idx"]], dict):
            lst[d["slot_idx"]]["caption"] = lvl
    else:
        for k in [d.get("slot")] + [x for x in ("x", "y", "dim", "panel", "color")
                                    if x != d.get("slot")]:
            f = s2.get(k)
            if isinstance(f, dict) and f.get("caption") == d["current"]:
                f["caption"] = lvl
    return s2


def _drop_window_dims(s):
    """Dims that are WINDOW table calcs (INDEX() = ROW_NUMBER over the view
    order) can't join a grouped query in kinds without window precompute.
    Drop them WITH a finding -- per-member values stay correct; only the
    INDEX-based ordering/limit is lost. Never a raw-column crash."""
    hit = []
    def _is_win(f):
        c = CALCS.get((f or {}).get("caption")) if isinstance(f, dict) else None
        return bool(c and c.get("window"))
    s2 = None
    for lk in ("y_dims", "x_dims"):
        lst = s.get(lk) or []
        if any(_is_win(f) for f in lst):
            import copy
            s2 = s2 or copy.deepcopy(s)
            hit += [f["caption"] for f in lst if _is_win(f)]
            s2[lk] = [f for f in lst if not _is_win(f)]
    for k in ("dim", "panel", "color"):
        if _is_win(s.get(k)):
            import copy
            s2 = s2 or copy.deepcopy(s)
            hit.append(s[k]["caption"])
            s2[k] = None
    if s2 is not None:
        findings.record("WARNING", s["name"], "window-dim-dropped",
                        "Table-calc dimension(s) %s (view-order INDEX/RANK) "
                        "not supported in kind '%s'; member ordering/limit "
                        "not applied -- values per member are unchanged."
                        % (sorted(set(hit)), s.get("kind")))
        return s2
    return s


def render_sheet(s, where_parts):
    """Render one sheet; a reviewer-approved, proven query fix on the sheet applies only while it renders."""
    global ACTIVE_QUERY_FIX, ACTIVE_FIX_RECORD, ACTIVE_FIX_KEY
    if isinstance(s, dict) and s.get('review_hold'):
        # X14: held back by the reviewer at deploy (reason recorded with the deploy approval): not drawn
        findings.record("INFO", s.get('name'), "sheet-held-back", s['review_hold'])
        st.info(f"{s.get('name')}: held back by the reviewer — {s['review_hold']}")
        return None
    if isinstance(s, dict) and s.get('review_note'):
        # X13: a known difference the reviewer signed off, shown to app users under the sheet
        try:
            return render_sheet(dict(s, review_note=None), where_parts)
        finally:
            st.caption(f"Known difference: {s['review_note']}")
    previous, previous_rec, previous_key = ACTIVE_QUERY_FIX, ACTIVE_FIX_RECORD, ACTIVE_FIX_KEY
    fix = s.get('query_fix') if isinstance(s, dict) else None
    ACTIVE_QUERY_FIX = fix if fix in QUERY_FIXES else None
    ACTIVE_FIX_KEY = s.get('query_fix_key') if ACTIVE_QUERY_FIX else None
    ACTIVE_FIX_RECORD = None
    if ACTIVE_QUERY_FIX:
        ACTIVE_FIX_RECORD = LAST_FIX_RENDER[s.get('name')] = {
            'Sheet': s.get('name'), 'Fix': ACTIVE_QUERY_FIX, 'Queries rewritten': 0,
            'Queries not applicable': 0, 'Rewrites failed': 0, 'Last error': ''}
    prev_ink = getattr(_CHART_INK, 'value', None)
    try:
        import v3_sheet_plan as _P
        _CHART_INK.value = _P.effective_background(s) if isinstance(s, dict) else None
    except Exception:
        _CHART_INK.value = None
    try:
        return _render_sheet_contained(s, where_parts)
    finally:
        _CHART_INK.value = prev_ink
        ACTIVE_QUERY_FIX, ACTIVE_FIX_RECORD, ACTIVE_FIX_KEY = previous, previous_rec, previous_key


def _render_sheet_contained(s, where_parts):
    """Contain preparation, SQL, and rendering failures within the worksheet; then (E19) the selection control of
    the general dashboard actions whose source is this sheet."""
    out = _render_sheet_drawn(s, where_parts)
    if isinstance(s, dict) and any(a.get('mode') == 'control' and a.get('status') == 'READY'
                                   and a.get('source') == s.get('name') for a in s.get('dashboard_actions') or []):
        try:
            import dashboard_actions
            dashboard_actions.source_control(s, where_parts, sys.modules[__name__])
        except Exception as exc:
            findings.record("WARNING", s.get("name", ""), "action-control-failed", type(exc).__name__)
    return out


def _render_sheet_drawn(s, where_parts):
    """Contain preparation, SQL, and rendering failures within the worksheet."""
    name = s.get("name", "(unnamed sheet)") if isinstance(s, dict) else "(invalid sheet)"
    try:
        problems = ir_contract.sheet_issues(s)
        if problems:
            raise ir_contract.IRValidationError(problems)
        if s.get("sheet_plan"):
            # Release 26: an exact plan from the workbook shelves (verified against Tableau when its evidence
            # says so). If the plan cannot run on this data, the original-engine approximation it carries is
            # drawn instead, labelled as such; the reason is recorded in the migration notes.
            import v3_sheet_plan
            import sys
            try:
                return v3_sheet_plan.render(s, where_parts, sys.modules[__name__])
            except Exception as exc:
                findings.record("WARNING", name, "exact-plan-not-run", f"{type(exc).__name__}: {exc}")
                if isinstance(s.get("approximation"), dict):
                    approx = dict(s["approximation"], **{k: s[k] for k in ("_canvas", "_hpx", "_wpx", "_dark", "_legends",
                                                                          "_compact") if k in s})
                    return _render_sheet_drawn(approx, where_parts)
                raise
        if s.get("ai_adaptation"):
            from cortex_assist import render
            import sys
            return render(s, where_parts, sys.modules[__name__])
        support = s.get("support") or {}
        if s.get("kind") == "unsupported" or support.get("status") in {"UNSUPPORTED", "INVALID"}:
            reason = support.get("reason", "No safe renderer is available")
            findings.record("BLOCKER", name, "sheet-unsupported", reason)
            st.warning(f"{name}: unsupported — {reason}")
            return
        if support.get("status") == "PARTIAL" and not s.get("non_data"):
            reason = support.get("reason", "Approximate rendering")
            findings.record("WARNING", name, "sheet-approximation", reason)
            if s.get("_canvas"):
                pass                         # release 26f: a caption inside the zone changes Tableau's layout; notes only
            elif s.get("approximated"):      # release 25a: one short line on the sheet; the reason is in the notes
                st.caption("Approximated with the original engine · not yet verified against Tableau")
            else:
                st.caption(f"Approximation: {reason}")
        if 'table_execution' in s.get('worksheet_semantics', {}):
            from table_plan import render
            import sys
            return render(s, where_parts, sys.modules[__name__])
        if 'pane_execution' in s.get('worksheet_semantics', {}):
            from worksheet_semantics import render_panes
            import sys
            return render_panes(s, where_parts, sys.modules[__name__])
        with field_scope(s):
            return _render_sheet_impl(s, where_parts)
    except Exception as exc:
        findings.record("BLOCKER", name, "sheet-lifecycle-failed", f"{type(exc).__name__}: {exc}")
        st.warning(f"{name}: could not render ({type(exc).__name__}: {exc})")


def _render_sheet_impl(s, where_parts):
    import sys
    if s.get('worksheet_semantics', {}).get('projection', {}).get('renderer') == 'static-tooltip':
        import decoration_plan
        return decoration_plan.render(s, sys.modules[__name__])
    if s.get('worksheet_semantics', {}).get('projection', {}).get('renderer') == 'member-card' and s['worksheet_semantics']['projection'].get('family') in ('parameter-buttons','parameter-button'):
        import member_card_plan
        return member_card_plan.render(s, '', sys.modules[__name__])
    # non-data sheet (text box / show-hide toggle / blank): nothing to query.
    # render nothing, record no error -- Tableau scaffolding, not a failure.
    if s.get("non_data"):
        findings.record("INFO", s["name"], "non-data-sheet",
                        "Text/control sheet (no data fields) -- nothing to "
                        "convert; rendered blank.")
        return
    title = _layout_text_value(s.get("title") or s["name"])
    show = (s.get("geom") or {}).get("show_title", True)
    if show and s.get("title_runs"):
        # Release 27 (F2): the worksheet title as Tableau formats it (sizes, fonts, colours, glyphs), on every path
        body, align = _runs_html(s["title_runs"], 12)
        st.markdown(f"<div style='text-align:{align};line-height:1.2'>{body}</div>", unsafe_allow_html=True)
    elif show:
        st.markdown(f"**{title}**")
    # UNMAPPED DATASOURCE = STOP. config.table_for() falls back to the default
    # table for an unknown caption, so a workbook nobody onboarded rendered
    # Superstore's numbers under this workbook's labels -- confident, plausible
    # and completely wrong (Fil Test: every sheet queried the dev Superstore
    # CSV instead of the extract the .twbx ships). Wrong data must fail loudly.
    ds = s.get("datasource")
    if ds and ds not in getattr(config, "DATASOURCES", {}):
        # Refuse rather than render Superstore's numbers under this sheet's
        # labels. Fail COMPACTLY (a caption, not a big red st.error) so a whole
        # dashboard of unmapped sheets does not become a wall of errors that
        # wrecks the column grid -- render_dashboard shows the one explanatory
        # banner. (Live-connection workbooks like EMEA ship no data and simply
        # cannot render locally; that is expected, not a code failure.)
        findings.record("WARNING", s["name"], "datasource-unmapped",
                        f"Datasource '{ds}' is not mapped to a local/Snowflake "
                        "table, so this sheet is not rendered (showing its "
                        "numbers would mean querying a different dataset).")
        st.caption(f"⚠ {s.get('title') or s['name']}: datasource '{ds}' not "
                   "mapped — see the note at the top of this dashboard.")
        return
    s = _apply_drill(s)
    if s.get("worksheet_semantics", {}).get("projection", {}).get("renderer") != "rank-card":
        s = _drop_window_dims(s)
    T = tbl(s)
    if s.get("blend"):
        findings.record("WARNING", s["name"], "blend-partial",
                        f"Sheet blends datasources {s['blend']}; only the primary "
                        f"({s.get('datasource')}) is queried.")
    if s.get("forecast"):
        findings.record("APPEARANCE", s["name"], "forecast-approximated",
                        "Tableau forecast (%s%s) is approximated with Holt-Winters "
                        "exponential smoothing; estimates differ from Tableau's model." %
                        (s["forecast"].get("model") or "ETS",
                         ", prediction bands" if s["forecast"].get("bands") else ""))
    if s.get("subtotals"):
        findings.record("APPEARANCE", s["name"], "subtotals-dropped",
                        "Worksheet subtotals/grand totals are not rendered "
                        "(detail rows only).")
    if s.get("viz_tooltip"):
        findings.record("COSMETIC", s["name"], "viz-in-tooltip",
                        "Viz-in-tooltip references sheet(s) %s; hover-embedded "
                        "charts are not reproduced." % ", ".join(s["viz_tooltip"]))
    if s.get("axis_flags"):
        findings.record("APPEARANCE", s["name"], "axis-scale",
                        "Axis uses %s in Tableau; rendered linear ascending here "
                        "-- visual comparison of magnitudes will differ."
                        % ", ".join(s["axis_flags"]))
    applicable = _parts_for_sheet(where_parts, s) if isinstance(where_parts, list) else where_parts
    where = _where_for(T, applicable) if isinstance(applicable, list) else (applicable or "")
    governed = ({p["col"] for p in applicable if isinstance(p, dict)}
                if isinstance(applicable, list) else set())
    dparts = applicable if isinstance(applicable, list) else None
    where = _apply_sheet_filters(s, where, T, governed=governed, dash_parts=dparts)
    from dashboard_actions import predicates as action_predicates
    import sys
    action_parts = action_predicates(s, sys.modules[__name__])
    if action_parts:
        where += (' AND ' if where.strip() else 'WHERE ') + ' AND '.join(action_parts)
    try:
        if s.get('worksheet_semantics', {}).get('projection', {}).get('renderer') == 'detail-record':
            import detail_record_plan
            return detail_record_plan.render(s, where, sys.modules[__name__])
        if s.get('worksheet_semantics', {}).get('projection', {}).get('renderer') == 'member-card':
            import member_card_plan
            return member_card_plan.render(s, where, sys.modules[__name__])
        if s.get('worksheet_semantics', {}).get('projection', {}).get('renderer') == 'bounded-overlay':
            import bounded_overlay
            return bounded_overlay.render(s, where, sys.modules[__name__])
        k = s["kind"]
        handler_name = ir_contract.CHART_HANDLERS.get(k)
        if handler_name is None:
            raise ValueError(f"No renderer registered for {k!r}")
        handler = globals()[handler_name]
        conds = context_conditions(s, governed, dparts)
        with lod_scope(("WHERE " + " AND ".join(conds)) if conds else ""):     # C17: FIXED before dim filters
            if k in ("line", "area"):
                handler(s, where, area=k == "area")
            else:
                handler(s, where)
    except Exception as e:
        findings.record("BLOCKER", s["name"], "render-failed",
                        f"{type(e).__name__}: {e}")
        st.warning(f"{title}: could not render ({type(e).__name__}: {e})")


def r_planned(s, where):
    """Release 26 planned sheets are drawn by v3_sheet_plan.render (dispatched in _render_sheet_contained)."""
    import v3_sheet_plan
    import sys
    return v3_sheet_plan.render(s, where, sys.modules[__name__])


def _rows_from_geom(sheets):
    """Group sheets into horizontal rows using Tableau zone geometry."""
    g = [s for s in sheets if s.get("geom")]
    if not g or len(g) != len(sheets):
        return None
    g = sorted(g, key=lambda s: (s["geom"]["y"], s["geom"]["x"]))
    rows, cur, ymax = [], [], None
    for s in g:
        gy, gh = s["geom"]["y"], s["geom"]["h"]
        if cur and gy < ymax - 0.4 * gh:        # vertically overlaps current row
            cur.append(s); ymax = max(ymax, gy + gh)
        else:
            if cur:
                rows.append(cur)
            cur, ymax = [s], gy + gh
    if cur:
        rows.append(cur)
    for r in rows:
        r.sort(key=lambda s: s["geom"]["x"])
    return rows


_CURRENT_DASH = None   # the dashboard being drawn (control / title zones read it)
_CURRENT_PARTS = []    # its WHERE parts (legend zones query with them)
_ZONE_PX = 0.011      # Tableau zone units (dashboard = 100000) -> pixels


def _zone_px(h):
    """Zone height -> a sane pixel height (clamped; content scrolls if it
    genuinely doesn't fit, which is also what Tableau does)."""
    if not h:
        return None
    return max(150, min(620, int(h * _ZONE_PX)))


def _luminance(hexcolor):
    try:
        h = hexcolor.lstrip("#")
        r, g, b = (int(h[i:i+2], 16) / 255 for i in (0, 2, 4))
        return 0.299 * r + 0.587 * g + 0.114 * b
    except Exception:
        return 1.0


_DARK_KEYS = []
_LIGHT_KEYS = []
_ZONE_CSS = []          # release 26d: per-zone rules, emitted once per dashboard


def _safe_dataframe(df, **kw):
    """st.dataframe with graceful degradation: SiS's older Streamlit lacks
    hide_index=/use_container_width=. Drop unsupported kwargs on TypeError.
    When hide_index isn't available, blank the index labels instead so the
    table still reads clean."""
    if kw.get("column_config") is None:
        kw.pop("column_config", None)
    zone_w = getattr(_ZONE_W, "value", None)
    if zone_w and kw.get("use_container_width"):
        # a canvas zone: its laid-out width (the canvas may be scaled to the page; see _zone_width)
        kw.pop("use_container_width")
        kw["width"] = max(40, int(zone_w) - 8)
    try:
        return st.dataframe(df, **kw)
    except TypeError:
        kw.pop("column_config", None)        # in-cell bars unsupported here
        kw.pop("hide_index", None)
        try:
            df = df.copy()
            df.index = [""] * len(df)      # emulate hide_index
        except Exception:
            pass
        try:
            return st.dataframe(df, **kw)
        except TypeError:
            kw.pop("use_container_width", None)
            return st.dataframe(df, **kw)


WRAP_TABLE_MAX_ROWS = 2000


def _wrapped_table(df, height=None, style=None):
    """A10: a text table whose workbook sets Format > Cells 'Wrap': an HTML table whose cells wrap on word
    boundaries (Streamlit's data grid cannot wrap). Header and cell font colour / size from the sheet's own
    formatting; scrolls inside the zone height. Values are escaped; nothing from the workbook is run."""
    import html as _h
    style = style or {}
    cell = style.get('cell') or {}
    head = style.get('header') or {}

    def css(rule, default_size):
        out = [f"font-size:{rule.get('font-size') + 'pt' if str(rule.get('font-size', '')).isdigit() else default_size}"]
        if str(rule.get('color', '')).startswith('#'):
            out.append(f"color:{rule['color'][:7]}")
        return ';'.join(out)
    cols = [str(c) for c in df.columns]
    th = ''.join(f"<th style='{css(head, '0.78rem')}'>{_h.escape(c)}</th>" for c in cols)
    body = []
    for row in df.itertuples(index=False):
        tds = ''.join(f"<td>{_h.escape('' if v is None or v != v else str(v))}</td>" for v in row)
        body.append(f'<tr>{tds}</tr>')
    box = f'max-height:{int(height)}px;' if height else ''
    st.markdown(
        "<style>table.v3wrap{border-collapse:collapse;width:100%;table-layout:auto}"
        "table.v3wrap th{position:sticky;top:0;background:#fafafa;text-align:left;font-weight:600;"
        "padding:4px 6px;border-bottom:1px solid #dcdcdc;white-space:normal;vertical-align:bottom}"
        f"table.v3wrap td{{padding:3px 6px;border-bottom:1px solid #f0f0f0;white-space:normal;"
        f"overflow-wrap:anywhere;vertical-align:top;{css(cell, '0.76rem')}}}</style>"
        f"<div style='{box}overflow:auto'><table class='v3wrap'><thead><tr>{th}</tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table></div>", unsafe_allow_html=True)


def _safe_container(**kw):
    """st.container with graceful degradation: Streamlit-in-Snowflake may run
    an older Streamlit than local (key= needs 1.39+, height='stretch' 1.44+).
    Drop unsupported kwargs instead of crashing the whole dashboard."""
    for drop in (None, "height", "key"):
        if drop:
            kw.pop(drop, None)
        try:
            return st.container(**kw)
        except TypeError:
            continue
        except Exception:
            continue
    return st.container()


def _max_col_nest():
    """Column-nesting depth cap. environment.yml pins Streamlit 1.52.2 in SiS,
    which (like local) allows deep nesting since 1.36 -- so the deployed app
    renders the same tree as local preview. Kept as a hook: if a future
    runtime restricts nesting, lower this. Defensive wrappers still degrade
    gracefully if a specific call is unsupported."""
    return 99


_INSERT_PREFIX = {"yr": "Year of ", "tyr": "Year of ", "qr": "Quarter of ", "tqr": "Quarter of ",
                  "mn": "Month of ", "tmn": "Month of ", "wk": "Week of ", "twk": "Week of ", "dy": "Day of ",
                  "tdy": "Day of ", "sum": "Sum of ", "avg": "Average of ", "cnt": "Count of ",
                  "ctd": "Distinct count of ", "min": "Minimum of ", "max": "Maximum of ", "med": "Median of "}


def _insert_value(part, caption):
    """The current selection of a dashboard control on this field (a single value), else 'All'."""
    if part in ("usr", "sum", "avg", "min", "max", "cnt", "ctd", "med", "agg"):
        return caption                     # a measure insert: its name (its value is not known here)
    part = None if part in ("none", "attr") else part
    for p in _CURRENT_PARTS:
        rf = p.get("runtime_filter") or {}
        if p.get("caption") != caption:
            continue
        if rf.get("kind") == "date_part" and rf.get("value") is not None and (part is None or rf.get("part") == part):
            v = rf["value"]
            return f"Q{v}" if rf.get("part") in ("qr", "tqr") else str(v)
        if rf.get("kind") == "categorical" and rf.get("value") is not None and part is None:
            return str(rf["value"])
    return "All"


def readable_inserts(text):
    """Tableau title/text field inserts -> words: '<[ds].[yr:Order Date:ok]>' -> 'Year of Order Date',
    '<[Parameters].[Churn Rate]>' -> 'Churn Rate', '<Sheet Name>' -> ''. Used where no live value is known."""
    def field(m):
        inner = m.group(1).replace("]]", "]")
        parts = inner.split(":")
        if len(parts) >= 3:
            return _INSERT_PREFIX.get(parts[0].lower(), "") + ":".join(parts[1:-1])
        return inner
    text = _re2.sub(r"<\s*\[(?:[^\]]|\]\])*\]\.\[((?:[^\]]|\]\])*)\]\s*>", field, str(text or ""))
    text = _re2.sub(r"<\s*(Sheet Name|Page Name|Workbook Name|Data Update Time|Data Source Name)\s*>", "", text)
    # F10: Tableau's user inserts show the signed-in viewer; when the app cannot know the viewer they are blank,
    # never the literal '<Full Name>'
    text = _re2.sub(r"<\s*(Full Name|User Name)\s*>", lambda m: _viewer_name(m.group(1)), text)
    return " ".join(text.split())


def _control_squeeze_css(key, inner_h, titled):
    """CSS that fits a compact quick filter (title line + box) into the zone the author drew. F7: the multiselect tag
    chip and its hidden search input are sized to the box too, so a short zone does not spill below itself."""
    box = max(12, int(inner_h - (12 if titled else 0)))
    k, t = f"div.st-key-{key}", max(8, box - 4)
    return (f"{k} [data-baseweb=select]>div,{k} .stMultiSelect div.group,{k} .stMultiSelect button,{k} [role=combobox]"
            f"{{min-height:{box}px !important;height:{box}px !important;max-height:{box}px;"
            f"padding-top:0;padding-bottom:0;overflow:hidden;}}"
            f"{k} .stMultiSelect svg{{width:12px;height:12px;}}"
            f"{k}>div:first-child [data-testid=stMarkdownContainer]>div{{line-height:1 !important;height:12px;overflow:hidden;}}"
            f"{k} [data-testid=stWidgetLabel]{{margin-bottom:0;}}"
            f"{k} [data-testid=stWidgetLabel] p{{line-height:1;}}"
            f"{k} [data-baseweb=tag]{{height:{t}px !important;min-height:0;margin-top:0 !important;"
            f"margin-bottom:0 !important;padding-top:0;padding-bottom:0;font-size:8pt;line-height:1;}}"
            f"{k} [data-baseweb=tag] span{{line-height:1;}}"
            f"{k} .stMultiSelect [data-baseweb=select]>div>div:first-child{{align-items:center;flex-wrap:nowrap;"
            f"padding-top:0 !important;padding-bottom:0 !important;height:{box}px;max-height:{box}px;overflow:hidden;}}"
            f"{k} .stMultiSelect input{{height:{t}px !important;min-height:0 !important;max-height:{t}px !important;"
            f"padding:0;margin:0;line-height:1 !important;}}")


def _viewer_name(kind):
    """The viewer's name for a <Full Name> / <User Name> insert, or '' when the app has no signed-in user."""
    # st.user first; st.experimental_user only on a Streamlit that has no st.user (1.52 shows a deprecation box)
    for attr in (("user",) if hasattr(st, "user") else ("experimental_user",)):
        try:
            u = getattr(st, attr, None)
            if u is None:
                continue
            get = u.get if hasattr(u, "get") else (lambda k, d=None: getattr(u, k, d))
            name = (get("name") if kind == "Full Name" else None) or get("user_name") or get("login_name")
            if name and isinstance(name, str):
                return name
        except Exception:
            continue
    return ""


def display_title(value):
    """A dashboard/app title for display (placeholders resolved, never raw Tableau tokens)."""
    try:
        return _layout_text_value(value)
    except Exception:
        return readable_inserts(_re2.sub(r"\{param:([^}]+)\}", r"\1", str(value or "")))


def _layout_text_value(value):
    """Resolve a Tableau dashboard text-zone string for the current rerun.

    Parser text leaves may contain ``{param:<internal field name>}``.  Map the
    internal name back through COLMAP, then apply the workbook's value alias so
    headings change with the same parameter widgets as their charts.
    """
    reverse_colmap = {str(v): k for k, v in COLMAP.items()}

    def repl(match):
        raw = match.group(1)
        cap = raw if raw in PARAMS else reverse_colmap.get(raw, raw)
        val = PARAMS.get(cap)
        if val is None:
            return cap
        val = _param_display_value(val)
        labels = value_labels(cap)
        return str(labels.get(val, val) if labels else val)

    text = _re2.sub(r"\{param:([^}]+)\}", repl, str(value or ""))
    # release 26g: a field insert shows the value the dashboard's control selects for that field, else 'All'
    # (Tableau: 'Executive Overview - Profitability (All)', 'Days to Ship by Product for Q4 of 2024')
    text = _re2.sub(r"<\s*\[(?:[^\]]|\]\])*\]\.\[((?:[^\]]|\]\])*)\]\s*>", lambda m: m.group(1), text)
    text = _re2.sub(r"\b(\w{2,4}):([^:,<>\[\]{}]+):(?:ok|nk|qk)\b",
                    lambda m: _insert_value(m.group(1), m.group(2)), text)
    text = readable_inserts(text)
    # Tableau's formatted-text runs can expose its legacy separator glyph in
    # XML. Treat it as whitespace instead of leaking "Æ" into the dashboard.
    return " ".join(text.replace("Æ", " ").split())


def canvas_zone_px(canvas):
    """Pixels per Tableau height unit: a fixed-size dashboard uses its height; a range / automatic one keeps the
    workbook's aspect ratio (min height / min width) at the app's page width (release 26c)."""
    canvas = canvas or {}
    lo, hi = canvas.get('minheight'), canvas.get('maxheight')
    if isinstance(hi, int) and hi > 0 and lo == hi:
        return hi / 100000
    w, h = canvas.get('minwidth'), canvas.get('minheight')
    if isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0:
        page = max(w, 1100)
        return page * (h / w) / 100000
    return _ZONE_PX


def _layout_with_canvas(node, canvas=None):
    """Use the authored fixed canvas height; leave unknown/dynamic sizes alone."""
    canvas = canvas or {}
    lo, hi = canvas.get('minheight'), canvas.get('maxheight')
    wide = canvas.get('maxwidth') if canvas.get('maxwidth') == canvas.get('minwidth') else None
    wscale = (wide / 100000 if isinstance(wide, int) and wide > 0 else
              max(canvas.get('minwidth') or 0, 1100) / 100000)
    scale = canvas_zone_px(canvas)
    def copy_node(source):
        result = dict(source)
        result['_zone_px'] = scale
        result['_zone_wpx'] = wscale
        if 'children' in source:
            result['children'] = [copy_node(child) for child in source['children']]
        if isinstance(source.get('floating'), list):
            result['floating'] = [dict(f, child=copy_node(f['child'])) for f in source['floating']]
        return result
    return copy_node(node)


def _render_layout(node, by_name, where_parts, compact=False, dark=False,
                   in_row=False, col_depth=0, _seq=[0]):
    """Contain malformed layout leaves while continuing valid siblings."""
    try:
        shallow = dict(node) if isinstance(node, dict) else node
        if isinstance(shallow, dict) and "children" in shallow and isinstance(shallow["children"], list):
            shallow["children"] = []
        problems = ir_contract.layout_issues(shallow, set(by_name))
        if problems:
            raise ir_contract.IRValidationError(problems)
        return _render_layout_impl(node, by_name, where_parts, compact, dark,
                                   in_row, col_depth, _seq)
    except Exception as exc:
        findings.record("BLOCKER", "(layout)", "layout-node-failed", f"{type(exc).__name__}: {exc}")
        st.warning(f"Layout component unavailable: {exc}")


DASH_NAV = None          # (session key, [dashboard names]) of the dashboard selector, set by the app


def _zone_box(px):
    """A dashboard zone's box: its authored height as a minimum (content is never clipped or scrolled).
    Controls inside are drawn compactly (Tableau's quick filters list one value per ~18 px row)."""
    _NAV_SEQ[1] = _NAV_SEQ[1] + 1
    key = f"v3zone{_NAV_SEQ[1]}"
    # collected and written once with the dashboard's other styles: a <style> element of its own takes a slot in
    # the column and adds a gap above every zone (the right-hand panel grew taller than Tableau's)
    _ZONE_CSS.append(f"div.st-key-{key}{{min-height:{int(px)}px;gap:2px;}}"
                     f"div.st-key-{key} [role=radiogroup]{{gap:0;}}"
                     f"div.st-key-{key} [role=radiogroup] label{{min-height:0;padding:1px 0;margin:0;}}"
                     f"div.st-key-{key} [role=radiogroup] p, div.st-key-{key} [data-testid=stWidgetLabel] p"
                     f"{{font-size:13px;line-height:1.3;}}")
    return _safe_container(key=key)


def _zone_key(zid):
    return f"v3show_{(_CURRENT_DASH or {}).get('name', '')}_{zid}"


def _zone_visible(zid, start_hidden):
    try:
        return bool(st.session_state.get(_zone_key(zid), not start_hidden))
    except Exception:                     # no session state (headless render): Tableau's state at open
        return not start_hidden
_NAV_SEQ = [0, 0]


def _render_layout_impl(node, by_name, where_parts, compact=False, dark=False,
                        in_row=False, col_depth=0, _seq=None):
    """Walk the dashboard's zone-container tree (Tableau declares grouping,
    proportions and container backgrounds -- render them instead of one
    column per sheet). Dark containers get a keyed st.container + CSS."""
    if node.get("zid") is not None and not _zone_visible(node["zid"], node.get("start_hidden")):
        return                                # hidden by its Tableau show/hide button
    if "sheet" in node:
        s = by_name.get(node["sheet"])
        if s is None:
            return
        s2 = dict(s)
        if compact:
            s2["_compact"] = True
        if dark:
            s2["_dark"] = True
        if node.get("h"):
            # Leaf zones can be intentionally short sparklines. The general
            # _zone_px() clamp (150px) is for free-standing charts and made a
            # 55-65px Tableau strip dwarf the KPI above it. Preserve the raw
            # authored height here; subtract title space only when shown.
            has_title = (s.get("geom") or {}).get("show_title", True)
            raw_px = int(node["h"] * node.get("_zone_px", _ZONE_PX))
            s2["_hpx"] = max(48, min(1600, raw_px - (36 if has_title else 0)))
        if node.get("w"):
            # zone width in pixels (release 26b: exact-plan sheets size their panes to the Tableau zone)
            s2["_wpx"] = max(80, int(node["w"] * node.get("_zone_wpx", _ZONE_PX)))
        render_sheet(s2, where_parts)
        return
    if "control" in node:                     # release 26c: a filter / parameter control in its Tableau zone
        dash = _CURRENT_DASH or {}
        zone_h = max(60, int((node.get("h") or 0) * node.get("_zone_px", _ZONE_PX)))
        with _zone_box(zone_h):                   # the zone keeps its authored height (as a minimum: never clipped)
            if node["control"] == "filter":
                f = _filter_for_zone(dash, node)
                if f is not None:
                    build_where(dict(dash, filters=[dict(f, mode=node.get("mode"))], params=[]))
            elif node.get("caption") in (dash.get("params") or []):
                build_where(dict(dash, filters=[], params=[node["caption"]]))
        return
    if "legend" in node:                      # a legend zone: drawn for exact-plan sheets (others keep theirs)
        s = by_name.get(node["legend"])
        if s is not None and s.get("sheet_plan"):
            try:
                import v3_sheet_plan
                import sys
                with _zone_box(max(60, int((node.get("h") or 0) * node.get("_zone_px", _ZONE_PX)))):
                    v3_sheet_plan.legend(s, list(_CURRENT_PARTS), sys.modules[__name__], node.get("legend_type"))
            except Exception as exc:
                findings.record("WARNING", node["legend"], "legend-zone", f"{type(exc).__name__}: {exc}")
        return
    if "title" in node:
        dash = _CURRENT_DASH or {}
        text = display_title(dash.get("title") or dash.get("name") or "")
        if text:
            px = int((node.get("h") or 0) * node.get("_zone_px", _ZONE_PX))
            st.markdown(f"<div style='font-size:1.55rem;font-weight:400;line-height:1.3;margin:0 0 .3rem 0;"
                        f"min-height:{max(0, px - 16)}px'>"
                        f"{text.replace('<', '&lt;')}</div>", unsafe_allow_html=True)
        return
    if "blank" in node:
        px = int((node.get("h") or 0) * node.get("_zone_px", _ZONE_PX))
        if px >= 8:
            st.markdown(f"<div style='height:{min(px, 400)}px'></div>", unsafe_allow_html=True)
        return
    if "image" in node:
        uri = (IR.get("images") or {}).get(node["image"])
        if uri:
            st.markdown(f"<img src='{uri}' style='max-width:100%;max-height:"
                        f"{max(24, int((node.get('h') or 0) * node.get('_zone_px', _ZONE_PX)))}px'/>",
                        unsafe_allow_html=True)
        else:
            findings.record("INFO", (_CURRENT_DASH or {}).get("name", "(dashboard)"), "image-zone",
                            f"Image {node['image']} is not packaged with the workbook")
        return
    if "nav" in node or "toggle" in node:     # Tableau buttons: navigation and show/hide, styled as authored
        _NAV_SEQ[0] += 1
        bkey = f"v3_btn_{_NAV_SEQ[0]}"
        px = max(16, int((node.get("h") or 0) * node.get("_zone_px", _ZONE_PX)))
        images = node.get("images") or []
        if "toggle" in node:
            ids, starts = tuple(node["toggle"]), tuple(node.get("start_hidden") or [])
            shown = _zone_visible(ids[0], starts[0]) if ids and starts else True
            # Tableau keeps the button's picture in step with what it shows
            idx = node.get("active", 0) if shown == (not (starts[0] if starts else False)) else 1 - node.get("active", 0)

            def _click(ids=ids, starts=starts):
                for zid, hidden in zip(ids, starts):
                    k = _zone_key(zid)
                    st.session_state[k] = not st.session_state.get(k, not hidden)
            disabled = False
        else:
            idx = 0
            key, names = DASH_NAV if DASH_NAV else (None, [])
            target = node["nav"]

            def _click(k=key, i=(names.index(target) if target in names else None)):
                if k and i is not None:
                    st.session_state[k] = i
            disabled = not (key and target in names)
        uri = (IR.get("images") or {}).get(images[min(max(idx, 0), len(images) - 1)]) if images else None
        css = [f"min-height:{px}px;height:{px}px;padding:0;"]
        if uri:
            css.append(f"background:url('{uri}') center/contain no-repeat;border:none;")
        elif node.get("fill"):
            css.append(f"background:{node['fill']};border-color:{node['fill']};")
        else:
            css.append("background:transparent;border:none;")
        if node.get("color"):
            css.append(f"color:{node['color']};")
        if node.get("fontsize"):
            css.append(f"font-size:{node['fontsize']}pt;")
        # F12 (CX2 fix queue): the authored look holds in every state. Streamlit's own :disabled / :hover rules are
        # more specific than ours and painted unstyled text buttons grey (E-Commerce navigation hot-spots whose
        # target dashboard is not migrated); Tableau draws them with the authored fill, else none.
        look = ''.join(css)
        _ZONE_CSS.append(f".st-key-{bkey} button{{{look}}}"
                         f".st-key-{bkey} button:disabled,.st-key-{bkey} button:hover,.st-key-{bkey} button:focus"
                         f"{{{look}opacity:1;}}.st-key-{bkey} button:disabled{{cursor:default;}}"
                         f".st-key-{bkey} button p{{color:inherit;font-size:inherit;}}")
        label = "\u2009" if uri else (node.get("caption") or "\u2009")
        st.button(label, key=bkey, on_click=_click, use_container_width=True, disabled=disabled,
                  help=node.get("tooltip") or (node.get("caption") if "toggle" in node else None))
        return
    if "unsupported_zone" in node:
        findings.record("INFO", (_CURRENT_DASH or {}).get("name", "(dashboard)"), "zone-not-drawn",
                        f"Tableau {node['unsupported_zone']} zone is not drawn in the app")
        return
    if "text" in node:
        text = _layout_text_value(node.get("text"))
        if text:
            # Escaping angle brackets prevents placeholders such as
            # "<Full Name>" from being interpreted as an HTML tag by Markdown.
            text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            px = int((node.get("h") or 0) * node.get("_zone_px", _ZONE_PX))
            # the text zone keeps its authored height (a taller zone pushed the zones below it down in Tableau)
            st.markdown(f"<div style='font-weight:600;min-height:{max(0, px - 16)}px'>{text}</div>",
                        unsafe_allow_html=True)
        return
    bg = (node.get("bg") or "").lower()
    border = node.get("border")
    is_dark = bool(bg) and _luminance(bg) < 0.5
    # cards in a row must share a bottom edge WITHOUT clipping content:
    # 'stretch' grows each card to its tallest sibling. (Fixed pixel heights
    # from zone h clipped table content into scrollbars -- user caught it.)
    hkw = {"height": "stretch"} if (bg and in_row) else {}
    if is_dark:
        _seq[0] += 1
        key = f"czone{_seq[0]}"
        _DARK_KEYS.append((key, bg))
        ctx = _safe_container(key=key, **hkw)
    elif bg or border:
        _seq[0] += 1
        key = f"czone{_seq[0]}"
        fill = bg or "#ffffff"
        border_color = (border or {}).get("color", "rgba(0,0,0,0.07)")
        _LIGHT_KEYS.append((key, fill, border_color))
        ctx = _safe_container(key=key, border=True, **hkw)   # white Tableau card
    else:
        ctx = None
    def _children():
        kids = node["children"]
        # side-by-side only while column nesting is within the platform's
        # limit; beyond it, stack vertically (renders everywhere, just taller)
        if (node["dir"] == "horz" and len(kids) > 1
                and col_depth < _max_col_nest()):
            weights = [max(1, k.get("w", 1)) for k in kids]
            comp = compact or sum(1 for k in kids if not k.get("_spacer")) >= 3
            for c, k in zip(st.columns(weights), kids):
                with c:
                    _render_layout(k, by_name, where_parts, comp,
                                   dark or is_dark, in_row=True,
                                   col_depth=col_depth + 1)
        else:
            for k in kids:
                _render_layout(k, by_name, where_parts, compact,
                               dark or is_dark, in_row=in_row,
                               col_depth=col_depth)
    def _layered():
        # Release 26c: Tableau's background image and floating zones. The container is the positioned
        # layer; each floating zone is drawn at its authored rectangle on top of the tiled zones.
        if ctx is not None:
            with ctx:
                _children()
        else:
            _children()
        zone_px = node.get("_zone_px", _ZONE_PX)
        for f in node.get("floating") or []:
            _seq[0] += 1
            fkey = f"v3float{_seq[0]}"
            left = 100.0 * (f["x"] - f["rx"]) / max(1, f["rw"])
            width = 100.0 * f["w"] / max(1, f["rw"])
            top = int((f["y"] - f["ry"]) * zone_px)
            _ZONE_CSS.append(f"div.st-key-{fkey}{{position:absolute;left:{left:.2f}%;top:{top}px;"
                        f"width:{width:.2f}%;z-index:3;}}")
            with _safe_container(key=fkey):
                _render_layout(f["child"], by_name, where_parts, True, dark, in_row=False, col_depth=col_depth)
    back = node.get("backdrop")
    if node.get("floating") or isinstance(back, dict):
        _seq[0] += 1
        lkey = f"v3layer{_seq[0]}"
        rule = "position:relative;"
        uri = (IR.get("images") or {}).get(back.get("image")) if isinstance(back, dict) else None
        if uri:
            rule += f"background-image:url('{uri}');background-size:100% 100%;background-repeat:no-repeat;"
        elif isinstance(back, dict):
            findings.record("INFO", (_CURRENT_DASH or {}).get("name", "(dashboard)"), "image-zone",
                            f"Image {back.get('image')} is not packaged with the workbook")
        _ZONE_CSS.append(f"div.st-key-{lkey}{{{rule}}}")
        with _safe_container(key=lkey):
            _layered()
    elif ctx is not None:
        with ctx:
            _children()
    else:
        _children()


CANVAS_PAGE_PX = 1100       # the app page width a range / automatic dashboard is drawn at
SHEET_TITLE_PX = 26


def canvas_zoom_css(selector, W, canvas, H=0):
    """Tableau's Automatic / Range sizing fills the browser window: the canvas, laid out at W px, is enlarged to the
    page width (in steps of 2 %, up to 1.6x) when the page is wider; a fixed-size dashboard keeps its exact size and
    a narrower page keeps the minimum size (scrolls sideways, as Tableau does). CSS container queries only: the
    page width is the browser's, never guessed. A transform (not CSS zoom): every chart and widget keeps measuring
    its zone at the laid-out size, so nothing is sized twice; the space below grows with the scale."""
    canvas = canvas or {}
    if canvas.get("maxwidth") and canvas.get("maxwidth") == canvas.get("minwidth"):
        return ""
    rules = []
    for step in range(1, 31):
        z = round(1 + step * 0.02, 2)
        rules.append(f"@container (min-width:{int(W * z) + 2}px){{{selector}{{transform:scale({z});"
                     f"transform-origin:0 0;margin-bottom:{int(H * (z - 1)) + 1}px;}}}}")
    return "".join(rules)


def canvas_size(canvas):
    """(width, height) in px of the dashboard canvas: Tableau's fixed size, else its minimum size scaled to the
    page width (aspect ratio kept). Zone x / w scale with the width, y / h with the height."""
    canvas = canvas or {}
    fixed_w = canvas.get("maxwidth") if canvas.get("maxwidth") and canvas.get("maxwidth") == canvas.get("minwidth") else None
    width = int(fixed_w or max(canvas.get("minwidth") or 0, CANVAS_PAGE_PX))
    return width, int(round(canvas_zone_px(canvas) * 100000))


def _title_px(s):
    """Height of a sheet title as Tableau formats it (its lines at their font sizes)."""
    runs = s.get("title_runs")
    if not runs:
        return SHEET_TITLE_PX
    lines, tallest, total = [], 0.0, 0.0
    for r in runs + [{"br": True}]:
        if r.get("br"):
            total += (tallest or 12) * 4 / 3 * 1.2
            tallest = 0.0
            continue
        try:
            tallest = max(tallest, float(r.get("size") or 12))
        except ValueError:
            tallest = max(tallest, 12.0)
    return int(total) + 4


_LABEL_DEFAULT_PT = 9      # Tableau's default mark-label / text size when a run names none
_SERIF_FONTS = {"cambria", "georgia", "times new roman", "garamond", "book antiqua", "palatino linotype",
                "constantia", "baskerville", "merriweather"}


def _font_stack(name):
    """Release 27 (F2): an authored font name -> a CSS stack that falls back to the same family class (a viewer
    without Cambria still sees a serif, as the author intended). Tableau's own 'Tableau Book' etc. -> app font."""
    name = str(name or "").strip().replace("'", "")
    if not name or name.lower().startswith("tableau"):
        return "inherit"
    generic = "Georgia, serif" if name.lower() in _SERIF_FONTS else "Arial, sans-serif"
    return f"'{name}', {generic}"


def _runs_html(runs, default_pt, default_bold=False):
    """Tableau formatted-text runs -> HTML spans (font size in pt, bold, colour, line breaks, alignment)."""
    import html as _html
    parts, align = [], None
    for r in runs or []:
        if r.get("br"):
            parts.append("<br/>")
            continue
        raw = r.get("text") or ""
        text = _layout_text_value(raw)
        if not text and not raw.strip(" "):
            text = raw                       # a run of spaces separates the runs around it
        if not text:
            continue
        # spaces at the edges of a run separate it from its neighbours ('for' + ' Q4' + ' of ' + '2024')
        text = raw[:len(raw) - len(raw.lstrip())] + text.strip() + raw[len(raw.rstrip()):]
        align = align or r.get("align")
        css = [f"font-size:{_html.escape(str(r.get('size') or default_pt))}pt"]
        if r.get("bold") or (default_bold and "bold" not in r):
            css.append("font-weight:700")
        if r.get("italic"):
            css.append("font-style:italic")
        if r.get("color"):
            css.append(f"color:{_html.escape(r['color'])}")
        font = str(r.get("font") or "")
        if font and not font.lower().startswith("tableau"):
            css.append(f"font-family:{_html.escape(_font_stack(font), quote=True)}")   # inside style='...': quotes escaped
        parts.append(f"<span style='{';'.join(css)};white-space:pre-wrap'>{_html.escape(text)}</span>")
    return "".join(parts), (align or "left")


def _runs_lines_html(runs, default_pt):
    """A worksheet title as Tableau lays it out: one block per authored line, each line as tall as its largest run
    (font size x 1.2), so the drawn title takes exactly the height _title_px reserves for it."""
    lines, cur = [], []
    for r in list(runs or []) + [{"br": True}]:
        if r.get("br"):
            lines.append(cur)
            cur = []
        else:
            cur.append(r)
    out, align = [], "left"
    for line in lines:
        pts = []
        for r in line:
            try:
                pts.append(float(r.get("size") or default_pt))
            except ValueError:
                pts.append(float(default_pt))
        tallest = max(pts) if pts else 12.0
        body, a = _runs_html(line, default_pt) if line else ("", None)
        align = a if line and a else align
        out.append(f"<div style='font-size:{tallest:g}pt;line-height:1.2;min-height:{tallest * 4 / 3 * 1.2:.1f}px;"
                   f"white-space:pre-wrap;overflow:hidden'>{body or '&#8203;'}</div>")
    return "".join(out), align


def _highlight_key(caption):
    return f"v3hl::{(_CURRENT_DASH or {}).get('name', '')}::{caption}"


def highlight_value(caption):
    """The member a dashboard highlighter selects for this field (None = no highlight)."""
    if not any(z.get("kind") == "highlighter" and z.get("caption") == caption
               for z in ((_CURRENT_DASH or {}).get("canvas_zones") or [])):
        return None
    try:
        v = st.session_state.get(_highlight_key(caption))
    except Exception:
        return None
    return None if v in (None, "", "(None)") else v


# the widget's own title label only -- never the labels of its options (a checkbox list's members, radio items)
_WIDGET_TITLE_ONLY = ":not(.stCheckbox *):not([role=radiogroup] *)"


def _zone_title(z, key):
    """A zone's own (custom) title as Tableau formats it; the widget's own label is hidden."""
    runs = z.get("title_runs")
    if not runs:
        return
    body, align = _runs_html(runs, 9)
    _ZONE_CSS.append(f"div.st-key-{key} [data-testid=stWidgetLabel]{_WIDGET_TITLE_ONLY}{{display:none;}}")
    st.markdown(f"<div style='text-align:{align};line-height:1.2;margin-bottom:2px'>{body}</div>",
                unsafe_allow_html=True)


def _quick_filter_title(z, by_name):
    """The quick-filter title the author typed on the worksheet (<style-rule element='quick-filter'>
    <format attr='title' field='[ds].[yr:Order Date:ok]' value='Order Year'/>)."""
    s = by_name.get(z.get("scope_sheet")) or {}
    fields = (s.get("ws_style") or {}).get("fields") or {}
    rule = fields.get(f"{z.get('datepart') or 'none'}:{z.get('caption')}") or {}
    return rule.get("quick-filter.title")


def _zone_overflow(z, fit=None):
    """A list quick filter longer than its zone scrolls inside it (Tableau shows a scrollbar); a dropdown
    control keeps its few pixels of Streamlit height visible; a sheet in 'standard' / 'fit width' sizing scrolls
    when its authored rows are taller than the zone; anything else is clipped to its zone."""
    kind = z.get("kind")
    if kind == "control":
        return "auto" if z.get("mode") in ("radiolist", "checklist", "list") else "visible"
    if kind == "legend":
        return "hidden auto"            # a long legend scrolls inside its zone, as in Tableau
    if kind == "sheet":
        # Tableau's Entire View / Fit Width / Fit Height never scroll: the chart is fitted to the zone (CSS below)
        # Entire View / Fit Height never scroll: the chart is fitted to the zone (CSS below); Fit Width fits the
        # width and scrolls down when its rows are taller than the zone, as in Tableau
        return "hidden" if fit in ("entire-view", "fit-height") else "hidden auto"
    return "hidden"


FIT_MODES = ("entire-view", "fit-width", "fit-height")


def _sheet_title_budget(z, by_name):
    s = by_name.get(z.get("sheet")) or {}
    return _title_px(s) if z.get("show_title", True) and (s.get("geom") or {}).get("show_title", True) else 0


def _fit_css(key, mode, w, h):
    """Tableau's sheet fit, exactly: the chart (an SVG with a viewBox) is scaled to the zone -- Entire View to the
    whole zone below the title, Fit Width to its width, Fit Height to its height -- keeping its proportions, so it
    is never cut off and never scrolls, whatever the browser's fonts did to the computed size."""
    w, h = max(20, int(w)), max(20, int(h))
    sel = f"div.st-key-{key} .vega-embed"
    if mode == "fit-width":
        box = f"max-width:{w}px !important;height:auto !important;"
    elif mode == "fit-height":
        box = f"height:{h}px !important;width:auto !important;max-width:{w}px;"
    else:
        box = f"width:{w}px !important;height:{h}px !important;"
    flex = ""
    if mode != "fit-width":
        # the chart takes exactly the space the title leaves (a title that wraps in a narrow zone included)
        z = f"div.st-key-{key}"
        flex = (f"{z}{{display:flex;flex-direction:column;}}"
                f"{z}>div:has(.vega-embed){{flex:1 1 0;min-height:0;}}"
                f"{z} div:has(>.vega-embed),{z} div:has(>div>.vega-embed),{z} div:has(>div>div>.vega-embed)"
                f"{{height:100%;}}{sel},{sel} .chart-wrapper{{height:100% !important;}}"
                f"{sel} svg.marks{{max-height:100%;}}")
    return (f"{sel} svg.marks{{{box}display:block;}}" + flex +
            f"{sel} canvas{{max-width:{w}px;max-height:{h}px;}}")


def _zone_hidden(z):
    for h in z.get("hide_with") or []:
        if not _zone_visible(h.get("zid"), h.get("start_hidden")):
            return True
    return False


def _container_default(style, ds):
    """V12: a layout container without its own fill / border takes the dashboard's container formatting
    (<style-rule element='dash-container'>: background-color, border-style, border-color)."""
    rule = (ds or {}).get("dash-container") or {}
    out = dict(style)
    bg = rule.get("background-color")
    if not out.get("bg") and bg and not (len(bg) == 9 and bg.endswith("00")):
        out["bg"] = bg[:7]
    if not out.get("border") and rule.get("border-style") not in (None, "", "none"):
        out["border"] = {"width": 1, "color": (rule.get("border-color") or "#d9d9d9")[:7]}
    return out


def _render_canvas(dash, zones, by_name, where_parts):
    """Release 26f: the dashboard exactly as the workbook XML lays it out. Every zone is pinned at its own
    rectangle (x / y / w / h of the 100000 x 100000 grid, scaled to the canvas size), minus its margins, with
    its padding, background and border, in Tableau's drawing order (later zones on top). Nothing is re-flowed by
    Streamlit columns, so nothing can drift."""
    W, H = canvas_size(dash.get("canvas"))
    sx, sy = W / 100000, H / 100000
    fit = dash.get("fit") or {}
    ds = dash.get("dash_style") or {}
    shade = (ds.get("table") or {}).get("background-color")        # Format Dashboard > shading
    if shade and not (len(shade) == 9 and shade.endswith("00")):
        _ZONE_CSS.append(f"div.st-key-v3canvas{{background:{shade[:7]};}}")
    for el, sel in (("parameter-ctrl", "[data-baseweb=select] *,[data-baseweb=input] input"),
                    ("parameter-ctrl-title", "[data-testid=stWidgetLabel] p"),
                    ("quick-filter", "[data-baseweb=select] *,[role=radiogroup] p"),
                    ("quick-filter-title", "[data-testid=stWidgetLabel] p")):
        rule = ds.get(el) or {}
        css = "".join(([f"font-family:'{rule['font-family']}',sans-serif;"] if rule.get("font-family") else []) +
                      ([f"color:{rule['color'][:7]};"] if rule.get("color") else []) +
                      ([f"font-size:{rule['font-size']}pt;"] if rule.get("font-size") else []))
        if css:
            _ZONE_CSS.append(f"div.st-key-v3canvas div[class*=st-key-v3cz] {sel.replace(',', ',div.st-key-v3canvas div[class*=st-key-v3cz] ')}{{{css}}}")
    _ZONE_CSS.append(
        f"div.st-key-v3canvas{{position:relative;width:{W}px !important;min-width:{W}px;height:{H}px !important;"
        f"min-height:{H}px;flex:none;gap:0;}}"
        "div.st-key-v3canvaswrap{overflow-x:auto;container-type:inline-size;}"
        # R46: Streamlit's wide page keeps 5rem each side; a 1366 px fixed dashboard then scrolled and lost its
        # right edge in a 1,500 px window where Tableau shows it whole. The page holding a dashboard keeps 1rem.
        ":is([data-testid=stMainBlockContainer],.block-container):has(div.st-key-v3canvas)"
        "{padding-left:1rem !important;padding-right:1rem !important;max-width:none !important;}"
        "div.st-key-v3canvas div[class*=st-key-v3cz]{gap:0;}"
        # the chart is an inline block: without this its line box adds a descender gap under every chart
        "div.st-key-v3canvas .vega-embed{vertical-align:top;}"
        "div.st-key-v3canvas div[class*=st-key-v3cz] [data-testid=stVerticalBlock]{gap:0;}"
        "div.st-key-v3canvas div[class*=st-key-v3cz] .stCheckbox [data-testid=stWidgetLabel]{min-height:0;}"
        "div.st-key-v3canvas div[class*=st-key-v3cz] .stCheckbox label{align-items:center;}"
        "div.st-key-v3canvas div[class*=st-key-v3cz] [data-testid=stMarkdownContainer] p{margin:0;}"
        "div.st-key-v3canvas div[class*=st-key-v3cz] [data-testid=stMarkdown] *{margin-bottom:0 !important;}"
        # Streamlit's hover toolbar (full screen, download) is not part of a Tableau dashboard and overhangs the zone
        "div.st-key-v3canvas div[class*=st-key-v3cz] [data-testid=stElementToolbar]{display:none;}"
        # R46: so is the chart's own "..." actions menu (vega-embed). Chart sizing rules size only the chart's
        # SVG (svg.marks): a bare 'svg' rule also blew the menu's icon up into large dark circles over the bars
        "div.st-key-v3canvas .vega-embed details{display:none !important;}"
        # Release 27 (F13): Tableau draws no control on an action-source chart (you click its marks). The
        # app's action fallback icon floats in the zone corner and appears on hover only: no layout, no clip.
        # (zones are already absolutely positioned by the canvas, so the icon anchors to its own zone)
        "div.st-key-v3canvas div[class*=st-key-v3cz] [data-testid=stPopover]{position:absolute !important;"
        "top:0;right:0;margin:0 !important;width:auto;transition:opacity .15s;z-index:3;}"
        # hidden only over a chart (the marks are the authored control); a zone holding nothing else keeps it
        "div.st-key-v3canvas div[class*=st-key-v3cz]:has(.vega-embed) [data-testid=stPopover]{opacity:0;}"
        "div.st-key-v3canvas div[class*=st-key-v3cz]:has(.vega-embed):hover [data-testid=stPopover]{opacity:1;}"
        "div.st-key-v3canvas div[class*=st-key-v3cz] [data-testid=stElementContainer]{margin:0;}"
        # Tableau's quick filters are compact: a 9pt title line and a ~24px control
        "div.st-key-v3canvas div[class*=st-key-v3cz] [data-testid=stWidgetLabel]{min-height:0;margin:0 0 2px 0;}"
        "div.st-key-v3canvas div[class*=st-key-v3cz] [data-testid=stWidgetLabel] p{font-size:8pt;line-height:1.1;}"
        "div.st-key-v3canvas div[class*=st-key-v3cz] [data-baseweb=select]>div{min-height:18px;height:18px;font-size:8.5pt;}"
        "div.st-key-v3canvas div[class*=st-key-v3cz] [data-baseweb=select] svg{width:14px;height:14px;}"
        # newer Streamlit draws the dropdown as a combobox (same compact size)
        "div.st-key-v3canvas div[class*=st-key-v3cz] :is(.stSelectbox,.stMultiSelect) [class*=ComboBox]>div"
        "{min-height:20px;height:20px;font-size:8.5pt;overflow:hidden;}"
        "div.st-key-v3canvas div[class*=st-key-v3cz] :is(.stSelectbox,.stMultiSelect) [class*=ComboBox] input,"
        "div.st-key-v3canvas div[class*=st-key-v3cz] :is(.stSelectbox,.stMultiSelect) [class*=ComboBox] div"
        "{padding-top:0;padding-bottom:0;min-height:0;line-height:18px;font-size:8.5pt;}"
        "div.st-key-v3canvas div[class*=st-key-v3cz] .stMultiSelect [data-baseweb=tag]{height:16px;margin:1px;font-size:8pt;}"
        # F7: the multiselect's hidden search input (and its wrapper) kept Streamlit's 26px and spilled below
        # short filter zones; it is sized to the compact box and the chosen values stay on one line
        "div.st-key-v3canvas div[class*=st-key-v3cz] .stMultiSelect input{height:16px !important;"
        "min-height:0 !important;max-height:16px !important;padding:0;margin:0;line-height:16px !important;}"
        "div.st-key-v3canvas div[class*=st-key-v3cz] .stMultiSelect div:has(>input){height:auto;min-height:0;"
        "max-height:18px;padding-top:0;padding-bottom:0;}"
        "div.st-key-v3canvas div[class*=st-key-v3cz] .stMultiSelect [data-baseweb=select]>div>div:first-child"
        "{flex-wrap:nowrap;overflow:hidden;}"
        "div.st-key-v3canvas div[class*=st-key-v3cz] [data-baseweb=select] [aria-selected],"
        "div.st-key-v3canvas div[class*=st-key-v3cz] [data-baseweb=select] div{padding-top:0;padding-bottom:0;line-height:1.2;}"
        "div.st-key-v3canvas div[class*=st-key-v3cz] [data-baseweb=input]>div{min-height:20px;height:22px;font-size:9pt;}"
        "div.st-key-v3canvas div[class*=st-key-v3cz] [data-baseweb=input] input{padding-top:0;padding-bottom:0;font-size:9pt;}"
        "div.st-key-v3canvas div[class*=st-key-v3cz] [data-testid=stSlider]{padding:0 6px;}"
        "div.st-key-v3canvas div[class*=st-key-v3cz] .stButton button{min-height:0;}"
        "div.st-key-v3canvas div[class*=st-key-v3cz] [role=radiogroup]{gap:0;}"
        "div.st-key-v3canvas div[class*=st-key-v3cz] [role=radiogroup] label{min-height:0;padding:0;margin:0;"
        "height:16px;align-items:center;}"
        "div.st-key-v3canvas div[class*=st-key-v3cz] [role=radiogroup] p{font-size:8.5pt;line-height:1.35;}"
        "div.st-key-v3canvas div[class*=st-key-v3cz] [role=radiogroup] label>div:first-child{width:12px;height:12px;"
        "min-width:12px;margin-right:4px;}"
        "div.st-key-v3canvas div[class*=st-key-v3cz] .stCheckbox{min-height:0;}"
        "div.st-key-v3canvas div[class*=st-key-v3cz] .stCheckbox label{min-height:0;padding:0;margin:0;gap:4px;"
        "height:16px;align-items:center;}"
        "div.st-key-v3canvas div[class*=st-key-v3cz] .stCheckbox p{font-size:8.5pt;line-height:1.35;}"
        "div.st-key-v3canvas div[class*=st-key-v3cz] .stCheckbox label>span:first-child,"
        "div.st-key-v3canvas div[class*=st-key-v3cz] .stCheckbox label>div:first-child{width:12px;height:12px;min-width:12px;}")
    _ZONE_CSS.append(canvas_zoom_css("div.st-key-v3canvas", W, dash.get("canvas"), H))
    with _safe_container(key="v3canvaswrap"):
        with _safe_container(key="v3canvas"):
            for i, z in enumerate(zones):
                if _zone_hidden(z):
                    continue
                st_ = _container_default(z.get("style") or {}, ds) if z.get("kind") == "box" else (z.get("style") or {})
                m = st_.get("margin") or [0, 0, 0, 0]
                pd_ = st_.get("padding") or [0, 0, 0, 0]
                left, top = z["x"] * sx + m[3], z["y"] * sy + m[0]
                width, height = z["w"] * sx - m[1] - m[3], z["h"] * sy - m[0] - m[2]
                if width < 2 or height < 2:
                    continue
                kind = z.get("kind")
                rule = [f"position:absolute;left:{left:.1f}px;top:{top:.1f}px;width:{width:.1f}px;"
                        f"height:{height:.1f}px;box-sizing:border-box;z-index:{i + 1};"
                        f"padding:{pd_[0]}px {pd_[1]}px {pd_[2]}px {pd_[3]}px;"
                        # a quick filter longer than its zone scrolls inside it, as in Tableau
                        f"overflow:{_zone_overflow(z, fit.get(z.get('sheet')))};"]
                if st_.get("bg"):
                    rule.append(f"background:{st_['bg']};")
                if st_.get("border"):
                    b = st_["border"]
                    rule.append(f"border:{b.get('width', 1)}px solid {b.get('color', '#d9d9d9')};")
                key = f"v3cz{i}"
                _ZONE_CSS.append(f"div.st-key-{key}{{{''.join(rule)}}}")
                if kind == "image":
                    _ZONE_CSS.append(f"div.st-key-{key} [data-testid=stMarkdownContainer]{{line-height:0;}}")
                if kind == "control" and z.get("show_title") is False:
                    # the author hid this quick filter's title in Tableau
                    _ZONE_CSS.append(f"div.st-key-{key} [data-testid=stWidgetLabel]{_WIDGET_TITLE_ONLY}{{display:none;}}")
                if z.get("dark") or (st_.get("bg") and _luminance(st_["bg"]) < 0.5):
                    # a zone on a dark container: Tableau's text there is light
                    _ZONE_CSS.append(
                        f"div.st-key-{key} [data-testid=stMetricValue],div.st-key-{key} [data-testid=stMetricLabel],"
                        f"div.st-key-{key} [data-testid=stMarkdownContainer] p:not([style*=color]),"
                        f"div.st-key-{key} strong{{color:#ffffff !important;}}")
                inner_w = width - pd_[1] - pd_[3] - 2 * ((st_.get("border") or {}).get("width") or 0)
                inner_h = height - pd_[0] - pd_[2] - 2 * ((st_.get("border") or {}).get("width") or 0)
                mode = fit.get(z.get("sheet")) if kind == "sheet" else None
                if mode in FIT_MODES:
                    _ZONE_CSS.append(_fit_css(key, mode, inner_w, inner_h - _sheet_title_budget(z, by_name)))
                if kind == "legend":
                    # a legend larger than its zone shrinks into it (never clipped); a smaller one keeps its size
                    _ZONE_CSS.append(f"div.st-key-{key} .vega-embed svg.marks{{max-width:{max(20, int(inner_w))}px;"
                                     f"max-height:{max(12, int(inner_h))}px;width:auto;height:auto;display:block;}}")
                if kind in ("control", "highlighter") and _zone_overflow(z) == "visible" and inner_h < 34:
                    # Tableau fits a compact dropdown (title line + box) into the zone the author drew; squeeze
                    # the box so it does not spill below the zone.
                    _ZONE_CSS.append(_control_squeeze_css(key, inner_h, z.get("show_title", True)))
                with _safe_container(key=key):
                    try:
                        if kind == "control" and z.get("control") == "filter" and not z.get("title_runs"):
                            qt = _quick_filter_title(z, by_name)
                            if qt:
                                z = dict(z, title_runs=[{"text": qt}])
                        if kind in ("control", "highlighter") and z.get("show_title", True):
                            _zone_title(z, key)
                        _ZONE_W.value = inner_w
                        try:
                            _canvas_zone(dash, z, kind, by_name, where_parts, fit, inner_w, inner_h)
                        finally:
                            _ZONE_W.value = None
                    except Exception as exc:
                        findings.record("WARNING", dash.get("name", "(dashboard)"), "zone-failed",
                                        f"{kind} zone: {type(exc).__name__}: {exc}")


# CX2 rule (worksheet shading): <style-rule element='table'><format attr='background-color'> is the chart's
# background. Streamlit's chart theme paints every chart white, which hid dark containers behind charts whose
# shading is 'None' (#rrggbb00). Thread-local, so concurrent sessions never see another session's value.
import threading as _threading
_CHART_BG = _threading.local()
_CHART_BG.value = None


def _with_background(chart, bg):
    c = chart.copy(deep=False)
    cfg = c.config if c.config is not alt.Undefined else alt.Config()
    cfg = cfg.copy(deep=False)
    cfg.background = bg
    cfg.view = alt.ViewConfig(stroke=None, fill=None) if bg == "transparent" else (cfg.view if cfg.view is not alt.Undefined else alt.Undefined)
    c.config = cfg
    return c


# E20: the colour a sheet is drawn on (its shading, else its container); a dark one turns the chart's generated
# ink (axis, legend, header and title text, gridlines) light, keeping any colour the chart already sets.
_CHART_INK = _threading.local()
_CHART_INK.value = None
_DARK_INK_CONFIG = {'axis': {'labelColor': '#e8e8e8', 'titleColor': '#e8e8e8', 'gridColor': '#444444',
                             'domainColor': '#8a8a8a', 'tickColor': '#8a8a8a'},
                    'legend': {'labelColor': '#e8e8e8', 'titleColor': '#e8e8e8'},
                    'header': {'labelColor': '#e8e8e8', 'titleColor': '#e8e8e8'},
                    'title': {'color': '#e8e8e8', 'subtitleColor': '#c8c8c8'}}


_ZONE_W = _threading.local()
_ZONE_W.value = None


def _zone_width(chart, w):
    """A single / layered chart sized to a zone width: the whole chart (axes included) fits it."""
    try:
        c = chart.copy(deep=False)
        auto = c.autosize if c.autosize is not alt.Undefined else None
        kind = (auto.get('type') if isinstance(auto, dict) else getattr(auto, 'type', None)) if auto is not None else None
        c.width = max(20, int(w) - 2)
        if kind not in ('fit', 'fit-x'):
            c.autosize = alt.AutoSizeParams(type='fit-x', contains='padding')
        return c
    except Exception:
        return chart


def _compound(chart):
    if isinstance(chart, (alt.VConcatChart, alt.HConcatChart, alt.ConcatChart, alt.FacetChart)):
        return True
    enc = getattr(chart, 'encoding', alt.Undefined) if isinstance(chart, alt.Chart) else alt.Undefined
    try:
        return enc is not alt.Undefined and any(getattr(enc, ch, alt.Undefined) is not alt.Undefined
                                                for ch in ('row', 'column', 'facet'))
    except Exception:
        return False


def _keep_rows(chart):
    """Rows written inline in a chart (values lists, e.g. a chart rebuilt from a spec) are consolidated by Altair into
    the spec's 'datasets', which st.altair_chart replaces with its own Arrow datasets -- such a chart drew no marks
    in the app while every image export showed them. Inline rows become DataFrames, which Streamlit carries."""
    try:
        import pandas as _pd
        if getattr(chart, 'datasets', alt.Undefined) not in (alt.Undefined, None):
            import v3_sheet_plan as _P                  # rows by dataset name -> written where they are used
            chart = type(chart).from_dict(_P.inline_datasets(chart.to_dict()))

        def fix(c):
            d = getattr(c, 'data', alt.Undefined)
            if isinstance(d, alt.InlineData) and isinstance(getattr(d, 'values', None), (list, alt.InlineDataset)):
                vals = d.values.to_dict() if hasattr(d.values, 'to_dict') else d.values
                if isinstance(vals, list):
                    c = c.copy(deep=False)
                    c.data = _pd.DataFrame(vals)
            for key in ('vconcat', 'hconcat', 'concat', 'layer'):
                kids = getattr(c, key, alt.Undefined)
                if isinstance(kids, list):
                    c = c.copy(deep=False)
                    setattr(c, key, [fix(k) for k in kids])
            spec = getattr(c, 'spec', alt.Undefined)
            if spec is not alt.Undefined and hasattr(spec, 'copy') and not isinstance(spec, dict):
                c = c.copy(deep=False)
                c.spec = fix(spec)
            return c
        return fix(chart)
    except Exception:
        return chart


def dark_ink(chart, bg=None):
    bg = bg if bg is not None else getattr(_CHART_INK, 'value', None)
    try:
        import v3_sheet_plan as _P
        if not _P.is_dark(bg):
            return chart
        c = chart.copy(deep=False)
        cfg = c.config if c.config is not alt.Undefined else alt.Config()
        cfg = cfg.copy(deep=False)
        kinds = {'axis': alt.AxisConfig, 'legend': alt.LegendConfig, 'header': alt.HeaderConfig,
                 'title': alt.TitleConfig}
        for key, attrs in _DARK_INK_CONFIG.items():
            cur = getattr(cfg, key, alt.Undefined)
            d = cur.to_dict() if cur is not alt.Undefined else {}
            for k, v in attrs.items():
                d.setdefault(k, v)
            setattr(cfg, key, kinds[key].from_dict(d))
        c.config = cfg
        return c
    except Exception:
        return chart                     # styling never stops a chart from drawing


def _install_chart_background():
    orig = st.altair_chart
    if getattr(orig, "_v3_bg", False):
        return

    def altair_chart(chart, *args, **kwargs):
        chart = _keep_rows(chart)
        zone_w = getattr(_ZONE_W, 'value', None)
        if zone_w and kwargs.get('use_container_width') and not _compound(chart):
            # inside a dashboard canvas zone: the chart takes the zone's laid-out width itself. The canvas may be
            # scaled to the page (canvas_zoom_css) and the browser then reports the scaled width as the container's,
            # which drew every chart wider than its zone.
            chart = _zone_width(chart, zone_w)
            kwargs['use_container_width'] = False
        if _compound(chart) and kwargs.get('use_container_width'):
            # Vega-Lite cannot size a concatenated / faceted chart to its container: Streamlit's container width
            # drew such a chart 0 px wide (an empty zone). Its panel widths are computed from the zone already.
            kwargs['use_container_width'] = False
        chart = dark_ink(chart)
        bg = getattr(_CHART_BG, "value", None)
        if bg:
            try:
                chart = _with_background(chart, bg)
            except Exception:
                pass                     # a chart we cannot restyle is drawn as before
        return orig(chart, *args, **kwargs)
    altair_chart._v3_bg = True
    st.altair_chart = altair_chart


_install_chart_background()


def _canvas_zone(dash, z, kind, by_name, where_parts, fit, inner_w, inner_h):
    if kind == "box":
        st.markdown("<div></div>", unsafe_allow_html=True)       # the container paints its own box
        return
    if kind == "sheet":
        s = by_name.get(z.get("sheet"))
        if s is None:
            return
        s2 = dict(s)
        geom = dict(s.get("geom") or {})
        geom["show_title"] = bool(z.get("show_title", True))
        s2["geom"] = geom
        s2["_hpx"] = max(24, int(inner_h - (_title_px(s) if geom["show_title"] else 0)))
        s2["_wpx"] = max(40, int(inner_w) - 10)     # the chart's own padding stays inside the zone
        s2["_fit"] = fit.get(z.get("sheet"), "standard")
        s2["_canvas"] = True
        if z.get("dark"):
            s2["_dark"] = True
        import axis_xml
        _CHART_BG.value = axis_xml.table_background(s.get("ws_style"))
        try:
            render_sheet(s2, where_parts)
        finally:
            _CHART_BG.value = None
        return
    node = dict(z, h=None)          # the canvas sizes the zone; leaf renderers must not add their own minimum height
    node.pop("x", None)
    if kind == "control":
        node["control"] = z.get("control")
        f = _filter_for_zone(dash, node) if z.get("control") == "filter" else None
        if z.get("control") == "filter" and f is not None:
            build_where(dict(dash, filters=[dict(f, mode=z.get("mode"), show_all=z.get("show_all", True))], params=[]))
        elif z.get("control") == "param" and z.get("caption") in (dash.get("params") or []):
            build_where(dict(dash, filters=[], params=[z["caption"]]))
        return
    if kind == "legend":
        s = by_name.get(z.get("legend"))
        if s is not None and s.get("sheet_plan"):
            import v3_sheet_plan
            import sys
            if z.get("title_runs"):
                body, align = _runs_html(z["title_runs"], 9)
                st.markdown(f"<div style='text-align:{align};line-height:1.2'>{body}</div>", unsafe_allow_html=True)
            v3_sheet_plan.legend(s, list(_CURRENT_PARTS), sys.modules[__name__], z.get("legend_type"),
                                 layout=z.get("layout") or "vert", width=inner_w, title=not z.get("title_runs"))
        else:
            findings.record("INFO", dash.get("name", "(dashboard)"), "legend-zone",
                            f"Legend of {z.get('legend')} is drawn inside its chart (the sheet has no exact plan)")
        return
    if kind == "highlighter":
        s = by_name.get(z.get("scope_sheet")) or next(iter(by_name.values()), None)
        if s is None or not z.get("caption"):
            return
        with field_scope(s):
            expr = rdim(z["caption"])
            vals = q(f"SELECT DISTINCT {expr} AS V FROM {tbl(s)} WHERE {expr} IS NOT NULL ORDER BY 1")["V"].tolist()
        st.selectbox(f"Highlight {z['caption']}", ["(None)"] + [str(v) for v in vals],
                     key=_highlight_key(z["caption"]))
        return
    if kind in ("text", "title"):
        runs = z.get("runs") or []
        if kind == "title" and not runs:
            runs = [{"text": display_title(dash.get("title") or dash.get("name") or "")}]
        ds = (dash.get("dash_style") or {}).get("dash-title" if kind == "title" else "dash-text") or {}
        runs = [dict(r, **{k: v for k, v in (("size", ds.get("font-size")), ("color", ds.get("color")),
                                              ("font", ds.get("font-family"))) if v and k not in r})
                if not r.get("br") else r for r in runs]
        body, align = _runs_html(runs, 15 if kind == "title" else 10)
        if body:
            color = "#ffffff" if z.get("dark") and not any(r.get("color") for r in runs) else "inherit"
            # Release 27 (F2): Tableau centres a dashboard text object's lines vertically in its zone
            # (checked on Tableau Cloud: header, navigation labels and footer all sit mid-zone)
            box = (f"display:flex;flex-direction:column;justify-content:center;min-height:{max(0, int(inner_h) - 2)}px;"
                   if inner_h else "")
            st.markdown(f"<div style='{box}text-align:{align};line-height:1.15;color:{color}'><div>{body}</div></div>",
                        unsafe_allow_html=True)
        return
    if kind == "image":
        uri = (IR.get("images") or {}).get(z.get("image"))
        if uri:
            # sized in px: a percentage of the zone's auto-height content box does not constrain the image
            fit_css = (f"width:{int(inner_w)}px;height:{int(inner_h)}px;object-fit:contain;" if z.get("fit")
                       else f"max-width:{int(inner_w)}px;max-height:{int(inner_h)}px;")
            st.markdown(f"<img src='{uri}' style='{fit_css}display:block'/>", unsafe_allow_html=True)
        else:
            findings.record("INFO", dash.get("name", "(dashboard)"), "image-zone",
                            f"Image {z.get('image')} is not packaged with the workbook")
        return
    if kind in ("nav", "toggle"):
        node = {k: v for k, v in z.items() if k not in ("x", "y", "w")}
        node["h"], node["_zone_px"] = inner_h, 1          # the button fills its zone
        _render_layout_impl(node, by_name, where_parts)
        return
    if kind == "unsupported":
        findings.record("INFO", dash.get("name", "(dashboard)"), "zone-not-drawn",
                        f"Tableau {z.get('what')} zone is not drawn in the app")


def _emit_dark_css():
    if _ZONE_CSS:
        st.markdown("<style>" + "".join(_ZONE_CSS) + "</style>", unsafe_allow_html=True)
        _ZONE_CSS.clear()
    if not _DARK_KEYS and not _LIGHT_KEYS:
        return
    rules = []
    for key, bg in _DARK_KEYS:
        rules.append(
            f"div.st-key-{key} {{background:{bg}; border-radius:10px; "
            f"padding:14px 16px;}} "
            f"div.st-key-{key} [data-testid='stMetricValue'], "
            f"div.st-key-{key} [data-testid='stMetricLabel'], "
            f"div.st-key-{key} p, div.st-key-{key} span, "
            f"div.st-key-{key} strong {{color:#ffffff !important;}}")
    for key, bg, border_color in _LIGHT_KEYS:
        # Streamlit's own border=True is a near-invisible hairline with no
        # background fill or elevation -- give the zone the same "card"
        # weight Tableau's own light containers have (fill, shadow, radius,
        # breathing room), instead of Streamlit's bare 1px line.
        rules.append(
            f"div.st-key-{key} {{background:{bg}; border-radius:10px; "
            f"padding:14px 16px; box-shadow:0 1px 3px rgba(0,0,0,0.08); "
            f"border-color:{border_color} !important;}}")
    st.markdown("<style>" + "\n".join(rules) + "[class*=st-key-czone] [data-testid=stVerticalBlock]{gap:6px;}[data-testid=stMarkdownContainer]:has(.tableau-kpi-label){margin-bottom:0 !important;}" + "</style>",
                unsafe_allow_html=True)


def _is_canvas_dashboard(dash):
    """True when the dashboard is drawn as a Tableau canvas (release 26f): it has placed sheet zones."""
    return isinstance(dash, dict) and any(isinstance(z, dict) and z.get("kind") == "sheet"
                                          for z in (dash.get("canvas_zones") or []))


DEVICE_WIDTH_PX = {"Phone": 375, "Tablet": 768}
_PHONE_UA = _re2.compile(r"iPhone|iPod|Android.*Mobile|Mobile Safari|Windows Phone", _re2.I)
_TABLET_UA = _re2.compile(r"iPad|Tablet|Android(?!.*Mobile)", _re2.I)


def device_from_user_agent(ua):
    """EN14: the device class a browser reports (Phone, Tablet, else Desktop)."""
    ua = str(ua or "")
    return "Phone" if _PHONE_UA.search(ua) else "Tablet" if _TABLET_UA.search(ua) else "Desktop"


def _device_choice(dash):
    """The layout to draw: the viewer's pick, else the device the browser reports (Automatic)."""
    variants = (dash or {}).get("device_variants") or {}
    if not variants:
        return "Desktop"
    # Tableau shows no layout picker: the device the browser reports decides (a '?device=Phone' URL parameter
    # can force one for checking)
    try:
        forced = st.query_params.get("device")
        if forced in variants or forced == "Desktop":
            return forced
    except Exception:
        pass
    try:
        ua = st.context.headers.get("User-Agent")
    except Exception:
        ua = None
    return device_from_user_agent(ua)


def device_variant(dash, device):
    """EN14: the dashboard as its Phone / Tablet layout authors it (its own zones, size and sheets); the default
    layout when the workbook has none for that device."""
    variants = (dash or {}).get("device_variants") or {}
    v = variants.get(device)
    if not v:
        return dash
    canvas = dict(v.get("canvas") or {})
    width = DEVICE_WIDTH_PX.get(device)
    if width and not canvas.get("maxwidth"):
        canvas.update(maxwidth=width, minwidth=width)          # a device layout is drawn at the device's width
    shown = set(v.get("sheets") or [])
    return dict(dash, layout=v.get("layout") or dash.get("layout"), canvas_zones=v.get("canvas_zones") or [],
                canvas=canvas, device=device, sheets=[s for s in dash.get("sheets") or [] if s.get("name") in shown])


def render_dashboard(dash, presentation="authored"):
    """A shared control/layout failure cannot stop subsequent dashboard tabs."""
    name = dash.get("name", "(dashboard)") if isinstance(dash, dict) else "(invalid dashboard)"
    try:
        if presentation not in ("authored", "native"):
            raise ValueError("Unknown dashboard presentation")
        if presentation == "native" and _is_canvas_dashboard(dash):
            # Release 27 (F0): a dashboard drawn as a Tableau canvas shows what the workbook XML authors --
            # customized-label runs, rank tables without invented header rows, hidden axes -- not the
            # older app's native st.metric cards. Native stays for dashboards without a canvas.
            presentation = "authored"
        dash = device_variant(dash, _device_choice(dash))      # EN14: Phone / Tablet layouts
        if presentation == "native" and _is_canvas_dashboard(dash):
            presentation = "authored"
        if presentation == "native":
            # Presentation is an isolated sheet copy; source execution proofs
            # and the cached IR are never rewritten to match the older app.
            dash = dict(dash, sheets=[dict(s, _presentation="native") for s in dash["sheets"]])
        return _render_dashboard_impl(dash)
    except Exception as exc:
        findings.record("BLOCKER", name, "dashboard-failed", f"{type(exc).__name__}: {exc}")
        st.warning(f"{name}: dashboard unavailable ({type(exc).__name__}: {exc})")


def _render_dashboard_impl(dash):
    # Streamlit-in-Snowflake renders st.metric values LARGER than local, so a
    # wide KPI row (7 across on Superstore's Executive Overview) truncates
    # ("$2,326,..."). Cap the value/label font so full numbers fit. Emitted
    # every render (the <style> must be in the DOM each rerun); repeating it is
    # harmless. Field-found on the deployed demo.
    st.markdown(
        "<style>"
        "[data-testid='stMetricValue']{font-size:1.5rem !important;"
        "line-height:1.25;white-space:normal;overflow:visible;"
        "text-overflow:clip;}"
        "[data-testid='stMetricLabel']{font-size:0.82rem !important;}"
        # Tableau desktop dashboards can put four or more compact zones on a
        # single row. Nested zones can also be narrow on a desktop screen.
        # Let each row wrap based on the available width of its own container
        # so charts and native controls retain a usable minimum width.
        "@media(max-width:1100px){"
        "[data-testid='stHorizontalBlock']:has(> [data-testid='stColumn']:nth-child(2))"
        "{flex-wrap:wrap !important;}"
        "[data-testid='stHorizontalBlock']:has(> [data-testid='stColumn']:nth-child(2))"
        ">[data-testid='stColumn']{flex:1 1 230px !important;min-width:min(100%,230px) !important;}"
        "}"
        "[data-testid='stHorizontalBlock']:has([class*='st-key-v3_parameter_button_'])"
        "{flex-wrap:wrap !important;}"
        "[data-testid='stColumn']:has([class*='st-key-v3_parameter_button_'])"
        "{min-width:min(100%,120px) !important;}"
        "</style>", unsafe_allow_html=True)
    if dash.get("device_layouts"):
        findings.record("INFO", dash["name"], "device-layouts",
                        "Tableau defines %s device layout(s); one responsive "
                        "desktop layout is rendered (Streamlit reflows on "
                        "narrow screens)." % ", ".join(dash["device_layouts"]))
    # ONE clear banner when this dashboard's data is not mapped locally, so an
    # un-onboarded / live-connection workbook explains itself instead of
    # painting every sheet with an error (each sheet then shows a compact note).
    unmapped = sorted({s.get("datasource") for s in dash["sheets"]
                       if s.get("datasource")
                       and s["datasource"] not in getattr(config, "DATASOURCES", {})})
    if unmapped:
        st.warning(
            "This dashboard's datasource(s) — "
            + ", ".join(f"**{d}**" for d in unmapped)
            + " — are not mapped to local or Snowflake tables. If the workbook "
            "ships an extract, run `python init_workbook.py <workbook>` to "
            "onboard it. If it connects live (e.g. Snowflake), it renders only "
            "after deployment to that warehouse. Sheets below that depend on "
            "unmapped data are skipped rather than filled with another "
            "dataset's numbers.")
    if any(s.get("_presentation") == "native" for s in dash["sheets"]):
        st.markdown("<style>[class*=st-key-czone]{container-type:inline-size;}"
                    "[class*=st-key-czone] [data-testid=stMetricValue]{font-size:min(2.35rem,12cqi) !important;white-space:nowrap;}"
                    "[class*=st-key-czone] [data-testid=stMetricLabel]{font-size:.95rem !important;}"
                    "table.rk th,table.rk td{border:1px solid #e7eaf1;}"
                    "</style>", unsafe_allow_html=True)
    global _CURRENT_DASH
    _CURRENT_DASH = dash
    canvas_zones = [z for z in (dash.get("canvas_zones") or []) if isinstance(z, dict)]
    canvas_controls = [z for z in canvas_zones if z.get("kind") == "control"]
    placed = canvas_controls or (dash.get("layout") and (_has_leaf(dash["layout"], "control")))
    if placed:
        # Release 26c: the control zones are drawn in place (_render_layout); here only their values are read.
        with _state_only_widgets():
            where_parts = build_where(dash)
        zones = canvas_controls or _leaves(dash["layout"], "control")
        shown_filters = [f for f in (dash.get("filters") or [])
                         if not any(_filter_for_zone(dash, z) is f for z in zones if z.get("control") == "filter")]
        shown_params = [p for p in (dash.get("params") or [])
                        if not any(z.get("control") == "param" and z.get("caption") == p for z in zones)]
        if (shown_filters or shown_params) and not canvas_controls:
            # a control with no zone in the layout keeps the control row (the canvas draws only what Tableau shows:
            # a control without a visible zone is not on the Tableau dashboard; its value still applies)
            build_where(dict(dash, filters=shown_filters, params=shown_params))
    else:
        where_parts = build_where(dash)
    _CURRENT_PARTS[:] = where_parts if isinstance(where_parts, list) else []
    # Tableau draws a legend only where the dashboard PLACES one (its own
    # zone); a chart never carries one inside its pane. Tell each sheet which
    # legends this dashboard shows so a renderer can drop the rest -- an
    # in-chart legend Tableau doesn't have was eating a third of a narrow
    # panel whose marks the workbook labels directly.
    legends = dash.get("legends")
    zone_legends = {z.get("legend") for z in _leaves(dash.get("layout") or {}, "legend")} | \
        {z.get("legend") for z in canvas_zones if z.get("kind") == "legend"}
    for s in dash["sheets"]:
        s["_legends"] = (legends.get(s["name"], []) if isinstance(legends, dict)
                         else None)
        # an exact-plan sheet whose legend has its own zone draws it there, not inside the chart
        s["_legend_zone"] = bool(s.get("sheet_plan")) and s["name"] in zone_legends
    if canvas_zones and any(z.get("kind") == "sheet" for z in canvas_zones):
        _DARK_KEYS.clear()
        _ZONE_CSS.clear()
        _NAV_SEQ[0] = _NAV_SEQ[1] = 0
        _render_canvas(dash, canvas_zones, {s["name"]: s for s in dash["sheets"]}, where_parts)
        _emit_dark_css()
        return
    if dash.get("layout"):
        _DARK_KEYS.clear()
        _ZONE_CSS.clear()
        _NAV_SEQ[0] = _NAV_SEQ[1] = 0
        _LIGHT_KEYS.clear()
        by_name = {s["name"]: s for s in dash["sheets"]}
        # Release 26c: the dashboard is drawn at the canvas width its zones were sized for (Tableau's fixed or
        # minimum width, at least the app's page width). A wider browser no longer stretches the columns while
        # the charts inside keep the computed size (charts narrower than their zones, labels crowding).
        canvas = dash.get("canvas") or {}
        fixed = canvas.get("maxwidth") if canvas.get("maxwidth") and canvas.get("maxwidth") == canvas.get("minwidth") else None
        page_px = int(fixed or max(canvas.get("minwidth") or 0, 1100))
        st.markdown(f"<style>div.st-key-v3dashcanvas{{max-width:{page_px}px;}}</style>", unsafe_allow_html=True)
        with _safe_container(key="v3dashcanvas"):
            _render_layout(_layout_with_canvas(dash["layout"], dash.get("canvas")), by_name, where_parts)
        _emit_dark_css()
        return
    rows = _rows_from_geom(dash["sheets"])
    if rows is None:                            # no geometry -> simple 2-per-row
        kpis = [s for s in dash["sheets"] if s["kind"] == "kpi"]
        rest = [s for s in dash["sheets"] if s["kind"] != "kpi"]
        for s in kpis:
            render_sheet(s, where_parts)
        if kpis:
            st.divider()
        for i in range(0, len(rest), 2):
            cols = st.columns(2)
            for c, s in zip(cols, rest[i:i + 2]):
                with c:
                    render_sheet(s, where_parts)
        return
    # geometry-driven layout: one st.columns per row, widths from zone w.
    # Dense rows (3+ charts side by side, e.g. KPI sparkline bands) render
    # compact -- full-height charts in a 4-across row dwarf the layout.
    for r in rows:
        if len(r) == 1:
            render_sheet(r[0], where_parts)
        else:
            weights = [max(1, s["geom"]["w"]) for s in r]
            for c, s in zip(st.columns(weights), r):
                with c:
                    if len(r) >= 3:
                        s = dict(s, _compact=True)
                    render_sheet(s, where_parts)


def _render_findings():
    """Surface conversion findings in-app (the transparency contract)."""
    fs = findings.all_findings()
    if not fs:
        return
    n_block = sum(1 for f in fs if f["severity"] == "BLOCKER")
    # "Migration notes" is wrong for an authored app -- nothing was migrated.
    # The authored/ prefix on source_file is the same marker config.set_profile
    # keys off to route to profile_default, so the two stay consistent.
    _authored = "field_scopes" not in IR and str(IR.get("source_file") or "").startswith("authored/")
    _noun = "App notes" if _authored else "Migration notes"
    label = f"{_noun} ({len(fs)} finding{'s' if len(fs) != 1 else ''}"
    label += f", {n_block} blocking)" if n_block else ")"
    with st.expander(label):
        _safe_dataframe(pd.DataFrame(fs)[["severity", "sheet", "code", "message"]],
                        use_container_width=True, hide_index=True)


def param_option_labels(cap):
    """{display value: label} for a list parameter's control. F11: Tableau's control shows the parameter's own
    aliases ('$ Sales'), not the raw value ('Sales'). The parameter definition keys its aliases by the XML
    literal ('"Sales"'), while the control's options are display values, so both are normalised."""
    definitions=[p for d in IR.get('dashboards',[]) for sheet in d.get('sheets',[])
                 for p in sheet.get('worksheet_semantics',{}).get('parameter_definitions',{}).values() if p.get('caption')==cap]
    aliases={}
    if definitions:
        if any(p!=definitions[0] for p in definitions):raise ValueError('Parameter display definitions disagree')
        aliases=definitions[0].get('aliases',{})
    if not aliases:
        aliases=value_labels(cap) or {}
    return {_param_display_value(k):str(v) for k,v in aliases.items()}


def _param_widget(cap, default, scope, host=None):
    """One parameter control. `host` = st (dashboard row) or st.sidebar."""
    host = host or st
    default_text = str(default or "").strip().strip('"')
    temporal = (default_text.startswith("#") and default_text.endswith("#")
                and len(default_text) > 1)
    raw = _param_display_value(PARAMS.get(cap,default))
    key = "param::" + scope + "::" + cap
    state_has_value = hasattr(st.session_state, "__contains__") and key in st.session_state
    dom = (PARAM_DOMAIN.get(cap) or [])
    callback_kwargs={}
    if 'field_scopes' in IR:
        import parameter_actions,sys
        callback_kwargs=dict(on_change=parameter_actions.widget_changed,
            args=(parameter_actions.context(sys.modules[__name__]),cap,key,temporal))
    if dom:                       # list-domain parameter = Tableau dropdown
        opts = [_param_display_value(x) for x in dom]
        idx = opts.index(raw) if raw in opts else 0
        aliases=param_option_labels(cap)
        import parameter_actions
        source_default=_param_display_value(PARAM_DEFS.get(cap,default))
        parameter_actions.register_default(IR,st,key,source_default if source_default in opts else opts[0])
        val = host.selectbox(cap, opts, index=None if state_has_value else idx, key=key,format_func=lambda value:aliases.get(str(value),str(value)),**callback_kwargs)
    else:
        try:
            import parameter_actions
            parameter_actions.register_default(IR,st,key,float(_param_display_value(PARAM_DEFS.get(cap,default))))
            val = host.number_input(cap, value=None if state_has_value else float(raw), key=key,**callback_kwargs)
        except ValueError:
            import parameter_actions
            parameter_actions.register_default(IR,st,key,_param_display_value(PARAM_DEFS.get(cap,default)))
            val = host.text_input(cap, value=None if state_has_value else raw, key=key,**callback_kwargs)
    # Keep Tableau's temporal marker in runtime state even though it must not
    # be shown to a user.  param_sql_literal() uses that marker to emit DATE /
    # TIMESTAMP rather than an ambiguous VARCHAR argument to DATE_TRUNC.
    PARAMS[cap] = f"#{val}#" if temporal else val
    return val


def _param_is_live(cap):
    """True when this parameter can actually change something: some calc's SQL
    carries its token, or a sheet uses it as a top-N count.

    A parameter that is DECLARED in the datasource but referenced by nothing
    (Fil Test's 'Top Customers' / 'Profit Bin Size' -- authored, then never
    placed on a sheet) has no control in Tableau, so it gets none here. We
    used to render a widget for EVERY declared parameter, which put phantom
    controls in the app for fields the workbook does not use."""
    tok = param_token(cap)
    for d in (IR.get("dashboards") or []):
        for s in d.get("sheets", []):
            if (tok in str(s.get("n_param") or "")
                    or s.get("n_param") == cap or s.get("param_select") == cap):
                return True
            refs = []
            for key in ("measures", "filters", "applied_filters"):
                refs.extend(x.get("caption") for x in (s.get(key) or [])
                            if isinstance(x, dict) and x.get("caption"))
            for key in ("x", "y", "color", "size", "detail"):
                x = s.get(key)
                if isinstance(x, dict) and x.get("caption"):
                    refs.append(x["caption"])
            refs.extend(x for x in (s.get("text_fields") or []) if x)
            scoped_calcs = IR.get('field_scopes', {}).get(s.get('datasource_id'), {}).get('calcs', CALCS)
            if any(tok in str((scoped_calcs.get(ref) or {}).get("sql") or "")
                   for ref in refs):
                return True
    return False


def _placed_params():
    """Captions of parameters the workbook places on a dashboard (rendered in
    that dashboard's control row). Derived from IR so EVERY caller agrees --
    the converter used to call _render_param_controls() with no argument and
    re-rendered placed params in the sidebar, producing a SECOND 'Select
    Region' widget that fought the dashboard-row one (changing either did
    nothing; PDF showed sidebar=South-East vs row=North-West)."""
    return {p for d in (IR.get("dashboards") or []) for p in (d.get("params") or [])}


def _render_param_controls(placed=None):
    """Sidebar what-if controls for parameters the workbook does NOT place on a
    dashboard but that a calc actually uses. Placed parameters render in their
    dashboard's control row instead (that is where Tableau shows them). `placed`
    defaults to the IR's placed set so callers cannot drift out of sync."""
    if not PARAM_DEFS:
        return
    # A migrated workbook's visible controls are authoritative.  Declared but
    # unplaced parameters are part of Tableau's authoring model, not its
    # published dashboard UI; inventing a global sidebar leaks controls from
    # hidden/helper sheets across every tab.  Programmatically authored IRs
    # keep the useful sidebar fallback because they have no Tableau placement
    # metadata to preserve.
    source = str(IR.get("source_file") or "")
    if "field_scopes" in IR or (source and not source.startswith("authored/")):
        return
    if placed is None:
        placed = _placed_params()
    show = [c for c in PARAM_DEFS if c not in placed and _param_is_live(c)]
    if not show:
        return
    st.sidebar.header("Parameters")
    for cap in show:
        _param_widget(cap, PARAM_DEFS[cap], "sidebar", st.sidebar)


def run(ir):
    findings.clear()
    configure(ir)
    for cap, formula in (ir.get("calc_drops") or {}).items():
        findings.record("WARNING", "(workbook)", "calc-untranslated",
                        f"Calculated field '{cap}' could not be translated to SQL: "
                        f"{formula.strip()[:120]}")
    boards = {b.get("name") for b in ir.get("story_boards") or []}
    for story in ir.get("stories") or []:
        if story not in boards:
            findings.record("WARNING", story, "story-unsupported",
                            "Tableau Stories are not converted (the sheets they "
                            "reference are available as tabs).")
    d0 = ir["dashboards"][0]["title"] if ir["dashboards"] else "Dashboard"
    st.set_page_config(page_title=d0, layout="wide", initial_sidebar_state="expanded")
    # The app's own title, not a product banner. A net-new app authored in the
    # App Studio has no Tableau anywhere in its history, and announcing itself as
    # a migration is a lie about its provenance that a business user will query.
    # Migrated apps get their dashboard title here too, which is more useful than
    # a constant. `app_title` lets an IR override it explicitly.
    st.title(ir.get("app_title") or d0 or "Tableau → Streamlit (Snowflake)")
    # parameters the workbook PLACES on a dashboard render in that dashboard's
    # control row (where Tableau shows them), not in the sidebar
    _render_param_controls()
    stories = ir.get("story_boards") or []
    tabs = st.tabs([d["title"] for d in ir["dashboards"]] + [s["title"] for s in stories])
    for tab, dash in zip(tabs, ir["dashboards"]):
        with tab:
            if "field_scopes" in ir:
                render_dashboard(dash, presentation="native")
            else:
                render_dashboard(dash)
    for tab, story in zip(tabs[len(ir["dashboards"]):], stories):
        with tab:
            _render_story(ir, story)
    _render_findings()


def story_dashboard(ir, point):
    """V13: the dashboard a story point shows (a dashboard of that name, else the dashboard holding that sheet),
    as a copy with the point's captured categorical filters added to the sheets they belong to. None if unknown."""
    import copy
    name = point.get("sheet")
    dash = next((d for d in ir.get("dashboards") or [] if d.get("name") == name), None) or \
        next((d for d in ir.get("dashboards") or [] if any(s.get("name") == name for s in d.get("sheets") or [])), None)
    if dash is None:
        return None
    dash = copy.deepcopy(dash)
    for f in point.get("filters") or []:
        for s in dash.get("sheets") or []:
            if s.get("name") == f["worksheet"]:
                keep = [a for a in s.get("applied_filters") or [] if a.get("caption") != f["caption"]]
                s["applied_filters"] = keep + [{"caption": f["caption"], "kind": "in", "values": list(f["values"]),
                                                "source": "story-point"}]
    return dash


def _render_story(ir, story):
    """V13: a Tableau story -- one tab per story point (its caption), showing the captured dashboard / sheet."""
    points = story.get("points") or []
    if not points:
        return
    for tab, point in zip(st.tabs([p["caption"][:60] or f"Point {i + 1}" for i, p in enumerate(points)]), points):
        with tab:
            st.caption(point["caption"])
            dash = story_dashboard(ir, point)
            if dash is None:
                st.info(f"Story point shows '{point.get('sheet')}', which is not in the migrated dashboards.")
                continue
            render_dashboard(dash, presentation="native" if "field_scopes" in ir else "authored")
