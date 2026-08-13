"""
tableau_parser.py  --  Stage 1: parse a Tableau workbook into render-ready specs.

Workbook-agnostic. For EVERY dashboard and EVERY worksheet it:
  * reads the cols/rows shelves and all encodings (color/size/text/detail),
  * classifies each field as dimension / measure / date (role + aggregation),
  * INFERS the chart kind the way Tableau's "Automatic" mark would
    (kpi / bar / mbar / line / area / scatter / heatmap / map / table),
  * captures number formats, sort order, color scales and titles,
  * emits a flat spec the code generator renders directly.

Usage:
  python tableau_parser.py Superstore.twb -o workbook_ir.json          # all dashboards
  python tableau_parser.py Superstore.twb --dashboard Overview -o ir.json
"""

import argparse
import json
import re
import io
import zipfile
import xml.etree.ElementTree as ET

FIELD_RE = re.compile(r"\[([^\]]+)\]\.\[([^\]]+)\]")
TOKEN_RE = re.compile(r"\[[^\]]+\](?:\.\[[^\]]+\])+")   # full dotted shelf token
DATE_PARTS = {"yr", "qr", "mn", "tmn", "tyr", "tqr", "twk", "wk", "tdy", "dy",
              "mdy", "md", "wd", "hr"}
# coarse -> fine granularity rank, used to pick the finest date field on a shelf
DATEPART_RANK = {"yr": 0, "tyr": 0, "qr": 1, "tqr": 1, "mn": 2, "tmn": 2,
                 "wk": 3, "twk": 3, "tdy": 4, "dy": 4, "mdy": 4, "md": 4,
                 "wd": 4, "hr": 5}
AGGS = {"sum", "avg", "min", "max", "cnt", "ctd", "med", "stdev", "var"}
# 'io' = set membership (In/Out) shelf derivation
PREFIXES = DATE_PARTS | AGGS | {"none", "usr", "fVal", "pcto", "pcdf", "rank", "io"}
INTERNAL_OBJ = "__tableau_internal_object_id__"


def load_twb_xml(path):
    if path.lower().endswith(".twbx"):
        with zipfile.ZipFile(path) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".twb"))
            return ET.parse(io.BytesIO(z.read(name))).getroot()
    return ET.parse(path).getroot()


def column_meta(root):
    """internal name -> {caption, role, datatype, fmt}.

    Tableau 2024.x wraps some column declarations in feature-flag tags like
    <_.fcp.ObjectModelTableType.true...column datatype='table' ...> (the
    count-of-records objects live there), so match any tag ENDING in
    'column', not just literal <column>."""
    meta, capByName = {}, {}
    cols = [el for el in root.iter()
            if isinstance(el.tag, str) and el.tag.endswith("column")]
    for col in cols:
        nm = col.get("name")
        if not nm:
            continue
        cap = col.get("caption") or nm.strip("[]")
        meta[nm] = {"caption": cap, "role": col.get("role"),
                    "datatype": col.get("datatype"),
                    "fmt": col.get("default-format")}
        capByName[nm] = cap
    return meta, capByName


def fmt_token(fmt, datatype=None):
    """Map a Tableau default-format string to a renderer format token."""
    f = fmt or ""
    fl = f.lstrip()
    head = fl[:1]
    if head in ("p", "P"):
        return "pct"
    if head in ("c", "C") or "$" in f:
        return "cur2" if ("0.00" in f or ".00" in f) else "cur0"
    if "%" in f:
        return "pct"
    if head == "n" or datatype == "integer":
        return "num0"
    return "num2"


def parse_field(token, meta):
    """Shelf token -> {caption, agg, role, is_date, datepart, internal, ds,
    pct_total, count_records}. Handles 2-part ([ds].[fld]) AND 3-part tokens
    ([ds].[__tableau_internal_object_id__].[cnt:Orders_<GUID>:qk])."""
    groups = re.findall(r"\[([^\]]+)\]", token or "")
    groups = [g for g in groups if g != INTERNAL_OBJ]
    if len(groups) >= 2:
        ds, inner = groups[0], groups[-1]
    else:
        ds, inner = None, (groups[0] if groups else (token or "").strip("[]"))
    parts = inner.split(":")
    # trailing pill-type suffix: qk = continuous, ok/nk = DISCRETE (dimension)
    suffix = parts.pop() if (len(parts) >= 2 and parts[-1] in ("qk", "ok", "nk")) else None
    # strip ALL stacked leading prefixes (e.g. rank:sum:Sales, pcto:sum:Sales)
    consumed = []
    while len(parts) >= 2 and parts[0] in PREFIXES:
        consumed.append(parts.pop(0))
    base = parts[0]
    # aggregation = the last consumed prefix that is an agg/datepart/none/usr
    agg = None
    for pfx in consumed:
        if pfx in AGGS or pfx in DATE_PARTS or pfx in ("usr", "none", "fVal"):
            agg = pfx
    internal = "[" + base + "]"
    info = meta.get(internal, {})
    if not info:                        # count-of-records object (datatype="table")
        info = meta.get("[%s].[%s]" % (INTERNAL_OBJ, base), {})
    count_records = info.get("datatype") == "table"
    caption = info.get("caption", base)
    datatype = info.get("datatype")
    role = info.get("role")
    is_date = (datatype in ("date", "datetime")) or (agg in DATE_PARTS)
    # classify role
    if count_records:
        kind, agg = "measure", (agg or "cnt")
    elif agg in AGGS or agg in ("fVal",):
        kind = "measure"
    elif agg in DATE_PARTS or agg == "none":
        kind = "dimension"
    elif role == "measure":
        kind = "measure"
    else:
        kind = "dimension"
    if base.startswith("Calculation_") and role == "measure":
        kind = "measure"
    # a DISCRETE pill (ok/nk) acts as a dimension on the shelf, whatever the
    # underlying field role is (e.g. a calc used as an ordinal bucket).
    if suffix in ("ok", "nk") and not is_date:
        kind = "dimension"
    return {"caption": caption, "agg": agg, "kind": kind,
            "is_date": is_date, "datepart": agg if agg in DATE_PARTS else None,
            "internal": base, "ds": ds if (ds or "").startswith("federated.") else None,
            "discrete": suffix in ("ok", "nk"),
            "pct_total": ("pcto" in consumed) or ("pcdf" in consumed),
            "count_records": count_records}


def _measure_entry(fld, meta):
    """Measure dict carrying caption, aggregation, format token and display label."""
    cap = fld["caption"]
    agg = fld["agg"]
    info = meta.get("[" + fld["internal"] + "]", {})
    fmt = fmt_token(info.get("fmt"), info.get("datatype"))
    if agg in ("ctd", "cnt"):
        fmt = "num0"
        # Tableau labels COUNT(<Entity> Name) as "Count of <Entity>s".
        base_lbl = cap[:-5] + "s" if cap.endswith(" Name") else cap
        label = "Count of " + base_lbl
    else:
        label = cap
    m = {"caption": cap, "agg": agg, "fmt": fmt, "label": label}
    if fld.get("count_records"):
        m["count_records"] = True
    if fld.get("pct_total"):
        m["pct_total"] = True
        m["fmt"] = "pct"
    return m


def _m(fld, meta=None):
    """Positional measure ref for a spec axis (carries count-of-records flag
    and the workbook's number format, so axes render % as %, $ as $)."""
    d = {"caption": fld["caption"], "agg": fld["agg"]}
    if fld.get("count_records"):
        d["count_records"] = True
    if meta is not None:
        info = meta.get("[" + fld.get("internal", "") + "]", {})
        if info.get("fmt"):
            d["fmt"] = fmt_token(info.get("fmt"), info.get("datatype"))
    return d


def shelf_fields(text, meta):
    out = []
    for tok in TOKEN_RE.findall(text or ""):
        if ":Measure Names" in tok or "Multiple Values" in tok or "Measure Values" in tok:
            continue
        last = re.findall(r"\[([^\]]+)\]", tok)[-1]
        if "Latitude" in last or "Longitude" in last:
            out.append({"caption": last, "kind": "geo_gen", "agg": None,
                        "is_date": False, "datepart": None, "internal": last})
            continue
        out.append(parse_field(tok, meta))
    return out


def encodings(ws, meta):
    enc = {}
    for e in ws.findall(".//pane/encodings/*"):
        col = e.get("column")
        if not col or "Measure Names" in col:
            continue
        enc.setdefault(e.tag, parse_field(col, meta))
    return enc


def measure_names_from_filter(ws, meta):
    """Exact set+order of measures shown via the Measure Names shelf filter."""
    out = []
    for f in ws.findall(".//filter"):
        col = f.get("column") or ""
        if ":Measure Names" not in col:
            continue
        for gf in f.findall(".//groupfilter"):
            if gf.get("function") != "member":
                continue
            mm = FIELD_RE.search(gf.get("member") or "")
            if not mm:
                continue
            tok = "[" + mm.group(1) + "].[" + mm.group(2) + "]"
            m = _measure_entry(parse_field(tok, meta), meta)
            if not any(o["caption"] == m["caption"] and o["agg"] == m["agg"] for o in out):
                out.append(m)
    return out


def measure_names_list(ws, meta):
    """Measures shown via Measure Names/Values (KPI strips, text tables, bar panels).
    Prefer the explicit Measure Names filter membership; fall back to a scan."""
    flt = measure_names_from_filter(ws, meta)
    if flt:
        return flt
    out = []
    for ci in ws.findall(".//datasource-dependencies//column-instance"):
        deriv = (ci.get("derivation") or "").lower()
        if deriv not in ("sum", "avg", "user", "countd"):
            continue
        nm = (ci.get("name") or "").strip("[]")
        m = _measure_entry(parse_field("[d].[" + nm + "]", meta), meta)
        if not any(o["caption"] == m["caption"] for o in out):
            out.append(m)
    return out


def color_scale(ws, meta, active_caption):
    """Color encoding scale for the active color field: palette + domain + custom colors."""
    best = None
    for enc in ws.findall(".//style-rule[@element='mark']/encoding[@attr='color']"):
        fld = parse_field(enc.get("field") or "", meta)
        sc = {}
        if enc.get("palette"):
            sc["palette"] = enc.get("palette")
        if enc.get("type"):
            sc["type"] = enc.get("type")
        for k in ("min", "max"):
            try:
                if enc.get(k) is not None:
                    sc[k] = float(enc.get(k))
            except ValueError:
                pass
        cp = enc.find("color-palette")
        if cp is not None:
            cols = [c.text for c in cp.findall("color") if c.text]
            if cols:
                sc["colors"] = cols
        if active_caption and fld["caption"] == active_caption:
            return sc
        if best is None:
            best = sc
    return best


def sheet_sort(ws, meta):
    """Row/bar sort = the sheet's VIEW ORDER. Two XML forms, both real:

    <computed-sort using='[sum:Sales:qk]' column='[none:Sub-Category:nk]'>
    <shelf-sort-v2 measure-to-sort-by='[sum:Sales:qk]'
                   dimension-to-sort='[none:Sub-Category:nk]' direction='DESC'>

    shelf-sort-v2 is what Tableau 2020+ writes for "sort by Sales descending"
    on a shelf. Missing it meant view order was UNKNOWN, which is why an
    INDEX()<=5 filter could not be pushed -- the sheet rendered all 17 rows
    instead of Tableau's top 5 (Fil Test Sheet 1)."""
    cs = ws.find(".//computed-sort")
    if cs is not None and cs.get("using"):
        using = parse_field(cs.get("using"), meta)
        on = parse_field(cs.get("column") or "", meta)
        return {"field": using["caption"], "agg": using["agg"] or "sum",
                "dir": (cs.get("direction") or "DESC").lower(), "on": on["caption"]}
    sv = ws.find(".//shelf-sorts/shelf-sort-v2")
    if sv is not None and sv.get("measure-to-sort-by"):
        using = parse_field(sv.get("measure-to-sort-by"), meta)
        on = parse_field(sv.get("dimension-to-sort") or "", meta)
        return {"field": using["caption"], "agg": using["agg"] or "sum",
                "dir": (sv.get("direction") or "DESC").lower(), "on": on["caption"],
                "shelf": sv.get("shelf")}
    return None


