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

import findings
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
    return to_phys(COLMAP.get(caption, caption))


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
IR = {}             # the configured IR (dashboards consulted for param usage)


CMAPS = {}          # caption -> {value: #hex} -- Tableau's EXACT mark colors
PALETTE_REFS = {}   # caption -> named palette (no explicit maps)


def configure(ir):
    """One-stop engine configuration from an IR (calcs, aliases, params, colmap)."""
    global PARAM_DEFS, PARAMS, COLMAP, CMAPS, PARAM_DOMAIN, IR
    IR = ir or {}
    # PER-WORKBOOK CLIENT PROFILE (2026-07-21 MVP fix): resolve config.PROFILE
    # for THIS workbook before anything reads MEASURE_LIBRARY/DIM_VALUE_*, so a
    # foreign workbook never silently inherits another client's curated
    # measure SQL/formats/colors (see config.py's profile registry).
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
    """The physical table this sheet queries (datasource captured by the parser)."""
    return table_for(s.get("datasource"))


def _fmt_guess(cap):
    l = cap.lower()
    if "ratio" in l or "discount" in l or l.endswith("%"):
        return "pct"
    return "float"


def resolve_measure(T, sheet, m):
    r = _resolve_measure(T, sheet, m)
    if r and m.get("fmt"):
        r["fmt"] = m["fmt"]        # workbook default-format wins
    return r


def _resolve_measure(T, sheet, m):
    """Resolve a measure ref {caption, agg, count_records?} to {sql, fmt[, window]}.
    Resolution order: count-of-records -> workbook calc -> profile library ->
    physical column. NO silent fallback: unresolvable -> finding + None."""
    cap = m["caption"]
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
        return dict(MEASURE_LIBRARY[key])
    col = px(cap)
    if col in table_columns(T):
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
    if cap not in CALCS and expr not in table_columns(T):
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


@st.cache_data(ttl=600)
def _q_exec(sql):
    return run_sql(sql)


import re as _re2


def q(sql):
    # substitute parameter tokens BEFORE the cache key is formed, so changing
    # a what-if parameter in the sidebar re-queries instead of hitting stale cache
    sql = sub_params(sql)
    if "__TBL__" in sql:
        # table-scoped scalar LODs reference the query's own table: resolve
        # the placeholder to the first REAL FROM target in this statement
        # (skipping the placeholder occurrences themselves)
        for m in _re2.finditer(r'\bFROM\s+([A-Za-z0-9_."]+)', sql):
            if m.group(1) != "__TBL__":
                sql = sql.replace("__TBL__", m.group(1))
                break
    try:
        return _q_exec(sql)
    except Exception as e:
        # aggregate-of-FIXED: AGG( ... win OVER (PARTITION BY ..) ... ) is
        # illegal inline -- hoist row-grain windows into base subqueries and
        # retry. Innermost-first, one layer per pass (window-in-window chains
        # need several). Deterministic rewrite of our own generated shape.
        if not _is_win_in_agg_error(e):
            raise
        cur = sql
        for _ in range(3):
            cur = _hoist_windows(cur)
            if not cur:
                raise
            try:
                return _q_exec(cur)
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


_WIN_EXPR = _re2.compile(
    r"\b(SUM|AVG|MIN|MAX|COUNT|MEDIAN|STDEV|VAR)\s*\(", _re2.I)


