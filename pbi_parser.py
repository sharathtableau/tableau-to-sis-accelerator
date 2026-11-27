"""
pbi_parser.py  --  Power BI project (PBIP: PBIR report + TMDL model) -> report-ir/1.

Source-specific front end of the Power BI path:

    .pbip / project folder / .zip
      -> pbi_pbir.read_report_folder   (pages, visuals, filters)
      -> pbi_tmdl.read_tmdl_folder     (tables, measures, relationships, roles)
      -> this module: bind tables to Snowflake, translate DAX with dax_rules,
         classify every feature, emit report-ir/1 (report_ir.validate()).

Never silent: every page, visual, measure, column, relationship, role, filter
and Power Query step ends up either converted or as a row in ir["features"]
with its support level and the follow-up it needs.

Only PBIP with the PBIR report format and the TMDL model format is accepted
(MVP decision in the plan). PBIX/PBIT: open in Power BI Desktop and use
File > Save as > Power BI project.
"""

import os
import re
import shutil
import tempfile
import zipfile

import dax_rules as D
import pbi_pbir
import pbi_tmdl
from report_ir import IR_VERSION, feature, validate, worst

# --------------------------------------------------------------------------- #
# input discovery
# --------------------------------------------------------------------------- #


class PbipInputError(ValueError):
    pass


def _safe_extract(zpath):
    """Extract a .zip into a new temp dir, refusing path traversal."""
    out = tempfile.mkdtemp(prefix="pbip_")
    with zipfile.ZipFile(zpath) as z:
        for member in z.infolist():
            target = os.path.realpath(os.path.join(out, member.filename))
            if not target.startswith(os.path.realpath(out) + os.sep) and \
                    target != os.path.realpath(out):
                shutil.rmtree(out, ignore_errors=True)
                raise PbipInputError(f"unsafe path in zip: {member.filename}")
        z.extractall(out)
    return out


def locate(path):
    """Return (project_root, report_dir, pbip_name) for a PBIP input."""
    if not os.path.exists(path):
        raise PbipInputError(f"input not found: {path}")
    low = path.lower()
    if low.endswith((".pbix", ".pbit")):
        raise PbipInputError(
            "PBIX/PBIT files are not read directly (their internal format is not "
            "a documented interface). Open the file in Power BI Desktop and use "
            "File > Save as > Power BI project (*.pbip), with the PBIR report "
            "format and the TMDL model format, then convert the saved folder.")
    if low.endswith(".zip"):
        path = _safe_extract(path)
    root = os.path.dirname(path) if os.path.isfile(path) else path
    pbips = []
    for dirpath, _, files in os.walk(root):
        pbips += [os.path.join(dirpath, f) for f in files if f.lower().endswith(".pbip")]
        if dirpath.count(os.sep) - root.count(os.sep) > 2:
            break
    reports = []
    for dirpath, dirs, _ in os.walk(root):
        reports += [os.path.join(dirpath, d) for d in dirs if d.endswith(".Report")]
    if os.path.isfile(path) and path.lower().endswith(".pbip"):
        name = os.path.splitext(os.path.basename(path))[0]
    elif pbips:
        name = os.path.splitext(os.path.basename(sorted(pbips)[0]))[0]
    else:
        name = None
    if name:
        cand = [r for r in reports if os.path.basename(r) == name + ".Report"]
        if cand:
            return root, cand[0], name
    if len(reports) == 1:
        return root, reports[0], os.path.basename(reports[0])[:-len(".Report")]
    raise PbipInputError(
        f"could not find exactly one <Name>.Report folder under {root} "
        f"(found {len(reports)}). Point at the .pbip file.")


def _model_dir(report_dir, dataset_reference):
    ref = dataset_reference or {}
    if "byPath" in ref:
        rel = (ref["byPath"] or {}).get("path", "")
        md = os.path.normpath(os.path.join(report_dir, rel))
        return md, None
    if "byConnection" in ref:
        return None, ("The report uses a live connection to a published semantic "
                      "model, so the model definition is not in the project. "
                      "Export that model as TMDL (or save it as a PBIP) and point "
                      "the report at it.")
    return None, "definition.pbir has no datasetReference"