def sheet_filters(ws, meta):
    out = []
    for f in ws.findall(".//filter"):
        col = f.get("column") or ""
        if "Action (" in col or ":Measure Names" in col:
            continue
        fi = parse_field(col, meta)
        kind = "date" if fi["is_date"] else ("range" if fi["kind"] == "measure" else "categorical")
        if not any(o["caption"] == fi["caption"] for o in out):
            out.append({"caption": fi["caption"], "kind": kind})
    return out


def _shelf_text(ws, which):
    el = ws.find(".//table/" + which)
    return (el.text if el is not None else "") or ""


def pane_marks(ws, meta):
    """Per-measure mark classes for dual-axis/combo sheets: each <pane> names
    its measure via y-axis-name/x-axis-name and carries its own mark."""
    out = {}
    for p in ws.findall(".//panes/pane"):
        ax = p.get("y-axis-name") or p.get("x-axis-name")
        m = p.find("mark")
        if ax and m is not None and (m.get("class") or "Automatic") != "Automatic":
            f = parse_field(ax, meta)
            out[(f["caption"], f["agg"])] = m.get("class").lower()
    return out


def _auto_date_kind(mark):
    """Mark for a date + measure view when the workbook does NOT pin one.

    Tableau's Automatic mark over a date + measure is a LINE -- whether the
    date pill is discrete (blue, :ok) or continuous (green, :qk). Discreteness
    controls the AXIS (per-period headers vs a continuous scale), never the
    mark type. Bars require the mark to be pinned to Bar.

    This function exists because the rule used to read
        mark == "Automatic" and finest["discrete"]  ->  bars
    which was over-generalised from workbooks whose sheets happened to pin
    mark='Bar'. It rendered Fil Test's Sheet 2/Sheet 4 as bars where Tableau
    draws lines (user screenshot, 2026-07-15). Corpus sweep: every dtbar sheet
    that was verified against a Tableau screenshot pins mark='Bar', so honoring
    the mark class keeps all of them and fixes only the Automatic ones.
    """
    return "line"


def _infer_core(ws, meta):
    """Build the render-ready spec (chart kind + encodings) for one worksheet."""
    name = ws.get("name")
    mark_el = ws.find(".//pane/mark")
    mark = mark_el.get("class") if mark_el is not None else "Automatic"
    cols_text = _shelf_text(ws, "cols")
    rows_text = _shelf_text(ws, "rows")
    cols = shelf_fields(cols_text, meta)
    rows = shelf_fields(rows_text, meta)
    enc = encodings(ws, meta)
    mnames = measure_names_list(ws, meta)
    filters = sheet_filters(ws, meta)

    all_f = cols + rows
    dims = [f for f in all_f if f["kind"] == "dimension" and not f["is_date"]]
    dates = [f for f in all_f if f["is_date"]]
    meas = [f for f in all_f if f["kind"] == "measure"]
    is_geo = mark == "Multipolygon" or any(f["kind"] == "geo_gen" for f in all_f)

    spec = {"name": name, "mark": mark, "filters": filters}

    # ---- MAP ----
    if is_geo:
        geo = None
        for f in (enc.get("detail"), enc.get("lod"), *dims):
            if f and f.get("kind") in ("dimension",):
                geo = f["caption"]; break
        cm = enc.get("color")
        spec.update({"kind": "map", "geo": geo or "State/Province",
                     "color_measure": ({"caption": cm["caption"], "agg": cm["agg"] or "usr"}
                                       if cm else {"caption": "Profit Ratio", "agg": "usr"})})
        return spec

    col_meas = [f for f in cols if f["kind"] == "measure"]
    row_meas = [f for f in rows if f["kind"] == "measure"]
    col_dims = [f for f in cols if f["kind"] == "dimension" or f["is_date"]]
    row_dims = [f for f in rows if f["kind"] == "dimension" or f["is_date"]]

    def _dim(f):
        return {"caption": f["caption"], "datepart": f.get("datepart")}

    # ---- MULTI-MEASURE BAR PANEL (Measure Values on a positional shelf) ----
    # Tableau "Automatic" mark + Measure Values placed on rows/cols (not on Text)
    # renders one bar small-multiple per measure across a dimension.
    mvals_positional = any(("Multiple Values" in t or "Measure Values" in t)
                           for t in (cols_text, rows_text))
    if mvals_positional and mnames and not meas:
        on_cols = "Multiple Values" in cols_text or "Measure Values" in cols_text
        if on_cols:
            dimf = (row_dims[-1] if row_dims else (col_dims[-1] if col_dims else None))
        else:
            dimf = (col_dims[-1] if col_dims else (row_dims[-1] if row_dims else None))
        spec.update({"kind": "mbar",
                     "orient": "h" if on_cols else "v",
                     "dim": dimf["caption"] if dimf else None,
                     "measures": mnames})
        return spec

    # ---- PERCENT-OF-TOTAL STRIP (one pcto measure, no dims; e.g. ShipSummary) ----
    pct_meas = [f for f in meas if f.get("pct_total")]
    if pct_meas and not dims and not dates:
        cm = enc.get("color")
        spec.update({"kind": "pctbar",
                     "measure": _measure_entry(pct_meas[0], meta),
                     "segment": cm["caption"] if (cm and cm["kind"] == "dimension") else None,
                     "orient": "h" if any(f.get("pct_total") for f in col_meas) else "v"})
        return spec

    # ---- PIE (explicit Pie mark; wedge-size = the measure) ----
    if mark == "Pie":
        wm = enc.get("wedge-size") or (meas[0] if meas else None)
        cenc = enc.get("color")
        spec.update({"kind": "pie",
                     "measure": _m(wm, meta) if wm else None,
                     "segment": (cenc["caption"]
                                 if (cenc and cenc["kind"] == "dimension") else None),
                     "labels": enc.get("text") is not None})
        return spec

    # ---- HEATMAP / highlight table (Square; compound rows x col dim + color measure) ----
    if mark == "Square" and enc.get("color") and col_dims and row_dims:
        spec.update({"kind": "heatmap",
                     "x": _dim(col_dims[-1]),
                     "y_dims": [_dim(d) for d in row_dims],
                     "color_measure": _enc_measure(enc.get("color"), meas),
                     "text": enc.get("text") is not None})
        return spec

    # ---- CIRCLE plot: measure on cols, dims on rows, faceted by a col dim ----
    if mark in ("Circle", "Shape") and col_meas and row_dims:
        facet = [d["caption"] for d in col_dims]   # e.g. Segment
        detail = (enc.get("lod") or enc.get("detail") or {})
        spec.update({"kind": "circle",
                     "x": {"caption": col_meas[0]["caption"], "agg": col_meas[0]["agg"]},
                     "y_dims": [_dim(d) for d in row_dims],
                     "facet_col": facet[0] if facet else None,
                     "detail": detail.get("caption"),
                     "color_measure": _enc_measure(enc.get("color"), meas)
                                      if (enc.get("color") and enc["color"]["kind"] == "measure")
                                      else None})
        return spec

    # ---- DOT / strip plot (Circle/Shape; one measure + dimension, no facet) ----
    if mark in ("Circle", "Shape") and meas and (row_dims or col_dims):
        ydim = (row_dims[-1] if row_dims else col_dims[-1])["caption"]
        spec.update({"kind": "scatter",
                     "x": {"caption": meas[0]["caption"], "agg": meas[0]["agg"]},
                     "y": {"caption": ydim},
                     "color": _enc_field(enc.get("color")), "ydim": True})
        return spec

    # ---- DETAIL / CROSSTAB TABLE ----
    # A DEEP stack of discrete dimensions on ONE shelf (3+ DISTINCT: e.g.
    # Event Id / Product Name / Event Date / Channel / Payment Method) with
    # the OTHER shelf carrying no dimensions is Tableau's text/detail table --
    # a per-row LISTING, not a chart. (A real crosstab like Performance has
    # dims on BOTH shelves -> excluded; compound date shelves that repeat one
    # field are de-duplicated so they don't inflate the count.) Without this
    # it fell through to time-series and drew one meaningless bar.
    def _distinct_dims(fields):
        seen, out = set(), []
        for f in fields:
            if (f["kind"] == "dimension" or f["is_date"]) and f["caption"] not in seen:
                seen.add(f["caption"])
                out.append(f)
        return out
    rd, cd = _distinct_dims(rows), _distinct_dims(cols)
    if meas and ((len(rd) >= 3 and not cd) or (len(cd) >= 3 and not rd)):
        tdims = rd if len(rd) >= len(cd) else cd
        seen, umeas = set(), []
        for m in meas:
            key = (m["caption"], m.get("agg"))
            if key not in seen:
                seen.add(key)
                umeas.append(_measure_entry(m, meta))
        spec.update({"kind": "table",
                     "dims": [d["caption"] for d in tdims],
                     "measures": umeas})
        return spec

    # ---- MULTI-MEASURE SHELF (Tableau compound axis: [avg:A + avg:B]) ----
    # Tableau draws one stacked pane per measure. Route by what's on the
    # OTHER shelf: dates -> stacked time panes; dims -> multi-measure bar
    # panels; nothing + Circle mark -> per-measure dot strips.
    multi = col_meas if len(col_meas) >= 2 else (row_meas if len(row_meas) >= 2 else None)
    if multi:
        other = rows if multi is col_meas else cols
        o_dates = [f for f in other if f["is_date"]]
        o_dims = [f for f in other if f["kind"] == "dimension" and not f["is_date"]]
        cenc = enc.get("color")
        if o_dates:
            finest = max(o_dates, key=lambda f: DATEPART_RANK.get(f.get("datepart") or "mn", 2))
            kind = ("area" if mark == "Area" else
                    "dtbar" if mark == "Bar" else _auto_date_kind(mark))
            ys = [_m(f, meta) for f in multi]
            # TRUE DUAL AXIS: exactly two measures on one shelf = Tableau's
            # dual/combo chart (each pane may carry its own mark: bar+line).
            if len(multi) == 2 and not o_dims:
                pm = pane_marks(ws, meta)
                base_mark = "bar" if kind == "dtbar" else kind
                for f, ym in zip(multi, ys):
                    ym["mark"] = pm.get((f["caption"], f["agg"]), base_mark)
                kind = "dual"
            spec.update({"kind": kind,
                         "x": {"caption": finest["caption"],
                               "datepart": finest["datepart"] or "mn"},
                         "ys": ys,
                         "y": ys[0],
                         "panel": o_dims[0]["caption"] if o_dims else None,
                         "color": (_enc_field(cenc)
                                   if (cenc and cenc["kind"] == "dimension") else None)})
            return spec
        if o_dims:
            spec.update({"kind": "mbar",
                         "orient": "h" if multi is col_meas else "v",
                         "dim": o_dims[-1]["caption"],
                         "measures": [_measure_entry(f, meta) for f in multi],
                         "color": (_enc_field(cenc)
                                   if (cenc and cenc["kind"] == "dimension") else None)})
            return spec
        # IDENTICAL measure twice on one shelf + a measure on the other =
        # Tableau's layered dual-axis of ONE axis (Customers vs Revenue
        # scatter) -- a scatter, not per-measure strips
        o_meas = [f for f in other if f["kind"] == "measure"]
        same = len({(f["caption"], f["agg"]) for f in multi}) == 1
        if same and o_meas and mark in ("Circle", "Shape", "Automatic"):
            spec.update({"kind": "scatter",
                         "x": _m(o_meas[0], meta),
                         "y": _m(multi[0], meta),
                         "color": _enc_field(cenc) if (cenc and cenc["kind"] == "dimension") else None,
                         "detail": _enc_field(enc.get("lod") or enc.get("detail"))})
            return spec
        if mark in ("Circle", "Shape"):
            spec.update({"kind": "strips",
                         "measures": [_m(f, meta) for f in multi],
                         "detail": (enc.get("lod") or {}).get("caption"),
                         "color_measure": _enc_measure(cenc, multi)
                                          if (cenc and cenc["kind"] == "measure") else None})
            return spec

    # ---- SCATTER (two measures, opposing shelves) ----
    if col_meas and row_meas:
        spec.update({"kind": "scatter",
                     "x": _m(col_meas[0], meta),
                     "y": _m(row_meas[0], meta),
                     "color": _enc_field(enc.get("color")),
                     "detail": _enc_field(enc.get("lod") or enc.get("detail"))})
        return spec

    # ---- TIME SERIES (date present) -> area / line / DISCRETE-DATE BARS ----
    # The MARK CLASS decides. A discrete date pill (:ok) changes the AXIS
    # (month headers instead of a continuous scale), NOT the mark -- see
    # _auto_date_kind.
    if dates and meas:
        panel = dims[0]["caption"] if dims else None
        # compound date shelves (e.g. YEAR(Order Date) * WEEK(Order Date)):
        # plot at the FINEST granularity present, the way Tableau draws it.
        finest = max(dates, key=lambda f: DATEPART_RANK.get(f.get("datepart") or "mn", 2))
        kind = ("area" if mark == "Area" else
                "dtbar" if mark == "Bar" else _auto_date_kind(mark))
        spec.update({"kind": kind,
                     "x": {"caption": finest["caption"], "datepart": finest["datepart"] or "mn"},
                     "y": _m(meas[0], meta),
                     "panel": panel,
                     "color": _enc_field(enc.get("color"))})
        txt = enc.get("text")
        if txt and txt["kind"] == "measure":
            spec["labels"] = True
        return spec

    # ---- BAR (one measure + dimension) ----
    if meas and dims:
        cenc = enc.get("color")
        if row_meas and any(f["kind"] == "dimension" for f in cols):
            xcap = next(f["caption"] for f in cols if f["kind"] == "dimension")
            spec.update({"kind": "bar", "orient": "v",
                         "x": {"caption": xcap},
                         "y": _m(row_meas[0], meta),
                         "color": _enc_field(cenc)})
        else:
            # INNERMOST (last) row dimension = the bar axis; outer dims are
            # nesting groups (e.g. "Rank over 3 / Sales Person" -> Sales Person)
            ycap = next((f["caption"] for f in reversed(rows) if f["kind"] == "dimension"),
                        dims[-1]["caption"])
            spec.update({"kind": "bar", "orient": "h",
                         "y": {"caption": ycap},
                         "x": _m(meas[0], meta),
                         "color": _enc_field(cenc)})
        # nested shelf dims + color on the inner dim = GROUPED (side-by-side)
        # bars, the way Tableau draws "Category / Region" with Region on color
        if (cenc and cenc["kind"] == "dimension"
                and cenc["caption"] in {d["caption"] for d in dims}):
            spec["grouped"] = True
        txt = enc.get("text")
        if txt and txt["kind"] == "measure":
            spec["labels"] = True
        return spec

    # ---- GANTT / DOT TIMELINE (date dim x category dim, size/color encoded).
    # A CONTINUOUS date + a SIZE measure is how Tableau draws a Gantt bar:
    # the mark starts at the date and its LENGTH is the size measure
    # (e.g. DaystoShip: Product x Order Date, bar length = Days to Ship).
    if not meas and dims and dates and (enc.get("size") or enc.get("color")):
        cat = dims[-1]
        ddate = dates[0]
        sz = enc.get("size")
        is_gantt = (mark in ("Gantt", "GanttBar") or
                    (ddate.get("datepart") is None and sz is not None
                     and sz.get("kind") == "measure"))
        spec.update({"kind": "gantt" if is_gantt else "dots",
                     "x": {"caption": ddate["caption"], "datepart": ddate.get("datepart")},
                     "y": {"caption": cat["caption"]},
                     "color": _enc_field(enc.get("color")),
                     "size": _enc_field(sz)})
        return spec

    # ---- TREEMAP / PACKED BUBBLES (no positional shelves; size + text).
    # Tableau's Automatic mark here is a TREEMAP; an explicit Circle mark is
    # packed bubbles (e.g. TourismByCountry = treemap of Country by Tourism).
    if not all_f and enc.get("size") and enc["size"]["kind"] == "measure":
        txt = enc.get("text")
        lbl = (txt["caption"] if (txt and txt["kind"] == "dimension")
               else (enc.get("lod") or enc.get("detail") or {}).get("caption"))
        cenc = enc.get("color")
        spec.update({"kind": "bubbles" if mark in ("Circle", "Shape") else "treemap",
                     "size": _m(enc["size"], meta),
                     "label": lbl,
                     "color": (_enc_field(cenc)
                               if (cenc and cenc["kind"] == "dimension") else None)})
        return spec

    # ---- KPI strip / text table (measure-names sheets) ----
    if mnames and not meas:
        if not dims:
            spec.update({"kind": "kpi", "measures": mnames})
        else:
            spec.update({"kind": "table", "dims": [d["caption"] for d in dims],
                         "measures": mnames})
        return spec

    # ---- fallback: table ----
    spec.update({"kind": "table",
                 "dims": [d["caption"] for d in dims] or [f["caption"] for f in all_f],
                 "measures": [_measure_entry(m, meta) for m in meas] or mnames})
    return spec


