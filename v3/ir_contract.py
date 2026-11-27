"""V3 structural boundary. Valid shape is NOT proof of Tableau fidelity.

Legacy sheet dictionaries are accepted during migration to scoped semantic IR.
Unsupported sheets remain explicit placeholders; malformed IR cannot be bundled.
"""
from dataclasses import asdict, dataclass
import math
from worksheet_semantics import validate_sheet

VERSION = "3.0-boundary-3"
# Shared by validation, runtime dispatch, and compatibility reporting.
CHART_HANDLERS = {
    "kpi": "r_kpi", "mbar": "r_mbar", "bar": "r_bar",
    "line": "r_timeseries", "area": "r_timeseries", "scatter": "r_scatter",
    "heatmap": "r_heatmap", "circle": "r_circle", "map": "r_map",
    "pctbar": "r_pctbar", "dots": "r_dots", "gantt": "r_gantt",
    "bubbles": "r_bubbles", "strips": "r_strips", "dtbar": "r_dtbar",
    "treemap": "r_treemap", "pie": "r_pie", "dual": "r_dual", "table": "r_table",
    "planned": "r_planned",          # release 26: exact plan from the workbook shelves (v3_sheet_plan)
}
# Recognition only: combinations still require semantic verification.
KNOWN_MARKS = frozenset({"Automatic", "Bar", "Line", "Area", "Circle", "Square",
                         "Shape", "Text", "Pie", "GanttBar", "Multipolygon", "Polygon"})


@dataclass(frozen=True)
class Issue:
    code: str
    path: str
    message: str
    severity: str = "ERROR"

    def to_dict(self):
        return asdict(self)


class IRValidationError(ValueError):
    def __init__(self, issues):
        self.issues = issues
        super().__init__("; ".join(f"{i.path}: {i.message}" for i in issues))


def sheet_issues(sheet, path="sheet"):
    if not isinstance(sheet, dict):
        return [Issue("sheet-type", path, "Worksheet must be an object")]
    issues = []
    if not isinstance(sheet.get("name"), str) or not sheet["name"].strip():
        issues.append(Issue("sheet-name", path + ".name", "Worksheet needs a nonempty name"))
    kind = sheet.get("kind")
    if not isinstance(kind, str) or (kind not in CHART_HANDLERS and kind != "unsupported"):
        issues.append(Issue("chart-unsupported", path + ".kind",
                            f"No registered renderer for {kind!r}"))
    for key in ("measures", "ys", "filters", "applied_filters"):
        if key in sheet and not isinstance(sheet[key], list):
            issues.append(Issue("list-type", path + "." + key, f"{key} must be a list"))
    support = sheet.get("support")
    if support is not None and (not isinstance(support, dict) or
                                support.get("status") not in {"PARTIAL", "UNVERIFIED", "UNSUPPORTED", "INVALID"}):
        issues.append(Issue("support-state", path + ".support", "Invalid support status"))
    for key in ("measures", "ys"):
        if isinstance(sheet.get(key), list):
            for idx, value in enumerate(sheet[key]):
                if not isinstance(value, dict) or not isinstance(value.get("caption"), str):
                    issues.append(Issue("measure-reference", f"{path}.{key}[{idx}]",
                                        "Measure requires an explicit caption reference"))
    if kind == 'planned' or sheet.get('sheet_plan') is not None:
        from v3_sheet_plan import validate as validate_plan
        for problem in validate_plan(sheet.get('sheet_plan')):
            issues.append(Issue('sheet-plan', path + '.sheet_plan', problem))
    if sheet.get('ai_adaptation'):
        try:
            from cortex_assist import validate
            validate(sheet)
        except Exception as exc:
            issues.append(Issue('ai-adaptation', path, str(exc)))
    issues.extend(Issue(code, path + '.' + relative, message)
                  for code, relative, message in validate_sheet(sheet))
    return issues


ZONE_LEAVES = ("control", "legend", "title", "blank", "image", "nav", "toggle", "unsupported_zone")