# --------------------------------------------------------------------------- #
# Power Query (M) -> Snowflake binding
# --------------------------------------------------------------------------- #

_NAV = re.compile(r'\{\s*\[\s*Name\s*=\s*("(?:[^"]|"")*"|[A-Za-z_][\w.]*)\s*,\s*'
                  r'Kind\s*=\s*"(\w+)"\s*\]\s*\}\s*\[\s*Data\s*\]')
_STEP = re.compile(r'^\s*(#"[^"]+"|[A-Za-z_]\w*)\s*=\s*(.+?)\s*,?\s*$')


def _m_value(tok, params):
    if tok.startswith('"'):
        return tok[1:-1].replace('""', '"')
    return params.get(tok)


def bind_partition(source, params):
    """Read a Power Query partition. Returns (binding|None, steps, problems).

    binding = {"database","schema","object","kind"} when the query is a plain
    Snowflake navigation; steps = list of (name, function) for the report."""
    problems, steps = [], []
    if not source:
        return None, steps, ["partition has no source expression"]
    if "Snowflake.Databases" not in source:
        conn = re.search(r"\b([A-Z][A-Za-z]+\.(?:Database|Databases|Contents|Files|Tables))\(", source)
        return None, steps, [f"source is not Snowflake ({conn.group(1) if conn else 'unknown connector'})"]
    body = source
    m = re.search(r"\blet\b(.*)\bin\b", source, re.S)
    if m:
        body = m.group(1)
    for line in re.split(r",\s*\n", body):
        sm = _STEP.match(line.strip().rstrip(","))
        if sm:
            fn = re.match(r"([A-Za-z_][\w.]*)\s*\(", sm.group(2))
            steps.append((sm.group(1), fn.group(1) if fn else "navigation"))
    found = {}
    for nm in _NAV.finditer(source):
        val = _m_value(nm.group(1), params)
        if val is None:
            problems.append(f"navigation value {nm.group(1)} is not a literal or known parameter")
        found[nm.group(2).lower()] = val
    obj_kind = "table" if "table" in found else ("view" if "view" in found else None)
    if not (found.get("database") and found.get("schema") and obj_kind and found.get(obj_kind)):
        problems.append("could not read database/schema/table from the Snowflake navigation")
        return None, steps, problems
    if "Value.NativeQuery" in source:
        problems.append("native SQL query (Value.NativeQuery) is not converted")
    return ({"database": found["database"], "schema": found["schema"],
             "object": found[obj_kind], "kind": obj_kind}, steps, problems)


_HARMLESS_STEPS = {"Snowflake.Databases", "navigation", "Table.TransformColumnTypes"}


# --------------------------------------------------------------------------- #
# model
# --------------------------------------------------------------------------- #

_TYPE = {"string": "text", "int64": "integer", "double": "number",
         "decimal": "number", "dateTime": "datetime", "boolean": "boolean"}


def _fmt(format_string):
    """Power BI format string -> runtime format token."""
    if not format_string:
        return None
    f = format_string.split(";")[0].replace("\\", "")
    dec = 0
    m = re.search(r"0\.(0+)", f)
    if m:
        dec = len(m.group(1))
    if "%" in f:
        return f"pct{dec}"
    if "$" in f or "€" in f or "£" in f:
        return f"cur{dec}"
    if f.lower() in ("short date", "long date", "general date") or "yy" in f.lower():
        return "date"
    if "#,0" in f or "#,#" in f:
        return f"num{dec}"
    if re.fullmatch(r"0(\.0+)?", f):
        return f"plain{dec}"
    return None


def _is_calendar(partition):
    src = (partition.get("source") or "").upper()
    return partition.get("type") == "calculated" and (
        "CALENDAR(" in src or "CALENDARAUTO(" in src)


