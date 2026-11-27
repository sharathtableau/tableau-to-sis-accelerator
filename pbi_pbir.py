"""
pbi_pbir.py  --  read a Power BI report saved in the PBIR folder format.

Source-specific (Power BI only). Reads `<Name>.Report/definition/` of a PBIP
project: report.json, pages/pages.json, pages/<id>/page.json and
pages/<id>/visuals/<id>/visual.json. Returns plain dicts; it does not decide
what is supported (pbi_parser.py does that).

What is read from each visual: visualType, position, title, the field
projections per query role (Category, Y, Values, Rows, ...), sort and
visual-level filters. Field expressions understood: Column, Measure,
Aggregation (implicit measures) and HierarchyLevel (date hierarchies).
Anything else is returned with kind "unknown" and its raw JSON, so the
conversion report can name it.

The older single-file format (PBIR-legacy, `report.json` with "sections")
is detected and refused with an actionable message.

Format reference: Microsoft "Power BI Desktop project report folder"
(learn.microsoft.com/power-bi/developer/projects/projects-report) and the
published JSON schemas at developer.microsoft.com/json-schemas/fabric/item/report.
"""

import json
import os
import re

# Power BI QueryAggregateFunction enum (Aggregation.Function).
# 0/1/3/4/6/7/8 are unambiguous; 2 and 5 are both "count" flavours.
AGG_FUNCTIONS = {0: "sum", 1: "avg", 2: "count", 3: "min", 4: "max",
                 5: "count_non_null", 6: "median", 7: "stdev", 8: "var"}


class PbirFormatError(ValueError):
    pass


def _load(path):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


# --------------------------------------------------------------------------- #
# literals and field expressions
# --------------------------------------------------------------------------- #

def parse_literal(v):
    """PBIR literal text -> Python value. Strings are 'quoted' ('' escapes),
    ints end in L, doubles in D, decimals in M, datetimes are datetime'...'."""
    if v is None:
        return None
    s = str(v)
    if s == "null":
        return None
    if s in ("true", "false"):
        return s == "true"
    if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
        return s[1:-1].replace("''", "'")
    m = re.match(r"^datetime'(.*)'$", s)
    if m:
        return {"datetime": m.group(1)}
    m = re.match(r"^(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)([LDM])?$", s)
    if m:
        num = m.group(1)
        return int(num) if (m.group(2) == "L" and "." not in num) else float(num)
    return {"raw": s}


def _entity(expr, aliases):
    sr = (expr or {}).get("SourceRef") or {}
    if "Entity" in sr:
        return sr["Entity"]
    if "Source" in sr:
        return aliases.get(sr["Source"], sr["Source"])
    return None


def parse_field(field, aliases=None):
    """A PBIR field expression -> {kind, table, name, agg?, level?, raw?}."""
    aliases = aliases or {}
    if not isinstance(field, dict):
        return {"kind": "unknown", "raw": field}
    if "Column" in field:
        c = field["Column"]
        return {"kind": "column", "table": _entity(c.get("Expression"), aliases),
                "name": c.get("Property")}
    if "Measure" in field:
        c = field["Measure"]
        return {"kind": "measure", "table": _entity(c.get("Expression"), aliases),
                "name": c.get("Property")}
    if "Aggregation" in field:
        a = field["Aggregation"]
        inner = parse_field(a.get("Expression"), aliases)
        fn = a.get("Function")
        return {"kind": "aggregation", "table": inner.get("table"),
                "name": inner.get("name"), "agg": AGG_FUNCTIONS.get(fn, f"function_{fn}"),
                "inner_kind": inner.get("kind")}
    if "HierarchyLevel" in field:
        h = field["HierarchyLevel"]
        level = h.get("Level")
        hier = (h.get("Expression") or {}).get("Hierarchy") or {}
        pvs = (hier.get("Expression") or {}).get("PropertyVariationSource") or {}
        if pvs:
            return {"kind": "date_level", "table": _entity(pvs.get("Expression"), aliases),
                    "name": pvs.get("Property"), "level": level,
                    "hierarchy": hier.get("Hierarchy")}
        return {"kind": "hierarchy_level", "table": _entity(hier.get("Expression"), aliases),
                "name": hier.get("Hierarchy"), "level": level}
    return {"kind": "unknown", "raw": field}