def clean_title(text):
    """Strip Tableau dynamic title pieces: <[ds].[field]> / <Sheet> refs and the
    'AE' value sentinel, plus any dangling label/separator they leave behind."""
    t = text or ""
    i = t.find("<")
    if i != -1:                       # keep only the static lead before the 1st ref
        head = t[:i].rstrip()
        if head.endswith(":"):        # "...Label: <value>" -> drop the value's label
            head = head[:-1].rstrip()
            if " " in head:
                head = head[:head.rfind(" ")].rstrip()
        t = head
    t = t.replace("\u00c6", "").strip()   # Tableau dynamic-value sentinel
    t = " ".join(t.split())
    if i != -1 or t.endswith(":"):
        t = re.sub(r"\s+(for|of|by)$", "", t, flags=re.I)
        t = re.sub(r"[\s\-:,/(]+$", "", t).strip()
    return t


def _attr_suffix(el, suffix):
    """Attribute by local name, ignoring xmlns prefix (e.g. user:ui-enumeration)."""
    for k, v in el.attrib.items():
        if k.split("}")[-1].split(":")[-1] == suffix:
            return v
    return None


def _clean_member(v):
    """A categorical filter member literal -> its plain value, or None if it is a
    field reference (e.g. a Measure Names member) rather than a data value."""
    v = (v or "").strip().strip('"')
    if FIELD_RE.search(v):
        return None
    return v or None


def _scalar(s):
    """Filter bound -> date string (YYYY-MM-DD), float, or raw string."""
    s = (s or "").strip().strip("#").strip('"')
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    try:
        return float(s)
    except ValueError:
        return s


def context_columns(ws, meta):
    """Field captions that carry a CONTEXT filter (<filter context='true'>) on
    this worksheet -- captured SEPARATELY from applied_filters because a context
    filter often enumerates ALL members (the live value comes from a dashboard
    quick-filter), so applied_filters skips it as all-enumerated. The engine
    still needs to know REGION is a context column so it can inject the
    dashboard's live Region value into the top-N ranking subquery (Tableau
    applies context filters BEFORE top-N)."""
    cols = []
    for f in ws.findall(".//filter"):
        if f.get("context") != "true":
            continue
        col = f.get("column") or ""
        if "Action (" in col or ":Measure Names" in col:
            continue
        cap = parse_field(col, meta)["caption"]
        if cap not in cols:
            cols.append(cap)
    return cols


def applied_filters(ws, meta):
    """GENUINELY applied data filters with their VALUES (not just name+kind):
    quantitative ranges, categorical subsets, and ordinal/date-part ranges.
    All-members-enumerated, Measure Names and action filters are skipped."""
    out = []
    for f in ws.findall(".//filter"):
        col = f.get("column") or ""
        if "Action (" in col or ":Measure Names" in col:
            continue
        fld = parse_field(col, meta)
        # A CONTEXT filter (<filter context='true'>) is applied BEFORE top-N /
        # dimension filters in Tableau's order of operations -- the engine must
        # inject it INTO the top-N ranking subquery, or "top 10 customers in
        # Central" wrongly becomes "the global top 10, shown for Central only".
        is_context = f.get("context") == "true"
        if f.get("class") == "relative-date":
            # relative-date windows ("last 3 months", anchored dates) are not
            # translated -- captured so the engine reports the drop instead of
            # silently showing ALL dates.
            out.append({"caption": fld["caption"], "kind": "relative_date",
                        "period": f.get("period-type"),
                        "range": f.get("range-type"),
                        "n": f.get("period-count") or f.get("n"),
                        "anchor": f.get("anchor-date")})
            continue
        if f.get("class") == "quantitative":
            mn, mx = f.find("min"), f.find("max")
            if mn is not None or mx is not None:
                # date-PART range (e.g. [yr:Year:qk] 2001..2012): bounds are
                # part numbers, NOT raw column values -- engine must EXTRACT().
                out.append({"caption": fld["caption"], "kind": "range",
                            "is_date": fld["is_date"],
                            "datepart": fld.get("datepart"),
                            "min": _scalar(mn.text) if mn is not None else None,
                            "max": _scalar(mx.text) if mx is not None else None})
            continue
        # TOP-N filter: <groupfilter function='end' end='top' count='N'> wrapping
        # an <groupfilter function='order' expression='AVG([GDP])' direction='DESC'>.
        # The inner level-members is 'all' -- WITHOUT this check the filter would
        # be skipped as all-enumerated and every member would render (silent
        # correctness bug: "top 5 countries" becomes "all countries").
        topn = f.find(".//groupfilter[@function='end']")
        if topn is not None and topn.get("count"):
            order = topn.find(".//groupfilter[@function='order']")
            cnt = (topn.get("count") or "").strip()
            e = {"caption": fld["caption"], "kind": "top_n",
                 "dir": topn.get("end") or "top",
                 "order_expr": order.get("expression") if order is not None else None,
                 "order_dir": (order.get("direction") or "DESC") if order is not None else "DESC"}
            if cnt.lstrip("-").isdigit():
                e["n"] = int(cnt)
            else:
                # count referencing a PARAMETER ("[Parameters].[Top N]"):
                # keep the caption; engine emits a __PARAM__ token so the
                # sidebar what-if control drives N live.
                e["n_param"] = re.findall(r"\[([^\]]+)\]", cnt)[-1] if "[" in cnt else cnt
            out.append(e)
            continue
        members, all_enum, rng = [], False, None
        is_except = False
        for gf in f.findall(".//groupfilter"):
            fn = gf.get("function")
            if fn == "except":
                # EXCLUSION filter: "all members EXCEPT <list>" -- the member
                # entries below are what Tableau REMOVES, not what it keeps
                is_except = True
            elif fn == "level-members" and _attr_suffix(gf, "ui-enumeration") == "all":
                all_enum = True
            elif fn == "member":
                v = _clean_member(gf.get("member"))
                if v is not None:
                    members.append(v)
            elif fn == "range":
                rng = {"from": gf.get("from"), "to": gf.get("to")}
        if members and is_except:
            out.append({"caption": fld["caption"], "kind": "not_in",
                        "values": members, "context": is_context})
        elif members and not all_enum:
            e = {"caption": fld["caption"], "kind": "in", "values": members,
                 "context": is_context}
            # date-PART member filter (e.g. MONTH(Order Date) IN (4)): the values
            # are part numbers, NOT raw column values -- engine must EXTRACT().
            if fld.get("datepart"):
                e["datepart"] = fld["datepart"]
            out.append(e)
        elif rng and not all_enum:
            out.append({"caption": fld["caption"], "kind": "ord_range",
                        "datepart": fld["datepart"], "from": rng["from"],
                        "to": rng["to"], "context": is_context})
    return out


def table_calcs(ws, meta):
    """Table calculations on a field: RANK / PERCENT-OF-TOTAL / WINDOW, etc."""
    out = []
    for ci in ws.findall(".//column-instance"):
        tc = ci.find("table-calc")
        if tc is None or not (tc.get("type") or tc.get("rank-options")):
            continue
        fld = parse_field("[d].[" + (ci.get("name") or "").strip("[]") + "]", meta)
        out.append({"caption": fld["caption"], "agg": fld["agg"],
                    "type": tc.get("type"), "ordering": tc.get("ordering-type"),
                    "options": tc.get("rank-options"),
                    "ordering_field": (parse_field(tc.get("ordering-field"), meta)["caption"]
                                       if tc.get("ordering-field") else None)})
    return out