def build_model(tm, bindings=None, features=None):
    """TMDL model dict -> report-ir model section (+ feature rows)."""
    bindings = bindings or {}
    feats = features if features is not None else []
    params = {e["name"]: e["value"] for e in tm["expressions"] if e["is_parameter"]}
    for e in tm["expressions"]:
        if not e["is_parameter"]:
            feats.append(feature("power_query", f"shared expression {e['name']}", "manual",
                                 "shared Power Query expression is not converted",
                                 "rebuild it as a Snowflake view if a table depends on it"))
    tables, measures = {}, {}
    rels_by_to = {}
    for r in tm["relationships"]:
        rels_by_to.setdefault(r["to_table"], []).append(r)

    for t in tm["tables"]:
        name = t["name"]
        entry = {"name": name, "kind": "source", "binding": None, "columns": {},
                 "anchors": {}, "support": "auto", "notes": [], "hidden": t["is_hidden"]}
        parts = t["partitions"]
        if any(_is_calendar(p) for p in parts):
            entry["kind"] = "virtual_date"
            anchors = {r["from_table"]: r["from_column"] for r in rels_by_to.get(name, [])
                       if r["active"]}
            entry["anchors"] = anchors
            entry["support"] = "partial" if anchors else "unsupported"
            entry["notes"].append(
                "calculated date table (CALENDAR) is not materialized; its dates are "
                "derived from the related fact date column, so dates with no facts do "
                "not appear and time-intelligence functions are not available")
            feats.append(feature("model", f"table {name} (calculated date table)",
                                 entry["support"], entry["notes"][0],
                                 "materialize a date dimension in Snowflake if empty "
                                 "periods or time intelligence matter"))
        elif any(p.get("type") == "calculated" for p in parts):
            entry["kind"] = "calculated"
            entry["support"] = "unsupported"
            entry["notes"].append("calculated table (DAX) is not converted")
            feats.append(feature("model", f"table {name} (calculated table)", "unsupported",
                                 "DAX calculated tables are not converted",
                                 "create the table as a Snowflake view"))
        else:
            mparts = [p for p in parts if p.get("type") == "m"]
            if len(parts) != 1 or not mparts:
                entry["support"] = "manual"
                entry["notes"].append(f"{len(parts)} partition(s); only one Power Query partition is supported")
            override = bindings.get("tables", {}).get(name)
            if mparts:
                b, steps, problems = bind_partition(mparts[0]["source"], params)
                extra = [s for s in steps if s[1] not in _HARMLESS_STEPS]
                if extra:
                    entry["support"] = worst(entry["support"], "manual")
                    feats.append(feature(
                        "power_query", f"table {name}", "manual",
                        "Power Query steps not converted: " +
                        ", ".join(f"{s[0]} ({s[1]})" for s in extra),
                        "move these transformations into a Snowflake view and bind the table to it"))
                for p in problems:
                    feats.append(feature("power_query", f"table {name}", "manual", p,
                                         "bind the table explicitly with --bind"))
                if b:
                    entry["binding"] = b
                    feats.append(feature("power_query", f"table {name}", "auto",
                                         f"bound to {b['database']}.{b['schema']}.{b['object']} "
                                         "from the Snowflake connector navigation"))
            if override:
                parts3 = override.split(".")
                if len(parts3) != 3:
                    raise PbipInputError(f"--bind {name}={override}: use DATABASE.SCHEMA.OBJECT")
                entry["binding"] = {"database": parts3[0], "schema": parts3[1],
                                    "object": parts3[2], "kind": "override"}
            if entry["binding"]:
                if bindings.get("database"):
                    entry["binding"]["database"] = bindings["database"]
                if bindings.get("schema"):
                    entry["binding"]["schema"] = bindings["schema"]
            else:
                entry["support"] = "manual"
                entry["notes"].append("no Snowflake table bound")

        for c in t["columns"]:
            col = {"name": c["name"], "data_type": _TYPE.get(c["data_type"], c["data_type"]),
                   "physical": c["source_column"], "sql": None, "dax": c["expression"],
                   "format": _fmt(c["format_string"]), "sort_by": c["sort_by"],
                   "hidden": c["is_hidden"], "support": "auto", "notes": []}
            if entry["kind"] == "virtual_date" and not c["expression"]:
                # the key column of the calendar: becomes the fact date
                col["support"] = entry["support"]
            elif c["expression"] is None and not c["source_column"]:
                col["support"] = "manual"
                col["notes"].append("column has neither a source column nor an expression")
            entry["columns"][c["name"]] = col
        for h in t.get("hierarchies") or []:
            feats.append(feature("model", f"hierarchy {name}.{h}", "partial",
                                 "user hierarchies are not offered as drill-down; "
                                 "each level remains usable as a field"))
        for u in t.get("unparsed") or []:
            feats.append(feature("model", f"{name}: {u['kind']}", "unsupported",
                                 f"TMDL object not converted (line {u['line']}): {u['text'][:80]}"))
        tables[name] = entry
        for ms in t["measures"]:
            measures[ms["name"]] = {"name": ms["name"], "table": name,
                                    "dax": ms["expression"], "format": _fmt(ms["format_string"]),
                                    "hidden": ms["is_hidden"], "status": "held",
                                    "translation": None, "support": "manual", "notes": []}

    def col_ok(table, column):
        t = tables.get(table)
        return bool(t and column in t["columns"] and t["kind"] != "calculated")

    # calculated columns (row level)
    for tname, t in tables.items():
        for cname, c in t["columns"].items():
            if not c["dax"]:
                continue
            try:
                tr = D.translate_column(c["dax"], tname, col_ok)
                c["sql"], c["support"] = tr["sql"], worst(tr["support"], t["support"])
                c["notes"] += tr["notes"]
                feats.append(feature("calculated_column", f"{tname}[{cname}]", c["support"],
                                     "row-level DAX translated to SQL" +
                                     (": " + "; ".join(tr["notes"]) if tr["notes"] else ""),
                                     "spot-check values against Power BI"))
            except D.Unsupported as e:
                c["support"] = "manual"
                c["notes"].append(str(e))
                feats.append(feature("calculated_column", f"{tname}[{cname}]", "manual",
                                     f"not translated: {e}", "add it to a Snowflake view"))

    # measures
    env = D.MeasureEnv(col_ok,
                       lambda n: measures[n]["dax"] if n in measures else None,
                       lambda tb: tb in tables and tables[tb]["kind"] == "source")
    for mname, m in measures.items():
        try:
            tr = D.translate_measure(mname, env)
            m.update(status="ready", translation=tr, support=tr["support"],
                     notes=list(tr["notes"]))
            feats.append(feature("measure", mname, tr["support"],
                                 "translated by rule" +
                                 (": " + "; ".join(tr["notes"]) if tr["notes"] else ""),
                                 "" if tr["support"] == "auto" else
                                 "check totals and filtered values against Power BI"))
        except D.Unsupported as e:
            m["notes"].append(str(e))
            feats.append(feature("measure", mname, "manual",
                                 f"held (not deployed): {e}",
                                 "translate and prove against the published report "
                                 "(Phase 2: Cortex proposal + numeric proof)"))

    rels = []
    for r in tm["relationships"]:
        entry = {k: r[k] for k in ("from_table", "from_column", "to_table", "to_column",
                                   "active", "cross_filter")}
        entry["support"], entry["notes"] = "auto", []
        to = tables.get(r["to_table"])
        if not r["active"]:
            entry["support"] = "unsupported"
            entry["notes"].append("inactive relationship (USERELATIONSHIP) is not used")
        elif to and to["kind"] == "virtual_date":
            entry["support"] = "partial"
            entry["notes"].append("relationship to the calculated date table is resolved "
                                  "by deriving dates from the fact column")
        elif r["cross_filter"] == "bothDirections":
            entry["support"] = "partial"
            entry["notes"].append("bi-directional filtering is applied fact-to-dimension only")
        if (r["from_cardinality"], r["to_cardinality"]) not in (("many", "one"),):
            entry["support"] = worst(entry["support"], "manual")
            entry["notes"].append(f"{r['from_cardinality']}-to-{r['to_cardinality']} "
                                  "relationship is not supported")
        rels.append(entry)
        feats.append(feature("relationship",
                             f"{r['from_table']}[{r['from_column']}] -> {r['to_table']}[{r['to_column']}]",
                             entry["support"], "; ".join(entry["notes"]) or "many-to-one join"))

    roles = []
    for r in tm["roles"]:
        rr = {"name": r["name"], "table_permissions": r["table_permissions"],
              "support": "manual", "notes": [], "proposed_policy_sql": []}
        for tb, expr in r["table_permissions"].items():
            rr["proposed_policy_sql"].append(_propose_policy(r["name"], tb, expr, tables))
        rr["notes"].append("Power BI roles are not enforced by the generated app. "
                           "A Snowflake row access policy must be reviewed, applied and "
                           "signed off before production.")
        roles.append(rr)
        feats.append(feature("security", f"role {r['name']}", "manual",
                             "row-level security role; a row access policy is proposed "
                             "in the report, not applied",
                             "map Power BI role members to Snowflake roles, apply the "
                             "policy, and test as each role; see the runtime note on "
                             "owner's rights"))
    return {"tables": tables, "measures": measures, "relationships": rels,
            "roles": roles, "parameters": params}


