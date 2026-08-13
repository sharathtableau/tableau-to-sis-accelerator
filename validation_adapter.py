"""
validation_adapter.py -- R12: the ADAPTER LAYER between this accelerator and
the proof-first validation engine vendored as `validation_report.py`.

`validation_report.py` is deliberately decoupled from any one extraction
method: it compares/renders, and its own README states the accelerator must
supply four adapters (Tableau, Streamlit, backend, orchestrator). This
module is those adapters, built on machinery this project already has:

  * TABLEAU rows  -- tableau_server.query_view_data_csv (the REST crosstab
                     export; needs a workbook fetched from Tableau Server).
                     Absent for a file-uploaded workbook -> the chart is
                     honestly BLOCKED, never quietly passed.
  * STREAMLIT rows-- the REAL dataframe the generated app hands Altair,
                     captured via headless_render.capture_sheet_chart().
                     This is the app's OWN rendered data, NOT a second run
                     of the backend SQL -- using app-SQL for both legs would
                     be circular, the exact bug class this project keeps a
                     standing rule against.
  * BACKEND rows  -- canonical SQL built from the chart's own grain +
                     measure expressions and executed against the real
                     table (backend.run_sql).

WHY THE COLUMN MAPPING IS READ, NOT GUESSED: engine.py renders every chart
with internal column aliases (VAL/DIM/T/FLAG/PANEL/X/Y/C...), so the
captured dataframe's columns are NOT Tableau captions. Rather than
reverse-engineer ~15 chart kinds' aliasing (a wrong guess would silently
compare the wrong columns -- worse than not comparing), the mapping is
resolved from TWO REAL sources and nothing else:
  1. the rendered chart's OWN Vega-Lite encoding (which channel carries
     which column, and its `type` -> dimension vs measure), and
  2. that channel's title, or failing that the SHEET SPEC's own caption for
     the same channel.
A column that resolves to neither leaves the chart NOT_VALIDATED -- this
project's (and the validation engine's) "no proof, no pass" rule.

TOLERANCE comes from the workbook's OWN number format (the IR's `fmt`, e.g.
`cur0` = whole-dollar currency -> +/-$0.50), never a flat global percentage
and never inferred from a formatted screenshot -- which is exactly what
validation_report.derive_tolerance asks for.
"""
from __future__ import annotations

import datetime as _dt
import re as _re

import engine
import headless_render as HR
from backend import run_sql
from calc_translator import to_phys

# The IR's number-format vocabulary -> the validation engine's measure
# definition. Drives PRECISION-DERIVED tolerance (see module docstring).
# Unknown/absent format falls back to a 2-decimal number, the engine's own
# default, rather than silently claiming exactness.
_FMT_SPEC = {
    "cur0":   {"kind": "currency", "display_decimals": 0},
    "money0": {"kind": "currency", "display_decimals": 0},
    "cur2":   {"kind": "currency", "display_decimals": 2},
    "money2": {"kind": "currency", "display_decimals": 2},
    "num0":   {"kind": "integer",  "display_decimals": 0},
    "int":    {"kind": "integer",  "display_decimals": 0},
    "pct":    {"kind": "percent",  "display_decimals": 1, "value_scale": "fraction"},
    "float":  {"kind": "number",   "display_decimals": 2},
}

# Vega-Lite encoding channels that carry a real field worth comparing.
# Order matters only for deterministic output.
_DATA_CHANNELS = ("x", "y", "color", "row", "column", "size", "theta", "detail")
# Channels whose Vega-Lite `type` marks them a dimension (grain) vs measure.
_DIM_TYPES = {"nominal", "ordinal", "temporal"}


def measure_definition(caption, fmt=None):
    """One measure definition for the validation engine, with its tolerance
    basis taken from the WORKBOOK's own number format."""
    spec = dict(_FMT_SPEC.get(fmt or "", {"kind": "number", "display_decimals": 2}))
    spec.update({"name": caption, "field": caption})
    return spec