def layout_issues(node, names, path="layout", ancestors=None):
    ancestors = set() if ancestors is None else ancestors
    if not isinstance(node, dict):
        return [Issue("layout-type", path, "Layout node must be an object")]
    if id(node) in ancestors:
        return [Issue("layout-cycle", path, "Layout must be an acyclic tree")]
    ancestors = ancestors | {id(node)}
    issues = []
    tags = [key for key in ("sheet", "text", "children") + ZONE_LEAVES if key in node]
    if len(tags) != 1:
        return [Issue("layout-node", path, "Node must be exactly one sheet, text, zone, or container")]
    for key in ("w", "h"):
        if key in node and (isinstance(node[key], bool) or
                           not isinstance(node[key], (float, int)) or
                           not math.isfinite(node[key]) or node[key] < 0):
            issues.append(Issue("layout-geometry", path + "." + key,
                                "Geometry must be a finite nonnegative number"))
    if tags[0] == "sheet":
        if not isinstance(node["sheet"], str) or node["sheet"] not in names:
            issues.append(Issue("sheet-reference", path + ".sheet", "Unknown worksheet reference"))
    elif tags[0] == "text":
        if not isinstance(node["text"], str):
            issues.append(Issue("text-type", path + ".text", "Text must be a string"))
    elif tags[0] in ZONE_LEAVES:
        if tags[0] == "control" and node["control"] not in ("filter", "param"):
            issues.append(Issue("layout-control", path + ".control", "Expected a filter or parameter control"))
        if tags[0] == "legend" and (not isinstance(node["legend"], str) or node["legend"] not in names):
            issues.append(Issue("sheet-reference", path + ".legend", "Unknown worksheet reference"))
        for key in ("caption", "nav", "image"):
            if key in node and node[key] is not None and not isinstance(node[key], str):
                issues.append(Issue("layout-text", path + "." + key, "Expected a string"))
    else:
        if node.get("dir") not in ("horz", "vert"):
            issues.append(Issue("layout-direction", path + ".dir", "Expected horz or vert"))
        if not isinstance(node["children"], list):
            issues.append(Issue("layout-children", path + ".children", "Children must be a list"))
        else:
            for idx, child in enumerate(node["children"]):
                issues.extend(layout_issues(child, names, f"{path}.children[{idx}]", ancestors))
        if "backdrop" in node:
            if not isinstance(node["backdrop"], dict) or "image" not in node["backdrop"]:
                issues.append(Issue("layout-backdrop", path + ".backdrop", "A background layer must be an image zone"))
        floating = node.get("floating", [])
        if not isinstance(floating, list):
            issues.append(Issue("layout-floating", path + ".floating", "Floating zones must be a list"))
        else:
            for idx, item in enumerate(floating):
                if not isinstance(item, dict) or not isinstance(item.get("child"), dict) or not all(
                        isinstance(item.get(k), (int, float)) and not isinstance(item.get(k), bool)
                        for k in ("x", "y", "w", "h", "rx", "ry", "rw")):
                    issues.append(Issue("layout-floating", f"{path}.floating[{idx}]", "Floating zone needs a box"))
                else:
                    issues.extend(layout_issues(item["child"], names, f"{path}.floating[{idx}].child", ancestors))
    return issues


