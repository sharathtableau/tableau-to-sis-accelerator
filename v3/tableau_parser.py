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
import sys
import field_identity

FIELD_RE = re.compile(r"\[([^\]]+)\]\.\[([^\]]+)\]")
TOKEN_RE = re.compile(r"\[[^\]]+\](?:\.\[[^\]]+\])+")   # full dotted shelf token
DATE_PARTS = {"yr", "qr", "mn", "tmn", "tyr", "tqr", "twk", "wk", "tdy", "dy",
              "mdy", "md", "wd", "hr", "my"}
# coarse -> fine granularity rank, used to pick the finest date field on a shelf
DATEPART_RANK = {"yr": 0, "tyr": 0, "qr": 1, "tqr": 1, "mn": 2, "tmn": 2,
                 "wk": 3, "twk": 3, "tdy": 4, "dy": 4, "mdy": 4, "md": 4,
                 "wd": 4, "hr": 5, "my": 2}
AGGS = {"sum", "avg", "min", "max", "cnt", "ctd", "med", "stdev", "var"}
# 'io' = set membership (In/Out) shelf derivation
PREFIXES = DATE_PARTS | AGGS | {"none", "usr", "attr", "fVal", "pcto", "pcdf", "rank", "io"}
INTERNAL_OBJ = "__tableau_internal_object_id__"


_FCP = re.compile(r"^_\.fcp\.([^.]+)\.(true|false)\.\.\.(.+)$")


def normalize_feature_flags(root):
    """R47: Tableau 2024+ wraps elements in feature-flag tags: <_.fcp.Flag.true...relation> is what a current
    Tableau reads, <_.fcp.Flag.false...relation> the legacy twin older versions read. Every reader here expects the
    plain tags (as a current workbook without flags writes them), so the workbook is read the way Tableau reads it:
    a 'true' element takes its plain tag; a 'false' element is dropped when its flag has a 'true' element under the
    same parent (the legacy twin), else it takes its plain tag (it is then the only copy). In place; returns root."""
    if root is None:
        return root
    for parent in list(root.iter()):
        kids = list(parent)
        flagged = [(c, _FCP.match(c.tag)) for c in kids if isinstance(c.tag, str) and c.tag.startswith("_.fcp.")]
        if not flagged:
            continue
        live = {m.group(1) for c, m in flagged if m and m.group(2) == "true"}
        for c, m in flagged:
            if not m:
                continue
            if m.group(2) == "false" and m.group(1) in live:
                parent.remove(c)
            else:
                c.tag = m.group(3)
    if isinstance(root.tag, str):
        m = _FCP.match(root.tag)
        if m:
            root.tag = m.group(3)
    return root


def parse_xml(text):
    """A workbook / datasource XML string or bytes, read as Tableau reads it (feature flags resolved)."""
    return normalize_feature_flags(ET.fromstring(text))


def load_twb_xml(path):
    if path.lower().endswith(".twbx"):
        with zipfile.ZipFile(path) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".twb"))
            return normalize_feature_flags(ET.parse(io.BytesIO(z.read(name))).getroot())
    return normalize_feature_flags(ET.parse(path).getroot())


def column_meta(root, _scoped=False):
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
    if _scoped:
        by_ds = {}
        for did, scoped in field_identity.scoped_roots(root).items():
            local, _ = column_meta(scoped, _scoped=False)
            for nm, info in local.items():
                info.update(datasource_id=did, field_id=field_identity.field_id(did, nm))
            by_ds[did] = local
        parameters = root.find("./datasources/datasource[@name='Parameters']")
        if parameters is not None:
            by_ds['Parameters'] = column_meta(parameters, _scoped=False)[0]
        meta = field_identity.ScopedMetadata(meta, by_ds)
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
        if pfx in AGGS or pfx in DATE_PARTS or pfx in ("usr", "none", "attr", "fVal"):
            agg = pfx
    internal = "[" + base + "]"
    lookup = lambda key: meta.resolve(ds, key) if hasattr(meta, 'resolve') else meta.get(key, {})
    info = lookup(internal)
    if not info:                        # count-of-records object (datatype="table")
        info = lookup("[%s].[%s]" % (INTERNAL_OBJ, base))
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
            "internal": base, "ds": info.get('datasource_id', ds),
            **({k: info[k] for k in ('field_id', 'datasource_id') if k in info}),
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
    m.update({k: fld[k] for k in ('field_id', 'datasource_id', 'internal') if k in fld})
    if info.get("fmt"):
        m["tableau_format"] = info["fmt"]
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
    d.update({k: fld[k] for k in ('field_id', 'datasource_id', 'internal') if k in fld})
    if fld.get("count_records"):
        d["count_records"] = True
    if meta is not None:
        info = meta.get("[" + fld.get("internal", "") + "]", {})
        if info.get("fmt"):
            d["fmt"] = fmt_token(info.get("fmt"), info.get("datatype"))
            d["tableau_format"] = info["fmt"]
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