def _channel_caption(sheet, channel, enc_cfg):
    """The real Tableau caption for one encoding channel, from the chart's
    own title if it set one, else the SHEET SPEC's caption for that same
    channel. Returns None when neither source names it -- the caller then
    refuses to validate the chart rather than inventing a name."""
    title = enc_cfg.get("title")
    if isinstance(title, list):
        title = " ".join(str(x) for x in title)
    if isinstance(title, str) and title.strip():
        return title.strip()
    for holder in (enc_cfg.get("axis"), enc_cfg.get("legend")):
        if isinstance(holder, dict) and isinstance(holder.get("title"), str) \
                and holder["title"].strip():
            return holder["title"].strip()
    # Fall back to the sheet spec's OWN caption for this channel.
    spec_val = sheet.get(channel)
    if isinstance(spec_val, dict) and spec_val.get("caption"):
        return spec_val["caption"]
    # The sheet's bare `dim` string names a DIMENSION, so it may only stand in
    # for a channel Vega-Lite itself typed as a dimension. Applying it to any
    # untitled x/y channel produced `SUM(SALES_PERSON)` on a real mbar sheet
    # (the measure axis wrongly named after the dimension) -- found running
    # this for real, and exactly the guessing this module refuses to do.
    if enc_cfg.get("type") in _DIM_TYPES and isinstance(sheet.get("dim"), str):
        return sheet["dim"]
    return None


def _channel_fmt(sheet, channel):
    spec_val = sheet.get(channel)
    if isinstance(spec_val, dict):
        return spec_val.get("fmt")
    return None


def _top_encoding(spec):
    """A chart's encoding block, looking into the first layer of a layered
    spec (engine.py layers reference lines / labels over the real mark)."""
    enc = spec.get("encoding")
    if enc:
        return enc
    for key in ("layer", "concat", "hconcat", "vconcat"):
        for child in spec.get(key, []) or []:
            found = _top_encoding(child)
            if found:
                return found
    return {}


def resolve_chart_columns(sheet, chart):
    """(grain, measures, rename) for a captured chart, or (None, None,
    reason) when the mapping cannot be resolved from real sources.

    grain    -- list of Tableau captions forming the chart's displayed key
    measures -- list of validation-engine measure definitions
    rename   -- {captured_dataframe_column: Tableau caption}
    """
    enc = _top_encoding(chart.to_dict())
    if not enc:
        return None, None, "chart exposes no encoding channels to map"
    grain, measures, rename = [], [], {}
    for channel in _DATA_CHANNELS:
        cfg = enc.get(channel)
        if not isinstance(cfg, dict) or not cfg.get("field"):
            continue
        column = cfg["field"]
        if column in rename:                 # same column on two channels
            continue
        caption = _channel_caption(sheet, channel, cfg)
        if not caption:
            return None, None, (f"channel '{channel}' renders column "
                               f"'{column}' under no resolvable Tableau "
                               "caption -- refusing to guess which field it is")
        rename[column] = caption
        if cfg.get("type") in _DIM_TYPES:
            if caption not in grain:
                grain.append(caption)
        elif not any(m["name"] == caption for m in measures):
            measures.append(measure_definition(caption, _channel_fmt(sheet, channel)))
    if not grain:
        return None, None, "chart has no dimension channel -- no comparable row key"
    if not measures:
        return None, None, "chart has no measure channel -- nothing numeric to compare"
    clash = {m["name"] for m in measures} & set(grain)
    if clash:
        # The same caption resolved as BOTH a key and a measure -- the mapping
        # is ambiguous, so refuse rather than emit a query that groups by and
        # aggregates the same field.
        return None, None, (f"caption(s) {', '.join(sorted(clash))} resolved as both "
                           "a grain field and a measure -- ambiguous mapping")
    return grain, measures, rename


