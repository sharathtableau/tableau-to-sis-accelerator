"""
pbi_tmdl.py  --  read a Power BI semantic model saved as TMDL.

Source-specific (Power BI only). Reads the `<Name>.SemanticModel/definition/`
folder of a PBIP project and returns plain dicts: tables (columns, measures,
partitions), relationships, roles and shared expressions (M parameters).

Scope: the TMDL constructs Power BI Desktop writes for import / DirectQuery
models. Unknown properties are kept in `props` and never silently dropped;
anything this reader cannot place is returned in `unparsed` so the conversion
report can show it.

Format reference: Microsoft "Tabular Model Definition Language (TMDL)"
(learn.microsoft.com/analysis-services/tmdl/tmdl-overview). Indentation is
tabs; a declaration ending in `=` takes a multi-line expression on the
following lines indented at least two levels deeper; ``` fences are also
accepted.
"""

import os
import re

# --------------------------------------------------------------------------- #
# low-level: lines -> indented nodes
# --------------------------------------------------------------------------- #


def _indent_of(raw):
    n, i = 0, 0
    while i < len(raw):
        if raw[i] == "\t":
            n += 1
            i += 1
        elif raw.startswith("    ", i):
            n += 1
            i += 4
        else:
            break
    return n, raw[i:]


def read_name(text):
    """Split a leading TMDL object name off `text`.

    Returns (name, rest). Names are either single-quoted ('' escapes a quote)
    or a bare token ending at whitespace, '=', ':' or '.'."""
    text = text.lstrip()
    if text.startswith("'"):
        i, out = 1, []
        while i < len(text):
            c = text[i]
            if c == "'":
                if text[i + 1:i + 2] == "'":
                    out.append("'")
                    i += 2
                    continue
                return "".join(out), text[i + 1:]
            out.append(c)
            i += 1
        return "".join(out), ""
    m = re.match(r"[^\s=:.]+", text)
    if not m:
        return "", text
    return m.group(0), text[m.end():]


def parse_column_ref(text):
    """`Orders.'Order Date'` / `'Sales Fact'.Amount` -> (table, column)."""
    table, rest = read_name(text)
    rest = rest.lstrip()
    if not rest.startswith("."):
        return table, None
    col, _ = read_name(rest[1:])
    return table, col


class Node:
    __slots__ = ("indent", "text", "children", "expr_lines", "line_no")

    def __init__(self, indent, text, line_no):
        self.indent = indent
        self.text = text
        self.children = []
        self.expr_lines = []
        self.line_no = line_no


def _parse_nodes(src):
    lines = src.replace("\r\n", "\n").split("\n")
    roots, stack = [], []
    i = 0
    while i < len(lines):
        raw = lines[i]
        if not raw.strip() or raw.strip().startswith("///"):
            i += 1
            continue
        ind, text = _indent_of(raw)
        node = Node(ind, text.rstrip(), i + 1)
        while stack and stack[-1].indent >= ind:
            stack.pop()
        (stack[-1].children if stack else roots).append(node)
        stack.append(node)
        i += 1
        stripped = node.text.rstrip()
        # multi-line expression: `... =` followed by deeper lines, or a fence
        if stripped.endswith("```") and "=" in stripped:
            while i < len(lines) and lines[i].strip() != "```":
                node.expr_lines.append(lines[i].strip("\n"))
                i += 1
            i += 1
        elif stripped.endswith("="):
            while i < len(lines):
                if not lines[i].strip():
                    node.expr_lines.append("")
                    i += 1
                    continue
                ind2, _ = _indent_of(lines[i])
                if ind2 < ind + 2:
                    break
                node.expr_lines.append(lines[i])
                i += 1
    return roots


def _dedent_expr(lines):
    rows = [ln for ln in lines]
    while rows and not rows[-1].strip():
        rows.pop()
    if not rows:
        return ""
    base = min(_indent_of(r)[0] for r in rows if r.strip())
    out = []
    for r in rows:
        ind, text = _indent_of(r)
        out.append("\t" * max(ind - base, 0) + text)
    return "\n".join(out).strip()