def _propose_policy(role, table, expr, tables):
    """Best-effort Snowflake row access policy text for a simple role filter.

    Returned as text for review; never executed by the converter."""
    t = tables.get(table) or {}
    m = re.match(r"^\s*'?([^'\[]+)'?\[([^\]]+)\]\s*=\s*\"((?:[^\"]|\"\")*)\"\s*$", expr or "")
    if not m or not t.get("binding"):
        return f"-- role {role} on {table}: filter {expr!r} needs a manual policy"
    col = t["columns"].get(m.group(2), {})
    phys = col.get("physical") or m.group(2)
    val = m.group(3).replace('""', '"').replace("'", "''")
    b = t["binding"]
    pname = re.sub(r"\W+", "_", f"RAP_{table}_{role}").upper()
    return (f'CREATE ROW ACCESS POLICY "{b["database"]}"."{b["schema"]}"."{pname}"\n'
            f"  AS (v VARCHAR) RETURNS BOOLEAN ->\n"
            f"  NOT IS_ROLE_IN_SESSION('<SNOWFLAKE_ROLE_FOR_{re.sub(chr(39), '', role)}>') "
            f"OR v = '{val}';\n"
            f'-- ALTER TABLE "{b["database"]}"."{b["schema"]}"."{b["object"]}" '
            f'ADD ROW ACCESS POLICY "{pname}" ON ("{phys}");')