def _candidate_dataframes(chart):
    """Every real DataFrame reachable from a captured chart -- the top level
    plus every layer/hconcat/vconcat/concat child, depth-first.

    A LayerChart's top-level `.data` is `alt.Undefined` UNLESS every layer
    shares the exact same dataframe object. engine.py's own bar+text (or
    line+text, area+text...) pattern breaks that: the text-label layer adds
    derived columns (a stacking cumulative offset, a midpoint for label
    placement) on top of a copy of the base frame, so Altair leaves the
    data on each layer instead of hoisting it. FOUND LIVE 2026-08-11 on
    Regional Analysis: every stacked-bar and line-with-labels chart
    reported "captured chart exposed no dataframe to compare" even though
    the real rendered rows were one attribute-lookup away, on
    `chart.layer[0].data` instead of `chart.data`."""
    seen = []
    d = getattr(chart, "data", None)
    if hasattr(d, "columns"):          # real DataFrame, not alt.Undefined
        seen.append(d)
    for attr in ("layer", "hconcat", "vconcat", "concat"):
        kids = getattr(chart, attr, None)
        if not kids or not hasattr(kids, "__iter__"):
            continue
        for kid in kids:
            seen.extend(_candidate_dataframes(kid))
    return seen


def streamlit_rows(chart, rename):
    """The REAL rows the app renders, with engine aliases renamed to Tableau
    captions. Never re-runs SQL -- this is the app's own chart data.

    Prefers the first candidate dataframe that carries EVERY needed column
    (a full superset of `rename`'s keys) -- the base mark layer engine.py
    always builds first, before any label/reference-line layer that only
    ADDS derived columns. Falls back to the candidate with the most
    matching columns so a genuinely partial match still degrades to
    comparing what IS available rather than refusing outright, but never
    silently prefers a worse match over a complete one."""
    needed = set(rename)
    best, best_n = None, -1
    for df in _candidate_dataframes(chart):
        have = needed & set(df.columns)
        if len(have) == len(needed):       # full match -- stop here
            best = df
            break
        if len(have) > best_n:
            best, best_n = df, len(have)
    if best is None:
        return None
    keep = [c for c in rename if c in best.columns]
    if not keep:
        return None
    return best[keep].rename(columns=rename).to_dict("records")


# Tableau date-part token -> SQL DATE_TRUNC unit, reusing the SAME mapping
# engine.py's own chart builders use (engine.py:1427) rather than a second
# copy that could drift.
_TRUNC_PART = {"yr": "YEAR", "tyr": "YEAR", "qr": "QUARTER", "tqr": "QUARTER",
               "mn": "MONTH", "tmn": "MONTH", "wk": "WEEK", "twk": "WEEK",
               "dy": "DAY", "tdy": "DAY", "mdy": "DAY"}


def _grain_expr(sheet, caption):
    """The SQL expression producing this grain field's DISPLAYED values.

    Reuses engine.rdim() -- the app's OWN dimension resolver, so a
    calculated dimension ('Order Profitable?', 'Ship Status') resolves to
    its real calc SQL instead of a nonexistent physical column -- and
    applies the same DATE_TRUNC the chart applied when the sheet declares a
    date part (without it a monthly chart is compared against daily backend
    rows, which is a guaranteed false failure, found running this for real).
    """
    expr = engine.rdim(caption)
    for channel in ("x", "y", "color", "detail"):
        cfg = sheet.get(channel)
        if isinstance(cfg, dict) and cfg.get("caption") == caption:
            part = _TRUNC_PART.get((cfg.get("datepart") or "").lower())
            if part:
                return f"DATE_TRUNC('{part}', {expr})"
            break
    return expr


def _sql_literal(value):
    """A SQL literal for a displayed key value, or None when the value's
    type isn't safely representable (the caller then refuses to scope)."""
    import datetime as _d
    if value is None:
        return None
    # numpy/pandas scalars are NOT instances of Python bool/int/float, so a
    # numpy.bool_ fell through to str() and was emitted as the STRING
    # 'True' -- compared against a real boolean column that matches nothing,
    # which silently returned zero backend rows and reported every value as
    # failed (found running this for real on an "Order Profitable?" series).
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            value = value.item()
        except Exception:
            pass
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (_d.date, _d.datetime)):
        return "'" + value.isoformat(sep=" ") + "'"
    text = str(value)
    if hasattr(value, "isoformat"):          # pandas Timestamp / numpy datetime
        text = value.isoformat(sep=" ") if hasattr(value, "hour") else value.isoformat()
    return "'" + text.replace("'", "''") + "'"