def _hoist_windows(sql):
    """Rewrite `SELECT ..., AGG(..win OVER (PARTITION BY..)..) .. FROM T
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
    hoisted, i = [], (max(taken) + 1 if taken else 0)
    def _scan(s):
        nonlocal i
        out, pos = s, 0
        while True:
            w = _WIN_EXPR.search(out, pos)
            if not w:
                return out
            op = out.index("(", w.end() - 1)
            end = _match_paren_engine(out, op)
            if end < 0:
                return out
            rest = out[end:]
            om = _re2.match(r"\s*OVER\s*\(", rest, _re2.I)
            if not om:
                pos = w.end()       # step INTO this call: nested windows
                continue            # (AVG(.. MIN(x) OVER (..) ..)) must match
            oend = _match_paren_engine(out, end + om.end() - 1)
            if oend < 0:
                return out
            body = out[op + 1:end - 1]
            if _re2.search(r"\bOVER\s*\(", body, _re2.I):
                pos = w.end()       # window-in-window: hoist the INNER one
                continue            # this pass; outer goes on the next layer
            if _WIN_EXPR.search(body):
                pos = oend          # agg-grain window (MAX(SUM(x)) OVER ()):
                continue            # stays inline, must NOT move to row grain
            expr = out[w.start():oend]
            alias = f"__W{i}__"
            i += 1
            hoisted.append((expr, alias))
            out = out[:w.start()] + alias + out[oend:]
            pos = w.start() + len(alias)
    new_head = _scan(head)
    if not hoisted:
        return None
    inner_sel = ", ".join(f"{e} AS {a}" for e, a in hoisted)
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
    s = str(v).strip()
    if _re2.fullmatch(r"-?\d+(\.\d+)?", s):
        return int(float(s))
    d = pd.to_datetime(s, errors="coerce")
    if pd.isna(d):
        return None
    return {"YEAR": d.year, "QUARTER": (d.month - 1) // 3 + 1, "MONTH": d.month,
            "WEEK": int(d.isocalendar()[1]), "DAY": d.day}.get(part)


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
            and px(caption) not in table_columns(T)):
        return None
    r = _resolve_measure(T, "(sort)", {"caption": caption, "agg": agg})
    if not r or r.get("window"):
        return None                 # a windowed sort key is not a rankable order
    return r["sql"]


def _value_predicate(f):
    """SQL predicate for a FIXED-VALUE filter -- categorical IN / EXCLUDE,
    quantitative range, date-part member/range -- date-part aware. None if the
    filter carries no constrainable value or isn't a value filter (calc /
    top-N / relative-date are handled elsewhere).

    SINGLE SOURCE OF TRUTH: used both by _apply_sheet_filters' main loop (the
    outer WHERE) and by _context_cond (a context filter injected into the top-N
    ranking subquery), so the two can never drift on how a range / date-part /
    exclude filter becomes SQL."""
    pc = px(f["caption"])
    k = f.get("kind")
    part = _EXTRACT_PART.get(f.get("datepart") or "")
    if k == "range":
        conds = []
        if part:                              # date-part range: bounds are part numbers
            for key, op in (("min", ">="), ("max", "<=")):
                v = f.get(key)
                pv = _part_num(v, part) if v is not None else None
                if pv is not None:
                    conds.append(f"EXTRACT({part} FROM {pc}) {op} {pv}")
        else:
            lit = lambda v: (f"'{v}'" if isinstance(v, str) else f"{v}")
            if f.get("min") is not None:
                conds.append(f"{pc} >= {lit(f['min'])}")
            if f.get("max") is not None:
                conds.append(f"{pc} <= {lit(f['max'])}")
        return " AND ".join(conds) or None
    if k == "in" and f.get("values"):
        if part:                              # date-part members: numbers, not raw dates
            pvs = [_part_num(v, part) for v in f["values"]]
            nums = ", ".join(str(x) for x in pvs if x is not None)
            return f"EXTRACT({part} FROM {pc}) IN ({nums})" if nums else None
        keep = [v for v in f["values"] if str(v).lower() != "%null%"]
        clause = []
        if len(keep) != len(f["values"]):
            clause.append(f"{pc} IS NULL")
        if keep:
            vals = ", ".join("'" + _q1(str(v)) + "'" for v in keep)
            clause.append(f"{pc} IN ({vals})")
        return ("(" + " OR ".join(clause) + ")") if clause else None
    if k == "not_in" and f.get("values"):
        drop = [v for v in f["values"] if str(v).lower() != "%null%"]
        conds = []
        if len(drop) != len(f["values"]):
            conds.append(f"{pc} IS NOT NULL")
        if drop:
            vals = ", ".join("'" + _q1(str(v)) + "'" for v in drop)
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
    into a top-N ranking subquery. Now covers ranges + date-parts too (not just
    categorical IN/EXCLUDE) via the shared _value_predicate."""
    return _value_predicate(f)