def manual_sort(ws, meta):
    """Hand-arranged (drag) member order for a dimension, if any."""
    for ms in ws.findall(".//manual-sort"):
        col = ms.get("column") or ""
        if ":Measure Names" in col:
            continue
        fld = parse_field(col, meta)
        order = [v for v in (_clean_member(b.text) for b in ms.findall(".//bucket")) if v]
        if order:
            return {"on": fld["caption"], "order": order}
    return None


def reflines(ws, meta, params=None):
    """Reference / distribution lines: aggregation, value field/param, scope, bands.
    Parameter-valued lines resolve to a literal (the parameter's default)."""
    params = params or {}
    out = []
    for rl in ws.findall(".//reference-line"):
        axis = parse_field(rl.get("axis-column") or "", meta)
        vc = rl.get("value-column") or ""
        val = parse_field(vc, meta) if vc else None
        bands = [float(b.get("percentage")) for b in rl.findall("reference-line-value")
                 if b.get("percentage")]
        e = {"axis": axis["caption"], "formula": rl.get("formula"),
             "value_field": val["caption"] if val else None,
             "value_agg": val["agg"] if val else None,
             "is_param": vc.startswith("[Parameters]"),
             "scope": rl.get("scope"),
             "label": (rl.get("label") or "").replace("<Value>", "").strip() or None,
             "bands": bands}
        if e["is_param"] and val and params.get(val["caption"]) is not None:
            e["value_param"] = val["caption"]     # live lookup at render time
            try:
                e["value_literal"] = float(str(params[val["caption"]]).strip('"'))
            except ValueError:
                pass
        out.append(e)
    return out


def sheet_datasource(ws, dsmap=None):
    """Primary datasource caption for a worksheet: prefer the federated id used
    on its shelves, else the first non-Parameters datasource-dependency."""
    dsmap = dsmap or {}
    texts = [(ws.find(".//table/" + w).text if ws.find(".//table/" + w) is not None else "") or ""
             for w in ("cols", "rows")]
    for t in texts:
        m = re.search(r"\[(federated\.\w+)\]", t)
        if m:
            return dsmap.get(m.group(1), m.group(1))
    for dep in ws.findall(".//datasource-dependencies"):
        d = dep.get("datasource")
        if d and d != "Parameters":
            return dsmap.get(d, d)
    return None


def infer(ws, meta, dsmap=None, params=None):
    """Infer the chart, then attach title, sort and color-scale (all from XML)."""
    spec = _infer_core(ws, meta)
    # FIXED mark color (user picked a single color for all marks on this sheet)
    for f in ws.findall(".//pane//format"):
        if f.get("attr") in ("mark-color", "color") and f.get("value", "").startswith("#"):
            spec["mark_color"] = f.get("value")
            break
    ds = sheet_datasource(ws, dsmap)
    if ds:
        spec["datasource"] = ds
    deps = {d.get("datasource") for d in ws.findall(".//datasource-dependencies")
            if d.get("datasource") and d.get("datasource") != "Parameters"}
    if len(deps) > 1:
        spec["blend"] = sorted((dsmap or {}).get(d, d) for d in deps)

    t = ws.find("layout-options/title")
    if t is not None:
        ttl = clean_title("".join(t.itertext()))
        if ttl:
            spec["title"] = ttl

    srt = sheet_sort(ws, meta)
    if srt:
        spec["sort"] = srt

    enc = encodings(ws, meta)
    active = enc.get("color")
    if active is not None:
        sc = color_scale(ws, meta, active["caption"])
        if sc:
            for key in ("color", "color_measure"):
                if isinstance(spec.get(key), dict):
                    spec[key]["scale"] = sc

    # ---- capture every remaining rendering-relevant construct (generic) ----
    af = applied_filters(ws, meta)
    if af:
        spec["applied_filters"] = af
    cctx = context_columns(ws, meta)
    if cctx:
        spec["context_fields"] = cctx
    tc = table_calcs(ws, meta)
    if tc:
        spec["table_calcs"] = tc
    ms = manual_sort(ws, meta)
    if ms:
        spec["manual_sort"] = ms
    rl = reflines(ws, meta, params)
    if rl:
        spec["reflines"] = rl
    if enc.get("size"):
        spec["size"] = _enc_field(enc["size"])
    if enc.get("shape"):
        spec["shape"] = _enc_field(enc["shape"])
    if ws.findall(".//style-rule[@element='trendline']"):
        spec["trendline"] = {"model": "linear"}
    tips = [parse_field(e.get("column"), meta)["caption"]
            for e in ws.findall(".//pane/encodings/tooltip") if e.get("column")]
    if tips:
        spec["tooltip_fields"] = tips
    # TEXT (label) encodings: measures shown as text marks. Order preserved --
    # rank-table sheets (MIN(0) placeholder axis) print these as columns.
    texts = []
    for e in ws.findall(".//pane/encodings/text"):
        if e.get("column"):
            f = parse_field(e.get("column"), meta)
            if f["caption"] not in texts:
                texts.append(f["caption"])
    if texts:
        spec["text_fields"] = texts
    # forecast overlay (ETS prediction bands) -- actuals convert, the model does not
    fc = ws.find(".//forecast-specification")
    if fc is not None and fc.get("enabled") != "false":
        spec["forecast"] = {"model": fc.get("model-type"),
                            "bands": fc.get("show-prediction-bands") == "true"}
    # worksheet subtotals / grand totals
    if ws.find(".//subtotals") is not None:
        spec["subtotals"] = True
    # viz-in-tooltip: tooltip CDATA embeds <Sheet name="..."> references
    vt = sorted({m.group(1) for m in
                 re.finditer(r'<Sheet name="([^"]+)"', "".join(ws.itertext()))})
    if vt:
        spec["viz_tooltip"] = vt
    ax = axis_flags(ws)
    if ax:
        spec["axis_flags"] = ax
    # NON-DATA sheet: Tableau text boxes, show/hide toggle helpers and blank
    # placeholders carry NO plottable field. Generic signal (no name match):
    # nothing on any data shelf -> mark it so the engine skips quietly and
    # the report grades it 'n/a' instead of a false 'failed'.
    if not _has_data_fields(spec):
        spec["non_data"] = True
    return spec


def _has_data_fields(spec):
    """True when a sheet has ANY field that resolves to a data query.
    Everything else (text/button/blank scaffolding) is non-data. Inclusive
    on purpose: better to render an odd chart than to hide a real one."""
    if spec.get("measures") or spec.get("ys") or spec.get("y_dims"):
        return True
    # any of these present -- as a {caption:...} dict OR a bare column string
    # (geo/dim are strings; x/y/color_measure/size/label are dicts) -- is data
    for k in ("x", "y", "dim", "segment", "measure", "panel", "geo",
              "color", "color_measure", "size", "label", "detail"):
        v = spec.get(k)
        if isinstance(v, str) and v.strip():
            return True
        if isinstance(v, dict) and v.get("caption"):
            return True
    return False


def axis_flags(ws):
    """Log-scale / reversed axes change value interpretation -- captured so
    the engine reports them (our Altair specs always render linear ascending)."""
    return sorted({f"{fmt.get('attr')}={fmt.get('value')}"
                   for sr in ws.findall(".//style-rule[@element='axis']")
                   for fmt in sr.findall("format")
                   if fmt.get("attr") in ("scale", "reversed")
                   and (fmt.get("value") or "").lower() not in ("", "linear", "false")})


def drill_hierarchies(root, meta):
    """Tableau hierarchies (<drill-path>) -> {name: [level captions]}.
    Streamlit has no click-to-drill, so the engine renders a drill-level
    SELECTOR for sheets whose dimension is a hierarchy level."""
    out = {}
    for dp in root.findall(".//drill-path"):
        nm = dp.get("name")
        levels = []
        for f in dp.findall("field"):
            internal = (f.text or "").strip()
            cap = meta.get(internal, {}).get("caption") or internal.strip("[]")
            if cap not in levels:
                levels.append(cap)
        if nm and len(levels) >= 2:
            out.setdefault(nm, levels)
    return out


def datasource_notes(root):
    """Data-model constructs that hide logic OUTSIDE worksheets: custom SQL
    (<relation type='text'>SELECT...</relation>) and initial SQL (connection
    one-time-sql). They silently change row grain if ignored -- surfaced in
    the assessment even though nothing renders differently."""
    csql = custom_sql_sources(root)
    notes = []
    for ds in root.findall(".//datasources/datasource"):
        dsname = ds.get("caption") or ds.get("name") or "?"
        for rel in ds.findall(".//relation[@type='text']"):
            sql_text = (rel.text or "").strip()
            info = csql.get(dsname)
            status = ("executed live against Snowflake, no data copy" if info and info["queryable"]
                      else "detected only -- %s" % info["reason"] if info
                      else "detected only (has its own extract -- already materialized)")
            notes.append({
                "datasource": dsname, "kind": "custom-sql",
                "detail": "Custom SQL relation '%s' (%s): %s%s" % (
                    rel.get("name") or "?", status, sql_text[:220],
                    "..." if len(sql_text) > 220 else "")})
        for conn in ds.findall(".//connection"):
            osql = conn.get("one-time-sql")
            if osql:
                notes.append({
                    "datasource": dsname, "kind": "initial-sql",
                    "detail": "Initial SQL on connection: %s%s" % (
                        osql[:220], "..." if len(osql) > 220 else "")})
    for cap, info in live_connections(root).items():
        if not info["queryable"]:
            notes.append({
                "datasource": cap, "kind": "live-connection",
                "detail": "Live connection (class '%s'): %s" % (
                    info["class"], info["reason"])})
    for b in blends(root):
        links = ", ".join(
            "%s = %s" % (l["primary_field"], l["secondary_field"]) for l in b["links"])
        notes.append({
            "datasource": b["primary"], "kind": "blend",
            "detail": "Data BLEND with '%s' on %s (sheets: %s). Tableau links these "
                      "at query time and aggregates the secondary to the link grain "
                      "-- it is NOT a row-level join, so the secondary's fields are "
                      "not merged into the primary's table. Remodel guidance (pre-"
                      "aggregate the secondary, then LEFT JOIN) is generated per "
                      "blend; review before use." % (
                          b["secondary"], links or "(no link fields declared)",
                          ", ".join(b["sheets"]) or "none")})
    for cap, mem in union_members(root).items():
        notes.append({
            "datasource": cap, "kind": "union",
            "detail": "Union of %d inputs (%s) -- combined row-wise (UNION ALL) "
                      "at onboarding." % (len(mem), ", ".join(mem))})
    return notes


def union_members(root):
    """Datasources whose relation is a UNION -> {caption: [member file basenames
    in order]}.

    A Tableau union (<relation type='union'> containing child <relation
    type='table'> members) stacks same-schema inputs row-wise, like SQL UNION
    ALL -- multiple CSVs, Excel sheets, or DB tables. Each member relation names
    a `connection`; that named-connection carries the member's `filename`. Only
    file-backed members (csv/excel) are resolved here (the onboarding path that
    materializes them reads files); a union of live DB tables would need the
    same UNION ALL built in SQL and is out of this MVP's scope. Returns [] for a
    union whose members can't be resolved to files, so onboarding falls back to
    its normal single-file behavior instead of silently dropping rows."""
    out = {}
    for ds in root.findall(".//datasources/datasource"):
        cap = ds.get("caption") or ds.get("name")
        if ds.get("name") == "Parameters":
            continue
        union = ds.find(".//relation[@type='union']")
        if union is None:
            continue
        _base = lambda p: (p or "").replace("\\", "/").rsplit("/", 1)[-1]
        # named-connection name -> its file basename
        conn_file = {}
        for nc in ds.findall(".//named-connection"):
            nm = nc.get("name")
            c = nc.find("connection")
            fn = (c.get("filename") if c is not None else None) or ""
            if nm and fn:
                conn_file[nm] = _base(fn)
        members = []
        for rel in union.findall("./relation"):
            fn = conn_file.get(rel.get("connection"))
            if not fn:
                # fall back to the relation's own name if it looks like a file
                nm = _base(rel.get("name") or "")
                if "." in nm:
                    fn = nm
            if fn and fn not in members:
                members.append(fn)
        if len(members) >= 2:
            out[cap] = members
    return out