_MAX_SCOPED_KEYS = 500

# Aggregate functions that make a grain expression unusable in a WHERE clause.
_AGG_IN_EXPR = _re.compile(
    r"\b(SUM|AVG|COUNT|COUNTD|MIN|MAX|MEDIAN|STDDEV|VARIANCE|RANK|ROW_NUMBER)\s*\(",
    _re.I)


def backend_rows(ir, sheet, table, grain, measures, displayed_rows, all_metrics):
    """Canonical SQL at the CHART's own grain, executed against the real
    table -- an independent recomputation of the values the app displays.

    SCOPED TO THE DISPLAYED KEYS, deliberately. A chart legitimately shows a
    SUBSET of the table (a top-30 ranking, a sheet filter, a date range); an
    unscoped backend query returns every key and the comparison then reports
    a huge false 'missing keys' failure that is really just a scope
    difference (found running this for real: CustomerRank displayed 30 rows
    against 800 unscoped backend rows). Scoping asks the backend the
    question that actually matters -- 'for exactly these displayed marks,
    what are the true values?' -- which still independently catches a wrong
    number, a wrong aggregation or a mis-routed table.

    The KEY SET itself is validated against TABLEAU's own export (the
    authority on which marks should exist), not against the backend; when no
    Tableau export is available the validation engine reports that gate
    BLOCKED rather than passing it.

    Returns (rows, sql) or (None, reason)."""
    import parity
    # MUST be a set: parity._resolve_measure_sql does `m["cols"] <= table_cols`
    # (a subset test), and engine.table_columns returns a LIST -- passing it
    # through raised TypeError on every chart (found running this for real).
    table_cols = set(engine.table_columns(table) or ())
    exprs, sel, precomputed = [], [], []
    for cap in grain:
        try:
            expr = _grain_expr(sheet, cap)
        except Exception as e:
            return None, f"grain field '{cap}' did not resolve -- {type(e).__name__}: {e}"
        if not expr:
            return None, f"grain field '{cap}' did not resolve to a SQL expression"
        if _AGG_IN_EXPR.search(expr):
            # An aggregate/WINDOW-grain calculated dimension (a FIXED LOD or
            # table calc rendered as a colour series, e.g. Superstore's
            # "Order Profitable?") is illegal directly in a WHERE clause.
            # Precompute it in a base CTE and filter on the alias instead --
            # the SAME pattern engine.py itself uses for these calcs
            # (engine.py's `WITH base AS (SELECT *, <flag_expr> AS FLAG ...)`),
            # reused rather than refusing the chart outright.
            alias = f"G{len(precomputed)}"
            precomputed.append(f'{expr} AS "{alias}"')
            expr = f'"{alias}"'
        exprs.append(expr)
        sel.append(f'{expr} AS "{cap}"')
    for m in measures:
        resolved = parity._resolve_measure_sql(ir, m["name"], "sum", table_cols, all_metrics)
        if not resolved:
            return None, f"measure '{m['name']}' does not resolve against {table}"
        sel.append(f'{resolved[0]} AS "{m["name"]}"')

    keys = [tuple(row.get(cap) for cap in grain) for row in displayed_rows]
    keys = [k for k in dict.fromkeys(keys)]
    if not keys:
        return None, "chart displayed no rows to scope the backend query to"
    if len(keys) > _MAX_SCOPED_KEYS:
        return None, (f"chart displays {len(keys)} keys, above this check's "
                     f"{_MAX_SCOPED_KEYS}-key scoping limit")
    tuples = []
    for key in keys:
        literals = [_sql_literal(v) for v in key]
        if any(lit is None for lit in literals):
            return None, "a displayed key contains a NULL/unrepresentable value"
        tuples.append("(" + ", ".join(literals) + ")")
    where = f"({', '.join(exprs)}) IN ({', '.join(tuples)})"
    source = table
    if precomputed:
        source = f"(SELECT *, {', '.join(precomputed)} FROM {table}) AS _base"
    sql = (f"SELECT {', '.join(sel)} FROM {source} WHERE {where} "
           f"GROUP BY {', '.join(str(i + 1) for i in range(len(exprs)))}")
    try:
        df = run_sql(sql)
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    df.columns = [str(c) for c in df.columns]
    lookup = {c.upper(): c for c in df.columns}
    out = []
    for _, row in df.iterrows():
        rec = {}
        for cap in grain + [m["name"] for m in measures]:
            key = lookup.get(cap.upper())
            rec[cap] = row[key] if key else None
        out.append(rec)
    return out, sql


