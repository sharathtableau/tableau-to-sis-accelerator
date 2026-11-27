"""
report_runtime.py  --  render a report-ir/1 report as a Streamlit app.

Shared runtime for the Power BI path (any source that emits report-ir/1).
Generated apps embed the IR and call run(IR). Same code runs in Streamlit in
Snowflake and locally (report_data.py picks the backend).

What it renders (MVP): page navigation, KPI cards, tables (with a total row
computed by the model, so non-additive measures total correctly), matrices,
bar / column / stacked / 100% stacked, line, area, scatter, combo, text, and
slicers (list, date range, numeric range). Everything else is a visible
placeholder that says why; held measures show as "Pending verification".

Transparency rule: a visual that cannot be compiled never disappears; it
shows the reason, and the same reason is in the conversion report page.
"""

import math

import altair as alt
import pandas as pd
import streamlit as st

import report_data
from report_sql import CompileError, Compiler

MAX_CATEGORIES = 50
MAX_TABLE_ROWS = 500
PALETTE = ["#118DFF", "#12239E", "#E66C37", "#6B007B", "#E044A7", "#744EC2",
           "#D9B300", "#D64550", "#197278", "#1AAB40"]          # Power BI default theme


# --------------------------------------------------------------------------- #
# formatting
# --------------------------------------------------------------------------- #

def fmt_value(v, fmt):
    try:
        if v is None or (isinstance(v, float) and math.isnan(v)) or pd.isna(v):
            return "(Blank)"
    except (TypeError, ValueError):
        pass
    if not fmt or fmt == "date":
        if hasattr(v, "strftime"):
            return v.strftime("%Y-%m-%d")
        if isinstance(v, float):
            return f"{v:,.2f}"
        return str(v)
    try:
        x = float(v)
    except (TypeError, ValueError):
        return str(v)
    kind, dec = fmt.rstrip("0123456789"), int(fmt[len(fmt.rstrip("0123456789")):] or 0)
    if kind == "pct":
        return f"{x:.{dec}%}"
    if kind == "cur":
        s = f"${abs(x):,.{dec}f}"
        return f"({s})" if x < 0 else s
    if kind == "num":
        return f"{x:,.{dec}f}"
    if kind == "plain":
        return f"{x:.{dec}f}"
    return str(v)


def axis_format(fmt):
    if not fmt:
        return None
    kind = fmt.rstrip("0123456789")
    return {"pct": ".0%", "cur": "$,.0f", "num": ",.0f", "plain": ",.1f"}.get(kind)


def _measure_fmt(ir, f):
    if f["kind"] == "measure":
        return ir["model"]["measures"][f["name"]].get("format")
    col = ir["model"]["tables"][f["table"]]["columns"][f["name"]]
    return col.get("format")


def _is_ready(ir, f):
    return f["kind"] != "measure" or ir["model"]["measures"][f["name"]]["status"] == "ready"


# --------------------------------------------------------------------------- #
# layout
# --------------------------------------------------------------------------- #

def layout_rows(visuals):
    """Group visuals into rows by vertical overlap, left to right."""
    rows = []
    for v in sorted(visuals, key=lambda x: (x["position"]["y"], x["position"]["x"])):
        p = v["position"]
        top, bottom = p["y"], p["y"] + max(p["height"], 1)
        for r in rows:
            if top < r["bottom"] - 0.25 * max(p["height"], 1):
                r["items"].append(v)
                r["bottom"] = max(r["bottom"], bottom)
                break
        else:
            rows.append({"bottom": bottom, "items": [v]})
    return [sorted(r["items"], key=lambda x: x["position"]["x"]) for r in rows]


# --------------------------------------------------------------------------- #
# slicers -> filters
# --------------------------------------------------------------------------- #

def _slicer_key(page, v):
    return f"slicer::{page['id']}::{v['id']}"


def _slicer_field(v):
    fs = v["fields"].get("values") or []
    return fs[0] if fs else None