# --------------------------------------------------------------------------- #
# filters
# --------------------------------------------------------------------------- #

_CMP = {0: "=", 1: ">", 2: ">=", 3: "<", 4: "<="}


def _condition(cond, aliases):
    """One semantic-query Where condition -> list of {field, op, values}|None."""
    if "In" in cond:
        ins = cond["In"]
        exprs = ins.get("Expressions") or []
        vals = ins.get("Values") or []
        if len(exprs) != 1:
            return None
        values = [parse_literal((row[0].get("Literal") or {}).get("Value"))
                  for row in vals if row]
        return [{"field": parse_field(exprs[0], aliases), "op": "in", "values": values}]
    if "Not" in cond:
        inner = _condition(cond["Not"].get("Expression") or {}, aliases)
        if inner and len(inner) == 1 and inner[0]["op"] == "in":
            return [dict(inner[0], op="not_in")]
        return None
    if "Comparison" in cond:
        c = cond["Comparison"]
        op = _CMP.get(c.get("ComparisonKind"))
        right = (c.get("Right") or {}).get("Literal")
        if op is None or right is None:
            return None
        return [{"field": parse_field(c.get("Left"), aliases), "op": op,
                 "values": [parse_literal(right.get("Value"))]}]
    if "And" in cond:
        a = cond["And"]
        left = _condition(a.get("Left") or {}, aliases)
        right = _condition(a.get("Right") or {}, aliases)
        if left is None or right is None:
            return None
        return left + right
    return None


def parse_filter(f, scope):
    """A filterConfig entry -> normalized filter dict (never dropped).

    `conditions` is None when the filter shape is not understood; the caller
    must then report it rather than apply a partial filter."""
    out = {"name": f.get("name"), "scope": scope, "type": f.get("type"),
           "field": parse_field(f.get("field")), "conditions": None,
           "is_hidden": bool(f.get("isHiddenInViewMode")),
           "is_locked": bool(f.get("isLockedInViewMode")),
           "raw": f}
    q = f.get("filter")
    if not q:
        out["conditions"] = []          # declared but nothing selected
        return out
    aliases = {s.get("Name"): s.get("Entity") for s in q.get("From") or []}
    conds = []
    for w in q.get("Where") or []:
        c = _condition(w.get("Condition") or {}, aliases)
        if c is None:
            return out                   # conditions stays None -> unsupported
        conds.extend(c)
    out["conditions"] = conds
    return out


def _filters(obj, scope):
    fc = (obj or {}).get("filterConfig") or {}
    return [parse_filter(f, scope) for f in fc.get("filters") or []]


# --------------------------------------------------------------------------- #
# visuals and pages
# --------------------------------------------------------------------------- #

def _title(visual):
    for key in ("visualContainerObjects", "objects"):
        objs = (visual.get("visual") or {}).get(key) or {}
        for t in objs.get("title") or []:
            props = t.get("properties") or {}
            txt = ((props.get("text") or {}).get("expr") or {}).get("Literal")
            if txt:
                v = parse_literal(txt.get("Value"))
                if isinstance(v, str):
                    return v
    return None


def _sort(query):
    out = []
    for s in ((query or {}).get("sortDefinition") or {}).get("sort") or []:
        d = str(s.get("direction", "Ascending")).lower()
        out.append({"field": parse_field(s.get("field")),
                    "direction": "desc" if d.startswith("desc") else "asc"})
    return out