_NUM_RE = _re.compile(r"-?[\d,]*\.?\d+")


def parse_displayed(text, kind):
    """A displayed KPI string ('$2,326,534', '12.6%') back to a comparable
    number. A percent is returned on the FRACTION scale, matching how the
    backend computes a ratio and how the tolerance model is configured.
    Returns None when the string carries no parseable number (e.g. 'n/a'),
    so the tile is reported unvalidated rather than compared against a
    fabricated zero."""
    if text is None:
        return None
    raw = str(text).strip()
    match = _NUM_RE.search(raw.replace(",", ""))
    if not match:
        return None
    try:
        value = float(match.group(0))
    except ValueError:
        return None
    if "%" in raw or kind == "percent":
        value /= 100.0
    return value


def build_kpi_chart_spec(ir, sheet, table, all_metrics, base, dashboard_name=None):
    """A KPI/text-only sheet as ONE comparable grand-total row: each tile is
    its own measure column, keeping its own number format (so a currency
    tile and a percent tile each get their correct precision tolerance).
    Returns a chart spec, or {**base, "skip_reason": ...}.

    `dashboard_name` is threaded through to HR.capture_sheet_kpis -- this is
    a SECOND render_sheet() call for the same sheet build_chart_spec's own
    Altair capture already made, so it must tag engine.py's evidence
    recorder with the same dashboard the first call used (see
    capture_sheet_kpis' own docstring for the duplicate-entry bug this
    fixes)."""
    import parity
    pairs, reason = HR.capture_sheet_kpis(sheet, dashboard_name=dashboard_name)
    if pairs is None:
        return {**base, "skip_reason": reason}

    seen = {}
    for label, _value in pairs:
        seen[label] = seen.get(label, 0) + 1
    usable = [(l, v) for l, v in pairs if seen[l] == 1]
    if not usable:
        return {**base, "skip_reason": (
            "every KPI tile on this sheet shares a duplicated label, so a tile "
            "cannot be matched to its measure unambiguously")}

    by_label = {}
    for m in sheet.get("measures", []) or []:
        by_label.setdefault(m.get("label") or m.get("caption"), m)

    table_cols = set(engine.table_columns(table) or ())
    grain_field = "KPI tile"
    s_row = {grain_field: base["title"]}
    b_row = {grain_field: base["title"]}
    measures = []
    for label, shown in usable:
        meta = by_label.get(label)
        if not meta:
            continue                       # tile with no declared measure -- skip it
        definition = measure_definition(label, meta.get("fmt"))
        value = parse_displayed(shown, definition["kind"])
        if value is None:
            continue                       # 'n/a' tile -- not comparable
        resolved = parity._resolve_measure_sql(ir, meta["caption"],
                                               meta.get("agg") or "sum",
                                               table_cols, all_metrics)
        if not resolved:
            continue                       # not resolvable against this table
        try:
            df = run_sql(f'SELECT {resolved[0]} AS V FROM {table}')
            backend_value = float(df["V"].iloc[0])
        except Exception:
            continue
        measures.append(definition)
        s_row[label] = value
        b_row[label] = backend_value
    if not measures:
        return {**base, "skip_reason": (
            "no KPI tile on this sheet resolved to a comparable measure on "
            f"{table}")}
    return {**base,
            "grain": [grain_field],
            "measures": measures,
            "backend_sql": f"-- one aggregate per KPI tile against {table}",
            "tableau_rows": None,
            "streamlit_rows": [s_row],
            "backend_rows": [b_row],
            "formulas": [], "interactions": []}