def _apply_sheet_filters(s, where, T, governed=None, dash_parts=None):
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
        if f.get("context") and px(f["caption"]) not in governed:
            c = _context_cond(f)
            if c:
                context_conds.append(c)
    context_where = ("WHERE " + " AND ".join(context_conds)) if context_conds else ""
    cons = []
    view_order_filters = []          # INDEX()/RANK() gates -- applied LAST (below)
    for f in s.get("applied_filters", []):
        pc = px(f["caption"])
        # A dashboard filter widget already governs this column -- do NOT also
        # apply the sheet's saved value for it (Tableau treats them as one).
        if pc in governed:
            findings.record("INFO", s["name"], "filter-governed-by-dashboard",
                            f"Filter on '{f['caption']}' is controlled by the "
                            "dashboard's own filter widget; the sheet's saved "
                            "value is not additionally applied (matches Tableau).")
            continue
        # boolean filter with BOTH values selected = no-op; skip silently
        if f.get("kind") == "in" and {str(v).lower() for v in f.get("values", [])} >= {"true", "false"}:
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
            rule = alt.Chart(pd.DataFrame({"V": [float(v)]})).mark_rule(
                color="#888", strokeDash=[4, 4]).encode(**{axis: alt.X("V:Q") if axis == "x" else alt.Y("V:Q")})
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


def build_where(dash):
    """Render this dashboard's PLACED controls as a row; return [{col, clause}].

    'Placed' means the .twb has a <zone type='filter'|'paramctrl'> for it --
    Tableau's own control surface. Sheet-level filters still apply to their
    sheets (via _apply_sheet_filters); they are simply not widgets, exactly
    as in Tableau. Each sheet keeps only the parts whose column exists in ITS
    table."""
    cols_avail = table_columns(ORDERS)
    usable = [f for f in dash["filters"]
              if f["kind"] in ("categorical", "date", "date_part")
              and px(f["caption"]) in cols_avail]
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
        cap, pc = f["caption"], px(f["caption"])
        part = f.get("datepart")
        scope = f.get("scope_sheet")     # worksheet this filter zone is bound to
        key = dash["name"] + "::" + cap + "::" + str(part)
        with w:
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
                vals = q(f"SELECT DISTINCT {expr} AS V FROM {ORDERS} "
                         f"WHERE {pc} IS NOT NULL ORDER BY 1")["V"].tolist()
                lbl = f"{_PART_LABEL.get(part, part.title())} of {cap}"
                sel = st.selectbox(lbl, ["All"] + [str(int(v)) for v in vals], key=key)
                # Always emit a part so the widget GOVERNS its column even at
                # "All" (clause=None). "All" means no restriction -- and must
                # still OVERRIDE a sheet's own saved filter on that column, or a
                # context filter's stale saved value (e.g. Region='Central')
                # silently re-narrows the sheet when the widget reads "All".
                parts.append({"col": pc, "clause": (f"{expr} = {int(sel)}" if sel != "All" else None),
                              "caption": cap, "scope": scope, "governs": True})
            elif f["kind"] == "categorical":
                vals = q(f"SELECT DISTINCT {pc} AS V FROM {ORDERS} WHERE {pc} IS NOT NULL ORDER BY 1")["V"].tolist()
                sel = st.selectbox(cap, ["All"] + [str(v) for v in vals], key=key)
                parts.append({"col": pc, "clause": (f"{pc} = '{_q1(sel)}'" if sel != "All" else None),
                              "caption": cap, "scope": scope, "governs": True})
            else:
                # positional access, NOT b["lo"]/b["hi"]: DuckDB keeps unquoted
                # aliases lowercase but Snowflake folds them to UPPERCASE, so a
                # by-name lowercase key raised KeyError('lo') ONLY in the
                # deployed SiS app (field-found; every local test is DuckDB and
                # so never hit it). iloc is dialect-agnostic.
                b = q(f"SELECT MIN({pc}) AS LO, MAX({pc}) AS HI FROM {ORDERS}")
                lo = pd.to_datetime(b.iloc[0, 0]).date()
                hi = pd.to_datetime(b.iloc[0, 1]).date()
                r = st.date_input(cap, value=(lo, hi), min_value=lo, max_value=hi, key=key)
                if isinstance(r, (list, tuple)) and len(r) == 2:
                    parts.append({"col": pc, "clause": f"{pc} BETWEEN '{r[0]}' AND '{r[1]}'",
                                  "caption": cap, "scope": scope, "governs": True})
    return parts


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
    own_filter_cols = {px(f["caption"]) for f in s.get("applied_filters", [])}
    return [p for p in where_parts
            if not isinstance(p, dict)
            or not p.get("scope")
            or p.get("scope") == s.get("name")
            or p.get("col") in own_filter_cols]