def measure_names_param(ws, param_alias=None):
    """Parameter caption driving a Measure Names filter set to Condition -> By
    formula (Tableau's genuinely native, no-calculated-field measure selector) --
    or None. Distinct from measure_names_from_filter's enumerated-members case
    (function='member'): this one is function='formula', e.g. Tableau stores
    '[Measure Names] = [Parameters].[Selected Measure]' as the groupfilter's
    formula attribute. The formula references the parameter by its INTERNAL
    name, not necessarily its caption -- param_alias (internal -> caption,
    from extract_calcs_params) resolves it the same way calc formulas do."""
    param_alias = param_alias or {}
    for f in ws.findall(".//filter"):
        col = f.get("column") or ""
        if ":Measure Names" not in col:
            continue
        for gf in f.findall(".//groupfilter"):
            if gf.get("function") != "formula":
                continue
            m = re.search(r"\[Parameters\]\.\[([^\]]+)\]", gf.get("formula") or "")
            if m:
                name = m.group(1)
                return param_alias.get(name, name)
    return None


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
    its measure via y-axis-name/x-axis-name and carries its own mark/color.

    The color must come from the pane itself.  A workbook-wide Measure Names
    palette is only a fallback: parameter-swap calculations commonly reuse the
    same caption on unrelated sheets, so a global first-match color can assign
    another sheet's palette to a dual axis.
    """
    out = {}
    for p in ws.findall(".//panes/pane"):
        ax = p.get("y-axis-name") or p.get("x-axis-name")
        m = p.find("mark")
        if ax and m is not None and (m.get("class") or "Automatic") != "Automatic":
            f = parse_field(ax, meta)
            color = next((fmt.get("value") for fmt in p.findall(".//format")
                          if fmt.get("attr") == "mark-color"
                          and (fmt.get("value") or "").startswith("#")), None)
            out[(f["caption"], f["agg"])] = {
                "mark": m.get("class").lower(), "color": color}
    return out


def pane_mark_sequence(ws, meta):
    """Ordered axis-specific pane marks, preserving duplicate measures.

    A Tableau sparkline commonly overlays the SAME measure twice (Area then
    Line). ``pane_marks`` is intentionally a lookup for ordinary dual axes,
    but a dict cannot represent two identical keys; the last pane used to
    overwrite the first and erase the area fill. This ordered companion is
    used when the positional shelf contains duplicate measure pills.
    """
    out = []
    for p in ws.findall(".//panes/pane"):
        ax = p.get("y-axis-name") or p.get("x-axis-name")
        m = p.find("mark")
        if not ax or m is None:
            continue                    # the first pane is the shared All card
        mark = m.get("class") or "Automatic"
        if mark == "Automatic":
            continue
        f = parse_field(ax, meta)
        color = next((fmt.get("value") for fmt in p.findall(".//format")
                      if fmt.get("attr") == "mark-color"
                      and (fmt.get("value") or "").startswith("#")), None)
        out.append({"caption": f["caption"], "agg": f["agg"],
                    "mark": mark.lower(), "color": color})
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


def _infer_core(ws, meta, param_alias=None):
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
    param_select = measure_names_param(ws, param_alias)
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
        if param_select:
            # native Measure Names filter conditioned on a parameter (Condition
            # -> By formula, no calculated field): the engine narrows `measures`
            # down to the ONE the parameter currently selects at render time.
            spec["param_select"] = param_select
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

    # ---- NUMERIC GANTT BAR (dimension + numeric start/size, no date) ----
    # Tableau also uses GanttBar as a compact list/selector: a constant
    # placeholder sits on the axis and Size carries the meaningful numeric
    # length.  If the generic multi-measure branch runs first, two duplicated
    # placeholder pills become two unrelated bar panels.  Preserve the
    # explicit mark's semantics and render the duration as one horizontal bar.
    if (mark in ("Gantt", "GanttBar") and dims and not dates
            and enc.get("size") and enc["size"].get("kind") == "measure"):
        cenc = enc.get("color")
        spec.update({"kind": "bar", "orient": "h",
                     "y": {"caption": dims[-1]["caption"]},
                     "x": _m(enc["size"], meta),
                     "color": _enc_field(cenc)})
        txt = enc.get("text")
        if txt and txt.get("kind") == "measure":
            spec["labels"] = True
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
                seq = pane_mark_sequence(ws, meta)
                duplicate_axis = len({(f["caption"], f["agg"])
                                      for f in multi}) == 1 and len(seq) >= 2
                base_mark = "bar" if kind == "dtbar" else kind
                for i, (f, ym) in enumerate(zip(multi, ys)):
                    pane = (seq[i] if duplicate_axis
                            else pm.get((f["caption"], f["agg"])) or {})
                    ym["mark"] = pane.get("mark", base_mark)
                    if pane.get("color"):
                        ym["color"] = pane["color"]
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
                 "support": {"status": "PARTIAL", "reason":
                             "No exact chart mapping selected; table approximation"},
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
            # C3: first-period / last-period are whole-period offsets from the anchor (anchor-date, else today)
            def _int(v):
                try:
                    return int(str(v).strip())
                except (TypeError, ValueError):
                    return None
            anchor = (f.get("anchor-date") or "").strip("#").strip() or None
            out.append({"caption": fld["caption"], "kind": "relative_date",
                        "period": (f.get("period-type") or "").lower() or None,
                        "range": f.get("range-type"),
                        "n": f.get("period-count") or f.get("n"),
                        "first": _int(f.get("first-period")), "last": _int(f.get("last-period")),
                        "anchor": anchor[:10] if anchor else None,
                        "include_null": str(f.get("include-null")).lower() == "true",
                        "context": is_context})
            continue
        if f.get("class") == "quantitative":
            # E06: included-values decides Nulls (in-range / non-null exclude them, in-range-or-null keeps them,
            # null keeps only them, all = no filter); a 'non-null' filter has no bounds and still filters.
            inc = (f.get("included-values") or "in-range").lower()
            if inc == "all":
                continue
            mn, mx = f.find("min"), f.find("max")
            if mn is not None or mx is not None or inc in ("non-null", "null"):
                # date-PART range (e.g. [yr:Year:qk] 2001..2012): bounds are
                # part numbers, NOT raw column values -- engine must EXTRACT().
                # measure=True: an aggregated measure (sum:/avg:/...) filters AFTER aggregation (HAVING).
                out.append({"caption": fld["caption"], "kind": "range",
                            "is_date": fld["is_date"],
                            "datepart": fld.get("datepart"),
                            "min": _scalar(mn.text) if mn is not None else None,
                            "max": _scalar(mx.text) if mx is not None else None,
                            "nulls": {"in-range-or-null": "include", "null": "only"}.get(inc, "exclude"),
                            "agg": fld["agg"], "measure": fld["kind"] == "measure",
                            "context": is_context})
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
            e = {"caption": fld["caption"], "kind": "not_in", "values": members, "context": is_context}
            if fld.get("datepart"):           # DSF: an excluded date part is a part number, like 'in'
                e["datepart"] = fld["datepart"]
            out.append(e)
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


def workbook_filters(root, meta):
    """DSF: filters that live outside every worksheet and still apply to it, by data source id:
      * "Apply to worksheets -> All using this data source" (<shared-views><shared-view name=DS><filter>): an
        ordinary filter on every worksheet of DS (scope 'all_using_datasource');
      * data source filters (<datasource><filter>, direct children): applied first in Tableau's order of
        operations, so carried as CONTEXT filters (scope 'datasource').
    Parsed with applied_filters, so every filter class it knows (members, except, ranges, dates) is covered."""
    out = {}

    def add(did, elems, scope, context):
        if not did or not elems:
            return
        local = meta.for_datasource(did) if hasattr(meta, 'for_datasource') else meta
        holder = ET.Element('holder')
        holder.extend(elems)
        for f in applied_filters(holder, local):
            f['scope'], f['from_datasource'] = scope, did
            if context:
                f['context'] = True
            out.setdefault(did, []).append(f)

    for sv in root.findall("./shared-views/shared-view"):
        add(sv.get("name"), sv.findall("filter"), 'all_using_datasource', False)
    for ds in root.findall("./datasources/datasource"):
        add(ds.get("name"), ds.findall("filter"), 'datasource', True)
    return out


def add_workbook_filters(spec, wide):
    """Append the workbook-wide filters of the sheet's data source (workbook_filters) to its applied_filters. A
    filter the worksheet itself sets on the same field (and date part) wins: Tableau shows one filter per field."""
    import copy
    extra = (wide or {}).get(spec.get('datasource_id')) or []
    if not extra:
        return spec
    have = {(f.get('caption'), f.get('datepart')) for f in spec.get('applied_filters') or []}
    for f in extra:
        key = (f.get('caption'), f.get('datepart'))
        if key not in have:
            spec.setdefault('applied_filters', []).append(copy.deepcopy(f))
            have.add(key)
    return spec


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


def sheet_datasource_id(ws, dsmap=None):
    """Primary datasource caption for a worksheet: prefer the federated id used
    on its shelves, else the first non-Parameters datasource-dependency."""
    dsmap = dsmap or {}
    texts = [(ws.find(".//table/" + w).text if ws.find(".//table/" + w) is not None else "") or ""
             for w in ("cols", "rows")]
    for t in texts:
        for token in TOKEN_RE.findall(t):
            did = re.findall(r'\[([^\]]+)\]', token)[0]
            if did in dsmap and did != 'Parameters':
                return did
    # no field on rows / columns (a pie, a text card): Tableau's primary is the first datasource the view lists
    for d in ws.findall(".//view/datasources/datasource"):
        if d.get("name") and d.get("name") != "Parameters":
            return d.get("name")
    for dep in ws.findall(".//datasource-dependencies"):
        d = dep.get("datasource")
        if d and d != "Parameters":
            return d
    return None


def sheet_datasource(ws, dsmap=None):
    did = sheet_datasource_id(ws, dsmap)
    return (dsmap or {}).get(did, did)


def infer(ws, meta, dsmap=None, params=None, param_alias=None):
    """Keep a failed worksheet in the dashboard as an explicit invalid slot."""
    try:
        return _infer_impl(ws, meta, dsmap, params, param_alias)
    except Exception as exc:
        return {"name": ws.get("name") or "(unnamed)", "kind": "unsupported",
                "support": {"status": "INVALID", "reason":
                            f"Worksheet extraction failed: {type(exc).__name__}: {exc}"}}


def _infer_impl(ws, meta, dsmap=None, params=None, param_alias=None):
    """Infer the chart, then attach title, sort and color-scale (all from XML)."""
    from ir_contract import KNOWN_MARKS
    did = sheet_datasource_id(ws, dsmap)
    if hasattr(meta, 'for_datasource'):
        meta = meta.for_datasource(did)
    spec = _infer_core(ws, meta, param_alias)
    if did:
        spec['datasource_id'] = did
    unknown_marks = sorted({m.get("class") for m in ws.findall(".//pane/mark")
                            if m.get("class") and m.get("class") not in KNOWN_MARKS})
    if unknown_marks:
        spec["kind"] = "unsupported"
        spec["support"] = {"status": "UNSUPPORTED", "reason":
                           "Unrecognized Tableau mark classes: " + ", ".join(unknown_marks)}
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
        # Keep parameter-driven titles as runtime placeholders. Concatenating
        # the formatted-text runs first reconstructs Tableau titles whose '<',
        # parameter token, and '>' are stored in separate XML runs.
        ttl = dynamic_text("".join(t.itertext()), param_alias or {})
        if not ttl:
            ttl = clean_title("".join(t.itertext()))
        if ttl:
            spec["title"] = ttl
        ft = t.find("formatted-text")
        if ft is not None:                  # release 26g: the title as Tableau formats it (sizes, lines, colours)
            runs = _text_runs(ft, param_alias or {})
            if runs:
                spec["title_runs"] = runs
            elif not "".join(ft.itertext()).strip():
                # Release 27 (F2): an authored EMPTY title -- Tableau draws a blank title line, never the sheet name
                spec["title"] = ""
                spec["title_runs"] = [{"text": " "}]

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
        # A measure placed on Tableau's Text shelf is a mark label even when
        # another text field (for example the row dimension rendered as
        # stars/shapes) appears first. Looking only at the primary encoding
        # caused otherwise identical KPI bars to lose their values depending
        # on Text-shelf order.
        if spec.get("kind") == "bar":
            measure_caps = {
                a.get("caption") for a in (spec.get("x"), spec.get("y"))
                if isinstance(a, dict) and a.get("agg") not in (None, "none")
            }
            if measure_caps.intersection(texts):
                spec["labels"] = True
    label_runs = customized_label_runs(ws, meta)
    if label_runs:
        # The label template is the visual contract for a KPI text mark. It
        # distinguishes the displayed value/delta/title from tooltip-only and
        # helper measures, and retains Tableau's authored color semantics.
        spec["label_runs"] = label_runs
    number_formats = worksheet_number_formats(ws, meta)
    if number_formats:
        for key in ("x", "y"):
            value = spec.get(key)
            if isinstance(value, dict) and value.get("caption") in number_formats:
                value["tableau_format"] = number_formats[value["caption"]]
        for key in ("ys", "measures"):
            for value in spec.get(key) or []:
                if (isinstance(value, dict)
                        and value.get("caption") in number_formats):
                    value["tableau_format"] = number_formats[value["caption"]]
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
    chrome = axis_chrome(ws, meta)
    if chrome.get("titles"):
        # an axis title the author TYPED ('$ Sales') beats any name we could
        # derive for the field -- put it straight on the measure ref
        for key in ("x", "y"):
            v = spec.get(key)
            if isinstance(v, dict) and v.get("caption") in chrome["titles"]:
                v["label"] = chrome["titles"][v["caption"]]
        for lst in ("ys", "measures"):
            for v in spec.get(lst) or []:
                if isinstance(v, dict) and v.get("caption") in chrome["titles"]:
                    v["label"] = chrome["titles"][v["caption"]]
    if chrome.get("hide_axis"):
        spec["hide_axis"] = chrome["hide_axis"]
    if chrome.get("hide_field_labels"):
        spec["hide_field_labels"] = chrome["hide_field_labels"]
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


def axis_chrome(ws, meta):
    """What the author did to the axes THEMSELVES, read from the worksheet's
    style rules -- three things a KPI-tile style bar chart routinely sets
    and we were ignoring, which is why a 119px Tableau tile with five bars
    and a value on each came back as a full chart with a redundant
    '0M 4M' axis and a vertical field title:

      titles            {field caption: text}  -- <style-rule element='axis'>
                        <format attr='title' value='$ Sales' field=...>: an
                        axis title the author typed over the field name.
      hide_axis         ['cols'|'rows']        -- <format attr='display'
                        value='false' scope='cols' field=...>: the value
                        axis (ticks, labels, title) is hidden entirely.
      hide_field_labels ['rows'|'cols']        -- <style-rule
                        element='worksheet'><format attr='display-field-labels'
                        value='false' scope='rows'>: the shelf's field name
                        (the 'Country' header over the row labels) is off.

    Found live 2026-09-10 on the real 'Sales Dashboard Ver 01' KPI tiles
    (all three set) and its Monthly / Scater axes (typed titles '$ Sales'
    and '# Quantity')."""
    out = {"titles": {}, "hide_axis": [], "hide_field_labels": []}
    for sr in ws.findall(".//style-rule[@element='axis']"):
        for fmt in sr.findall("format"):
            attr, val = fmt.get("attr"), fmt.get("value")
            fld, scope = fmt.get("field"), fmt.get("scope")
            if attr == "title" and val and fld:
                try:
                    cap = parse_field(fld, meta)["caption"]
                except Exception:
                    continue
                out["titles"][cap] = val.strip()
            elif attr == "display" and (val or "").lower() == "false" and scope:
                if scope not in out["hide_axis"]:
                    out["hide_axis"].append(scope)
    for sr in ws.findall(".//style-rule[@element='worksheet']"):
        for fmt in sr.findall("format"):
            if (fmt.get("attr") == "display-field-labels"
                    and (fmt.get("value") or "").lower() == "false"
                    and fmt.get("scope")
                    and fmt.get("scope") not in out["hide_field_labels"]):
                out["hide_field_labels"].append(fmt.get("scope"))
    return {k: v for k, v in out.items() if v}


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
        # A MULTI-TABLE datasource authored by real Tableau Desktop wraps its
        # table relations in a single <relation type='collection'> (verified
        # against the real E-Commerce workbook's own Tableau-authored XML);
        # only a SINGLE-table datasource puts its <relation type='table'>
        # directly under <connection> (verified against the real KPI Live
        # workbook). The R9 direct-child scan was confirmed against the
        # single-table shape ONLY, so a real multi-table live model fell
        # through to "no single named table relation found" -- an inaccurate
        # reason, not a wrong bind. Descend exactly one level through a
        # collection so both real shapes are read correctly. This does NOT
        # reopen the R9 bug: the object-graph's per-object relations live
        # under a sibling of <connection>, never inside it, so they are still
        # out of scope. Expanding only 'collection' (never 'join') also keeps
        # the legacy-join detection below intact.
        expanded = []
        for r in rel_scope:
            if r.get("type") == "collection":
                expanded.extend(r.findall("./relation"))
            else:
                expanded.append(r)
        rels = [r for r in expanded if r.get("type")]
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


def blend_fields(root):
    """EN41: per datasource, the display name Tableau links blends on -> the field's name (dimensions only). A
    <column> shows its caption (else its name); a column known only from the connection metadata (text / date /
    boolean) shows its local name. Tableau links a secondary to the primary automatically on dimensions with the
    same display name."""
    out = {}
    for ds in root.findall("./datasources/datasource"):
        name = ds.get("name")
        if not name or name == "Parameters":
            continue
        names = {}
        for rec in ds.findall(".//metadata-record[@class='column']"):
            local = (rec.findtext("local-name") or "").strip()
            if local.startswith("[") and local.endswith("]"):
                local = local[1:-1]
            kind = (rec.findtext("local-type") or "").strip()
            if local and "].[" not in local and kind in ("string", "date", "datetime", "boolean"):
                names.setdefault(local, local)
        for col in ds.findall("./column"):
            raw = col.get("name") or ""
            if not raw.startswith("[") or "].[" in raw or col.get("datatype") == "table":
                continue
            field = raw[1:-1]
            if col.get("caption") and names.get(field) == field:
                names.pop(field)                    # renamed: Tableau shows the caption only
            if col.get("role") == "dimension":      # blends link on dimensions
                names[col.get("caption") or field] = field
            else:
                names = {k: v for k, v in names.items() if v != field}
        out[name] = names
    return out


def story_points(root, meta=None):
    """V13: Tableau stories: [{'name', 'title', 'points': [{'caption', 'sheet', 'filters': [{'worksheet', 'caption',
    'values'}]}]}] -- each story point shows a dashboard or sheet (captured-sheet) with the categorical filters it
    captured (<capturedDeltas>) applied."""
    out = []
    for d in root.findall(".//dashboards/dashboard"):
        if d.get("type") != "storyboard":
            continue
        title = "".join(r.text or "" for r in d.findall("./layout-options/title/formatted-text/run")).strip()
        points = []
        for sp in d.findall(".//story-points/story-point"):
            filters = []
            for ws in sp.findall("./capturedDeltas/worksheet"):
                for flt in ws.findall("filter"):
                    if flt.get("class") != "categorical":
                        continue
                    col = flt.get("column") or ""
                    m = re.search(r"\[(?:none|[a-z]+):([^:\]]+):[a-z]+\]$", col)
                    if not m:
                        continue
                    vals = [re.sub(r'^"|"$', "", g.get("member") or "")
                            for g in flt.iter("groupfilter") if g.get("function") == "member"]
                    if vals:
                        filters.append({"worksheet": ws.get("name"), "caption": m.group(1), "values": vals})
            points.append({"caption": sp.get("caption") or sp.get("captured-sheet") or "",
                           "sheet": sp.get("captured-sheet"), "filters": filters})
        if points:
            out.append({"name": d.get("name"), "title": title or d.get("name"), "points": points})
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
        # Only a TOP-LEVEL relation is a standalone object -- a <relation
        # type='table'> nested inside another <relation> is one OPERAND of
        # that parent (a legacy <relation type='join'> tree's own member),
        # not a separate bindable table. FOUND LIVE: Globalsalesdashboard
        # .twbx pre-joins SAMPLE_SUPER_STORE_ORDERS/PEOPLE/RETURNS into one
        # already-resolved object via nested <relation type='join'> (real
        # shape: <connection><relation type='join'><clause/><relation
        # type='join'><clause/><relation type='table'.../><relation
        # type='table'.../></relation><relation type='table'.../></relation
        # ></connection>) -- the old `.//relation[@type='table']` scan
        # walked INTO both join levels and counted all 3 leaf tables, even
        # though each one still carries the SAME upstream `connection=`
        # attribute as a real top-level relation and so passed the filter
        # below. Reported as a misleading "3-table data model" for a
        # datasource that is actually ONE pre-joined object.
        parent_map = {child: parent for parent in ds.iter() for child in parent}

        def _is_top_level(rel):
            p = parent_map.get(rel)
            return p is None or p.tag != "relation"

        top_level_join = any(r.get("type") == "join" and _is_top_level(r)
                             for r in ds.findall(".//relation[@type='join']"))
        for rel in ds.findall(".//relation[@type='table']"):
            if rel.get("connection") not in upstream or not _is_top_level(rel):
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
        elif top_level_join and not info["tables"]:
            info["reason"] = ("single pre-joined object via a legacy <relation "
                              "type='join'> tree -- not a single physical table, "
                              "not currently bindable")
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
    return {"caption": f["caption"], "kind": f["kind"], "agg": f.get("agg"),
            **{k: f[k] for k in ('field_id', 'datasource_id', 'internal') if k in f}}


def _enc_measure(f, meas):
    if f and f["kind"] == "measure":
        return _m(f)
    if meas:
        return _m(meas[0])
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
        if id(z) in skip_ids or z.get("hidden-by-user") == "true":
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


def _zone_border(z):
    """Visible container border authored in the dashboard zone style."""
    zs = z.find("zone-style")
    if zs is None:
        return None
    values = {f.get("attr"): f.get("value") for f in zs.findall("format")}
    if values.get("border-style") in (None, "none"):
        return None
    try:
        width = int(values.get("border-width") or 0)
    except ValueError:
        width = 0
    if width <= 0:
        return None
    return {"color": values.get("border-color") or "#d9d9d9", "width": width}


_PARAM_TOKEN_RE = re.compile(
    r"<\s*\[Parameters\]\.\[([^\]]+)\]\s*>")
_FIELD_TOKEN_RE = re.compile(r"<\[[^\]]+\]\.\[([^\]]+)\]>")


def dynamic_text(value, param_alias):
    """Formatted Tableau text/title -> runtime-safe text with param tokens."""
    # Release 27 (F2): a '●' run is an authored glyph (coloured legend bullets in titles); keep it
    text = str(value or "").replace("Æ", " ")
    text = _PARAM_TOKEN_RE.sub(
        lambda m: "{param:%s}" % param_alias.get(m.group(1), m.group(1)), text)
    text = _FIELD_TOKEN_RE.sub(lambda m: m.group(1), text)
    return " ".join(text.split()).strip()


def customized_label_runs(ws, meta):
    """Capture a Tableau mark's authored text template as ordered runs.

    Text-shelf fields alone are insufficient for KPI sheets: helper measures
    can be on Tooltip while only a current value and one of two direction
    deltas appear in the label. The template is workbook metadata, not a name
    heuristic, and also carries author-selected positive/negative colors.
    """
    ft = ws.find(".//pane/customized-label/formatted-text")
    if ft is None:
        return []
    out = []
    for run in ft.findall("run"):
        raw = run.text or ""
        attrs = {key: run.get(key) for key in ("fontcolor", "fontsize", "bold", "italic", "fontname", "fontalignment")
                 if run.get(key) is not None}
        raw = _paragraph_marks(raw)
        cursor = 0
        for match in FIELD_RE.finditer(raw):
            text = raw[cursor:match.start()].replace("<", "").replace(">", "")
            if text:
                out.append(dict(attrs, text=text))
            out.append(dict(attrs, field=parse_field(match.group(0), meta)["caption"]))
            cursor = match.end()
        text = raw[cursor:].replace("<", "").replace(">", "")
        if text:
            out.append(dict(attrs, text=text))
    return out


def worksheet_number_formats(ws, meta):
    """Per-sheet field number formats, which override datasource defaults."""
    out = {}
    for fmt in ws.findall(".//style-rule/format"):
        if fmt.get("attr") != "text-format" or not fmt.get("field"):
            continue
        try:
            cap = parse_field(fmt.get("field"), meta)["caption"]
        except Exception:
            continue
        if fmt.get("value"):
            out[cap] = fmt.get("value")
    return out


def _zone_text(z, param_alias):
    """A dashboard text zone's content, with Tableau's dynamic tokens turned
    into runtime placeholders. Tableau writes a parameter reference as
    `<[Parameters].[Parameter 1]>` (the INTERNAL name); the engine
    substitutes `{param:<caption>}` with the parameter's CURRENT value at
    render time, so a heading like 'Countries - <Metric>' reads
    'Countries - Sales' and follows the dropdown, exactly as in Tableau.
    Returns None for an empty zone."""
    ft = z.find("formatted-text")
    if ft is None:
        return None
    return dynamic_text("".join(r.text or "" for r in ft.iter("run")),
                        param_alias) or None


def _has_placed(node):
    """A control, legend, title or text zone (anything the dashboard shows besides charts)."""
    if node is None:
        return False
    if any(k in node for k in ("control", "legend", "title", "text", "image", "nav", "toggle")):
        return True
    return any(_has_placed(k) for k in node.get("children", []))


def _has_sheet(node):
    if node is None:
        return False
    if "sheet" in node:
        return True
    return any(_has_sheet(k) for k in node.get("children", []))


LAYOUT_VERSION = 'zones/2'      # release 26c: every zone kept in place (controls, legends, text, title, blank, image)


HGAP = 1000     # a horizontal gap narrower than 1% of the dashboard is padding, not an empty column
               # (each Streamlit column adds its own gutter, so a padding column would cost more than it keeps)


def _box(z):
    """A zone's authored rectangle in Tableau units (dashboard = 100000 x 100000)."""
    out = {}
    for k in ("x", "y", "w", "h"):
        try:
            out[k] = int(z.get(k))
        except (TypeError, ValueError):
            pass
    return out