def validate_ir(ir):
    if not isinstance(ir, dict) or not isinstance(ir.get("dashboards"), list):
        return [Issue("workbook-type", "dashboards", "Workbook requires a dashboard list")]
    issues = []
    if 'fields' in ir or 'field_scopes' in ir:
        fields, scopes = ir.get('fields'), ir.get('field_scopes')
        if not isinstance(fields, dict) or not isinstance(scopes, dict):
            issues.append(Issue('field-catalog', 'fields', 'Fields and scopes must be objects'))
        else:
            for fid, entry in fields.items():
                if (not isinstance(entry, dict) or entry.get('id') != fid or
                        entry.get('datasource_id') not in scopes):
                    issues.append(Issue('field-catalog', f'fields.{fid}', 'Invalid catalog ownership'))
            def references(value, path, owner=None):
                if isinstance(value, dict):
                    owner = value.get('datasource_id', owner)
                    if owner is not None and owner not in scopes:
                        issues.append(Issue('datasource-reference', path, 'Unknown datasource ID'))
                    bindings = value.get('field_bindings', {})
                    if not isinstance(bindings, dict):
                        issues.append(Issue('field-reference', path + '.field_bindings',
                                            'Field bindings must be an object'))
                        bindings = {}
                    refs = dict(bindings)
                    if value.get('field_id'):
                        refs['field_id'] = value['field_id']
                    for key, fid in refs.items():
                        entry = fields.get(fid) if isinstance(fid, str) else None
                        if not isinstance(entry, dict) or entry.get('datasource_id') != owner:
                            issues.append(Issue('field-reference', path + '.' + key,
                                                'Missing field or incorrect datasource ownership'))
                    for key, child in value.items():
                        references(child, path + '.' + key, owner)
                elif isinstance(value, list):
                    for idx, child in enumerate(value):
                        references(child, f'{path}[{idx}]', owner)
            references(ir['dashboards'], 'dashboards')
    for idx, dash in enumerate(ir["dashboards"]):
        path = f"dashboards[{idx}]"
        if not isinstance(dash, dict) or not isinstance(dash.get("sheets"), list):
            issues.append(Issue("dashboard-type", path, "Dashboard requires a worksheet list"))
            continue
        for key in ("name", "title"):
            if not isinstance(dash.get(key), str):
                issues.append(Issue("dashboard-label", path + "." + key, "Expected a string"))
        names = set()
        for j, sheet in enumerate(dash["sheets"]):
            issues.extend(sheet_issues(sheet, f"{path}.sheets[{j}]"))
            if isinstance(sheet, dict):
                plan = sheet.get('worksheet_semantics', {}).get('table_execution', {})
                if plan.get('status') == 'READY':
                    try:
                        from filter_plan import validate_catalog
                        validate_catalog(plan['filter_plan'], ir.get('fields', {}))
                    except Exception as exc:
                        issues.append(Issue('filter-catalog', f'{path}.sheets[{j}]', str(exc)))
            if isinstance(sheet, dict) and sheet.get('ai_adaptation'):
                try:
                    from cortex_assist import validate
                    validate(sheet, ir.get('fields', {}))
                except Exception as exc:
                    issues.append(Issue('ai-adaptation', f'{path}.sheets[{j}]', str(exc)))
            name = sheet.get("name") if isinstance(sheet, dict) else None
            if isinstance(name, str):
                if name in names:
                    issues.append(Issue("duplicate-sheet", path, f"Duplicate worksheet {name!r}"))
                names.add(name)
        if dash.get("layout") is not None:
            issues.extend(layout_issues(dash["layout"], names, path + ".layout"))
    return issues


def require_valid_ir(ir):
    issues = validate_ir(ir)
    if issues:
        raise IRValidationError(issues)


def assess_ir(ir):
    """Fail closed: this pass never claims data or visual fidelity PASS."""
    issues = validate_ir(ir)
    rows = []
    if isinstance(ir, dict) and isinstance(ir.get("dashboards"), list):
        for dash in ir["dashboards"]:
            if not isinstance(dash, dict) or not isinstance(dash.get("sheets"), list):
                continue
            for sheet in dash["sheets"]:
                if not isinstance(sheet, dict):
                    continue
                support = sheet.get("support") or {}
                if not isinstance(support, dict):
                    support = {"status": "INVALID", "reason": "Invalid support metadata"}
                state = support.get("status", "UNVERIFIED")
                if not isinstance(sheet.get("kind"), str) or sheet.get("kind") not in CHART_HANDLERS:
                    state = "UNSUPPORTED"
                if sheet.get('ai_adaptation'):
                    try:
                        from cortex_assist import validate
                        validate(sheet, ir.get('fields', {}))
                        state = 'PARTIAL'
                        support = {'reason': 'AI visual substitution; numeric/visual parity NOT_VERIFIED'}
                    except Exception as exc:
                        state = 'INVALID'
                        support = {'reason': str(exc)}
                rows.append({"dashboard": dash.get("name"), "sheet": sheet.get("name"),
                             "kind": sheet.get("kind"), "state": state,
                             "reason": support.get("reason", "Semantics and fidelity require validation")})
    dropped = list((ir.get("calc_drops") or {}).keys()) if isinstance(ir, dict) else []
    if isinstance(ir, dict) and isinstance(ir.get('field_scopes'), dict):
        dropped = sorted({fid for scope in ir['field_scopes'].values()
                          for fid in scope.get('calc_drops', {})})
    blocked = bool(issues or dropped or any(r["state"] in {"UNSUPPORTED", "INVALID"} for r in rows))
    return {"version": VERSION, "state": "BLOCKED" if blocked else "NEEDS_REVIEW",
            "issues": [i.to_dict() for i in issues], "sheets": rows,
            "untranslated_calculations": dropped,
            "data_verified": False, "visual_verified": False}