def _where_for(T, parts):
    """Assemble the WHERE clause for one sheet's table from dashboard parts.
    A part with clause=None is an 'All' selection -- it governs its column (see
    _parts_for_sheet) but adds no restriction, so it contributes no SQL."""
    avail = table_columns(T)
    keep = [p["clause"] for p in parts if p.get("clause") and p["col"] in avail]
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
    row = {}
    if ok:
        sel = ", ".join(sp["sql"] + f" AS M{i}" for i, m, sp in ok)
        try:
            row = q(f"SELECT {sel} FROM {T} {where}").to_dict("records")[0]
        except Exception:
            # one bad measure poisons the combined query -> resolve per measure
            for i, m, sp in ok:
                try:
                    row[f"M{i}"] = q(f"SELECT {sp['sql']} AS V FROM {T} {where}")["V"][0]
                except Exception as e:
                    row[f"M{i}"] = None
                    findings.record("BLOCKER", s["name"], "measure-query-failed",
                                    f"Measure '{m['caption']}': {type(e).__name__}: {e}")
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
                dsign = -1 if _re2.search(r"-\s*%|\B-%", resolved[j][0]["caption"]) else 1
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


def r_mbar(s, where):
    """Multi-measure bar panel: one horizontal bar small-multiple per measure,
    sharing a single row dimension (e.g. Region), value-labelled per measure."""
    if _is_placeholder_only(s) and _placeholder_list(s, where):
        return
    T = tbl(s)
    dim = s.get("dim")
    ms = s.get("measures", [])
    if not dim or not ms:
        st.info(f"{s['name']}: nothing to show"); return
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
        elif px(ccap) in table_columns(T) or ccap in CALCS:
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
        vmax = float(sub["VAL"].max() or 1)
        longest_lbl = sub["LBL"].astype(str).map(len).max()
        pad_frac = min(0.05 * longest_lbl + 0.05, 0.55)
        xscale = alt.Scale(domain=[0, vmax / (1 - pad_frac)])
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
    df["DIM"] = df["DIM"].astype(str)
    _apply_labels(df, "DIM", dcap)
    if cdim:
        _apply_labels(df, "C", cdim["caption"])
    ranked = any(tc.get("type") == "Rank" for tc in s.get("table_calcs", []))
    if ranked:
        df = df.reset_index(drop=True); df["RNK"] = (df.index + 1).astype(str)
    # sort: manual order if captured, else by value
    morder = _manual_order(s, dcap, df["DIM"].tolist())
    dim_sort = morder if morder else ("-x" if horiz else "-y")
    enc_dim = (alt.Y("DIM:N", sort=dim_sort, title=dcap) if horiz
               else alt.X("DIM:N", sort=dim_sort, title=dcap))
    val_axis = alt.Axis(format=_axis_fmt(meas["fmt"]) if meas["fmt"] != "float" else "$~s")
    enc_val = (alt.X("VAL:Q", title=None, axis=val_axis) if horiz
               else alt.Y("VAL:Q", title=None, axis=val_axis))
    tip = ["DIM:N", alt.Tooltip("VAL:Q", format=",.0f")]
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
        lfmt = meas["fmt"] if meas["fmt"] != "float" else "money0"
        df["LBLTXT"] = df["VAL"].map(lambda v: fmt_val(v, lfmt))
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
            tkw = ({"align": "left", "dx": 3} if horiz else {"baseline": "bottom", "dy": -3})
            tenc = {k: v for k, v in enc.items() if k in ("x", "y", "xOffset", "yOffset")}
            layers.append(alt.Chart(df).mark_text(fontSize=10, color="#3a3a3a", **tkw)
                          .encode(text="LBLTXT:N", **tenc))
    layers += _refline_rule(s, df, "VAL", axis="x" if horiz else "y")
    chart = alt.layer(*layers) if len(layers) > 1 else bars
    st.altair_chart(chart.properties(height=360), use_container_width=True)


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


