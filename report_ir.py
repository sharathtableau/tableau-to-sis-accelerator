"""
report_ir.py  --  the shared, source-neutral report IR ("report-ir/1").

The Tableau path keeps its own IR (tableau_parser -> engine). That IR is shaped
around Tableau shelves, marks and single-table datasources, so Power BI's
model-level measures, relationships and filter context would not fit it
without distortion. report-ir/1 is the shared contract for the new path:

    source parser (pbi_parser.py today; Tableau later, optional)
      -> report-ir/1  (this module validates it)
      -> report_sql.py (query compiler)  -> report_runtime.py (Streamlit)
      -> conversion_report.py

Nothing in here is Power BI specific; source-specific detail lives under
`source` and in the parser.

Support levels for every feature row, measure, column, visual:
    auto         converted and rendered with no known semantic gap
    partial      converted; a documented gap remains (see notes)
    manual       not converted; needs a person or a later phase (held)
    unsupported  not converted in this release; shown as a placeholder

Top-level shape (all keys required):
{
  "ir_version": "report-ir/1",
  "source":   {"kind": "powerbi", "format": "...", "name": "...", "path": "..."},
  "model": {
    "tables":   {name: {"name", "kind": "source"|"virtual_date"|"calculated",
                        "binding": {"database","schema","object"} | None,
                        "columns": {name: {"name","data_type","physical",
                                           "sql","format","sort_by","support","notes"}},
                        "anchors": {fact_table: fact_column}   # virtual_date only
                        "support", "notes"}},
    "measures": {name: {"name","table","dax","format","status": "ready"|"held",
                        "translation": {...} | None, "support","notes"}},
    "relationships": [{"from_table","from_column","to_table","to_column",
                       "active","cross_filter","support","notes"}],
    "roles": [{"name","table_permissions","support","notes","proposed_policy_sql"}],
    "parameters": {name: value}
  },
  "pages": [{"id","name","hidden","width","height","filters": [filter],
             "visuals": [visual]}],
  "report_filters": [filter],
  "features": [{"area","item","support","detail","follow_up"}]
}
visual = {"id","source_type","kind","title","position": {x,y,width,height,z},
          "fields": {role: [field]}, "sort": [{"field","direction"}],
          "filters": [filter], "support","notes": [...], "held_measures": [...]}
field  = {"kind": "column"|"measure"|"aggregation"|"date_level",
          "table","name", "agg"?, "level"?, "label"}
filter = {"field": field, "op": "in"|"not_in"|"="|">"|">="|"<"|"<=",
          "values": [...], "scope": "report"|"page"|"visual"}
"""

IR_VERSION = "report-ir/1"
SUPPORT_LEVELS = ("auto", "partial", "manual", "unsupported")
VISUAL_KINDS = ("kpi", "bar", "column", "line", "area", "scatter", "table",
                "matrix", "combo", "stacked100", "slicer", "text",
                "unsupported")
FIELD_KINDS = ("column", "measure", "aggregation", "date_level")
FILTER_OPS = ("in", "not_in", "=", ">", ">=", "<", "<=")

_RANK = {k: i for i, k in enumerate(SUPPORT_LEVELS)}


def worst(*levels):
    """The most severe support level among `levels`."""
    lv = [x for x in levels if x]
    return max(lv, key=lambda x: _RANK[x]) if lv else "auto"


def feature(area, item, support, detail="", follow_up=""):
    assert support in SUPPORT_LEVELS, support
    return {"area": area, "item": item, "support": support,
            "detail": detail, "follow_up": follow_up}


def validate(ir):
    """Structural check. Returns a list of problems (empty = valid)."""
    errs = []
    if not isinstance(ir, dict):
        return ["IR is not an object"]
    if ir.get("ir_version") != IR_VERSION:
        errs.append(f"ir_version must be {IR_VERSION}")
    for k in ("source", "model", "pages", "report_filters", "features"):
        if k not in ir:
            errs.append(f"missing {k}")
    if errs:
        return errs
    model = ir["model"]
    tables = model.get("tables", {})
    measures = model.get("measures", {})
    for tname, t in tables.items():
        if t.get("kind") not in ("source", "virtual_date", "calculated"):
            errs.append(f"table {tname}: bad kind {t.get('kind')}")
        if t.get("support") not in SUPPORT_LEVELS:
            errs.append(f"table {tname}: bad support")
        for cname, c in t.get("columns", {}).items():
            if c.get("support") not in SUPPORT_LEVELS:
                errs.append(f"column {tname}[{cname}]: bad support")
    for mname, m in measures.items():
        if m.get("status") not in ("ready", "held"):
            errs.append(f"measure {mname}: bad status")
        if m.get("status") == "ready" and not m.get("translation"):
            errs.append(f"measure {mname}: ready without translation")
    for p in ir["pages"]:
        for v in p.get("visuals", []):
            where = f"{p.get('name')}/{v.get('id')}"
            if v.get("kind") not in VISUAL_KINDS:
                errs.append(f"visual {where}: bad kind {v.get('kind')}")
            if v.get("support") not in SUPPORT_LEVELS:
                errs.append(f"visual {where}: bad support")
            for role, fs in (v.get("fields") or {}).items():
                for f in fs:
                    if f.get("kind") not in FIELD_KINDS:
                        errs.append(f"visual {where}: field kind {f.get('kind')}")
                    elif f["kind"] == "measure" and f.get("name") not in measures:
                        errs.append(f"visual {where}: unknown measure {f.get('name')}")
                    elif f["kind"] != "measure" and (
                            f.get("table") not in tables
                            or f.get("name") not in tables[f["table"]]["columns"]):
                        errs.append(f"visual {where}: unknown column "
                                    f"{f.get('table')}[{f.get('name')}]")
    for f in ir["features"]:
        if f.get("support") not in SUPPORT_LEVELS:
            errs.append(f"feature {f.get('item')}: bad support")
    return errs