def build_chart_spec(ir, sheet, table, all_metrics, tableau_rows=None,
                     formulas=None, interactions=None, dashboard_name=None):
    """One chart specification for validation_report.generate_report, or a
    dict carrying `skip_reason` when the chart cannot be honestly compared.

    `dashboard_name` is threaded through to HR.capture_sheet_chart so the
    SAME render pass this function already triggers also tags any explicit
    validation_evidence_bridge.REGISTRY evidence engine.py's r_map/r_treemap/
    r_table/_rank_table record with the right dashboard -- no second render
    pass over the sheet."""
    title = sheet.get("title") or sheet.get("name")
    base = {"id": to_phys(sheet.get("name") or title).lower().replace("_", "-"),
            "title": title, "chart_type": sheet.get("kind", "chart")}
    chart_obj, reason = HR.capture_sheet_chart(sheet, dashboard_name=dashboard_name)
    if chart_obj is None:
        # No Altair chart -- but a KPI/text-only sheet still renders real
        # numbers (a dashboard's most-read ones). Validate those as a single
        # grand-total row rather than excluding the sheet entirely.
        kpi_spec = build_kpi_chart_spec(ir, sheet, table, all_metrics, base,
                                        dashboard_name=dashboard_name)
        if not kpi_spec.get("skip_reason"):
            return kpi_spec
        return {**base, "skip_reason": f"{reason}; {kpi_spec['skip_reason']}"}
    grain, measures, rename = resolve_chart_columns(sheet, chart_obj)
    if grain is None:
        return {**base, "skip_reason": rename}
    s_rows = streamlit_rows(chart_obj, rename)
    if s_rows is None:
        return {**base, "skip_reason": "captured chart exposed no dataframe to compare"}
    b_rows, sql_or_reason = backend_rows(ir, sheet, table, grain, measures,
                                         s_rows, all_metrics)
    if b_rows is None:
        return {**base, "skip_reason": f"backend query unavailable -- {sql_or_reason}"}
    if s_rows and not b_rows:
        # The scoped backend query matched NOTHING. That is a limitation of
        # this adapter's key scoping (a type or expression mismatch), never
        # evidence that the migration is wrong -- reporting it as a
        # comparison would mark every displayed value FAIL. Refuse with a
        # stated reason instead of shipping a false failure.
        return {**base, "skip_reason": (
            "the scoped backend query returned no rows for this chart's "
            "displayed keys, so the comparison would be meaningless -- "
            "reported as not validated rather than as a false mismatch")}
    return {**base,
            "grain": grain,
            "measures": measures,
            "backend_sql": sql_or_reason,
            # A file-uploaded workbook has no Tableau export -- passing [] would
            # read as "Tableau returned nothing"; None keeps it honestly BLOCKED.
            "tableau_rows": tableau_rows if tableau_rows is not None else None,
            "streamlit_rows": s_rows,
            "backend_rows": b_rows,
            "formulas": formulas or [],
            "interactions": interactions or []}


# --- formula classification -------------------------------------------------
# parity._formula_match answers a BOOLEAN (shapes agree / don't). The
# validation engine wants a SEMANTIC classification, which carries the
# distinction that boolean check cannot express: a raw Tableau column
# rendered as SUM(col) is only equivalent AT THE CURRENT GRAIN -- correct
# here, not guaranteed if the grain changes. That case is exactly what
# Cortex kept flagging as risky while the boolean check called it "Match".

def classify_formula(twb, app_sql, kind, match, impact):
    if not match:
        return "MISMATCH", impact
    if kind == "column":
        return ("EQUIVALENT_AT_CURRENT_GRAIN",
                "Tableau references the raw column; the app aggregates it "
                "explicitly. Equal at this chart's grain; revisit if the grain changes.")
    if "NULLIF" in (app_sql or "").upper() and "NULLIF" not in (twb or "").upper():
        return ("SEMANTICALLY_EQUIVALENT",
                "Identical aggregation; the app adds a divide-by-zero guard.")
    if (twb or "").replace(" ", "").upper() == (app_sql or "").replace(" ", "").upper():
        return "EXACT", "None"
    return "SEMANTICALLY_EQUIVALENT", (impact or "Same aggregation shape and operators.")