def custom_sql_sources(root):
    """Datasources whose data is a LIVE custom-SQL query (no <extract> -- an
    extract-backed custom SQL datasource is already fully handled: the
    extract IS the custom SQL's materialized result, decoded by the existing
    hyper path, nothing to do here) -> {caption: {class, sql, queryable,
    reason}}.

    MVP SCOPE (2026-07-21): only Snowflake-class custom SQL is directly
    queryable -- the SQL text is already valid Snowflake SQL (Tableau doesn't
    translate dialects), so it can be run VERBATIM as a derived table with no
    rewriting. Any other class's custom SQL is written in a different SQL
    dialect (T-SQL, PL/SQL, ...) that would not reliably parse against
    Snowflake -- reported honestly instead of guessed at."""
    out = {}
    for ds in root.findall(".//datasources/datasource"):
        cap = ds.get("caption") or ds.get("name")
        if ds.get("name") == "Parameters" or ds.find(".//extract") is not None:
            continue
        rels = [r for r in ds.findall(".//relation") if r.get("type")]
        text_rels = [r for r in rels if r.get("type") == "text"]
        if not text_rels:
            continue
        conns = [c for c in ds.findall(".//connection")
                 if c.get("class") and c.get("class") != "federated"
                 and not c.get("filename")]
        cls = conns[0].get("class") if conns else None
        sql_text = "\n".join((r.text or "").strip() for r in text_rels if r.text)
        sql_text = sql_text.rstrip().rstrip(";").rstrip()  # trailing ';' breaks (subquery) AS alias
        info = {"class": cls, "sql": sql_text, "queryable": False, "reason": None}
        if len(text_rels) > 1:
            info["reason"] = "multiple custom-SQL relations on one datasource -- not yet supported"
        elif cls != "snowflake":
            info["reason"] = ("custom SQL written for class '%s' -- cannot run "
                              "verbatim against Snowflake (different SQL dialect)" % cls)
        else:
            info["queryable"] = True
        out[cap] = info
    return out


def live_connections(root):
    """Datasources with NO <extract> -- i.e. genuinely LIVE, not an extract --
    mapped to their connection info: {caption: {class, server, dbname, schema,
    warehouse, table, queryable, reason}}.

    MVP SCOPE (2026-07-21): only a live connection whose class is 'snowflake',
    querying a SINGLE named table (no join, no custom SQL -- those are
    separate constructs: a join/relationship model is R9's job, custom SQL is
    its own datasource_notes 'custom-sql' kind) is directly bound HERE --
    Snowflake is already the data plane, so the accelerator can point
    config.DATASOURCES straight at the live source's OWN db.schema.table, no
    data copy, genuinely live. Every other live class (sqlproxy -- a published
    Tableau Server/Cloud datasource, sqlserver, oracle, etc.) is reported
    HONESTLY via datasource_notes instead of being silently swapped for a
    stand-in table (the prior behavior). A live MULTI-TABLE model is reported
    honestly here too (not directly bindable) and handled by the data-model
    view path instead (semantic_layer.describe_model / pipeline.
    build_data_model_tables) -- see the R9 fix below for why that path already
    works for a live model once this function stops mis-detecting it.

    R9 BUG FIX (2026-07-26, found live testing R9): the relation scan used to
    be `ds.findall(".//relation")` -- EVERY <relation> anywhere in the
    datasource, which also matches the per-OBJECT <relation> elements nested
    inside the object-model's <object-graph> (a totally separate sibling of
    <connection>, describing each joined table for Stage 3's data-model view).
    For a 2-table live relationship model this silently returned 4 relations
    (2 real + 2 duplicates from the object-graph) and `next(r for r in rels if
    type=='table')` picked the FIRST one and called the WHOLE datasource
    single-table queryable at JUST that table -- silently dropping the second
    table and its join entirely, and reporting queryable=True (an actively
    wrong answer, not an honest refusal). FIX: scan only the relations that
    are DIRECT CHILDREN of the federated <connection> element (confirmed
    against the real KPI Live workbook's XML shape -- its one real relation is
    a direct child of <connection>; the object-graph's per-object relations
    live several levels below a totally different sibling element and are
    never direct children of <connection>). A genuinely single-table live
    datasource is unaffected (still exactly one direct-child relation); a
    multi-table one is now correctly NOT claimed queryable here."""
    out = {}
    for ds in root.findall(".//datasources/datasource"):
        cap = ds.get("caption") or ds.get("name")
        if ds.get("name") == "Parameters":
            continue
        if ds.find(".//extract") is not None:
            continue                            # has an extract -- unaffected
        conns = [c for c in ds.findall(".//connection")
                 if c.get("class") and c.get("class") != "federated"
                 and not c.get("filename")]
        # A connection carrying `filename` (excel-direct, textscan, ...) reads
        # a LOCAL file directly with no extract -- Tableau's XML represents
        # that identically to a real remote-database live connection (no
        # <extract>), but it is already handled generically by the existing
        # file-matching path (datasource_files/pick_local_file). Excluding
        # `filename`-bearing connections here is what keeps this function
        # scoped to genuine remote-database live connections only.
        if not conns:
            continue                            # no real remote source described
        c = conns[0]
        cls = c.get("class")
        info = {"class": cls, "server": c.get("server"), "dbname": c.get("dbname"),
                "schema": c.get("schema"), "warehouse": c.get("warehouse"),
                "table": None, "queryable": False, "reason": None}
        fed = ds.find(".//connection[@class='federated']")
        rel_scope = fed.findall("./relation") if fed is not None else ds.findall(".//relation")
        rels = [r for r in rel_scope if r.get("type")]
        table_rels = [r for r in rels if r.get("type") == "table"]
        has_join = any(r.get("type") == "join" for r in rels)
        has_custom_sql = any(r.get("type") == "text" for r in rels)
        if cls != "snowflake":
            info["reason"] = ("class '%s' is not Snowflake -- cannot query "
                              "directly (see the live-source migration kit "
                              "backlog item)" % cls)
        elif has_custom_sql:
            info["reason"] = "custom SQL relation -- see custom-SQL datasource support"
        elif has_join or len(table_rels) > 1:
            info["reason"] = ("%d tables in a live relationship/join model -- "
                              "see the data model view (Stage 3), not a "
                              "single-table direct bind" % max(len(table_rels), 2))
        elif not table_rels:
            info["reason"] = "no single named table relation found"
        else:
            table_rel = table_rels[0]
            # relation table='[TABLE]' (schema already on the connection) OR
            # '[SCHEMA].[TABLE]' (schema qualified on the relation itself) --
            # must NOT just dot-join every bracket segment onto info['table'],
            # or a connection that ALSO carries its own `schema` attribute
            # would double it into db.schema.schema.table downstream (every
            # caller builds the FQN as f"{dbname}.{schema}.{table}").
            parts = [p for p in table_rel.get("table", "").strip("[]").split("].[") if p]
            if len(parts) >= 2:
                info["schema"] = info["schema"] or parts[-2]
                info["table"] = parts[-1]
            elif parts:
                info["table"] = parts[0]
            info["queryable"] = bool(info["table"] and info["schema"] and info["dbname"])
            if not info["queryable"]:
                info["reason"] = "missing dbname/schema/table on the live connection"
        out[cap] = info
    return out


def blends(root):
    """Tableau DATA BLENDS -> [{primary, secondary, links, sheets, ...}].

    A blend is NOT a join. Tableau queries each datasource separately, aggregates
    the SECONDARY to the linking fields' grain, and left-joins that aggregate onto
    the primary's view. Modelling it as a row-level SQL join is wrong: it fans out
    the primary's rows and double-counts its measures. That is why this function
    EXTRACTS and REPORTS the blend rather than silently materializing one.

    THE XML (verified against Superstore's real 'Performance' sheet, not assumed):
        <datasource-relationships>
          <datasource-dependencies datasource='federated.a'>...</>
          <datasource-relationship source='federated.a' target='federated.b'>
            <column-mapping>
              <map key='[federated.a].[none:Category:nk]'
                   value='[federated.b].[none:Category:nk]' />
    `source` is the PRIMARY, `target` the SECONDARY. Each <map> is one candidate
    link. Tableau writes one map PER PILL DERIVATION, so a single 'Order Date'
    link appears repeatedly as mn:/yr:/tmn:/tyr: -- those are collapsed here to
    the underlying FIELD, with the derivations kept alongside, so callers see
    three real link fields rather than six near-duplicates.

    WHY THIS MATTERS BEYOND REPORTING: the Cortex calc-fallback previously had to
    GUESS a blend calc's join key and got it wrong (it proposed Region = Segment
    against the wrong table -- execute-clean but incorrect, which is exactly why
    that output ships as REVIEW). Feeding these real link fields into the prompt
    turns the join from a guess into a constraint."""
    dsmap = datasource_map(root)                    # federated.<id> -> caption
    meta, _cap = column_meta(root)
    out = []
    for rel in root.findall(".//datasource-relationships/datasource-relationship"):
        src, tgt = rel.get("source"), rel.get("target")
        if not src or not tgt:
            continue
        by_field = {}
        for m in rel.findall(".//map"):
            k, v = m.get("key"), m.get("value")
            if not k or not v:
                continue
            kf, vf = parse_field(k, meta), parse_field(v, meta)
            key = (kf["caption"], vf["caption"])
            ent = by_field.setdefault(key, {"primary_field": kf["caption"],
                                            "secondary_field": vf["caption"],
                                            "derivations": []})
            d = kf.get("datepart") or kf.get("agg")
            if d and d not in ent["derivations"]:
                ent["derivations"].append(d)
        if not by_field:
            continue
        pcap, scap = dsmap.get(src, src), dsmap.get(tgt, tgt)
        sheets = []
        for ws in root.findall(".//worksheet"):
            deps = {d.get("datasource") for d in ws.findall(".//datasource-dependencies")}
            if src in deps and tgt in deps:
                sheets.append(ws.get("name"))
        out.append({"primary": pcap, "secondary": scap,
                    "primary_name": src, "secondary_name": tgt,
                    "links": list(by_field.values()), "sheets": sheets})
    return out


def blend_remodel_sql(blend, table_for=None):
    """A CORRECT-BY-SEMANTICS starting point for replacing one blend with real
    SQL: aggregate the SECONDARY to the link grain FIRST, then LEFT JOIN it.

    Emitted as GUIDANCE (a commented template a human completes + reviews), never
    auto-deployed and never fed to the app. Two things it cannot know, both
    called out in the text it produces: which measures of the secondary matter
    (so the aggregate is a placeholder), and which of several candidate links
    Tableau actually activates for a given sheet (that depends on the fields on
    the view). Pre-aggregating is the part that IS knowable and is exactly what
    stops the row fan-out a naive join would cause."""
    # Function-local: tableau_parser is deliberately the BASE of the dependency
    # chain (stdlib only), and calc_translator pulls in config's file I/O at
    # import. Never re-implement to_phys here -- one transform, one definition.
    from calc_translator import to_phys
    tf = table_for or (lambda c: to_phys(c))
    p, s = blend["primary"], blend["secondary"]
    keys = blend["links"]
    on = " AND ".join(f'p.{to_phys(k["primary_field"])} = s.{to_phys(k["secondary_field"])}'
                      for k in keys) or "<no link fields found>"
    grp = ", ".join(to_phys(k["secondary_field"]) for k in keys) or "<link fields>"
    return "\n".join([
        f"-- BLEND: '{p}' (primary) + '{s}' (secondary)",
        f"-- Sheets affected: {', '.join(blend['sheets']) or '(none found)'}",
        "-- Tableau links these at QUERY time and aggregates the secondary to the",
        "-- link grain. Pre-aggregate BEFORE joining -- a row-level join would",
        "-- fan out the primary and double-count its measures.",
        "-- REVIEW REQUIRED: replace <SECONDARY MEASURE> with the field(s) the",
        "-- sheet actually uses, and confirm which link(s) Tableau activates for",
        "-- that sheet (candidates below are every link the workbook declares).",
        f"SELECT p.*, s.SECONDARY_VALUE",
        f"FROM {tf(p)} p",
        "LEFT JOIN (",
        f"  SELECT {grp}, SUM(<SECONDARY MEASURE>) AS SECONDARY_VALUE",
        f"  FROM {tf(s)}",
        f"  GROUP BY {grp}",
        f") s ON {on};",
    ])