# --------------------------------------------------------------------------- #
# visuals
# --------------------------------------------------------------------------- #

# Power BI visualType -> (IR kind, support, note)
VISUAL_MAP = {
    "card": ("kpi", "auto", None),
    "cardVisual": ("kpi", "partial", "new card visual: only the values are shown"),
    "multiRowCard": ("kpi", "partial", "multi-row card shown as KPI tiles"),
    "kpi": ("kpi", "partial", "KPI visual: target and trend are not drawn"),
    "clusteredBarChart": ("bar", "auto", None),
    "stackedBarChart": ("bar", "auto", None),
    "clusteredColumnChart": ("column", "auto", None),
    "stackedColumnChart": ("column", "auto", None),
    "hundredPercentStackedBarChart": ("stacked100", "partial",
                                      "100% stacked shares are computed in the app"),
    "hundredPercentStackedColumnChart": ("stacked100", "partial",
                                         "100% stacked shares are computed in the app"),
    "lineChart": ("line", "auto", None),
    "areaChart": ("area", "auto", None),
    "stackedAreaChart": ("area", "auto", None),
    "scatterChart": ("scatter", "auto", None),
    "tableEx": ("table", "auto", None),
    "pivotTable": ("matrix", "partial", "matrix rendered as a pivot without subtotals"),
    "lineClusteredColumnComboChart": ("combo", "partial", "combo chart: second axis is independent"),
    "lineStackedColumnComboChart": ("combo", "partial", "combo chart: second axis is independent"),
    "slicer": ("slicer", "auto", None),
    "advancedSlicerVisual": ("slicer", "partial", "button slicer shown as a list slicer"),
    "textbox": ("text", "partial", "text box shown as plain text"),
}

_ROLE_MAP = {
    "Category": "category", "Axis": "category", "Rows": "rows", "Columns": "columns",
    "Y": "values", "Values": "values", "Y2": "values2", "Series": "series",
    "Legend": "series", "X": "x", "Size": "size", "Details": "category",
    "Data": "values", "Fields": "values",
}

_DATE_LEVELS = {"Year": "year", "Quarter": "quarter", "Month": "month", "Day": "day"}