def slicer_filters(ir, page):
    out = []
    for v in page["visuals"]:
        if v["kind"] != "slicer":
            continue
        fld = _slicer_field(v)
        if not fld:
            continue
        val = st.session_state.get(_slicer_key(page, v))
        if not val:
            continue
        dt = ir["model"]["tables"][fld["table"]]["columns"][fld["name"]].get("data_type")
        if dt in ("datetime", "date", "number"):
            if isinstance(val, (list, tuple)) and len(val) == 2:
                out.append({"field": fld, "op": "between", "values": list(val), "scope": "slicer"})
        else:
            out.append({"field": fld, "op": "in", "values": list(val), "scope": "slicer"})
    return out


def render_slicer(ir, C, page, v, fixed):
    fld = _slicer_field(v)
    if not fld:
        st.caption("Slicer without a field")
        return
    label = v.get("title") or fld["label"]
    key = _slicer_key(page, v)
    dt = ir["model"]["tables"][fld["table"]]["columns"][fld["name"]].get("data_type")
    try:
        if dt in ("datetime", "date"):
            r = report_data.run_sql(C.range_values(fld, fixed))
            lo, hi = pd.to_datetime(r.iloc[0, 0]), pd.to_datetime(r.iloc[0, 1])
            if pd.isna(lo):
                st.caption(f"{label}: no data")
                return
            st.date_input(label, value=(lo.date(), hi.date()), min_value=lo.date(),
                          max_value=hi.date(), key=key)
        elif dt == "number":
            r = report_data.run_sql(C.range_values(fld, fixed))
            lo, hi = float(r.iloc[0, 0]), float(r.iloc[0, 1])
            st.slider(label, min_value=lo, max_value=hi, value=(lo, hi), key=key)
        else:
            vals = report_data.run_sql(C.distinct_values(fld, fixed))["V"].tolist()
            opts = [v2 for v2 in vals if v2 is not None and not (isinstance(v2, float) and math.isnan(v2))]
            st.multiselect(label, opts, key=key, placeholder="All")
    except CompileError as e:
        st.info(f"{label}: slicer not converted ({e})")


# --------------------------------------------------------------------------- #
# visuals
# --------------------------------------------------------------------------- #

def _order_for(v, dims, measures, default):
    out = []
    for s in v.get("sort") or []:
        f = s["field"]
        for i, d in enumerate(dims):
            if (d["table"], d["name"]) == (f["table"], f["name"]):
                out.append((f"D{i}", s["direction"]))
        for i, m in enumerate(measures):
            if (m.get("table"), m["name"]) == (f.get("table"), f["name"]):
                out.append((f"M{i}", s["direction"]))
    return out or default


def _held_note(ir, fields):
    held = [f["label"] for f in fields if not _is_ready(ir, f)]
    if held:
        st.caption("Pending verification (not deployed): " + ", ".join(held))
    return [f for f in fields if _is_ready(ir, f)]


def _query(C, dims, measures, filters, order=None, limit=None):
    sql, meta = C.visual_query(dims, measures, filters, order=order, limit=limit)
    return report_data.run_sql(sql), meta


def render_kpi(ir, C, v, filters):
    fields = v["fields"].get("values") or []
    cols = st.columns(max(len(fields), 1))
    for col, f in zip(cols, fields):
        with col:
            if not _is_ready(ir, f):
                st.metric(f["label"], "Pending",
                          help="This measure is held until its result is verified.")
                continue
            df, _ = _query(C, [], [f], filters)
            val = df.iloc[0]["M0"] if len(df) else None
            st.metric(f["label"], fmt_value(val, _measure_fmt(ir, f)))


def _dim_type(ir, d):
    dt = ir["model"]["tables"][d["table"]]["columns"][d["name"]].get("data_type")
    if d.get("level"):
        return "O"
    return "T" if dt in ("datetime", "date") else "N"