def parse_visual(data, path=None):
    v = data.get("visual") or {}
    query = v.get("query") or {}
    roles = {}
    for role, spec in (query.get("queryState") or {}).items():
        roles[role] = []
        for p in (spec or {}).get("projections") or []:
            fld = parse_field(p.get("field"))
            fld["query_ref"] = p.get("queryRef")
            fld["display_name"] = p.get("displayName")
            roles[role].append(fld)
    pos = data.get("position") or {}
    textbox = None
    if v.get("visualType") == "textbox":
        paras = (((v.get("objects") or {}).get("general") or [{}])[0]
                 .get("properties") or {}).get("paragraphs")
        if paras:
            textbox = " ".join(r.get("value", "") for p in paras
                               for r in p.get("textRuns") or [])
    return {
        "id": data.get("name"),
        "visual_type": v.get("visualType") or ("group" if data.get("visualGroup") else None),
        "title": _title(data),
        "position": {"x": pos.get("x", 0), "y": pos.get("y", 0), "z": pos.get("z", 0),
                     "width": pos.get("width", 0), "height": pos.get("height", 0)},
        "roles": roles,
        "sort": _sort(query),
        "filters": _filters(data, "visual"),
        "is_hidden": bool(data.get("isHidden")),
        "parent_group": data.get("parentGroupName"),
        "text": textbox,
        "has_drill": bool(v.get("drillFilterOtherVisuals")),
        "path": path,
    }


def read_report_folder(report_dir):
    """Read a `<Name>.Report` folder (PBIR). Returns a report dict."""
    definition = os.path.join(report_dir, "definition")
    if not os.path.isdir(definition):
        if os.path.exists(os.path.join(report_dir, "report.json")):
            raise PbirFormatError(
                "This report is saved in the older PBIR-legacy format (a single "
                "report.json). Open it in Power BI Desktop, enable the PBIR "
                "format (File > Options > Preview features, or a release where "
                "PBIR is the default) and save the project again.")
        raise PbirFormatError(f"No PBIR definition folder in {report_dir}")
    rep = {"pages": [], "filters": [], "dataset_reference": None, "unparsed": []}
    pbir = os.path.join(report_dir, "definition.pbir")
    if os.path.exists(pbir):
        rep["dataset_reference"] = _load(pbir).get("datasetReference")
    rj = os.path.join(definition, "report.json")
    if os.path.exists(rj):
        rep["filters"] = _filters(_load(rj), "report")
    pages_dir = os.path.join(definition, "pages")
    order = []
    pj = os.path.join(pages_dir, "pages.json")
    if os.path.exists(pj):
        meta = _load(pj)
        order = meta.get("pageOrder") or []
        rep["active_page"] = meta.get("activePageName")
    ids = [d for d in sorted(os.listdir(pages_dir))
           if os.path.isdir(os.path.join(pages_dir, d))] if os.path.isdir(pages_dir) else []
    ids = [i for i in order if i in ids] + [i for i in ids if i not in order]
    for pid in ids:
        pdir = os.path.join(pages_dir, pid)
        page_json = os.path.join(pdir, "page.json")
        if not os.path.exists(page_json):
            rep["unparsed"].append({"kind": "page-without-page.json", "id": pid})
            continue
        pd = _load(page_json)
        page = {"id": pd.get("name", pid), "name": pd.get("displayName") or pid,
                "width": pd.get("width"), "height": pd.get("height"),
                "hidden": str(pd.get("visibility", "")).lower() == "hiddeninviewmode",
                "filters": _filters(pd, "page"),
                "has_custom_interactions": bool(pd.get("visualInteractions")),
                "visuals": []}
        vdir = os.path.join(pdir, "visuals")
        if os.path.isdir(vdir):
            for vid in sorted(os.listdir(vdir)):
                vj = os.path.join(vdir, vid, "visual.json")
                if os.path.exists(vj):
                    page["visuals"].append(parse_visual(_load(vj), os.path.relpath(vj, report_dir)))
        rep["pages"].append(page)
    return rep