def layout_tree(d, sheet_names, skip_ids, param_alias=None, meta=None, windows=None):
    """Dashboard zone hierarchy -> nested layout tree the engine renders
    directly (containers WITH their background styles). Nodes:
      {"dir": "horz"|"vert", "bg": "#rrggbb"|None, "w": int, "children": [...]}
      {"sheet": name, "w": int, "h": int}
      {"text": "...", "w": int, "h": int}      -- a dashboard text zone
    Nav images and empty spacers prune away; TEXT zones are kept when they
    sit in a container that also holds a sheet (they are that card's
    heading -- 'Countries - <Metric>'), and dropped otherwise (a filter
    sidebar's labels have no widgets here to label). A zone Tableau marks
    hidden-by-user is not on the canvas at all and is skipped. Flow nodes
    with a single child collapse (outermost background wins). Returns None
    when the dashboard has no sheet-bearing zone tree."""
    seen = set()
    param_alias = param_alias or {}
    basic_pairs = {}         # id(node) -> (zone, node) children of a coordinate-positioned container

    def _partition(pairs, bg, region):
        """Release 26c: a `layout-basic` container (the dashboard canvas, and every container Tableau
        positions by coordinates, floating zones included) is rebuilt from its children's rectangles by
        guillotine cuts: a horizontal cut where no child crosses it stacks the parts; otherwise a vertical
        cut puts them side by side. Gaps between parts become spacers, so every zone keeps its authored
        x / y / width. Returns None when the children overlap (no clean cut) -- the caller then uses the
        row grouping below and the layout check reports what moved."""
        eps = 300

        def mk(prs):
            out = []
            for cz, node in prs:
                b = _box(cz)
                if not all(k in b for k in ("x", "y", "w", "h")):
                    return None
                out.append({"x0": b["x"], "y0": b["y"], "x1": b["x"] + b["w"], "y1": b["y"] + b["h"],
                            "node": node})
            return out
        items = mk(pairs)
        if items is None:
            return None

        def groups(its, a0, a1):
            its = sorted(its, key=lambda it: (it[a0], it[a1]))
            out, start, end = [], None, None
            for it in its:
                # tolerance for touching edges, never more than half of either zone (a tiny zone inside
                # another one overlaps it; it does not sit beside it)
                tol = min(eps, (it[a1] - it[a0]) / 2, (end - start) / 2) if out else eps
                if out and it[a0] < end - tol:
                    out[-1].append(it)
                    end = max(end, it[a1])
                else:
                    out.append([it])
                    start, end = it[a0], it[a1]
            return out

        def spacer(**kw):
            return dict({"blank": True, "w": 1, "h": 0, "_spacer": True}, **kw)

        def inside(o, c):
            return (o["x0"] >= c["x0"] - eps and o["x1"] <= c["x1"] + eps
                    and o["y0"] >= c["y0"] - eps and o["y1"] <= c["y1"] + eps)

        def cut(its, rx0, ry0, rx1, ry1):
            if len(its) == 1:
                it = its[0]
                left, right = it["x0"] - rx0, rx1 - it["x1"]
                if left > HGAP or right > HGAP:        # a zone narrower than its region keeps its x / width
                    row = ([spacer(w=left)] if left > HGAP else []) + [it["node"]] \
                        + ([spacer(w=right)] if right > HGAP else [])
                    node = {"x": rx0, "y": it["y0"], "dir": "horz", "bg": None, "w": rx1 - rx0,
                            "h": it["y1"] - it["y0"], "children": row}
                else:
                    node = it["node"]
                if it["y0"] - ry0 > eps:                # a zone below the top of its region keeps its y
                    node = {"x": rx0, "y": ry0, "dir": "vert", "bg": None, "w": rx1 - rx0,
                            "h": it["y1"] - ry0, "children": [spacer(h=it["y0"] - ry0), node]}
                return node
            rows = groups(its, "y0", "y1")
            if len(rows) > 1:
                kids, prev = [], ry0
                for g in rows:
                    top = min(it["y0"] for it in g)
                    if top - prev > eps:
                        kids.append(spacer(h=top - prev, w=rx1 - rx0))
                    bottom = max(it["y1"] for it in g)
                    kid = cut(g, rx0, top, rx1, bottom)
                    if kid is None:
                        return None
                    kids.append(kid)
                    prev = bottom
                return {"x": rx0, "y": ry0, "dir": "vert", "bg": None, "w": rx1 - rx0, "h": prev - ry0,
                        "children": kids}
            cols = groups(its, "x0", "x1")
            if len(cols) > 1:
                kids, prev = [], rx0
                for g in cols:
                    left = min(it["x0"] for it in g)
                    if left - prev > HGAP:
                        kids.append(spacer(w=left - prev))
                    right = max(it["x1"] for it in g)
                    kid = cut(g, left, ry0, right, ry1)
                    if kid is None:
                        return None
                    if "dir" in kid and not any(k in kid for k in ("x", "sheet")):
                        kid["w"] = right - left
                    kids.append(kid)
                    prev = right
                if rx1 - prev > HGAP:
                    kids.append(spacer(w=rx1 - prev))
                return {"x": rx0, "y": ry0, "dir": "horz", "bg": None, "w": rx1 - rx0,
                        "h": max(it["y1"] for it in its) - ry0, "children": kids}
            # overlapping zones (floating): a coordinate-positioned container that other zones float over is
            # opened up -- its children and the floating zones are placed together by their rectangles
            for it in its:
                sub = basic_pairs.get(id(it["node"]))
                if sub and any(o is not it and inside(o, it) for o in its):
                    expanded = mk(sub)
                    if expanded:
                        return cut([o for o in its if o is not it] + expanded, rx0, ry0, rx1, ry1)
            # overlapping zones (floating): an image or blank zone that other zones sit on is Tableau's
            # background layer -- it becomes the region's background and the zones on it are cut again
            backs = [it for it in its if ("image" in it["node"] or "blank" in it["node"])
                     and any(o is not it and inside(o, it) for o in its)]
            if backs:
                back = max(backs, key=lambda it: (it["x1"] - it["x0"]) * (it["y1"] - it["y0"]))
                rest = [it for it in its if it is not back]
                node = cut(rest, rx0, ry0, rx1, ry1)
                if node is None:
                    return None
                if "dir" not in node:
                    node = {"x": rx0, "y": ry0, "dir": "vert", "bg": None, "w": rx1 - rx0,
                            "h": ry1 - ry0, "children": [node]}
                if "image" in back["node"]:
                    node["backdrop"] = back["node"]
                return node
            # zones floating over another zone: the largest zone keeps the flow; the zones over it are drawn
            # at their own rectangles on top of it (Tableau's floating layer)
            base = max(its, key=lambda it: (it["x1"] - it["x0"]) * (it["y1"] - it["y0"]))
            over = [o for o in its if o is not base and o["x0"] < base["x1"] - eps and o["x1"] > base["x0"] + eps
                    and o["y0"] < base["y1"] - eps and o["y1"] > base["y0"] + eps]
            if not over:
                return None
            node = cut([it for it in its if not any(it is o for o in over)], rx0, ry0, rx1, ry1)
            if node is None:
                return None
            if "children" not in node:
                node = {"x": rx0, "y": ry0, "dir": "vert", "bg": None, "w": rx1 - rx0,
                        "h": ry1 - ry0, "children": [node]}
            node.setdefault("floating", []).extend(
                {"x": o["x0"], "y": o["y0"], "w": o["x1"] - o["x0"], "h": o["y1"] - o["y0"],
                 "rx": rx0, "ry": ry0, "rw": rx1 - rx0, "child": o["node"]} for o in over)
            return node

        rx0, ry0 = region.get("x", 0), region.get("y", 0)
        node = cut(items, rx0, ry0, rx0 + region.get("w", 100000), ry0 + region.get("h", 100000))
        if node is None:
            return None
        if bg:
            if "dir" in node and not node.get("bg"):
                node["bg"] = bg
            elif "dir" not in node:
                node = {"dir": "vert", "bg": bg, "w": node.get("w", 1), "children": [node]}
        return node

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

    def _vert_gaps(kids, box):
        """Release 26c: a vertical flow keeps the space Tableau leaves between its zones (padding, margins)
        and a zone narrower than the column keeps its x / width -- spacers carry both."""
        eps = 300
        if not all(all(k in kid for k in ("x", "y", "w", "h")) for kid in kids):
            return kids
        out, prev = [], box.get("y", 0)
        bx0, bx1 = box.get("x", 0), box.get("x", 0) + box.get("w", 0)
        for kid in kids:
            if kid["y"] - prev > eps:
                out.append({"blank": True, "w": 1, "h": kid["y"] - prev, "_spacer": True})
            left, right = kid["x"] - bx0, bx1 - (kid["x"] + kid["w"])
            if bx1 > bx0 and (left > HGAP or right > HGAP):
                row = []
                if left > HGAP:
                    row.append({"blank": True, "w": left, "h": 0, "_spacer": True})
                row.append(kid)
                if right > HGAP:
                    row.append({"blank": True, "w": right, "h": 0, "_spacer": True})
                out.append({"dir": "horz", "bg": None, "w": bx1 - bx0, "h": kid["h"], "children": row})
            else:
                out.append(kid)
            prev = max(prev, kid["y"] + kid["h"])
        return out

    # Tableau show/hide buttons: <toggle-action>... zone-ids=[17]</toggle-action> name the zones they show
    # and hide. A target zone is kept even when it is hidden at open, with its starting state.
    toggle_targets = {}
    for bz in d.findall(".//zone"):
        for ta in bz.findall("./button/toggle-action"):
            for zid in re.findall(r"zone-ids=\[([0-9, ]+)\]", ta.text or ""):
                for one in zid.split(","):
                    if one.strip():
                        toggle_targets[one.strip()] = None
    for tz in d.findall(".//zone"):
        if tz.get("id") in toggle_targets:
            toggle_targets[tz.get("id")] = tz.get("hidden-by-user") == "true"

    def build(z):
        node = _build(z)
        if node is not None and z.get("id") in toggle_targets:
            node.update(zid=z.get("id"), start_hidden=bool(toggle_targets[z.get("id")]))
        return node

    def _build(z):
        hidden = z.get("hidden-by-user") == "true"
        if id(z) in skip_ids or (hidden and z.get("id") not in toggle_targets):
            return None
        t = z.get("type-v2") or z.get("type") or ""
        nm = z.get("name")
        box = _box(z)
        if t == "text":
            txt = _zone_text(z, param_alias)
            if not txt:
                return None
            return dict(box, text=txt, w=int(z.get("w") or 1), h=int(z.get("h") or 0))
        # Release 26c: every other zone type is kept where Tableau places it. A filter / parameter control,
        # a legend, the dashboard title, a blank spacer or an image is part of the authored layout; dropping
        # them moved the controls to a separate row and shifted the charts' columns.
        if int(z.get("w") or 0) <= 0 or int(z.get("h") or 0) <= 0:
            if t in ("filter", "paramctrl", "color", "size", "shape", "title", "empty", "bitmap", "dashboard-object"):
                return None
        if t == "filter" and z.get("param"):
            try:
                f = parse_field(z.get("param"), meta) if meta is not None else {}
            except Exception:
                f = {}
            leaf = dict(box, control="filter", caption=f.get("caption"), datepart=f.get("datepart"),
                        field_id=f.get("field_id"), scope_sheet=nm, mode=z.get("mode") or "checklist")
            if f.get("datasource_id"):
                leaf["datasource_id"] = f["datasource_id"]      # the field's owner (the IR contract checks it)
            elif not f.get("field_id"):
                leaf.pop("field_id")
            return leaf
        if t == "paramctrl" and z.get("param"):
            internal = z.get("param").split("].[")[-1].strip("[]")
            return dict(box, control="param", caption=param_alias.get(internal, internal),
                        mode=z.get("mode") or "dropdown")
        if t in ("color", "size", "shape") and nm in sheet_names:
            return dict(box, legend=nm, legend_type=t)
        if t == "title":
            return dict(box, title=True)
        if t == "empty":
            return dict(box, blank=True)
        if t == "bitmap" and z.get("param"):
            return dict(box, image=z.get("param"))
        if t == "dashboard-object" and z.find("./button/toggle-action") is not None:
            ta = z.find("./button/toggle-action")
            ids = [i.strip() for m in re.findall(r"zone-ids=\[([0-9, ]+)\]", ta.text or "") for i in m.split(",")
                   if i.strip() and i.strip() in toggle_targets]
            tip = z.find("./button/button-visual-state/tooltip-text")
            if ids:
                b = z.find("button")
                imgs = [(ip.text or "").strip() for ip in b.findall("./button-visual-state/image-path")]
                try:
                    active = int(b.get("active-visual-state-index") or 0)
                except ValueError:
                    active = 0
                return dict(box, toggle=ids, caption=(tip.text or "").strip() if tip is not None and tip.text else "Show / hide",
                            start_hidden=[bool(toggle_targets[i]) for i in ids], images=[i for i in imgs if i],
                            active=active)
        if t == "dashboard-object":
            b = z.find("button")
            m = re.search(r'window-id="?(\{[^}"]+\})', (b.get("action") or "") if b is not None else "")
            if b is not None and m and (windows or {}).get(m.group(1).upper()):
                vs = b.find("button-visual-state")
                cap = "".join(vs.find("caption").itertext()).strip() if vs is not None and vs.find("caption") is not None else ""
                font = vs.find("button-caption-font-style") if vs is not None else None
                fill = vs.find("format[@attr='background-color']") if vs is not None else None
                img = vs.find("image-path") if vs is not None else None
                tip = vs.find("tooltip-text") if vs is not None else None
                return dict(box, nav=windows[m.group(1).upper()],
                            caption=cap if (cap or img is not None) else windows[m.group(1).upper()],
                            color=font.get("fontcolor") if font is not None else None,
                            fontsize=font.get("fontsize") if font is not None else None,
                            fill=fill.get("value") if fill is not None else None,
                            images=[img.text.strip()] if img is not None and img.text else [],
                            tooltip=(tip.text or "").strip() if tip is not None and tip.text else None)
        if t in ("dashboard-object", "web"):
            return dict(box, unsupported_zone=t, url=z.get("url"))
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
            # Tableau uses a zero-sized worksheet zone for the inactive side
            # of parameter/show-hide swaps. Streamlit cannot overlay zones;
            # rendering it as a forced one-unit column produces a narrow error
            # strip beside the active sheet. Preserve the workbook's saved
            # state by excluding the inactive leaf.
            if int(z.get("w") or 0) <= 0 or int(z.get("h") or 0) <= 0:
                return None
            seen.add(nm)
            return dict(box, sheet=nm, w=int(z.get("w") or 1), h=int(z.get("h") or 1))
        child_pairs = []
        for k in z.findall("zone"):
            kn = build(k)
            if kn:
                child_pairs.append((k, kn))
        kids = [kn for _, kn in child_pairs]
        if not kids:
            return None
        bg = _zone_bg(z)
        border = _zone_border(z)
        if len(kids) == 1:
            child = kids[0]
            if (bg or border) and "sheet" not in child:
                child["bg"] = bg
                child["border"] = border
            elif (bg or border) and "sheet" in child:
                # the card keeps its own rectangle; the sheet keeps its inset (padding) inside it
                child = dict(box, dir="vert", bg=bg, border=border, w=box.get("w", child["w"]),
                             children=_vert_gaps([child], box) if box else [child])
            return child
        # ABSOLUTE-positioning container: reconstruct rows from x/y geometry
        # (a layout-flow already encodes its direction via param, so only
        # layout-basic needs this). Tableau nests flow containers inside a
        # basic canvas, so this fires exactly where a stack would be wrong.
        if t == "layout-flow" and z.get("param") == "horz" and box:
            geo = _partition(child_pairs, bg, box)       # a row keeps its gaps (children narrower than the row)
            if geo is not None and geo.get("dir") == "horz":
                return dict(geo, border=border, **box)
        if t == "layout-basic" and z.get("param") != "horz":
            geo = _partition(child_pairs, bg, box) or _rows_from_geometry(child_pairs, bg)
            if geo is not None:
                if "children" in geo and box:
                    geo.update(box)
                basic_pairs[id(geo)] = child_pairs
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
                if (prev is not None and "sheet" not in k and "text" not in k
                        and prev and "sheet" not in prev and "text" not in prev
                        and k.get("dir") == "vert" and prev.get("dir") == "vert"   # two rows never merge into one
                        and not k.get("floating") and not prev.get("floating")
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
        if z.get("param") != "horz" and box:
            kids = _vert_gaps(kids, box)
        return dict(box, dir="horz" if z.get("param") == "horz" else "vert",
                    bg=bg, border=border, w=int(z.get("w") or 1),
                    h=int(z.get("h") or 0), children=kids)

    zones = d.find("zones")
    if zones is None:
        return None
    pairs = [(z, build(z)) for z in zones.findall("zone")]
    pairs = [(z, r) for z, r in pairs if r and (_has_sheet(r) or _has_placed(r))]
    roots = [r for _z, r in pairs]
    if not any(_has_sheet(r) for r in roots):
        return None
    if not roots:
        return None
    if len(roots) > 1:
        # Release 26c: floating zones are siblings of the tiled canvas; they are placed by their rectangles
        boxes = [_box(z) for z, _r in pairs]
        if all(all(k in b for k in ("x", "y", "w", "h")) for b in boxes):
            x0 = min(b["x"] for b in boxes)
            y0 = min(b["y"] for b in boxes)
            region = {"x": x0, "y": y0, "w": max(b["x"] + b["w"] for b in boxes) - x0,
                      "h": max(b["y"] + b["h"] for b in boxes) - y0}
            geo = _partition(pairs, None, region)
            if geo is not None:
                return geo
    return roots[0] if len(roots) == 1 else {"dir": "vert", "bg": None,
                                             "w": 1, "children": roots}


CANVAS_VERSION = 'canvas/1'     # release 26f: every zone drawn at its own XML rectangle


def _zone_style(z):
    """A zone's authored box style (<zone-style>): margins and padding in px, background, border."""
    vals = {f.get("attr"): f.get("value") for f in z.findall("./zone-style/format")}

    def px(key, side):
        v = vals.get(f"{key}-{side}", vals.get(key))
        try:
            return max(0, int(float(v)))
        except (TypeError, ValueError):
            return 0
    out = {"margin": [px("margin", s) for s in ("top", "right", "bottom", "left")],
           "padding": [px("padding", s) for s in ("top", "right", "bottom", "left")]}
    if vals.get("background-color"):
        out["bg"] = vals["background-color"]
    border = _zone_border(z)
    if border:
        out["border"] = border
    return out


def _paragraph_marks(raw):
    """Release 27 (F2): Tableau writes a paragraph end as 'Æ' + newline: ONE line break. A lone 'Æ' (no newline
    after it, e.g. the run between a KPI delta and its 'vs. ...' note) draws nothing: Tableau keeps that text on
    the same line (checked against Tableau Cloud)."""
    return raw.replace("Æ\r\n", "\n").replace("Æ\n", "\n").replace("Æ", "")


def _text_runs(ft, param_alias):
    """<formatted-text><run fontsize= bold= fontcolor= fontalignment=> as runs; Tableau's paragraph mark
    (Æ / newline) splits lines. Parameter and field tokens become runtime placeholders."""
    runs = []
    # a field / parameter token (<[Parameters].[X]>) can be split over several runs: join those runs first
    merged, pend = [], None
    for r in ft.iter("run"):
        if pend is not None:
            pend[1] += r.text or ""
            if ">" in pend[1][pend[1].rfind("<"):]:
                merged.append(tuple(pend))
                pend = None
            continue
        txt = r.text or ""
        if txt.rfind("<") > txt.rfind(">"):
            pend = [r, txt]
            continue
        merged.append((r, txt))
    if pend is not None:
        merged.append(tuple(pend))
    for r, raw in merged:
        raw = _paragraph_marks(raw)
        for i, line in enumerate(raw.split("\n")):
            if i:
                runs.append({"br": True})
            text = _PARAM_TOKEN_RE.sub(lambda m: "{param:%s}" % param_alias.get(m.group(1), m.group(1)), line)
            text = _FIELD_TOKEN_RE.sub(lambda m: m.group(1), text)
            if not text:
                continue
            run = {"text": text}
            if r.get("fontsize"):
                run["size"] = r.get("fontsize")
            if r.get("bold") == "true":
                run["bold"] = True
            if r.get("italic") == "true":
                run["italic"] = True
            if r.get("fontcolor"):
                run["color"] = r.get("fontcolor")
            if r.get("fontname"):
                run["font"] = r.get("fontname")
            if r.get("fontalignment") is not None:
                run["align"] = {"0": "left", "1": "center", "2": "right"}.get(r.get("fontalignment"), "left")
            runs.append(run)
    while runs and runs[-1].get("br"):
        runs.pop()
    return runs


def worksheet_style(ws):
    """Release 26g: a worksheet's own formatting (<table><style><style-rule element=..><format attr=..>), as
    {element: {attr: value}} for the unscoped rules and {element|rows / element|cols: {...}} for a scoped one.
    The chart uses it instead of the chart library's defaults (fonts, label sizes and colours, gridlines,
    axis lines, ticks, pane borders, background)."""
    out = {}
    for sr in ws.findall("./table/style/style-rule"):
        el = sr.get("element")
        for f in sr.findall("format"):
            if f.get("field"):
                # a field's own formatting (header width, label date format): keyed 'mn:Order Date'
                m = re.search(r"\[([a-z]+):(.+?):[a-z]+\]$", f.get("field") or "")
                if m:
                    out.setdefault("fields", {}).setdefault(f"{m.group(1)}:{m.group(2)}", {})[
                        f"{el}.{f.get('attr')}"] = f.get("value")
                continue
            key = el + ("|" + f.get("scope") if f.get("scope") else "")
            out.setdefault(key, {})[f.get("attr")] = f.get("value")
    # Release 27 (F2): marks formatted on the PANE (<panes><pane><style>: label alignment, vertical alignment,
    # mark label settings) -- applied when every pane that sets the attribute agrees; table-level rules win.
    pane_vals = {}
    for sr in ws.findall("./table/panes/pane/style/style-rule"):
        for f in sr.findall("format"):
            if not f.get("field"):
                pane_vals.setdefault((sr.get("element"), f.get("attr")), set()).add(f.get("value"))
    for (el, attr), vals in pane_vals.items():
        if len(vals) == 1:
            out.setdefault(el, {}).setdefault(attr, next(iter(vals)))
    return out


def dashboard_style(d):
    """The dashboard's own default formatting (<dashboard><style>): title, subtitle, text, quick filters,
    parameter controls, containers and the dashboard background."""
    out = {}
    for sr in d.findall("./style/style-rule"):
        for f in sr.findall("format"):
            out.setdefault(sr.get("element"), {})[f.get("attr")] = f.get("value")
    return out


def canvas_zones(d, sheet_names, skip_ids, param_alias=None, meta=None, windows=None, title_runs=None):
    """Release 26f: the dashboard as Tableau draws it -- EVERY visible zone at its own rectangle, in Tableau's
    drawing order (later zones on top), with its authored box style. Nothing is re-flowed: the app pins each
    zone at x / y / w / h of the 100000 x 100000 dashboard grid. Containers appear only when they paint
    something (background or border). Zones a show/hide button controls carry `hide_with`."""
    param_alias = param_alias or {}
    toggle_targets = {}
    for bz in d.findall(".//zone"):
        for ta in bz.findall("./button/toggle-action"):
            for grp in re.findall(r"zone-ids=\[([0-9, ]+)\]", ta.text or ""):
                for one in grp.split(","):
                    if one.strip():
                        toggle_targets[one.strip()] = None
    for tz in d.findall(".//zone"):
        if tz.get("id") in toggle_targets:
            toggle_targets[tz.get("id")] = tz.get("hidden-by-user") == "true"
    out, seen = [], set()

    def visit(z, hide_with, dark):
        if id(z) in skip_ids:
            return
        zid = z.get("id")
        hidden = z.get("hidden-by-user") == "true"
        if hidden and zid not in toggle_targets:
            return
        if zid in toggle_targets:
            hide_with = hide_with + [{"zid": zid, "start_hidden": bool(toggle_targets[zid])}]
        box = _box(z)
        if not all(k in box for k in ("x", "y", "w", "h")) or box["w"] <= 0 or box["h"] <= 0:
            return
        t = z.get("type-v2") or z.get("type") or ""
        nm = z.get("name")
        style = _zone_style(z)
        item = dict(box, style=style, id=zid)
        if hide_with:
            item["hide_with"] = hide_with
        if dark:
            item["dark"] = True
        kids = z.findall("zone")
        if t in ("layout-flow", "layout-basic") or (not t and not nm and kids):
            if style.get("bg") or style.get("border"):
                out.append(dict(item, kind="box"))
            bg = style.get("bg")
            now_dark = dark if not bg else _hex_luminance(bg) < 0.5
            for k in kids:
                visit(k, hide_with, now_dark)
            return
        if nm and nm in sheet_names and not t:
            if nm in seen:
                return
            seen.add(nm)
            out.append(dict(item, kind="sheet", sheet=nm, show_title=z.get("show-title") != "false"))
        elif t == "text":
            ft = z.find("formatted-text")
            runs = _text_runs(ft, param_alias) if ft is not None else []
            if runs:
                out.append(dict(item, kind="text", runs=runs))
        elif t == "title":
            out.append(dict(item, kind="title", runs=title_runs or []))
        elif t == "filter" and z.get("param"):
            try:
                f = parse_field(z.get("param"), meta) if meta is not None else {}
            except Exception:
                f = {}
            leaf = dict(item, kind="control", control="filter", caption=f.get("caption"),
                        datepart=f.get("datepart"), scope_sheet=nm, mode=z.get("mode") or "checklist",
                        show_title=z.get("show-title") != "false", show_all=z.get("show-all") != "false")
            if f.get("field_id") and f.get("datasource_id"):
                leaf.update(field_id=f["field_id"], datasource_id=f["datasource_id"])
            leaf.update(_custom_title(z, param_alias))
            out.append(leaf)
        elif t == "paramctrl" and z.get("param"):
            internal = z.get("param").split("].[")[-1].strip("[]")
            out.append(dict(item, kind="control", control="param", caption=param_alias.get(internal, internal),
                            mode=z.get("mode") or "dropdown", show_title=z.get("show-title") != "false",
                            **_custom_title(z, param_alias)))
        elif t in ("color", "size", "shape") and nm in sheet_names:
            out.append(dict(item, kind="legend", legend=nm, legend_type=t,
                            layout=z.get("leg-item-layout") or "vert", **_custom_title(z, param_alias)))
        elif t == "highlighter" and z.get("param"):
            try:
                f = parse_field(z.get("param"), meta) if meta is not None else {}
            except Exception:
                f = {}
            out.append(dict(item, kind="highlighter", caption=f.get("caption"), scope_sheet=nm,
                            show_title=z.get("show-title") != "false", **_custom_title(z, param_alias)))
        elif t == "empty":
            if style.get("bg") or style.get("border"):
                out.append(dict(item, kind="box"))
        elif t == "bitmap" and z.get("param"):
            out.append(dict(item, kind="image", image=z.get("param"), fit=z.get("is-scaled") == "true"))
        elif t == "dashboard-object" and z.find("button") is not None:
            b = z.find("button")
            vs = b.find("button-visual-state")
            tip = vs.find("tooltip-text") if vs is not None else None
            imgs = [(ip.text or "").strip() for ip in b.findall("./button-visual-state/image-path") if ip.text]
            font = vs.find("button-caption-font-style") if vs is not None else None
            fill = vs.find("format[@attr='background-color']") if vs is not None else None
            cap_el = vs.find("caption") if vs is not None else None
            common = dict(item, images=imgs, tooltip=(tip.text or "").strip() if tip is not None and tip.text else None,
                          caption="".join(cap_el.itertext()).strip() if cap_el is not None else "",
                          color=font.get("fontcolor") if font is not None else None,
                          fontsize=font.get("fontsize") if font is not None else None,
                          fill=fill.get("value") if fill is not None else None)
            ta = b.find("toggle-action")
            m = re.search(r'window-id="?(\{[^}"]+\})', b.get("action") or "")
            if ta is not None:
                ids = [i.strip() for g in re.findall(r"zone-ids=\[([0-9, ]+)\]", ta.text or "") for i in g.split(",")
                       if i.strip() in toggle_targets]
                if ids:
                    try:
                        active = int(b.get("active-visual-state-index") or 0)
                    except ValueError:
                        active = 0
                    out.append(dict(common, kind="toggle", toggle=ids,
                                    start_hidden=[bool(toggle_targets[i]) for i in ids], active=active))
            elif m and (windows or {}).get(m.group(1).upper()):
                out.append(dict(common, kind="nav", nav=windows[m.group(1).upper()]))
            else:
                out.append(dict(item, kind="unsupported", what="dashboard-object"))
        elif t in ("web", "flipboard", "flipboard-nav", "dashboard-object", "highlighter"):
            out.append(dict(item, kind="unsupported", what=t))

    zones = d.find("zones")
    if zones is None:
        return []
    for z in zones.findall("zone"):
        visit(z, [], False)
    return out


def _custom_title(z, param_alias):
    """A zone title the author typed (custom-title='true' with its own formatted text)."""
    if z.get("custom-title") != "true":
        return {}
    ft = z.find("formatted-text")
    runs = _text_runs(ft, param_alias or {}) if ft is not None else []
    return {"title_runs": runs} if runs else {}


def _hex_luminance(color):
    try:
        c = color.lstrip("#")
        r, g, b = (int(c[i:i + 2], 16) / 255 for i in (0, 2, 4))
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    except (ValueError, AttributeError, IndexError):
        return 1.0


def _dash_title_runs(d, param_alias):
    ft = d.find("./layout-options/title/formatted-text")
    return _text_runs(ft, param_alias or {}) if ft is not None else []


def dashboard_fit(root, name):
    """Each sheet's fit inside the dashboard (<window class='dashboard'><viewpoint><zoom type=..>): 'entire-view',
    'fit-width', 'fit-height', or 'standard' (no zoom element: Tableau's authored row/column sizes)."""
    out = {}
    for w in root.findall(".//windows/window"):
        if w.get("class") == "dashboard" and w.get("name") == name:
            for vp in w.findall("./viewpoints/viewpoint"):
                zm = vp.find("zoom")
                out[vp.get("name")] = zm.get("type") if zm is not None and zm.get("type") else "standard"
    return out


def zone_legends(d, sheet_names, skip_ids=frozenset()):
    """Which legends the dashboard actually SHOWS, per sheet: Tableau places
    a legend as its own zone (<zone type-v2='color'|'size'|'shape'
    name='Sheet'>) and never draws one inside the chart; a legend the author
    removed from the canvas is either absent or kept as
    hidden-by-user='true'. Returns {sheet: {'color', 'size', ...}} for the
    visible ones -- the engine hides any in-chart legend Tableau doesn't
    show, instead of letting a legend eat a third of a narrow panel that
    the workbook labels directly on the marks."""
    out = {}
    for z in d.findall(".//zone"):
        if id(z) in skip_ids or z.get("hidden-by-user") == "true":
            continue
        t = (z.get("type-v2") or z.get("type") or "").lower()
        nm = z.get("name")
        if t in ("color", "size", "shape") and nm in sheet_names:
            out.setdefault(nm, set()).add(t)
    return {k: sorted(v) for k, v in out.items()}


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
            key = (f.get('field_id') or (f.get('ds'), f['internal']), f.get("datepart"))
            if key in seen:
                continue
            seen.add(key)
            filters.append({"caption": f["caption"],
                            **{k: f[k] for k in ('field_id', 'datasource_id') if k in f},
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
    # Tableau navigation buttons point at a window by its uuid (<window name=..><simple-id uuid=..>)
    windows = {}
    for w in root.findall(".//windows/window"):
        sid = w.find("simple-id")
        if sid is not None and sid.get("uuid") and w.get("name"):
            windows[sid.get("uuid").upper()] = w.get("name")
    for d in root.findall(".//dashboards/dashboard"):
        if d.get("type") == "storyboard":
            continue                    # stories: detected, reported, not converted
        t = d.find(".//title")
        title = " ".join("".join(t.itertext()).split()) if t is not None else d.get("name")
        if not title or title.startswith("<"):
            title = d.get("name")
        dz = _device_zone_ids(d)
        sheets = []
        hidden_sheets = set()
        for z in d.findall(".//zone"):
            n = z.get("name")
            if id(z) not in dz and n and n not in sheets:
                sheets.append(n)
            # Only a BARE zone is the worksheet. Filter/color/highlighter
            # chrome reuses the worksheet name and may itself be hidden; that
            # must never mark the visible chart hidden.
            zone_type = z.get("type-v2") or z.get("type") or ""
            if (id(z) not in dz and n and not zone_type
                    and (z.get("hidden-by-user") == "true"
                         or int(z.get("w") or 1) <= 0
                         or int(z.get("h") or 1) <= 0)):
                hidden_sheets.add(n)
        entry = {"name": d.get("name"), "title": title, "sheet_names": sheets,
                 "geom": _zone_geometry(d, sheets, dz),
                 "layout": layout_tree(d, set(sheets), dz, param_alias, meta, windows),
                 "canvas_zones": canvas_zones(d, set(sheets), dz, param_alias, meta, windows,
                                              _dash_title_runs(d, param_alias)),
                 "canvas_version": CANVAS_VERSION,
                 "fit": dashboard_fit(root, d.get("name")),
                 "dash_style": dashboard_style(d),
                 "layout_version": LAYOUT_VERSION,
                 "legends": zone_legends(d, set(sheets), dz),
                 "hidden_sheets": hidden_sheets}
        size = d.find("size")
        if size is not None:
            entry["canvas"] = {k: int(size.get(k)) for k in
                               ("minwidth", "maxwidth", "minheight", "maxheight")
                               if str(size.get(k, "")).isdigit() and int(size.get(k)) > 0}
            entry["canvas"]["sizing_mode"] = size.get("sizing-mode", "")
        if meta is not None:
            zf, zp = zone_controls(d, meta, param_alias, dz)
            entry["zone_filters"], entry["zone_params"] = zf, zp
        # device layouts: rendered as ONE responsive desktop layout; names
        # captured so the drop is reported, never silent
        dls = [dl.get("name") for dl in d.findall(".//devicelayout")
               if dl.get("name")]
        if dls:
            entry["device_layouts"] = sorted(set(dls))
        variants = device_variants(d, set(sheets), param_alias, meta, windows)
        if variants:
            entry["device_variants"] = variants
        out.append(entry)
    return out


def device_variants(d, sheet_names, param_alias=None, meta=None, windows=None):
    """EN14: the Phone / Tablet layouts a dashboard authors with their OWN zones (a device layout without zones
    follows the default). Each one is laid out by the same code as the default: a canvas (zone rectangles) and a
    layout tree; its size mode and title come from the device layout itself."""
    out = {}
    for dl in d.findall("./devicelayouts/devicelayout"):
        name = dl.get("name")
        if not name or dl.find("zones") is None:
            continue
        shown = [z.get("name") for z in dl.findall(".//zone")
                 if z.get("name") in sheet_names and not (z.get("type-v2") or z.get("type"))]
        if not shown:
            continue
        size = dl.find("size")
        out[name] = {"layout": layout_tree(dl, set(sheet_names), set(), param_alias, meta, windows),
                     "canvas_zones": canvas_zones(dl, set(sheet_names), set(), param_alias, meta, windows,
                                                  _dash_title_runs(dl, param_alias) or _dash_title_runs(d, param_alias)),
                     "canvas": ({**{k: int(size.get(k)) for k in ("minwidth", "maxwidth", "minheight", "maxheight")
                                    if str(size.get(k, "")).isdigit() and int(size.get(k)) > 0},
                                 "sizing_mode": size.get("sizing-mode", "")} if size is not None else {}),
                     "sheets": shown}
    return out


def extract_calcs_params(root, meta):
    """Return (calc_defs, params, aliases, param_info).
    calc_defs  {caption: formula};  params {caption AND internal: value};
    aliases    {internal: formula}  (nested refs use internal names);
    param_info {'captions': {caption: value}, 'alias': {internal: caption},
               'domains': {caption: [allowed values]}  (list-domain params)}."""
    calc_defs, params, aliases = {}, {}, {}
    param_caps, param_alias, param_domains = {}, {}, {}
    import calc_translator as _ct
    _ct.INTEGER_PARAMS.clear()
    for col in root.findall(".//column"):
        nm = (col.get("name") or "").strip("[]")
        cap = col.get("caption") or nm
        if col.get("param-domain-type") is not None:
            params[cap] = col.get("value")
            if col.get("datatype") == "integer":           # C9: STR(integer parameter) is '2025', never '2025.0'
                import calc_translator as _ct
                _ct.INTEGER_PARAMS.update({cap, nm})
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
                    name2cap=None, physical_names=None):
    """Translate every calc; return (translated, dropped). Window (table-calc)
    translations carry window=True -- the engine computes them post-aggregation.
    `aliases` (internal name -> formula) resolves nested internal-name refs;
    `colmap` (caption -> source column) resolves workbook RENAMES in formulas."""
    from calc_translator import translate_formula, WIN_ORDER, is_window_sql
    merged = dict(aliases or {})
    # Tableau formula references use INTERNAL field identity. A physical field
    # can legally be named [Channel] while a different calculated field has the
    # DISPLAY caption "Channel" and an internal [Calculation_...] name. Adding
    # that caption to the nested-calc registry makes [Channel] recursively
    # resolve to the calculated field instead of the physical column. Keep the
    # internal calc aliases, but never let a display caption shadow a real
    # physical internal name. Found across multiple tabs of the BDO workbook;
    # this is field-identity handling, not a workbook-name exception.
    physical_names = {str(x).strip("[]") for x in (physical_names or set())}
    merged.update({cap: formula for cap, formula in calc_defs.items()
                   if cap not in physical_names})
    out, dropped = {}, {}
    for cap, formula in calc_defs.items():
        sql, agg_ready = translate_formula(formula, params, merged,
                                           param_alias=param_alias, colmap=colmap)
        if sql:
            entry = {"sql": sql, "agg_ready": agg_ready}
            # C6: every table function is a window the plan fills from the sheet's addressing -- INDEX / LOOKUP /
            # RUNNING_* / FIRST / LAST (__TC_ORDER__) and WINDOW_* / TOTAL (OVER ())
            if is_window_sql(sql):
                entry["window"] = True
                try:                         # EN26: windows of nested calculations, tagged by calculation
                    from calc_translator import translate_tagged
                    tagged, _a = translate_tagged(formula, params, merged, param_alias=param_alias, colmap=colmap)
                    if tagged and tagged != sql:
                        entry["sql_tagged"] = tagged
                except Exception:
                    pass
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


def _top_n_set(end, calc_defs, params):
    """C4: Top-N set -> In/Out. The level's members ranked by the order expression over the whole table (ROW_NUMBER,
    like the engine's top-N filter), top or bottom N; N a literal or a parameter (live token). None if any part
    cannot be translated (the set stays unresolved and is reported, never guessed)."""
    from calc_translator import translate_formula, to_phys, TBL
    order = end.find("groupfilter[@function='order']")
    lvl_el = end.find(".//groupfilter[@function='level-members']")
    if order is None or lvl_el is None or not order.get("expression"):
        return None
    expr, _ = translate_formula(order.get("expression"), params, calc_defs)
    count = (end.get("count") or "").strip()
    n = count if count.isdigit() else (translate_formula(count, params, calc_defs)[0] if count.startswith("[") else None)
    lvl = to_phys((lvl_el.get("level") or "").strip("[]"))
    if not expr or not n or not lvl:
        return None
    desc = (order.get("direction") or "DESC").upper() == "DESC"
    if (end.get("end") or "top").lower() == "bottom":
        desc = not desc
    rank = (f"SELECT {lvl} FROM (SELECT {lvl}, ROW_NUMBER() OVER (ORDER BY {expr} {'DESC' if desc else 'ASC'}) AS __RN "
            f"FROM {TBL} GROUP BY {lvl}) __t WHERE __RN <= {n}")
    return {"sql": f"CASE WHEN {lvl} IN ({rank}) THEN 'In' ELSE 'Out' END", "agg_ready": False, "set_level": lvl}


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
        end = g.find("groupfilter[@function='end']")
        if end is not None:
            entry = _top_n_set(end, calc_defs, params)
            if entry:
                out[cap] = entry
                if nm != cap:
                    out[nm] = entry
            continue
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
        default = (calc.get("default") or "").strip().strip('"')
        if default:                          # Tableau's label for members in no bin (e.g. "Not Clustered")
            other = "'" + default.replace("'", "''") + "'"
        elif (calc.get("use-other") or "").lower() == "true":
            other = "'Other'"
        else:
            other = base
        out[cap] = "CASE " + " ".join(whens) + f" ELSE {other} END"
    return out


def measure_name_aliases(root):
    """Release 26g: the labels the author gave Measure Names members (<column name='[:Measure Names]'><aliases>
    <alias key='"[ds].[ctd:Customer Name:qk]"' value='Count of Customers'/>): {'ctd:Customer Name': label}."""
    out = {}
    for col in root.findall(".//column[@name='[:Measure Names]']"):
        for a in col.findall("./aliases/alias"):
            m = re.search(r"\[([a-z]+):(.+?):[a-z]+\]", a.get("key") or "")
            if m and a.get("value"):
                out[f"{m.group(1)}:{m.group(2)}"] = a.get("value")
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


def build_ir(path, only=None, *, scoped=False):
    """Parse a workbook; V3 callers enable the scoped identity contract.

    Legacy consumers retain their compatibility view during the migration.
    Passing scoped=True can refuse sheets legacy renderers approximate.
    """
    root = load_twb_xml(path)
    meta, _ = column_meta(root, _scoped=scoped)
    import calc_translator as _ct              # C9: date fields, for Tableau's date +/- n days
    _ct.set_date_fields({_ct.to_phys(str(nm).split('].[')[-1].strip('[]'))
                         for nm, info in (meta.items() if hasattr(meta, 'items') else [])
                         if (info or {}).get('datatype') in ('date', 'datetime')})
    dsmap = datasource_map(root)
    # caption -> SOURCE column name, for fields the workbook RENAMED. Needed
    # BEFORE calc translation: formulas reference renamed fields too.
    calc_internal_names = {
        (c.get("name") or "").strip("[]")
        for c in root.findall(".//column")
        if c.get("name") and c.find("calculation") is not None
    }
    colmap = {}
    for nm, info in meta.items():
        base = nm.strip("[]")
        cap = info.get("caption")
        if (cap and cap != base and INTERNAL_OBJ not in nm
                and base not in calc_internal_names):
            colmap.setdefault(cap, base)

    calc_defs, params, aliases, param_info = extract_calcs_params(root, meta)
    fields, field_scopes = {}, {}
    if scoped:
        fields, field_scopes, _ = field_identity.build_scopes(
            root, sys.modules[__name__], params, param_info.get('alias'))
    name2cap = {nm: info.get("caption") for nm, info in meta.items()
                if info.get("caption")}
    physical_names = {
        (c.get("name") or "").strip("[]")
        for c in root.findall(".//column")
        if c.get("name") and c.find("calculation") is None
    }
    calcs, calc_drops = translate_calcs(calc_defs, params, aliases,
                                        param_info["alias"], colmap, name2cap,
                                        physical_names)
    # user GROUPS resolve like row-level calcs (rdim picks them up by caption)
    for cap, sql in extract_groups(root).items():
        calcs.setdefault(cap, {"sql": sql, "agg_ready": False})
    # user SETS resolve as In/Out calc dimensions
    merged_defs = dict(aliases); merged_defs.update(calc_defs)
    for cap, entry in extract_sets(root, merged_defs, params).items():
        calcs.setdefault(cap, entry)
    all_ws = {w.get("name"): w for w in root.findall(".//worksheets/worksheet")}
    wide_filters = workbook_filters(root, meta)

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
            s = infer(all_ws[n], meta, dsmap, params, param_info.get("alias"))
            add_workbook_filters(s, wide_filters)
            field_identity.bind_sheet(s, all_ws[n], meta, fields, field_scopes, sys.modules[__name__])
            s["geom"] = d.get("geom", {}).get(n)
            ws_style = worksheet_style(all_ws[n])
            if ws_style:
                s["ws_style"] = ws_style
            if n in d.get("hidden_sheets", set()):
                s["hidden_by_user"] = True
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
        if d.get("canvas"):
            entry["canvas"] = d["canvas"]
        if d.get("device_layouts"):
            entry["device_layouts"] = d["device_layouts"]
        if d.get("device_variants"):
            entry["device_variants"] = d["device_variants"]
        if d.get("layout"):
            entry["layout"] = d["layout"]
        if d.get("canvas_zones"):
            entry["canvas_zones"] = d["canvas_zones"]
            entry["canvas_version"] = d.get("canvas_version")
        if d.get("fit"):
            entry["fit"] = d["fit"]
        if d.get("dash_style"):
            entry["dash_style"] = d["dash_style"]
        if "legends" in d:          # {} is meaningful: this dashboard shows none
            entry["legends"] = d["legends"]
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
            s = infer(all_ws[n], meta, dsmap, params, param_info.get("alias"))
            add_workbook_filters(s, wide_filters)
            field_identity.bind_sheet(s, all_ws[n], meta, fields, field_scopes, sys.modules[__name__])
            # Tableau often leaves a visible authoring window such as the
            # default "Sheet 1"/"Sheet 43" even though it contains no marks,
            # fields, or data.  It is not a published workbook view and must
            # not become an empty Streamlit tab beside the real dashboards.
            if s.get("non_data") and not s.get("dims") and not s.get("measures"):
                continue
            dash_out.append({"name": n, "title": s.get("title") or n,
                             "filters": s.get("filters", []),
                             "params": ws_params.get(n, []), "sheets": [s]})

    stories = [d.get("name") for d in root.findall(".//dashboards/dashboard")
               if d.get("type") == "storyboard"]
    story_boards = story_points(root, meta)

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
                sheet_hiers = field_scopes.get(s.get('datasource_id'), {}).get('hierarchies', hiers)
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
                    hit = next((nm for nm, lv in sheet_hiers.items()
                                if cap in lv), None) if cap else None
                    if hit and (best is None
                                or sheet_hiers[hit].index(cap) > best[3]):
                        best = (key, idx, cap, sheet_hiers[hit].index(cap), hit)
                if best:
                    key, idx, cap, _, hit = best
                    s["drill"] = {"name": hit, "levels": sheet_hiers[hit],
                                  "current": cap, "slot": key, "slot_idx": idx}

    # workbook-wide per-measure colors -> attach to every measure-panel spec
    mc = measure_colors(root, meta)
    if mc:
        for d in dash_out:
            for s in d["sheets"]:
                local_mc = ({(m['caption'], m['agg']): m['color'] for m in
                             field_scopes[s['datasource_id']]['measure_colors']}
                            if scoped and s.get('datasource_id') in field_scopes else mc)
                # ys = dual-axis series (was skipped -> Gross/Net rendered in
                # default palette instead of the workbook's red/grey)
                for m in (s.get("measures") or []) + (s.get("ys") or []):
                    c = local_mc.get((m["caption"], m.get("agg"))) or local_mc.get((m["caption"], None))
                    if c and not m.get("color"):
                        m["color"] = c

    if scoped:
        calcs, colmap, ambiguous_fields = field_identity.compatibility_maps(fields)
        from worksheet_semantics import attach
        parameter_defs = {}
        for col in root.findall("./datasources/datasource[@name='Parameters']/column"):
            if col.get('param-domain-type') is None:
                continue
            internal = (col.get('name') or '').strip('[]')
            if internal in parameter_defs:
                raise ValueError('Ambiguous parameter declaration: ' + internal)
            parameter_defs[internal] = {'internal': internal, 'caption': col.get('caption') or internal,
                                        'datatype': col.get('datatype'), 'value': col.get('value'), 'aliases': {
                                            a.get('key'): a.get('value') for a in col.findall('./aliases/alias')}}
        for dashboard in dash_out:
            for sheet in dashboard['sheets']:
                if sheet.get('datasource_id') in field_scopes and sheet.get('name') in all_ws:
                    source = next((d for d in root.findall('./datasources/datasource')
                                   if d.get('name') == sheet['datasource_id']), None)
                    declared = {}
                    for action in root.findall('./actions/action'):
                        src=action.find('source');cmd=action.find('command');activation=action.find('activation')
                        if src is None or cmd is None or activation is None or src.get('dashboard')!=dashboard['name'] or src.get('type')!='sheet' or src.get('worksheet') not in all_ws:continue
                        if cmd.get('command')!='tsc:tsl-filter' or [(p.get('name'),p.get('value')) for p in cmd]!=[('special-fields','all'),('target',dashboard['name'])] or action.find('link') is not None or activation.attrib!={'auto-clear':'true','type':'on-select'}:continue
                        origin=all_ws[src.get('worksheet')];levels=[]
                        for text in [origin.findtext('./table/rows',''),origin.findtext('./table/cols','')]:
                            for token in TOKEN_RE.findall(text):
                                f=parse_field(token,meta.for_datasource(sheet['datasource_id']))
                                if f.get('ds')==sheet['datasource_id'] and f.get('agg')=='none' and not f.get('datepart'):
                                    name=f.get('internal')
                                    if name and name not in levels:levels.append(name)
                        # Tableau includes discrete text labels in all-fields links.
                        for enc in origin.findall('.//panes/pane/encodings/text'):
                            f=parse_field(enc.get('column',''),meta.for_datasource(sheet['datasource_id']))
                            if f.get('ds')==sheet['datasource_id'] and f.get('agg')=='none':
                                name=f.get('internal')
                                if name and name not in levels:levels.append(name)
                        declared[action.get('name')]={'group_fields':levels,'source':src.get('worksheet')}
                    attach(sheet, all_ws[sheet['name']], meta, fields, sys.modules[__name__], source, parameter_defs, declared, root, dashboard['name'])
    if scoped:
        from dashboard_actions import extract as extract_actions
        extract_actions(root, dash_out, fields)
    return {"source_file": path, "calcs": calcs, "calc_drops": calc_drops,
            **({'fields': fields, 'field_scopes': field_scopes,
                'ambiguous_fields': ambiguous_fields} if scoped else {}),
            "datasources": sorted(set(dsmap.values())),
            "params": param_info["captions"],
            "param_domains": param_info.get("domains", {}), "stories": stories, "story_boards": story_boards,
            "datasource_notes": ds_notes, "hierarchies": hiers,
            "live_connections": live_connections(root),
            "custom_sql_sources": custom_sql_sources(root),
            "source_tables": source_tables(root),
            "blends": blends(root),
            "blend_fields": blend_fields(root),
            "colmap": colmap, "color_maps": extract_color_maps(root, meta),
            "palette_refs": extract_palette_refs(root, meta),
            "aliases": extract_aliases(root), "measure_name_aliases": measure_name_aliases(root),
            "dashboards": dash_out,
            "images": layout_images(path, dash_out)}


MAX_IMAGE_BYTES = 600_000


def shrink_image(data, mime, max_side=1800):
    """EN25: an image over MAX_IMAGE_BYTES scaled to at most max_side px and re-encoded (PNG keeps transparency,
    else JPEG) until it fits. (None, mime) when it cannot be made to fit or cannot be read (no Pillow, a vector
    image, a decompression bomb) -- the app then records a note instead of drawing it, as before."""
    try:
        import io
        from PIL import Image
    except ImportError:
        return None, mime
    if "svg" in (mime or ""):
        return None, mime
    try:
        Image.MAX_IMAGE_PIXELS = 50_000_000
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        return None, mime
    alpha = im.mode in ("RGBA", "LA", "P")
    for side in (max_side, 1200, 800, 500):
        x = im.copy()
        x.thumbnail((side, side))
        buf = io.BytesIO()
        if alpha:
            x.convert("RGBA").save(buf, format="PNG", optimize=True)
            out_mime = "image/png"
        else:
            x.convert("RGB").save(buf, format="JPEG", quality=85, optimize=True)
            out_mime = "image/jpeg"
        if buf.tell() <= MAX_IMAGE_BYTES:
            return buf.getvalue(), out_mime
    return None, mime


def layout_images(path, dashboards_out):
    """Images placed on dashboards (bitmap zones), read from the packaged workbook as data URIs.
    A missing or oversized image is left out (the app records a note instead of drawing it)."""
    import base64
    import mimetypes
    import zipfile
    wanted = set()

    def walk(node):
        if isinstance(node, dict):
            if node.get("image"):
                wanted.add(node["image"])
            for extra in node.get("images") or []:          # button images (navigation, show/hide)
                wanted.add(extra)
            for k in node.get("children") or []:
                walk(k)
            walk(node.get("backdrop"))
            for f in node.get("floating") or []:
                walk(f.get("child"))
    for d in dashboards_out or []:
        walk(d.get("layout"))
        for z in d.get("canvas_zones") or []:      # release 26f: images and button images of every zone
            walk(z)
    if not wanted or not str(path).lower().endswith(".twbx"):
        return {}
    out = {}
    try:
        with zipfile.ZipFile(path) as z:
            names = {n.replace("\\", "/"): n for n in z.namelist()}
            for want in wanted:
                key = names.get(want.replace("\\", "/"))
                if key is None:
                    continue
                info = z.getinfo(key)
                if info.file_size > 40 * MAX_IMAGE_BYTES:
                    continue                                 # far too large to embed even shrunk
                data = z.read(key)
                mime = mimetypes.guess_type(key)[0] or "image/png"
                if len(data) > MAX_IMAGE_BYTES:
                    data, mime = shrink_image(data, mime)    # EN25: a large screenshot / background is scaled
                    if data is None:
                        continue
                out[want] = f"data:{mime};base64," + base64.b64encode(data).decode()
    except (OSError, zipfile.BadZipFile):
        return {}
    return out


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