def render_cartesian(ir, C, v, filters):
    kind = v["kind"]
    cat = (v["fields"].get("category") or [])[:1]
    series = (v["fields"].get("series") or [])[:1]
    meas = _held_note(ir, v["fields"].get("values") or [])
    meas2 = _held_note(ir, v["fields"].get("values2") or []) if kind == "combo" else []
    if not cat or not (meas or meas2):
        st.info("Nothing to draw: the axis or every value is missing or pending.")
        return
    dims = cat + series
    allm = meas + meas2
    timeish = kind in ("line", "area", "combo")
    default = [("S0" if _has_sort(ir, cat[0]) else "D0", "asc")] if timeish else [("M0", "desc")]
    order = _order_for(v, dims, allm, default)
    df, meta = _query(C, dims, allm, filters, order=order, limit=5000)
    if df.empty:
        st.caption("(No data)")
        return
    dname = "D0"
    sname = "D1" if series else None
    cat_order = list(dict.fromkeys(df[dname].tolist()))
    shown = cat_order
    if not timeish and len(cat_order) > MAX_CATEGORIES:
        shown = cat_order[:MAX_CATEGORIES]
        df = df[df[dname].isin(shown)]
        st.caption(f"Showing the first {MAX_CATEGORIES} of {len(cat_order)} categories.")
    xtype = _dim_type(ir, cat[0])
    x_enc_t = "T" if xtype == "T" else "O"
    cat_title = cat[0]["label"]
    m_labels = {f"M{i}": f["label"] for i, f in enumerate(allm)}
    long = df.melt(id_vars=[c for c in df.columns if not c.startswith("M")],
                   value_vars=[f"M{i}" for i in range(len(meas))],
                   var_name="_m", value_name="_v") if meas else None
    if long is not None:
        long["Measure"] = long["_m"].map(m_labels)
        color_field = sname if sname else ("Measure" if len(meas) > 1 else None)
        if color_field and color_field == sname:
            long[sname] = long[sname].astype(str)
    fmt1 = axis_format(_measure_fmt(ir, meas[0])) if meas else None

    def x_axis():
        if x_enc_t == "T":
            return alt.X(f"{dname}:T", title=cat_title)
        return alt.X(f"{dname}:N", title=cat_title,
                     sort=[str(s) for s in (shown if not timeish else cat_order)],
                     axis=alt.Axis(labelAngle=0) if len(shown) <= 12 else alt.Axis())

    series_order = None
    if sname:
        raw = df[sname].dropna().unique().tolist()
        try:
            raw = sorted(raw)
        except TypeError:
            raw = sorted(raw, key=str)
        series_order = [str(x) for x in raw]

    def color(field):
        dom = series_order if field == sname else None
        return alt.Color(f"{field}:N", title=(series[0]["label"] if field == sname else ""),
                         scale=alt.Scale(range=PALETTE, domain=dom) if dom else
                         alt.Scale(range=PALETTE), sort=dom)

    charts = []
    if meas:
        if kind in ("line",):
            mark = alt.Chart(long).mark_line(point=len(cat_order) < 40)
        elif kind == "area":
            mark = alt.Chart(long).mark_area(opacity=0.85)
        else:
            mark = alt.Chart(long).mark_bar()
        stack = "normalize" if kind == "stacked100" else (True if kind in ("area",) or sname else None)
        horiz = kind in ("bar",) or (kind == "stacked100" and v["source_type"].endswith("BarChart"))
        val_axis = alt.Axis(format=".0%" if kind == "stacked100" else fmt1)
        if horiz:
            enc = {"y": alt.Y(f"{dname}:N", title=cat_title, sort=[str(s) for s in shown]),
                   "x": alt.X("_v:Q", title=None, stack=stack, axis=val_axis)}
        else:
            enc = {"x": x_axis(), "y": alt.Y("_v:Q", title=None, stack=stack, axis=val_axis)}
        cf = sname if sname else ("Measure" if len(meas) > 1 else None)
        if cf:
            enc["color"] = color(cf)
            if not sname and kind in ("bar", "column", "combo"):
                enc["xOffset" if not horiz else "yOffset"] = "Measure:N"
        else:
            mark = mark.encode(color=alt.value(PALETTE[0]))
        enc["tooltip"] = [alt.Tooltip(f"{dname}:{'T' if x_enc_t == 'T' else 'N'}", title=cat_title)] + \
            ([alt.Tooltip(f"{sname}:N", title=series[0]["label"])] if sname else []) + \
            [alt.Tooltip("Measure:N"), alt.Tooltip("_v:Q", title="Value", format=fmt1 or ",.2f")]
        if not horiz and x_enc_t != "T":
            long[dname] = long[dname].astype(str)
        if horiz:
            long[dname] = long[dname].astype(str)
        charts.append(mark.encode(**enc))
    if meas2:
        l2 = df.melt(id_vars=[c for c in df.columns if not c.startswith("M")],
                     value_vars=[f"M{i}" for i in range(len(meas), len(allm))],
                     var_name="_m", value_name="_v")
        l2["Measure"] = l2["_m"].map(m_labels)
        if x_enc_t != "T":
            l2[dname] = l2[dname].astype(str)
        f2 = axis_format(_measure_fmt(ir, meas2[0]))
        charts.append(alt.Chart(l2).mark_line(point=True, color=PALETTE[2]).encode(
            x=x_axis(), y=alt.Y("_v:Q", title=meas2[0]["label"], axis=alt.Axis(format=f2)),
            tooltip=["Measure:N", alt.Tooltip("_v:Q", format=f2 or ",.2f")]))
    chart = charts[0] if len(charts) == 1 else alt.layer(*charts).resolve_scale(y="independent")
    height = max(320, 18 * len(shown)) if kind in ("bar",) else 320
    st.altair_chart(chart.properties(height=height), width="stretch")