def formulas_for_section(section):
    """Reuse the ALREADY-DECIDED per-metric formula verdicts a section
    carries (parity._formula_match), re-expressed as semantic
    classifications. Never re-judges a match/mismatch."""
    out = []
    for row in section.get("formula_rows", []):
        classification, impact = classify_formula(
            row.get("twb"), row.get("app_sql"),
            "column" if str(row.get("twb", "")).startswith("raw column") else "calc",
            row.get("match"), row.get("impact"))
        out.append({"id": to_phys(row["metric"]), "metric": row["metric"],
                    "tableau": row.get("twb"), "streamlit": row.get("app_sql"),
                    "classification": classification, "impact": impact})
    return out


def interactions_for_section(section):
    """The REAL interaction rows parity.compute_interaction_proof produced,
    in the validation engine's shape. NOT_VALIDATED stays NOT_VALIDATED."""
    out = []
    for row in section.get("interaction_rows", []):
        status = row.get("status", "NOT_VALIDATED")
        out.append({"name": row.get("interaction"),
                    "tableau": row.get("tableau"),
                    "streamlit": row.get("streamlit"),
                    "proof": row.get("proof"),
                    "status": "REVIEW" if status == "WARNING" else status})
    return out


def build_validation_spec(ir, sections, book_name, table_for_dashboard,
                          screenshots=None, tableau_rows_for=None,
                          run_id=None, environment="UAT"):
    """The full spec for validation_report.generate_report.

    `sections`      -- parity.build_cortex_dashboard_validation_report's
                       already-computed sections (formula + interaction
                       verdicts are REUSED, never recomputed).
    `screenshots`   -- {dashboard title: png path} (real captures only).
    `tableau_rows_for` -- optional callable (dashboard, sheet) -> rows or
                       None; supply Tableau's REST export when connected.
    """
    import cortex_semantic as CS
    all_metrics, _ = CS.build_metrics(ir)
    screenshots = screenshots or {}
    by_title = {s["title"]: s for s in sections}
    dashboards = []
    for dash in ir.get("dashboards", []):
        title = dash.get("title") or dash["name"]
        section = by_title.get(title, {})
        table = table_for_dashboard(dash)
        charts = []
        for sheet in dash.get("sheets", []):
            t_rows = tableau_rows_for(dash, sheet) if tableau_rows_for else None
            charts.append(build_chart_spec(ir, sheet, table, all_metrics,
                                           tableau_rows=t_rows,
                                           dashboard_name=title))
        shot = screenshots.get(title)
        dashboards.append({
            "id": to_phys(dash["name"]).lower().replace("_", "-"),
            "name": title,
            "visual": {"streamlit_screenshot": shot,
                       # No Tableau-side capture exists for a file-uploaded
                       # workbook; left absent so the engine reports the
                       # visual gate BLOCKED instead of implying a match.
                       "tableau_screenshot": None,
                       "similarity": None,
                       "checks": []},
            # Skipped charts stay IN the chart list (the vendored engine
            # reports them BLOCKED with their stated reason) -- dropping them
            # would make an unvalidated chart indistinguishable from a
            # validated one.
            "charts": charts,
            "formulas_source": formulas_for_section(section),
            "interactions_source": interactions_for_section(section),
        })
    # Attach dashboard-level formulas/interactions onto the first comparable
    # chart so the engine's dashboard rollup sees them (it aggregates from
    # charts). A dashboard with no comparable chart keeps them visible via
    # its own skipped-chart reporting.
    for d in dashboards:
        comparable = [c for c in d["charts"] if not c.get("skip_reason")]
        if comparable:
            comparable[0]["formulas"] = d.pop("formulas_source")
            comparable[0]["interactions"] = d.pop("interactions_source")
        else:
            d.pop("formulas_source", None)
            d.pop("interactions_source", None)
    return {"workbook": book_name,
            "run_id": run_id or f"VAL-{_dt.datetime.now().strftime('%Y%m%d-%H%M')}",
            "environment": environment,
            "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
            "dashboards": dashboards}