def _source_columns(ds):
    """Remote (source) column names this datasource's metadata-records declare.

    Tableau writes a <metadata-record class='column'> per column it read FROM
    THE SOURCE, carrying the source's own `remote-name`. That is the workbook's
    own record of what the upstream table looks like -- which is what makes it
    usable as EVIDENCE when deciding whether a same-named Snowflake table is
    really the same table (see pipeline.resolve_source_binding). Tableau's own
    synthetic columns are excluded: they never exist in the source."""
    out = []
    for mr in ds.iter("metadata-record"):
        if mr.get("class") != "column":
            continue
        remote = (mr.findtext("remote-name") or "").strip()
        if not remote or remote in out:
            continue
        if remote.startswith(("Calculation_", "__tableau_internal")):
            continue
        if remote in ("Number of Records", "Measure Names", "Measure Values"):
            continue
        out.append(remote)
    return out


# Connection classes that are NOT an upstream source table, and why:
#  * the extract's OWN engine -- a .hyper/.tde carries a `class='hyper'`
#    connection with schema='Extract' whose relations ([Extract].[Extract],
#    [Extract].[<table> (DB.TABLE)_<guid>]) describe the extract file's internal
#    tables. Reading those as "the source" would be circular: the extract is the
#    copy R3 exists to avoid, not the original. Worse, a bare '[Extract]' name
#    would happily name-match a table called EXTRACT somewhere in the account.
#  * file-backed classes -- their "tables" are Excel sheet names (Events$,
#    Customers$), never database tables; the existing file-matching path owns them.
_EXTRACT_ENGINE_CLASSES = {"hyper", "dataengine", "tde"}
_FILE_CLASSES = {"excel-direct", "excel", "textscan", "csv", "msaccess",
                 "googlesheets", "spatial"}


def _upstream_connections(ds):
    """(chosen upstream connection, {named-connection name -> class}) for the
    REAL remote source, ignoring the federated wrapper, the extract's own engine
    and file-backed connections. Returns (None, {}) when nothing upstream."""
    named = {}
    for nc in ds.findall(".//named-connection"):
        inner = nc.find("connection")
        if nc.get("name") and inner is not None:
            named[nc.get("name")] = inner.get("class")
    real = [c for c in ds.findall(".//connection")
            if c.get("class") and c.get("class") != "federated"
            and not c.get("filename")
            and c.get("class") not in _EXTRACT_ENGINE_CLASSES
            and c.get("class") not in _FILE_CLASSES]
    return (real[0] if real else None), named


def source_tables(root):
    """The UPSTREAM SOURCE each datasource was built from -> {caption: info}.

    THE DIFFERENCE FROM live_connections(): that function deliberately SKIPS any
    datasource carrying an <extract>, because an extract means the data travels
    inside the .twbx and there is nothing live to query. This one deliberately
    KEEPS them. An extract-based workbook still records where its data ORIGINALLY
    came from (connection dbname/schema + the relation's table name), and if that
    same table already lives in the target Snowflake account there is no reason to
    decode the extract and write_pandas a second copy of it -- the accelerator can
    point straight at the governed original (roadmap R3).

    Reading the XML is all this does. It NEVER decides that a match exists: the
    account-side matching + the confidence rules live in
    pipeline.resolve_source_binding, because only a live session can tell whether
    a table is really there. Returns per caption:
      class, dbname, schema, has_extract, tables [{schema, name}], columns,
      bindable (exactly one named table -> a single binding target), reason.

    A multi-table (relationship/star) datasource is reported with every table but
    bindable=False: replicating a star as real Snowflake objects is the data-model
    view path (pipeline.build_data_model_tables), not a single-table rebind."""
    out = {}
    for ds in root.findall(".//datasources/datasource"):
        cap = ds.get("caption") or ds.get("name")
        if ds.get("name") == "Parameters":
            continue
        c, named = _upstream_connections(ds)
        if c is None:
            continue                    # no remote source: file- or extract-only
        info = {"class": c.get("class"), "dbname": c.get("dbname"),
                "schema": c.get("schema"),
                "has_extract": ds.find(".//extract") is not None,
                "tables": [], "columns": _source_columns(ds),
                "bindable": False, "reason": None}
        # Only relations bound to a REAL upstream named-connection. An
        # extract-based datasource carries BOTH: the original source relations
        # (connection='snowflake.<guid>') and the extract's own internal ones
        # (no connection attr, named [Extract].[...]). Proven on the corpus --
        # Regional Analysis lists 3 real SANDBOX.DS tables alongside 3 mangled
        # [Extract].[NAME (DB.NAME)_<guid>] twins.
        upstream = {n for n, cls in named.items()
                    if cls not in _EXTRACT_ENGINE_CLASSES and cls not in _FILE_CLASSES}
        for rel in ds.findall(".//relation[@type='table']"):
            if rel.get("connection") not in upstream:
                continue
            # Shapes: '[TABLE]' (schema on the connection), '[SCHEMA].[TABLE]',
            # or '[DB].[SCHEMA].[TABLE]' (all three seen in the corpus). Take the
            # LAST segment as the table and the one before it as the schema --
            # never dot-join every segment, or a connection that ALSO carries
            # `schema` doubles it into db.schema.schema.table downstream.
            parts = [p for p in (rel.get("table") or "").strip("[]").split("].[") if p]
            if not parts:
                continue
            t = {"schema": parts[-2] if len(parts) >= 2 else info["schema"],
                 "name": parts[-1]}
            if t not in info["tables"]:
                info["tables"].append(t)
        if any(r.get("type") == "text" for r in ds.findall(".//relation")):
            info["reason"] = ("custom SQL relation -- see custom-SQL datasource "
                              "support, not a single source table")
        elif not info["tables"]:
            info["reason"] = "no named table relation found on the connection"
        elif len(info["tables"]) > 1:
            info["reason"] = ("%d-table data model -- replicating it as real "
                              "Snowflake objects is the data-model view path, "
                              "not a single-table rebind" % len(info["tables"]))
        else:
            info["bindable"] = True
        out[cap] = info
    return out


def _enc_field(f):
    if not f:
        return None
    return {"caption": f["caption"], "kind": f["kind"], "agg": f.get("agg")}


def _enc_measure(f, meas):
    if f and f["kind"] == "measure":
        return {"caption": f["caption"], "agg": f["agg"]}
    if meas:
        return {"caption": meas[0]["caption"], "agg": meas[0]["agg"]}
    return {"caption": f["caption"] if f else "Sales", "agg": "sum"}


def _device_zone_ids(d):
    """Zones under <devicelayout> (Phone/Tablet variants). Excluded from the
    default layout scan: phone-only zones would leak sheets/geometry into the
    desktop rendering."""
    return {id(z) for z in d.findall(".//devicelayout//zone")}


def _zone_geometry(d, sheet_names, skip_ids=frozenset()):
    """First (desktop) worksheet zone per sheet -> {name: {x,y,w,h,show_title}}."""
    geom = {}
    for z in d.findall(".//zone"):
        if id(z) in skip_ids:
            continue
        n = z.get("name")
        if not n or n not in sheet_names or n in geom:
            continue
        if z.get("type-v2") or z.get("param") or z.get("x") is None:
            continue   # skip filter/color/legend/flow container zones
        try:
            x, y, w, h = int(z.get("x")), int(z.get("y")), int(z.get("w")), int(z.get("h"))
        except (TypeError, ValueError):
            continue
        if w <= 0 or h <= 0:
            continue
        geom[n] = {"x": x, "y": y, "w": w, "h": h,
                   "show_title": z.get("show-title") != "false"}
    return geom


def _zone_bg(z):
    zs = z.find("zone-style")
    if zs is None:
        return None
    for f in zs.findall("format"):
        if f.get("attr") == "background-color":
            return f.get("value")
    return None


def layout_tree(d, sheet_names, skip_ids):
    """Dashboard zone hierarchy -> nested layout tree the engine renders
    directly (containers WITH their background styles). Nodes:
      {"dir": "horz"|"vert", "bg": "#rrggbb"|None, "w": int, "children": [...]}
      {"sheet": name, "w": int, "h": int}
    Chrome-only branches (nav images, text, empty spacers) prune away; flow
    nodes with a single child collapse (outermost background wins). Returns
    None when the dashboard has no sheet-bearing zone tree."""
    seen = set()

    def _rows_from_geometry(pairs, bg):
        """A `layout-basic` container positions its children by ABSOLUTE x/y,
        not a flow direction -- so a bare vertical stack drops Tableau's
        multi-column rows to one column (Regional Analysis View2: 'Region level
        Sales' + 'Profit by Category' sit at the same y, side by side, and
        rendered stacked). Reconstruct rows: group children whose vertical
        bands overlap into a horz row ordered by x, then stack the bands by y."""
        items = []
        for cz, node in pairs:
            try:
                y0 = int(cz.get("y") or 0)
                h = int(cz.get("h") or 0)
                x0 = int(cz.get("x") or 0)
                w = int(cz.get("w") or 1)
            except (TypeError, ValueError):
                return None                      # missing geometry -> caller falls back
            items.append({"x0": x0, "y0": y0, "y1": y0 + h, "w": w, "node": node})
        items.sort(key=lambda it: (it["y0"], it["x0"]))
        rows = []
        for it in items:
            if rows:
                ry0 = min(r["y0"] for r in rows[-1])
                ry1 = max(r["y1"] for r in rows[-1])
                # strict interval overlap (touching edges y1==y0 do NOT overlap)
                if it["y0"] < ry1 and it["y1"] > ry0:
                    rows[-1].append(it)
                    continue
            rows.append([it])
        row_nodes = []
        for row in rows:
            row.sort(key=lambda it: it["x0"])
            if len(row) == 1:
                row_nodes.append(row[0]["node"])
            else:
                row_nodes.append({"dir": "horz", "bg": None,
                                  "w": sum(it["w"] for it in row),
                                  "h": max((it["node"].get("h") or 0) for it in row),
                                  "children": [it["node"] for it in row]})
        if len(row_nodes) == 1:
            node = row_nodes[0]
            if bg and "sheet" not in node and not node.get("bg"):
                node["bg"] = bg
            elif bg and "sheet" in node:
                node = {"dir": "vert", "bg": bg, "w": node["w"], "children": [node]}
            return node
        return {"dir": "vert", "bg": bg,
                "w": max(rn.get("w") or 1 for rn in row_nodes),
                "h": sum(rn.get("h") or 0 for rn in row_nodes),
                "children": row_nodes}

    def build(z):
        if id(z) in skip_ids:
            return None
        t = z.get("type-v2") or z.get("type") or ""
        nm = z.get("name")
        # A zone is the actual worksheet ONLY when it carries no type: Tableau
        # gives a chart zone a bare <zone name='Sheet'>, while the filter widget,
        # color legend, and highlighter bound to that sheet REUSE its name but
        # carry type-v2='filter'|'color'|'highlighter' (corpus: 182 real sheet
        # zones have no type; 143 chrome zones reuse a sheet name). Treating a
        # legend as the sheet stole ProductDetails' identity (a 10227-wide legend
        # replaced the full-width chart) and dropped the real sheet as a dup.
        if nm and nm in sheet_names and not t:
            if nm in seen:
                return None
            seen.add(nm)
            return {"sheet": nm, "w": int(z.get("w") or 1), "h": int(z.get("h") or 1)}
        child_pairs = []
        for k in z.findall("zone"):
            kn = build(k)
            if kn:
                child_pairs.append((k, kn))
        kids = [kn for _, kn in child_pairs]
        if not kids:
            return None
        bg = _zone_bg(z)
        if len(kids) == 1:
            child = kids[0]
            if bg and not child.get("bg") and "sheet" not in child:
                child["bg"] = bg
            elif bg and "sheet" in child:
                child = {"dir": "vert", "bg": bg, "w": child["w"],
                         "children": [child]}
            return child
        # ABSOLUTE-positioning container: reconstruct rows from x/y geometry
        # (a layout-flow already encodes its direction via param, so only
        # layout-basic needs this). Tableau nests flow containers inside a
        # basic canvas, so this fires exactly where a stack would be wrong.
        if t == "layout-basic" and z.get("param") != "horz":
            geo = _rows_from_geometry(child_pairs, bg)
            if geo is not None:
                return geo
        # merge CONSECUTIVE same-background container children of a VERT
        # flow: Tableau splits one visual panel into stacked zones with
        # spacer zones between (pruned above) -- separate cards broke the
        # panel into fragments. NEVER merge across a horz row's columns.
        if z.get("param") != "horz":
            merged = []
            for k in kids:
                prev = merged[-1] if merged else None
                if (prev is not None and "sheet" not in k
                        and prev and "sheet" not in prev
                        and k.get("bg") and k.get("bg") == prev.get("bg")):
                    prev["children"] = (prev.get("children", [])
                                        + k.get("children", []))
                    prev["h"] = (prev.get("h") or 0) + (k.get("h") or 0)
                else:
                    merged.append(dict(k))
            kids = merged
        if len(kids) == 1:
            child = kids[0]
            if bg and not child.get("bg") and "sheet" not in child:
                child["bg"] = bg
            return child
        return {"dir": "horz" if z.get("param") == "horz" else "vert",
                "bg": bg, "w": int(z.get("w") or 1),
                "h": int(z.get("h") or 0), "children": kids}

    zones = d.find("zones")
    if zones is None:
        return None
    roots = [build(z) for z in zones.findall("zone")]
    roots = [r for r in roots if r]
    if not roots:
        return None
    return roots[0] if len(roots) == 1 else {"dir": "vert", "bg": None,
                                             "w": 1, "children": roots}