def _field(f, model, notes):
    k, t, n = f.get("kind"), f.get("table"), f.get("name")
    label = f.get("display_name") or n
    if k == "measure":
        if n in model["measures"]:
            return {"kind": "measure", "table": t, "name": n, "label": label}
        notes.append(f"unknown measure {t}[{n}] dropped")
        return None
    if k in ("column", "aggregation", "date_level"):
        tb = model["tables"].get(t)
        if not tb or n not in tb["columns"]:
            notes.append(f"unknown column {t}[{n}] dropped")
            return None
        out = {"kind": k, "table": t, "name": n, "label": label}
        if k == "aggregation":
            out["agg"] = f.get("agg")
            out["label"] = f.get("display_name") or f"{(f.get('agg') or '').title()} of {n}"
        if k == "date_level":
            lv = _DATE_LEVELS.get(f.get("level"))
            if not lv:
                notes.append(f"date hierarchy level {f.get('level')} dropped")
                return None
            out["level"] = lv
            out["label"] = f"{n} ({f.get('level')})"
        return out
    notes.append(f"field expression not understood: {str(f.get('raw'))[:80]}")
    return None


def _filters(raw_filters, model, feats, where):
    out = []
    for f in raw_filters:
        label = f"{where}: filter {f.get('name') or ''}".strip()
        if f["conditions"] is None:
            feats.append(feature("filter", label, "unsupported",
                                 f"{f.get('type') or 'advanced'} filter shape is not converted "
                                 "(top N, relative date and advanced filters are out of MVP scope)",
                                 "recreate it as a Snowflake view filter or a slicer"))
            continue
        for c in f["conditions"]:
            notes = []
            fld = _field(c["field"], model, notes)
            if fld is None:
                feats.append(feature("filter", label, "manual", "; ".join(notes)))
                continue
            vals = [v for v in c["values"] if not isinstance(v, dict) or "datetime" in v]
            vals = [v["datetime"] if isinstance(v, dict) else v for v in vals]
            out.append({"field": fld, "op": c["op"], "values": vals, "scope": f["scope"]})
            feats.append(feature("filter", label, "auto",
                                 f"{fld['table']}[{fld['name']}] {c['op']} {vals}"))
    return out


def build_visual(v, model, feats, page_name):
    notes = []
    vt = v["visual_type"] or "unknown"
    kind, support, note = VISUAL_MAP.get(vt, ("unsupported", "unsupported", None))
    if kind == "unsupported":
        note = f"visual type '{vt}' is not converted in this release"
    if note:
        notes.append(note)
    fields = {}
    for role, fs in (v.get("roles") or {}).items():
        target = _ROLE_MAP.get(role)
        if target is None:
            if fs:
                notes.append(f"field well '{role}' is not used")
                support = worst(support, "partial")
            continue
        for f in fs:
            fld = _field(f, model, notes)
            if fld:
                fields.setdefault(target, []).append(fld)
    if kind == "kpi" and fields.get("values"):
        pass
    if kind in ("bar", "column", "line", "area", "stacked100", "combo") \
            and len(fields.get("category", [])) > 1:
        notes.append("hierarchy on the axis: only the top level is shown (no drill-down)")
        support = worst(support, "partial")
        fields["category"] = fields["category"][:1]
    if len(fields.get("series", [])) > 1:
        fields["series"] = fields["series"][:1]
        notes.append("only the first legend field is used")
        support = worst(support, "partial")
    held = []
    for role in ("values", "values2", "x", "size"):
        for f in fields.get(role, []):
            if f["kind"] == "measure":
                m = model["measures"][f["name"]]
                if m["status"] == "held":
                    held.append(f["name"])
                else:
                    support = worst(support, m["support"])
            elif f["kind"] == "aggregation":
                try:
                    D.implicit_measure(f["table"], f["name"], f.get("agg"))
                except D.Unsupported as e:
                    notes.append(str(e))
                    support = worst(support, "partial")
    for role, fs in fields.items():
        for f in fs:
            if f["kind"] in ("column", "date_level"):
                c = model["tables"][f["table"]]["columns"][f["name"]]
                support = worst(support, "partial" if c["support"] != "auto" else "auto")
                if c["support"] != "auto":
                    why = (c["notes"] or model["tables"][f["table"]]["notes"] or
                           ["derived column"])[0]
                    n = f"{f['table']}[{f['name']}] is {c['support']}: {why}"
                    if n not in notes:
                        notes.append(n)
                if c["support"] == "manual":
                    held.append(f"{f['table']}[{f['name']}]")
    if held:
        notes.append("pending verification (held): " + ", ".join(held))
        support = worst(support, "partial")
    if kind != "unsupported" and v.get("has_drill") is False:
        pass
    sort = []
    for s in v.get("sort") or []:
        fl = _field(s["field"], model, [])
        if fl:
            sort.append({"field": fl, "direction": s["direction"]})
    title = v.get("title") or (v.get("text") if kind == "text" else None)
    out = {"id": v["id"], "source_type": vt, "kind": kind, "title": title,
           "position": v["position"], "fields": fields, "sort": sort,
           "filters": _filters(v.get("filters") or [], model, feats,
                               f"{page_name}/{title or v['id']}"),
           "support": support, "notes": notes, "held_measures": held,
           "text": v.get("text")}
    feats.append(feature("visual", f"{page_name}: {title or vt}", support,
                         f"{vt} -> {kind}" + ("; " + "; ".join(notes) if notes else ""),
                         "" if support == "auto" else
                         "compare this visual with the Power BI report"))
    return out