def _has_sort(ir, d):
    return bool(ir["model"]["tables"][d["table"]]["columns"][d["name"]].get("sort_by")) \
        and not d.get("level")


def render_scatter(ir, C, v, filters):
    det = (v["fields"].get("category") or [])[:1]
    xs = _held_note(ir, v["fields"].get("x") or [])
    ys = _held_note(ir, v["fields"].get("values") or [])
    if not det or not xs or not ys:
        st.info("Scatter needs a detail field and X and Y values.")
        return
    df, _ = _query(C, det, [xs[0], ys[0]], filters, limit=5000)
    df["D0"] = df["D0"].astype(str)
    fx, fy = axis_format(_measure_fmt(ir, xs[0])), axis_format(_measure_fmt(ir, ys[0]))
    chart = alt.Chart(df).mark_circle(size=50, color=PALETTE[0]).encode(
        x=alt.X("M0:Q", title=xs[0]["label"], axis=alt.Axis(format=fx)),
        y=alt.Y("M1:Q", title=ys[0]["label"], axis=alt.Axis(format=fy)),
        tooltip=[alt.Tooltip("D0:N", title=det[0]["label"]),
                 alt.Tooltip("M0:Q", title=xs[0]["label"], format=fx or ",.2f"),
                 alt.Tooltip("M1:Q", title=ys[0]["label"], format=fy or ",.2f")])
    st.altair_chart(chart.properties(height=360), width="stretch")


def render_table(ir, C, v, filters):
    fields = v["fields"].get("values") or []
    dims = [f for f in fields if f["kind"] in ("column", "date_level")]
    meas = _held_note(ir, [f for f in fields if f["kind"] in ("measure", "aggregation")])
    if not dims and not meas:
        st.info("Table has no convertible fields.")
        return
    order = _order_for(v, dims, meas, [("D0", "asc")] if dims else None)
    df, meta = _query(C, dims, meas, filters, order=order, limit=MAX_TABLE_ROWS + 1)
    if len(df) > MAX_TABLE_ROWS:
        st.caption(f"Showing the first {MAX_TABLE_ROWS} rows.")
        df = df.head(MAX_TABLE_ROWS)
    out = pd.DataFrame()
    for i, d in enumerate(dims):
        out[d["label"]] = df[f"D{i}"].map(lambda x, d=d: fmt_value(x, _measure_fmt(ir, d)))
    for i, m in enumerate(meas):
        out[m["label"]] = df[f"M{i}"].map(lambda x, m=m: fmt_value(x, _measure_fmt(ir, m)))
    if meas and dims:
        tot, _ = _query(C, [], meas, filters)
        row = {d["label"]: "" for d in dims}
        row[dims[0]["label"]] = "Total"
        for i, m in enumerate(meas):
            row[m["label"]] = fmt_value(tot.iloc[0][f"M{i}"], _measure_fmt(ir, m))
        out = pd.concat([out, pd.DataFrame([row])], ignore_index=True)
    st.dataframe(out, width="stretch", hide_index=True)