def _timeseries_one(s, where, area):
    T = tbl(s)
    x, y = s["x"], s["y"]
    xp = px(x["caption"])
    part = {"yr": "YEAR", "qr": "QUARTER", "mn": "MONTH", "tmn": "MONTH",
            "twk": "WEEK", "wk": "WEEK", "tdy": "DAY", "dy": "DAY",
            "mdy": "DAY"}.get(x.get("datepart") or "mn", "MONTH")
    meas = resolve_measure(T, s["name"], y)
    if meas is None:
        st.warning(f"{s['name']}: measure '{y['caption']}' not convertible -- see migration notes")
        return
    color = s.get("color")
    panel = s.get("panel")
    # A color dimension that is a row-level WINDOW calc (e.g. {FIXED Order ID:
    # SUM(Profit)}>0) must be computed in a base CTE, not in GROUP BY directly.
    flag_expr, flag_mode = flag_dim(s, T, color)
    flag_windowed = flag_mode == "window"
    sel = [f"DATE_TRUNC('{part}', {xp}) AS T", f"{meas['sql']} AS VAL"]
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
               f"SELECT DATE_TRUNC('{part}', {xp}) AS T, "
               + (f"{px(panel)} AS PANEL, " if panel else "")
               + "FLAG, " + f"{meas['sql']} AS VAL FROM base GROUP BY 1"
               + (", 2, 3" if panel else ", 2"))
    df = q(sql)
    df["T"] = pd.to_datetime(df["T"])
    # friendly labels: workbook aliases first, profile override second
    labels = value_labels((color or {}).get("caption", ""))
    if labels is not None and "FLAG" in df.columns:
        df["FLAG"] = df["FLAG"].astype(str).map(lambda v: _label_of(labels, v))
    val_fmt = {"pct": ".1%", "num0": ",.0f", "num2": ",.2f", "int": ",.0f"}.get(meas["fmt"], "$,.0f")
    y_axis = _axis_obj(meas["fmt"] if meas["fmt"] != "float" else "money0")

    def one(d, title=None):
        # stack areas; NEVER stack lines (Vega-Lite happily stacks lines too,
        # which silently turns a multi-line chart into cumulative offsets)
        enc = dict(x=alt.X("T:T", title=None),
                   y=alt.Y("VAL:Q", title=None, stack="zero" if area else None,
                           axis=y_axis),
                   tooltip=["T:T", alt.Tooltip("VAL:Q", format=val_fmt)])
        if "FLAG" in d.columns:
            dom = sorted(d["FLAG"].dropna().unique().tolist())
            cdom, crng = cat_colors((s.get("color") or {}).get("caption"), dom)
            if cdom:
                dom, rng = cdom, crng
            else:
                rng = PROFIT_COLORS if len(dom) == 2 else None
            enc["color"] = alt.Color("FLAG:N", title=(s.get("color") or {}).get("caption", ""),
                                     scale=alt.Scale(domain=dom, range=rng) if rng else alt.Undefined)
            enc["tooltip"] = ["T:T", "FLAG:N", alt.Tooltip("VAL:Q", format=val_fmt)]
        mc = s.get("mark_color")
        c = alt.Chart(d)
        c = (c.mark_area(opacity=0.85, **({"color": mc} if mc and "FLAG" not in d.columns else {}))
             if area else c.mark_line(point=bool(s.get("labels")),
                                      **({"color": mc} if mc and "FLAG" not in d.columns else {})))
        c = c.encode(**enc).properties(height=200 if not title else 150,
                                       title=title or "")
        if s.get("labels") and not area:
            lfmt = meas["fmt"] if meas["fmt"] != "float" else "money0"
            d = d.copy().sort_values("T").reset_index(drop=True)
            d["LBLTXT"] = d["VAL"].map(lambda v: fmt_val(v, lfmt))
            if len(d) > 16:              # dense series: label every k-th + last
                k = max(1, round(len(d) / 12))
                keep = set(range(0, len(d), k)) | {len(d) - 1}
                d.loc[~d.index.isin(keep), "LBLTXT"] = ""
            txt = alt.Chart(d).mark_text(fontSize=10, dy=-10, color="#3a3a3a").encode(
                x=enc["x"], y=enc["y"], text="LBLTXT:N")
            c = c + txt
        st.altair_chart(c, use_container_width=True)

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
        enc = dict(x=alt.X("X:Q", title=x["caption"], axis=alt.Axis(format=_axis_fmt(xs["fmt"]) if xs["fmt"] != "float" else "$~s")),
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
    xs = resolve_measure(T, s["name"], x)
    ys = resolve_measure(T, s["name"], y)
    if xs is None and ys is None:
        st.warning(f"{s['name']}: measures not convertible -- see migration notes"); return
    has_grain = grain and px(grain) in table_columns(T)
    sel = []
    if has_grain:
        sel.append(f"{px(grain)} AS G")
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
    sz = s.get("size")
    if isinstance(sz, dict) and sz.get("kind") == "measure" and has_grain:
        smeas = resolve_measure(T, s["name"], sz)
        if smeas and not smeas.get("window"):
            sel.append(f"{smeas['sql']} AS SZ")
    grp = " GROUP BY 1" if has_grain else ""
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
    enc = dict(x=alt.X("X:Q", title=x["caption"], axis=alt.Axis(format=_axis_fmt(xs["fmt"]) if (xs and xs["fmt"] != "float") else "$~s")),
               y=alt.Y("Y:Q", title=y["caption"], axis=alt.Axis(format=_axis_fmt(ys["fmt"]) if (ys and ys["fmt"] != "float") else "$~s")),
               tooltip=list(df.columns))
    if "C" in df.columns:
        if pd.api.types.is_numeric_dtype(df["C"]):
            enc["color"] = alt.Color("C:Q", title=(color or {}).get("caption", ""),
                                     scale=_alt_color_scale((color or {}).get("scale")))
        else:                       # calc measure returning labels (e.g. quota buckets)
            enc["color"] = alt.Color("C:N", title=(color or {}).get("caption", ""))
    else:
        enc["color"] = alt.value(s.get("mark_color") or PROFIT_COLORS[0])
    if "SZ" in df.columns:
        enc["size"] = alt.Size("SZ:Q", title=(sz or {}).get("caption", ""))
    mark_kw = dict(opacity=0.6) if "SZ" in df.columns else dict(size=60, opacity=0.6)
    pts = alt.Chart(df).mark_circle(**mark_kw).encode(**enc)
    layers = [pts]
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
    sel.append(f"{xs['sql']} AS X")
    cmeas = None
    if cm:
        cmeas = resolve_measure(T, s["name"], cm)
        if cmeas:
            sel.append(f"{cmeas['sql']} AS C")
    df = q(f"SELECT {', '.join(sel)} FROM {T} {where} GROUP BY {', '.join(grp)}")
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
        enc["size"] = alt.Size("SZ:Q", title=(sz or {}).get("caption", ""))
    h = max(300, 18 * df["Y"].nunique())
    st.altair_chart(alt.Chart(df).mark_circle(opacity=0.7).encode(**enc)
                    .properties(height=min(h, 700)), use_container_width=True)


def r_dual(s, where):
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
        x_enc = alt.X("T:O", title=None, axis=alt.Axis(labelAngle=0))
    else:
        df["T"] = pd.to_datetime(df["T"])
        x_enc = alt.X("T:T", title=None)
    default_cols = ["#4e79a7", "#f28e2b"]
    layers = []
    for i, (ym, sp) in enumerate(ok):
        col = ym.get("color") or default_cols[i]
        lbl = ym.get("label", ym["caption"])
        if sp["fmt"] != "float":
            fmt = _axis_fmt(sp["fmt"])
        elif float(df[f"V{i}"].abs().max() or 0) <= 1.5:
            fmt = "%"          # 0-1 rate scale: 0.4 reads as 40%, never "400m"
        else:
            fmt = "$~s"
        y_enc = alt.Y(f"V{i}:Q",
                      axis=alt.Axis(title=lbl, titleColor=col,
                                    orient="left" if i == 0 else "right",
                                    format=fmt,
                                    labelExpr='replace(datum.label, "G", "B")' if "$" in fmt else alt.Undefined))
        base = alt.Chart(df).encode(x=x_enc, y=y_enc,
                                    tooltip=["T:O" if yearly else "T:T",
                                             alt.Tooltip(f"V{i}:Q", title=lbl, format=",.1f")])
        mk = (ym.get("mark") or "line").lower()
        if mk == "bar":
            layers.append(base.mark_bar(color=col, opacity=0.75))
        elif mk == "area":
            layers.append(base.mark_area(color=col, opacity=0.5))
        else:
            layers.append(base.mark_line(color=col, point=True, strokeWidth=2.5))
    # bars behind lines
    layers.sort(key=lambda l: 0 if l.mark == "bar" or getattr(l.mark, "type", "") == "bar" else 1)
    ch = alt.layer(*layers).resolve_scale(y="independent")
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
                        legend=None if s.get("_compact") else alt.Undefined),
        order=alt.Order("VAL:Q", sort="descending"),
        tooltip=["SEG:N", alt.Tooltip("VAL:Q", format=",.0f", title=m["caption"])])
    # radius must fit the geometry column: 3-across rows give ~200px --
    # a fixed 130px arc CLIPS into a pac-man (caught by browser screenshot)
    r_out, r_in, tsize = (80, 50, 12) if s.get("_compact") else (130, 80, 16)
    pie = base.mark_arc(outerRadius=r_out, innerRadius=r_in)  # donut, as Tableau
    total = fmt_val(df["VAL"].sum(), fmt if fmt != "float" else "money0")
    center = alt.Chart(pd.DataFrame({"t": [f"Total {total}"]})).mark_text(
        size=tsize, fontWeight="bold").encode(text="t:N")
    layers = [pie, center]
    if s.get("labels"):
        layers.append(base.mark_text(radius=r_out + 24, fontSize=11 if s.get("_compact") else 12)
                      .encode(text="LBL:N"))
    hpie = 230 if s.get("_compact") else 360
    if s.get("_hpx"):
        hpie = min(hpie, s["_hpx"])
    st.altair_chart(alt.layer(*layers).properties(height=hpie),
                    use_container_width=True)