def _decl(node):
    """'measure 'X' = expr' -> (keyword, name, inline_expr|None, has_eq)."""
    m = re.match(r"(\w+)\s*(.*)$", node.text)
    if not m:
        return None, None, None, False
    kw, rest = m.group(1), m.group(2)
    name, after = read_name(rest)
    after = after.strip()
    if after.startswith("="):
        expr = after[1:].strip()
        if node.expr_lines:
            expr = _dedent_expr(node.expr_lines)
        elif expr.endswith("```"):
            expr = expr[:-3].strip()
        return kw, name, expr, True
    return kw, name, None, False


def _props(node):
    """Child `key: value` / bare flags / annotations of a node."""
    props, annotations, others = {}, {}, []
    for c in node.children:
        t = c.text
        m = re.match(r"([A-Za-z_][\w]*)\s*:\s*(.*)$", t)
        if m:
            props[m.group(1)] = m.group(2).strip()
            continue
        m = re.match(r"annotation\s+(.*)$", t)
        if m:
            k, rest = read_name(m.group(1))
            annotations[k] = rest.strip().lstrip("=").strip()
            continue
        if re.match(r"^[A-Za-z_]\w*$", t) and not c.children:
            props[t] = True
            continue
        others.append(c)
    return props, annotations, others


# --------------------------------------------------------------------------- #
# object builders
# --------------------------------------------------------------------------- #


def _column(node):
    _, name, expr, has_eq = _decl(node)
    props, ann, _ = _props(node)
    return {
        "name": name,
        "data_type": props.get("dataType"),
        "source_column": props.get("sourceColumn"),
        "expression": expr if has_eq else None,
        "format_string": props.get("formatString"),
        "sort_by": read_name(props["sortByColumn"])[0] if props.get("sortByColumn") else None,
        "is_hidden": bool(props.get("isHidden")),
        "is_key": bool(props.get("isKey")),
        "summarize_by": props.get("summarizeBy"),
        "data_category": props.get("dataCategory"),
        "props": props,
    }


def _measure(node, table):
    _, name, expr, _ = _decl(node)
    props, ann, _ = _props(node)
    return {
        "name": name,
        "table": table,
        "expression": expr or "",
        "format_string": props.get("formatString"),
        "is_hidden": bool(props.get("isHidden")),
        "display_folder": props.get("displayFolder"),
        "props": props,
    }


def _partition(node):
    _, name, kind, _ = _decl(node)
    props, ann, others = _props(node)
    source = None
    for c in node.children:
        kw, _, expr, has_eq = _decl(c)
        if kw == "source" and has_eq:
            # `source =` has no name token: _decl read '=' as the name
            source = _dedent_expr(c.expr_lines) if c.expr_lines else \
                c.text.split("=", 1)[1].strip()
    return {"name": name, "type": (kind or "").strip(), "mode": props.get("mode"),
            "source": source, "props": props}


def _table(node):
    _, name, _, _ = _decl(node)
    props, ann, others = _props(node)
    t = {"name": name, "columns": [], "measures": [], "partitions": [],
         "hierarchies": [], "is_hidden": bool(props.get("isHidden")),
         "data_category": props.get("dataCategory"), "props": props,
         "unparsed": []}
    for c in others:
        kw = c.text.split(None, 1)[0] if c.text.split() else ""
        if kw == "column":
            t["columns"].append(_column(c))
        elif kw == "measure":
            t["measures"].append(_measure(c, name))
        elif kw == "partition":
            t["partitions"].append(_partition(c))
        elif kw == "hierarchy":
            t["hierarchies"].append(read_name(c.text.split(None, 1)[1])[0])
        elif kw in ("calculationGroup", "calculationItem"):
            t["unparsed"].append({"kind": kw, "text": c.text, "line": c.line_no})
        else:
            t["unparsed"].append({"kind": kw, "text": c.text, "line": c.line_no})
    return t