def zone_controls(d, meta, param_alias=None, skip_ids=frozenset()):
    """The dashboard's OWN control surface, read from the XML.

    Tableau declares EXACTLY which controls sit on the canvas:
        <zone type='filter'    param='[ds].[yr:Order Date:ok]'>
        <zone type='paramctrl' param='[Parameters].[Parameter 3]'>
    Anything not placed here has NO control in Tableau -- a parameter that
    exists in the datasource but is never placed (or never used on a sheet)
    is invisible to the user.

    We used to ignore these zones entirely and synthesise a control surface
    from the UNION of every sheet's internal filters plus EVERY declared
    parameter. On Fil Test that showed 6 filters + 3 parameters + a drill
    selector where Tableau shows 2 filters + 1 parameter -- and missed the
    one filter (Year of Order Date) that actually matters. Read, don't guess.
    """
    filters, params = [], []
    seen = set()
    for z in d.findall(".//zone"):
        if id(z) in skip_ids:
            continue                     # device-layout duplicate of a zone
        t = (z.get("type-v2") or z.get("type") or "").lower()
        p = z.get("param") or ""
        if t == "filter" and p:
            f = parse_field(p, meta)
            key = (f["caption"], f.get("datepart"))
            if key in seen:
                continue
            seen.add(key)
            filters.append({"caption": f["caption"],
                            "datepart": f.get("datepart"),
                            # the worksheet this filter zone is BOUND to (Tableau
                            # writes the source sheet's name on the filter zone).
                            # The dashboard filter applies to THIS sheet (plus any
                            # sheet that filters on the same field in its own XML)
                            # -- NOT to every sheet that merely has the column.
                            # Without this, a Region quick-filter bound to one
                            # chart bled onto a parameter-driven chart on the same
                            # datasource and AND'd with its parameter -> blank.
                            "scope_sheet": z.get("name"),
                            "kind": ("date_part" if (f["is_date"] and f.get("datepart"))
                                     else "date" if f["is_date"] else "categorical")})
        elif t == "paramctrl" and p:
            internal = p.split("].[")[-1].strip("[]")
            cap = (param_alias or {}).get(internal, internal)
            if cap not in params:
                params.append(cap)
    return filters, params


def worksheet_shown_params(root, param_alias=None):
    """Worksheet name -> [parameter captions Tableau shows as controls ON that
    sheet]. A DASHBOARD places a parameter as a <zone type='paramctrl'>
    (zone_controls reads those); a standalone WORKSHEET instead records a shown
    parameter control as a <card type='parameter' param='[Parameters].[<internal>]'>
    inside its <window>. Without reading these cards, a parameter a worksheet tab
    shows in Tableau falls through to the app's GLOBAL sidebar (via
    engine._param_is_live) instead of rendering on its own tab, so the tab looks
    like it's 'missing' the control the user sees in Tableau. Read, don't guess --
    same discipline as zone_controls."""
    param_alias = param_alias or {}
    out = {}
    for w in root.findall(".//windows/window"):
        if w.get("class") != "worksheet" or w.get("hidden"):
            continue
        caps = []
        for card in w.iter("card"):
            if card.get("type") != "parameter":
                continue
            internal = (card.get("param") or "").split("].[")[-1].strip("[]")
            if not internal:
                continue
            cap = param_alias.get(internal, internal)
            if cap not in caps:
                caps.append(cap)
        if caps:
            out[w.get("name")] = caps
    return out


def dashboards(root, meta=None, param_alias=None):
    out = []
    for d in root.findall(".//dashboards/dashboard"):
        if d.get("type") == "storyboard":
            continue                    # stories: detected, reported, not converted
        t = d.find(".//title")
        title = " ".join("".join(t.itertext()).split()) if t is not None else d.get("name")
        if not title or title.startswith("<"):
            title = d.get("name")
        dz = _device_zone_ids(d)
        sheets = []
        for z in d.findall(".//zone"):
            n = z.get("name")
            if id(z) not in dz and n and n not in sheets:
                sheets.append(n)
        entry = {"name": d.get("name"), "title": title, "sheet_names": sheets,
                 "geom": _zone_geometry(d, sheets, dz),
                 "layout": layout_tree(d, set(sheets), dz)}
        if meta is not None:
            zf, zp = zone_controls(d, meta, param_alias, dz)
            entry["zone_filters"], entry["zone_params"] = zf, zp
        # device layouts: rendered as ONE responsive desktop layout; names
        # captured so the drop is reported, never silent
        dls = [dl.get("name") for dl in d.findall(".//devicelayout")
               if dl.get("name")]
        if dls:
            entry["device_layouts"] = sorted(set(dls))
        out.append(entry)
    return out


def extract_calcs_params(root, meta):
    """Return (calc_defs, params, aliases, param_info).
    calc_defs  {caption: formula};  params {caption AND internal: value};
    aliases    {internal: formula}  (nested refs use internal names);
    param_info {'captions': {caption: value}, 'alias': {internal: caption},
               'domains': {caption: [allowed values]}  (list-domain params)}."""
    calc_defs, params, aliases = {}, {}, {}
    param_caps, param_alias, param_domains = {}, {}, {}
    for col in root.findall(".//column"):
        nm = (col.get("name") or "").strip("[]")
        cap = col.get("caption") or nm
        if col.get("param-domain-type") is not None:
            params[cap] = col.get("value")
            param_caps[cap] = col.get("value")
            # list-domain parameters carry their allowed values as <members>;
            # captured so the control is a DROPDOWN like Tableau's, not a
            # free-text box the user can put anything into
            mem = [m.get("value") for m in col.findall("./members/member")
                   if m.get("value") is not None]
            if mem:
                param_domains[cap] = mem
            if nm and nm != cap:
                params[nm] = col.get("value")   # formulas use internal names too
                param_alias[nm] = cap
            continue
        c = col.find("calculation")
        if c is not None and c.get("class") == "tableau" and c.get("formula"):
            calc_defs.setdefault(cap, c.get("formula"))
            if nm and nm != cap:
                aliases.setdefault(nm, c.get("formula"))
    return calc_defs, params, aliases, {"captions": param_caps, "alias": param_alias,
                                        "domains": param_domains}


def translate_calcs(calc_defs, params, aliases=None, param_alias=None, colmap=None,
                    name2cap=None):
    """Translate every calc; return (translated, dropped). Window (table-calc)
    translations carry window=True -- the engine computes them post-aggregation.
    `aliases` (internal name -> formula) resolves nested internal-name refs;
    `colmap` (caption -> source column) resolves workbook RENAMES in formulas."""
    from calc_translator import translate_formula, WIN_ORDER
    merged = dict(aliases or {})
    merged.update(calc_defs)           # captions win on collision
    out, dropped = {}, {}
    for cap, formula in calc_defs.items():
        sql, agg_ready = translate_formula(formula, params, merged,
                                           param_alias=param_alias, colmap=colmap)
        if sql:
            entry = {"sql": sql, "agg_ready": agg_ready}
            if WIN_ORDER in sql:
                entry["window"] = True
            out[cap] = entry
        else:
            dropped[cap] = formula
    # register under the INTERNAL name too (sets lesson: shelves and color
    # encodings reference [Calculation_...], not the caption). Resolution
    # goes through the XML's OWN internal->caption mapping -- formula-text
    # matching mis-bound '(copy)' calcs that share a formula with a
    # different-caption calc (rank ordering silently used the wrong measure).
    for nm, cap in (name2cap or {}).items():
        base = nm.strip("[]")
        if base not in out and cap in out:
            out[base] = out[cap]
    # fallback for aliases the meta didn't caption (rare)
    by_formula = {}
    for cap, formula in calc_defs.items():
        if cap in out:
            by_formula.setdefault(formula, cap)
    for nm, formula in (aliases or {}).items():
        if nm not in out and formula in by_formula:
            out[nm] = out[by_formula[formula]]
    return out, dropped


def _bucket_value(text):
    """A style-rule <bucket> literal -> plain value ('"Shipped Early"' ->
    Shipped Early; '"50-75\\%"' -> 50-75%; true -> true)."""
    v = (text or "").strip().strip('"')
    return v.replace("\\%", "%")


def extract_palette_refs(root, meta):
    """Categorical color encodings that reference a NAMED palette without
    explicit per-value maps: {field caption: palette name}. Tableau assigns
    palette colors in legend order; the engine mirrors that."""
    out = {}
    for enc in root.findall(".//style-rule[@element='mark']/encoding[@attr='color']"):
        tok = enc.get("field") or ""
        if ":Measure Names" in tok or enc.findall("map"):
            continue
        if enc.get("type") == "interpolated" or enc.get("min") is not None:
            continue                     # continuous scale, not categorical
        pal = enc.get("palette")
        if not pal:
            continue
        f = parse_field(tok, meta)
        if f["kind"] == "dimension":
            out.setdefault(f["caption"], pal)
    return out


def extract_color_maps(root, meta):
    """Tableau's EXACT per-value mark colors: {field caption: {value: #hex}}.
    This is what makes Ship Status render blue/brown/grey like the original."""
    out = {}
    for enc in root.findall(".//style-rule[@element='mark']/encoding[@attr='color']"):
        tok = enc.get("field") or ""
        if ":Measure Names" in tok:
            continue                     # per-measure colors handled per sheet
        f = parse_field(tok, meta)
        m = out.setdefault(f["caption"], {})
        for mp in enc.findall("map"):
            b = mp.find("bucket")
            if b is None or b.text is None or not mp.get("to"):
                continue
            m[_bucket_value(b.text)] = mp.get("to")
        if not m:
            out.pop(f["caption"], None)
    return out


def measure_colors(root, meta):
    """Per-measure mark colors from the Measure Names color encodings (these
    live at DATASOURCE level, workbook-wide): {(caption, agg): #hex}."""
    out = {}
    for enc in root.findall(".//style-rule[@element='mark']/encoding[@attr='color']"):
        if ":Measure Names" not in (enc.get("field") or ""):
            continue
        for mp in enc.findall("map"):
            b = mp.find("bucket")
            if b is None or b.text is None or not mp.get("to"):
                continue
            tok = (b.text or "").strip().strip('"')
            # skip table-calc pills (rank:/pcto: belong to other sheets) and
            # black text-mark colors (text tables color TEXT, not bars)
            if "[rank:" in tok or ":rank:" in tok or "[pcto:" in tok or ":pcto:" in tok:
                continue
            if mp.get("to", "").lower() in ("#000000", "#000"):
                continue
            f = parse_field(tok, meta)
            out.setdefault((f["caption"], f["agg"]), mp.get("to"))
    return out