# --------------------------------------------------------------------------- #
# top level
# --------------------------------------------------------------------------- #

def parse_pbip(path, bindings=None):
    """PBIP input -> (report-ir/1 dict). Raises PbipInputError on bad input."""
    root, report_dir, name = locate(path)
    feats = []
    try:
        rep = pbi_pbir.read_report_folder(report_dir)
    except pbi_pbir.PbirFormatError as e:
        raise PbipInputError(str(e)) from None
    mdir, problem = _model_dir(report_dir, rep.get("dataset_reference"))
    if problem:
        raise PbipInputError(problem)
    tm = pbi_tmdl.read_tmdl_folder(os.path.join(mdir, "definition"))
    for u in tm["unparsed"]:
        feats.append(feature("model", u["kind"], "unsupported",
                             f"TMDL object not converted: {u['text'][:80]}"))
    model = build_model(tm, bindings, feats)
    pages = []
    for p in rep["pages"]:
        page = {"id": p["id"], "name": p["name"], "hidden": p["hidden"],
                "width": p["width"], "height": p["height"],
                "filters": _filters(p["filters"], model, feats, p["name"]),
                "visuals": []}
        if p["hidden"]:
            feats.append(feature("page", p["name"], "partial",
                                 "hidden page (used for drill-through or tooltips) is not shown"))
        else:
            feats.append(feature("page", p["name"], "auto", "page -> navigation entry"))
        if p["has_custom_interactions"]:
            feats.append(feature("interaction", p["name"], "partial",
                                 "custom visual interactions are not reproduced; every slicer "
                                 "filters every visual on its page"))
        for v in sorted(p["visuals"], key=lambda x: (x["position"]["y"], x["position"]["x"])):
            if v.get("is_hidden"):
                feats.append(feature("visual", f"{p['name']}: {v.get('title') or v['visual_type']}",
                                     "partial", "hidden visual is not shown"))
                continue
            if v["visual_type"] == "group":
                continue
            page["visuals"].append(build_visual(v, model, feats, p["name"]))
        pages.append(page)
    feats.append(feature("interaction", "cross-filtering between visuals", "unsupported",
                         "clicking a visual does not filter the others; slicers do",
                         "use slicers, or Phase 2 interaction support"))
    feats.append(feature("interaction", "drill-down / drill-through / tooltips pages",
                         "unsupported", "not converted in this release"))
    ir = {"ir_version": IR_VERSION,
          "source": {"kind": "powerbi", "format": "pbip (PBIR report + TMDL model)",
                     "name": name, "path": os.path.basename(path.rstrip("/\\"))},
          "model": model, "pages": pages,
          "report_filters": _filters(rep["filters"], model, feats, "report"),
          "features": feats}
    errs = validate(ir)
    if errs:
        raise AssertionError("parser produced an invalid IR: " + "; ".join(errs[:10]))
    return ir