def _relationship(node):
    _, rid, _, _ = _decl(node)
    props, _, _ = _props(node)
    ft, fc = parse_column_ref(props.get("fromColumn", ""))
    tt, tc = parse_column_ref(props.get("toColumn", ""))
    return {
        "id": rid, "from_table": ft, "from_column": fc,
        "to_table": tt, "to_column": tc,
        "from_cardinality": props.get("fromCardinality", "many"),
        "to_cardinality": props.get("toCardinality", "one"),
        "cross_filter": props.get("crossFilteringBehavior", "oneDirection"),
        "active": str(props.get("isActive", "true")).lower() != "false",
        "props": props,
    }


def _role(node):
    _, name, _, _ = _decl(node)
    props, _, others = _props(node)
    perms = {}
    for c in node.children:
        m = re.match(r"tablePermission\s+(.*)$", c.text)
        if m:
            t, rest = read_name(m.group(1))
            rest = rest.strip()
            expr = rest[1:].strip() if rest.startswith("=") else ""
            if c.expr_lines:
                expr = _dedent_expr(c.expr_lines)
            perms[t] = expr
    return {"name": name, "model_permission": props.get("modelPermission"),
            "table_permissions": perms, "members": []}


_EXPR_RE = re.compile(r'^\s*(.*?)\s+meta\s*\[(.*)\]\s*$', re.S)


def _expression(node):
    _, name, expr, _ = _decl(node)
    expr = expr or ""
    meta = {}
    m = _EXPR_RE.match(expr)
    if m:
        expr = m.group(1)
        for kv in re.finditer(r'(\w+)\s*=\s*("([^"]*)"|\w+)', m.group(2)):
            meta[kv.group(1)] = kv.group(3) if kv.group(3) is not None else kv.group(2)
    value = None
    if meta.get("IsParameterQuery", "").lower() == "true":
        sm = re.match(r'^"((?:[^"]|"")*)"$', expr.strip())
        if sm:
            value = sm.group(1).replace('""', '"')
        else:
            value = expr.strip()
    return {"name": name, "expression": expr, "meta": meta,
            "is_parameter": meta.get("IsParameterQuery", "").lower() == "true",
            "value": value}


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #


def parse_tmdl_text(src, model=None):
    """Parse one TMDL document into (and return) a model dict."""
    model = model if model is not None else new_model()
    for n in _parse_nodes(src):
        kw = n.text.split(None, 1)[0] if n.text.split() else ""
        if kw == "table":
            model["tables"].append(_table(n))
        elif kw == "relationship":
            model["relationships"].append(_relationship(n))
        elif kw == "role":
            model["roles"].append(_role(n))
        elif kw == "expression":
            model["expressions"].append(_expression(n))
        elif kw == "model":
            props, ann, others = _props(n)
            model["model_props"].update(props)
            for c in others:
                if c.text.startswith("ref table"):
                    model["table_order"].append(read_name(c.text[len("ref table"):])[0])
        elif kw == "database":
            props, _, _ = _props(n)
            model["database_props"].update(props)
        elif kw == "ref" and n.text.startswith("ref table"):
            model["table_order"].append(read_name(n.text[len("ref table"):])[0])
        elif kw in ("annotation", "ref", "cultureInfo", "culture",
                    "perspective", "dataSource", "createOrReplace"):
            if kw in ("perspective", "cultureInfo", "dataSource"):
                model["unparsed"].append({"kind": kw, "text": n.text, "line": n.line_no})
        else:
            model["unparsed"].append({"kind": kw, "text": n.text, "line": n.line_no})
    return model


def new_model():
    return {"tables": [], "relationships": [], "roles": [], "expressions": [],
            "model_props": {}, "database_props": {}, "table_order": [],
            "unparsed": [], "files": []}


def read_tmdl_folder(definition_dir):
    """Read every .tmdl file under a SemanticModel `definition/` folder."""
    if not os.path.isdir(definition_dir):
        raise FileNotFoundError(
            f"TMDL definition folder not found: {definition_dir}. Save the "
            "report as a Power BI project (.pbip) with the TMDL model format.")
    model = new_model()
    for root, _, files in os.walk(definition_dir):
        for f in sorted(files):
            if f.lower().endswith(".tmdl"):
                p = os.path.join(root, f)
                with open(p, encoding="utf-8-sig") as fh:
                    parse_tmdl_text(fh.read(), model)
                model["files"].append(os.path.relpath(p, definition_dir))
    return model