def _windowize_aggs(sql, partition):
    """Turn every aggregate call in `sql` into a window over `partition`
    (SUM(SALES) -> SUM(SALES) OVER (PARTITION BY STATE)). Paren-matched, so
    nested expressions inside the aggregate are safe."""
    import re as _r
    out, i, n = [], 0, len(sql)
    pat = _r.compile(r"\b(SUM|AVG|MIN|MAX|COUNT|MEDIAN|STDDEV|VARIANCE)\s*\(", _r.I)
    while i < n:
        m = pat.search(sql, i)
        if not m:
            out.append(sql[i:])
            break
        start, open_p = m.start(), m.end() - 1
        depth, j = 0, open_p
        while j < n:
            if sql[j] == "(":
                depth += 1
            elif sql[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        out.append(sql[i:j + 1])
        out.append(f" OVER (PARTITION BY {partition})")
        i = j + 1
    return "".join(out)


def extract_sets(root, calc_defs=None, params=None):
    """User SETS -> In/Out CASE SQL, keyed by caption.
    * conditional sets (filter expression over a level, e.g. sum(Sales)>50000
      per State) -> aggregate-level CASE (correct when grouped by the level;
      engine treats agg_ready dims as SELECT-only buckets).
    * static member-list sets -> row-level CASE IN (...) THEN 'In' ELSE 'Out'."""
    from calc_translator import translate_formula, to_phys
    out = {}
    for g in root.findall(".//datasource/group"):
        nm = (g.get("name") or "").strip("[]")
        if "Action (" in nm or "Measure Names" in nm or g.get("hidden") == "true":
            continue
        cap = g.get("caption") or nm
        cond = g.find("groupfilter[@function='filter']")
        if cond is not None and cond.get("expression"):
            sql, _ = translate_formula(cond.get("expression"), params, calc_defs)
            lvl_el = cond.find("groupfilter[@function='level-members']")
            lvl = (lvl_el.get("level") or "").strip("[]") if lvl_el is not None else None
            if sql and lvl:
                # Tableau evaluates set membership PER LEVEL over the whole
                # (filtered) data, independent of the viz grain -> window
                # aggregates partitioned by the level, applied row-level.
                win = _windowize_aggs(sql, to_phys(lvl))
                entry = {"sql": f"CASE WHEN {win} THEN 'In' ELSE 'Out' END",
                         "agg_ready": False, "set_level": lvl}
                out[cap] = entry
                if nm != cap:
                    out[nm] = entry      # shelves reference the internal name
            continue
        members = [(m.get("member") or "").strip().strip('"')
                   for m in g.findall(".//groupfilter[@function='member']")]
        members = [m for m in members if m and not FIELD_RE.search(m)]
        lvl_el = g.find(".//groupfilter[@function='level-members']")
        base = (lvl_el.get("level") or "").strip("[]") if lvl_el is not None else None
        if members and base:
            inlist = ", ".join("'" + m.replace(chr(39), chr(39)*2) + "'" for m in members)
            entry = {"sql": f"CASE WHEN {to_phys(base)} IN ({inlist}) "
                            f"THEN 'In' ELSE 'Out' END", "agg_ready": False}
            out[cap] = entry
            if nm != cap:
                out[nm] = entry          # shelves reference the internal name
    return out


def extract_groups(root):
    """User GROUPS (categorical-bin columns: 'Illinois & Indiana' from member
    lists) -> row-level CASE SQL, keyed by caption. Ungrouped members keep
    their own value unless the group uses 'Other'."""
    from calc_translator import to_phys
    out = {}
    for col in root.findall(".//column"):
        calc = col.find("calculation")
        if calc is None or calc.get("class") != "categorical-bin":
            continue
        cap = col.get("caption") or (col.get("name") or "").strip("[]")
        if cap in out:
            continue
        base = to_phys((calc.get("column") or "").strip("[]"))
        whens = []
        for b in calc.findall("bin"):
            name = (b.get("value") or "").strip().strip('"').replace("'", "''")
            vals = [(v.text or "").strip().strip('"').replace("'", "''")
                    for v in b.findall("value")]
            vals = [v for v in vals if v]
            if name and vals:
                inlist = ", ".join(f"'{v}'" for v in vals)
                whens.append(f"WHEN {base} IN ({inlist}) THEN '{name}'")
        if not whens:
            continue
        other = "'Other'" if (calc.get("use-other") or "").lower() == "true" else base
        out[cap] = "CASE " + " ".join(whens) + f" ELSE {other} END"
    return out


def extract_aliases(root):
    """Tableau value aliases: {field caption: {raw value: display label}}.
    Generic replacement for hand-written label maps (e.g. Order Profitable?
    false->Unprofitable). Field-reference keys (Measure Names) are skipped."""
    out = {}
    for col in root.findall(".//column"):
        al = col.find("aliases")
        if al is None:
            continue
        cap = col.get("caption") or (col.get("name") or "").strip("[]")
        m = out.setdefault(cap, {})
        for a in al.findall("alias"):
            k, v = a.get("key"), a.get("value")
            if k is None or v is None or FIELD_RE.search(k):
                continue
            m[k.strip().strip('"')] = v
        if not m:
            out.pop(cap, None)
    return out


def datasource_map(root):
    """federated id -> datasource caption."""
    out = {}
    for ds in root.findall("./datasources/datasource"):
        nm = ds.get("name")
        if nm and nm != "Parameters":
            out[nm] = ds.get("caption") or nm
    return out


def build_ir(path, only=None):
    root = load_twb_xml(path)
    meta, _ = column_meta(root)
    dsmap = datasource_map(root)
    # caption -> SOURCE column name, for fields the workbook RENAMED. Needed
    # BEFORE calc translation: formulas reference renamed fields too.
    colmap = {}
    for nm, info in meta.items():
        base = nm.strip("[]")
        cap = info.get("caption")
        if cap and cap != base and INTERNAL_OBJ not in nm:
            colmap.setdefault(cap, base)

    calc_defs, params, aliases, param_info = extract_calcs_params(root, meta)
    name2cap = {nm: info.get("caption") for nm, info in meta.items()
                if info.get("caption")}
    calcs, calc_drops = translate_calcs(calc_defs, params, aliases,
                                        param_info["alias"], colmap, name2cap)
    # user GROUPS resolve like row-level calcs (rdim picks them up by caption)
    for cap, sql in extract_groups(root).items():
        calcs.setdefault(cap, {"sql": sql, "agg_ready": False})
    # user SETS resolve as In/Out calc dimensions
    merged_defs = dict(aliases); merged_defs.update(calc_defs)
    for cap, entry in extract_sets(root, merged_defs, params).items():
        calcs.setdefault(cap, entry)
    all_ws = {w.get("name"): w for w in root.findall(".//worksheets/worksheet")}

    dash_out = []
    used_sheets = set()
    for d in dashboards(root, meta, param_info.get("alias")):
        if only and d["name"] != only:
            continue
        specs, filters = [], []
        for n in d["sheet_names"]:
            if n not in all_ws:
                continue
            used_sheets.add(n)
            s = infer(all_ws[n], meta, dsmap, params)
            s["geom"] = d.get("geom", {}).get(n)
            specs.append(s)
            for f in s.get("filters", []):
                if not any(x["caption"] == f["caption"] for x in filters):
                    filters.append(f)
        # INTERACTIVE controls = what the dashboard PLACED (zone_filters), not
        # the union of every sheet's internal filters. Sheet-level filters are
        # still applied per sheet via applied_filters -- they are just not
        # widgets. Falls back to the union only for a dashboard with no zone
        # controls at all (older/partial XML), so nothing regresses silently.
        placed = d.get("zone_filters")
        entry = {"name": d["name"], "title": d["title"],
                 "filters": placed if placed is not None else filters,
                 "sheet_filters": filters,
                 "params": d.get("zone_params") or [],
                 "sheets": specs}
        if d.get("device_layouts"):
            entry["device_layouts"] = d["device_layouts"]
        if d.get("layout"):
            entry["layout"] = d["layout"]
        dash_out.append(entry)

    # STANDALONE worksheets shown as tabs (worksheet windows not consumed by
    # any dashboard) -> one pseudo-dashboard each, the way Tableau tabs them.
    # A worksheet tab renders its OWN shown parameter controls in a control row
    # (like a dashboard's placed params), read from the sheet's <window> cards
    # -- otherwise those params fall through to the global sidebar instead of
    # appearing on the tab where Tableau shows them (Superstore's What If
    # Forecast: New Business Growth + Churn Rate).
    if not only:
        ws_params = worksheet_shown_params(root, param_info.get("alias"))
        windowed = [w.get("name") for w in root.findall(".//windows/window")
                    if w.get("class") == "worksheet" and not w.get("hidden")]
        for n in windowed:
            if n in used_sheets or n not in all_ws:
                continue
            s = infer(all_ws[n], meta, dsmap, params)
            dash_out.append({"name": n, "title": s.get("title") or n,
                             "filters": s.get("filters", []),
                             "params": ws_params.get(n, []), "sheets": [s]})

    stories = [d.get("name") for d in root.findall(".//dashboards/dashboard")
               if d.get("type") == "storyboard"]

    ds_notes = datasource_notes(root)

    # hierarchies -> drill-level selector on sheets whose AXIS dimension is a
    # hierarchy level (color-only membership doesn't get a selector: swapping
    # a color legend level without the axis reads as a different chart)
    hiers = drill_hierarchies(root, meta)
    if hiers:
        def _dim_cap(f):
            cap = f.get("caption") if isinstance(f, dict) else None
            if not cap or f.get("agg") in AGGS or f.get("datepart"):
                return None
            return cap
        for d in dash_out:
            for s in d["sheets"]:
                # scalar dim slots first; then dim LISTS (heatmap rows, nested
                # circle dims) where the DEEPEST hierarchy level is the one
                # the selector swaps
                cands = [(k, None, _dim_cap(s.get(k)))
                         for k in ("x", "y", "dim", "panel")]
                for lk in ("y_dims", "x_dims"):
                    for i, f in enumerate(s.get(lk) or []):
                        cands.append((lk, i, _dim_cap(f)))
                best = None
                for key, idx, cap in cands:
                    hit = next((nm for nm, lv in hiers.items()
                                if cap in lv), None) if cap else None
                    if hit and (best is None
                                or hiers[hit].index(cap) > best[3]):
                        best = (key, idx, cap, hiers[hit].index(cap), hit)
                if best:
                    key, idx, cap, _, hit = best
                    s["drill"] = {"name": hit, "levels": hiers[hit],
                                  "current": cap, "slot": key, "slot_idx": idx}

    # workbook-wide per-measure colors -> attach to every measure-panel spec
    mc = measure_colors(root, meta)
    if mc:
        for d in dash_out:
            for s in d["sheets"]:
                # ys = dual-axis series (was skipped -> Gross/Net rendered in
                # default palette instead of the workbook's red/grey)
                for m in (s.get("measures") or []) + (s.get("ys") or []):
                    c = mc.get((m["caption"], m.get("agg"))) or mc.get((m["caption"], None))
                    if c:
                        m["color"] = c

    return {"source_file": path, "calcs": calcs, "calc_drops": calc_drops,
            "datasources": sorted(set(dsmap.values())),
            "params": param_info["captions"],
            "param_domains": param_info.get("domains", {}), "stories": stories,
            "datasource_notes": ds_notes, "hierarchies": hiers,
            "live_connections": live_connections(root),
            "custom_sql_sources": custom_sql_sources(root),
            "source_tables": source_tables(root),
            "blends": blends(root),
            "colmap": colmap, "color_maps": extract_color_maps(root, meta),
            "palette_refs": extract_palette_refs(root, meta),
            "aliases": extract_aliases(root), "dashboards": dash_out}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("twb")
    ap.add_argument("--dashboard", default=None)
    ap.add_argument("-o", "--out", default="workbook_ir.json")
    a = ap.parse_args()
    ir = build_ir(a.twb, a.dashboard)
    json.dump(ir, open(a.out, "w"), indent=2)
    for d in ir["dashboards"]:
        print(f"# {d['name']}  ({d['title']})  filters={[f['caption'] for f in d['filters']]}")
        for s in d["sheets"]:
            print(f"    {s['name']:<24} -> {s['kind']}")
    print("->", a.out)


if __name__ == "__main__":
    main()