def r_dtbar(s, where):
    """Stacked bars per period (Tableau Bar mark / Automatic over a DISCRETE
    date pill, e.g. Tourism Income by Region: one bar per year, stacked by
    Region in the workbook's colors)."""
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
    det = s.get("detail")
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
    T = tbl(s)
    cols_avail = table_columns(T)
    # a dim converts if it is a physical column, a row-level calc/GROUP, or an
    # aggregate-level bucket calc (conditional SET / Achieved-Quota style):
    # the last kind is SELECT-only -- it cannot be part of GROUP BY.
    def _dim_kind(d):
        if px(d) in cols_avail:
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
    if geo not in table_columns(T):
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
    # non-data sheet (text box / show-hide toggle / blank): nothing to query.
    # render nothing, record no error -- Tableau scaffolding, not a failure.
    if s.get("non_data"):
        findings.record("INFO", s["name"], "non-data-sheet",
                        "Text/control sheet (no data fields) -- nothing to "
                        "convert; rendered blank.")
        return
    title = s.get("title") or s["name"]
    show = (s.get("geom") or {}).get("show_title", True)
    if show:
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
    s = _drop_window_dims(s)
    T = tbl(s)
    if s.get("blend"):
        findings.record("WARNING", s["name"], "blend-partial",
                        f"Sheet blends datasources {s['blend']}; only the primary "
                        f"({s.get('datasource')}) is queried.")
    if s.get("forecast"):
        findings.record("APPEARANCE", s["name"], "forecast-dropped",
                        "Tableau forecast overlay (%s%s) is not reproduced; "
                        "historical actuals only." %
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
    try:
        k = s["kind"]
        if k == "kpi": r_kpi(s, where)
        elif k == "mbar": r_mbar(s, where)
        elif k == "bar": r_bar(s, where)
        elif k == "line": r_timeseries(s, where, area=False)
        elif k == "area": r_timeseries(s, where, area=True)
        elif k == "scatter": r_scatter(s, where)
        elif k == "heatmap": r_heatmap(s, where)
        elif k == "circle": r_circle(s, where)
        elif k == "map": r_map(s, where)
        elif k == "pctbar": r_pctbar(s, where)
        elif k == "dots": r_dots(s, where)
        elif k == "gantt": r_gantt(s, where)
        elif k == "bubbles": r_bubbles(s, where)
        elif k == "strips": r_strips(s, where)
        elif k == "dtbar": r_dtbar(s, where)
        elif k == "treemap": r_treemap(s, where)
        elif k == "pie": r_pie(s, where)
        elif k == "dual": r_dual(s, where)
        else: r_table(s, where)
    except Exception as e:
        findings.record("BLOCKER", s["name"], "render-failed",
                        f"{type(e).__name__}: {e}")
        st.warning(f"{title}: could not render ({type(e).__name__}: {e})")


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


def _safe_dataframe(df, **kw):
    """st.dataframe with graceful degradation: SiS's older Streamlit lacks
    hide_index=/use_container_width=. Drop unsupported kwargs on TypeError.
    When hide_index isn't available, blank the index labels instead so the
    table still reads clean."""
    if kw.get("column_config") is None:
        kw.pop("column_config", None)
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


def _render_layout(node, by_name, where_parts, compact=False, dark=False,
                   in_row=False, col_depth=0, _seq=[0]):
    """Walk the dashboard's zone-container tree (Tableau declares grouping,
    proportions and container backgrounds -- render them instead of one
    column per sheet). Dark containers get a keyed st.container + CSS."""
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
            s2["_hpx"] = max(110, _zone_px(node["h"]) - 60)  # minus title/pad
        render_sheet(s2, where_parts)
        return
    bg = (node.get("bg") or "").lower()
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
    elif bg:
        ctx = _safe_container(border=True, **hkw)   # white Tableau card
    else:
        ctx = None
    def _children():
        kids = node["children"]
        # side-by-side only while column nesting is within the platform's
        # limit; beyond it, stack vertically (renders everywhere, just taller)
        if (node["dir"] == "horz" and len(kids) > 1
                and col_depth < _max_col_nest()):
            weights = [max(1, k.get("w", 1)) for k in kids]
            comp = compact or len(kids) >= 3
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
    if ctx is not None:
        with ctx:
            _children()
    else:
        _children()


def _emit_dark_css():
    if not _DARK_KEYS:
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
    st.markdown("<style>" + "\n".join(rules) + "</style>",
                unsafe_allow_html=True)


def render_dashboard(dash):
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
    where_parts = build_where(dash)
    if dash.get("layout"):
        _DARK_KEYS.clear()
        by_name = {s["name"]: s for s in dash["sheets"]}
        _render_layout(dash["layout"], by_name, where_parts)
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
    label = f"Migration notes ({len(fs)} finding{'s' if len(fs) != 1 else ''}"
    label += f", {n_block} blocking)" if n_block else ")"
    with st.expander(label):
        _safe_dataframe(pd.DataFrame(fs)[["severity", "sheet", "code", "message"]],
                        use_container_width=True, hide_index=True)


def _param_widget(cap, default, scope, host=None):
    """One parameter control. `host` = st (dashboard row) or st.sidebar."""
    host = host or st
    raw = str(default or "").strip().strip('"')
    key = "param::" + scope + "::" + cap
    dom = (PARAM_DOMAIN.get(cap) or [])
    if dom:                       # list-domain parameter = Tableau dropdown
        opts = [str(x).strip('"') for x in dom]
        idx = opts.index(raw) if raw in opts else 0
        val = host.selectbox(cap, opts, index=idx, key=key)
    else:
        try:
            val = host.number_input(cap, value=float(raw), key=key)
        except ValueError:
            val = host.text_input(cap, value=raw, key=key)
    PARAMS[cap] = val
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
    for c in CALCS.values():
        if isinstance(c, dict) and tok in str(c.get("sql") or ""):
            return True
    return any(tok in str(s.get("n_param") or "") or s.get("n_param") == cap
               for d in (IR.get("dashboards") or []) for s in d.get("sheets", []))


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
    for story in ir.get("stories") or []:
        findings.record("WARNING", story, "story-unsupported",
                        "Tableau Stories are not converted (the sheets they "
                        "reference are available as tabs).")
    d0 = ir["dashboards"][0]["title"] if ir["dashboards"] else "Dashboard"
    st.set_page_config(page_title=d0, layout="wide", initial_sidebar_state="expanded")
    st.title("Tableau → Streamlit (Snowflake)")
    # parameters the workbook PLACES on a dashboard render in that dashboard's
    # control row (where Tableau shows them), not in the sidebar
    _render_param_controls()
    tabs = st.tabs([d["title"] for d in ir["dashboards"]])
    for tab, dash in zip(tabs, ir["dashboards"]):
        with tab:
            render_dashboard(dash)
    _render_findings()