def render_matrix(ir, C, v, filters):
    rows = v["fields"].get("rows") or []
    cols = v["fields"].get("columns") or []
    meas = _held_note(ir, v["fields"].get("values") or [])
    if not rows or not meas:
        st.info("Matrix needs row fields and values.")
        return
    dims = rows + cols
    df, _ = _query(C, dims, meas, filters, limit=20000)
    rnames = [r["label"] for r in rows]
    for i, d in enumerate(dims):
        df[d["label"]] = df[f"D{i}"].astype(str)
    if cols:
        cn = [c["label"] for c in cols]
        piv = df.pivot_table(index=rnames, columns=cn, values="M0", aggfunc="first")
        fmt = _measure_fmt(ir, meas[0])
        piv = piv.apply(lambda s: s.map(lambda x: fmt_value(x, fmt)))
        st.dataframe(piv, width="stretch")
    else:
        out = df[rnames].copy()
        for i, m in enumerate(meas):
            out[m["label"]] = df[f"M{i}"].map(lambda x, m=m: fmt_value(x, _measure_fmt(ir, m)))
        st.dataframe(out, width="stretch", hide_index=True)


RENDERERS = {"kpi": render_kpi, "bar": render_cartesian, "column": render_cartesian,
             "line": render_cartesian, "area": render_cartesian,
             "stacked100": render_cartesian, "combo": render_cartesian,
             "scatter": render_scatter, "table": render_table, "matrix": render_matrix}


def render_visual(ir, C, page, v, filters, fixed):
    title = v.get("title")
    try:
        box = st.container(border=True)
    except TypeError:
        box = st.container()
    with box:
        if v["kind"] == "slicer":
            render_slicer(ir, C, page, v, fixed)
            return
        if title and v["kind"] != "kpi":
            st.markdown(f"**{title}**")
        if v["kind"] == "text":
            st.markdown(v.get("text") or "")
            return
        fn = RENDERERS.get(v["kind"])
        if fn is None:
            st.info(f"Not converted: {v['source_type']}. " + " ".join(v.get("notes") or []))
            return
        try:
            fn(ir, C, v, filters + v.get("filters", []))
        except CompileError as e:
            st.info(f"Not converted: {e}")
        if v["support"] != "auto" and v.get("notes"):
            st.caption("Conversion note: " + "; ".join(v["notes"]))


# --------------------------------------------------------------------------- #
# pages
# --------------------------------------------------------------------------- #

def render_page(ir, C, page):
    st.header(page["name"])
    fixed = list(ir.get("report_filters") or []) + list(page.get("filters") or [])
    filters = fixed + slicer_filters(ir, page)
    for row in layout_rows(page["visuals"]):
        widths = [max(v["position"]["width"], 1) for v in row]
        cols = st.columns(widths) if len(row) > 1 else [st.container()]
        for col, v in zip(cols, row):
            with col:
                render_visual(ir, C, page, v, filters, fixed)


def render_conversion_report(ir):
    import conversion_report
    st.header("Conversion report")
    summary = conversion_report.summary(ir)
    cols = st.columns(4)
    for c, lvl in zip(cols, ("auto", "partial", "manual", "unsupported")):
        c.metric(conversion_report.LABELS[lvl], summary.get(lvl, 0))
    df = pd.DataFrame(ir["features"])
    if not df.empty:
        df["support"] = df["support"].map(conversion_report.LABELS)
        st.dataframe(df[["area", "item", "support", "detail", "follow_up"]],
                     width="stretch", hide_index=True)
    st.markdown(conversion_report.RUNTIME_SECURITY_NOTE)


def run(ir):
    try:
        st.set_page_config(page_title=ir["source"].get("name") or "Report", layout="wide")
    except Exception:
        pass
    C = Compiler(ir)
    pages = [p for p in ir["pages"] if not p.get("hidden")]
    names = [p["name"] for p in pages] + ["Conversion report"]
    st.sidebar.title(ir["source"].get("name") or "Report")
    choice = st.sidebar.radio("Pages", names, key="__report_page")
    if choice == "Conversion report":
        render_conversion_report(ir)
        return
    render_page(ir, C, next(p for p in pages if p["name"] == choice))
