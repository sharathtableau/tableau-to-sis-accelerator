"""
dax_rules.py  --  rule-based DAX -> Snowflake SQL for a documented subset.

This is NOT a general DAX translator, and it must never pretend to be one.
It recognizes an explicit list of patterns (see SUPPORTED below and
docs/POWERBI_CONVERTER.md). Anything else raises Unsupported with a reason,
and the measure is HELD: not deployed, shown as "pending" in the app and
listed in the conversion report. (Decision recorded 2026-11-26: a measure
that cannot be proven is held and the rest of the report ships.)

Output shape for a measure (filter-context aware):

    {"expr":   "CASE WHEN {{a:1}} ... END",      # combines aggregate leaves
     "leaves": [{"sql": "SUM({{c:Orders::Sales}})",
                 "overrides": [["Orders", "Category"]],   # CALCULATE filters
                 "tables": ["Orders"]}],
     "support": "auto" | "partial",
     "notes": [...], "depends_on": [...]}

Each LEAF is one aggregate. The query compiler (report_sql.py) evaluates every
leaf in the filter context the visual gives it, except that a leaf drops the
visual/slicer filters on its `overrides` columns -- that is how CALCULATE's
"replace the filter on this column" semantics are kept. `{{c:T::C}}` is a
column token and `{{a:N}}` a leaf token; the compiler resolves both.

BLANK vs NULL (DAX semantics preserved where the subset allows):
  * a + b, a - b   DAX treats one BLANK operand as 0, two BLANKs as BLANK.
                   Emitted as CASE WHEN a IS NULL AND b IS NULL THEN NULL
                   ELSE COALESCE(a,0) +/- COALESCE(b,0) END.
  * a * b          BLANK * x = BLANK, same as SQL NULL propagation.
  * a / b          DAX gives Infinity/NaN on zero; emitted as a / NULLIF(b,0)
                   (BLANK). Marked PARTIAL; DIVIDE() is exact.
  * DIVIDE(a,b,c)  BLANK (or c) when b is BLANK or 0: exact.
  * COUNT-family   DAX returns BLANK over no rows, SQL COUNT returns 0:
                   wrapped in NULLIF(...,0).
  * DISTINCTCOUNT  DAX counts BLANK as a value, SQL COUNT(DISTINCT) does
                   not: the missing 1 is added back when a NULL is present.
  * x = 0 / x = "" DAX treats BLANK as equal to 0 and "": the column side is
                   COALESCEd for these two literal comparisons.
"""

import re

AUTO, PARTIAL, MANUAL, UNSUPPORTED = "auto", "partial", "manual", "unsupported"
_RANK = {AUTO: 0, PARTIAL: 1, MANUAL: 2, UNSUPPORTED: 3}

SUPPORTED = {
    "measure_auto": ["SUM", "AVERAGE", "MIN", "MAX", "COUNT", "COUNTA",
                     "COUNTROWS", "DISTINCTCOUNT", "DISTINCTCOUNTNOBLANK",
                     "DIVIDE", "measure references [M]", "+ - * with numbers",
                     "VAR ... RETURN (substituted when no CALCULATE/iterator)"],
    "measure_partial": ["/ operator (zero gives BLANK, DAX gives Infinity)",
                        "CALCULATE(<expr>, T[c] = <literal> | T[c] IN {..})",
                        "SUMX/AVERAGEX/MINX/MAXX(<base table>, <row expression>)"],
    "row_partial": ["IF", "AND", "OR", "NOT", "&&", "||", "comparisons", "IN {..}",
                    "DATEDIFF(a, b, DAY|MONTH|QUARTER|YEAR)", "YEAR", "MONTH",
                    "DAY", "QUARTER", "FORMAT(date, \"MMM\"|\"MMMM\"|\"YYYY\")",
                    "ABS", "ROUND", "&", "BLANK()", "TRUE()", "FALSE()"],
}


class Unsupported(Exception):
    pass


# --------------------------------------------------------------------------- #
# tokenizer
# --------------------------------------------------------------------------- #

_TOKEN = re.compile(r"""
    (?P<ws>\s+|//[^\n]*|--[^\n]*|/\*.*?\*/)
  | (?P<num>\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?
  | (?P<str>"(?:[^"]|"")*")
  | (?P<qtable>'(?:[^']|'')*')
  | (?P<bracket>\[(?:[^\]]|\]\])*\])
  | (?P<op><=|>=|<>|==|&&|\|\||[-+*/&=<>(),{}^!])
  | (?P<ident>[A-Za-z_][A-Za-z0-9_.]*)
""", re.X | re.S)


def tokenize(src):
    out, i = [], 0
    while i < len(src):
        m = _TOKEN.match(src, i)
        if not m:
            raise Unsupported(f"cannot read DAX near {src[i:i + 20]!r}")
        i = m.end()
        k = m.lastgroup
        if k == "ws":
            continue
        t = m.group(0)
        if k == "num":
            out.append(("num", float(t)))
        elif k == "str":
            out.append(("str", t[1:-1].replace('""', '"')))
        elif k == "qtable":
            out.append(("table", t[1:-1].replace("''", "'")))
        elif k == "bracket":
            out.append(("bracket", t[1:-1].replace("]]", "]")))
        elif k == "op":
            out.append(("op", t))
        else:
            out.append(("ident", t))
    out.append(("eof", None))
    return out


# --------------------------------------------------------------------------- #
# parser -> AST tuples
# --------------------------------------------------------------------------- #

_PREC = [("||",), ("&&",), ("=", "==", "<>", "<", ">", "<=", ">=", "IN"),
         ("&",), ("+", "-"), ("*", "/"), ("^",)]


class _P:
    def __init__(self, src):
        self.t = tokenize(src)
        self.i = 0

    def peek(self, k=0):
        return self.t[self.i + k]

    def take(self):
        tok = self.t[self.i]
        self.i += 1
        return tok

    def expect(self, kind, val=None):
        tok = self.take()
        if tok[0] != kind or (val is not None and tok[1] != val):
            raise Unsupported(f"unexpected {tok[1]!r} in DAX")
        return tok

    def parse(self):
        e = self.expr()
        if self.peek()[0] != "eof":
            raise Unsupported(f"unexpected {self.peek()[1]!r} after expression")
        return e

    def expr(self):
        if self.peek()[0] == "ident" and self.peek()[1].upper() == "VAR":
            binds = []
            while self.peek()[0] == "ident" and self.peek()[1].upper() == "VAR":
                self.take()
                name = self.expect("ident")[1]
                self.expect("op", "=")
                binds.append((name, self.expr()))
            if not (self.peek()[0] == "ident" and self.peek()[1].upper() == "RETURN"):
                raise Unsupported("VAR without RETURN")
            self.take()
            return ("let", binds, self.expr())
        return self.binary(0)

    def binary(self, lvl):
        if lvl == len(_PREC):
            return self.unary()
        left = self.binary(lvl + 1)
        while True:
            tok = self.peek()
            op = tok[1].upper() if tok[0] in ("op", "ident") else None
            if op not in _PREC[lvl]:
                return left
            self.take()
            if op == "IN":
                right = self.unary()
                left = ("in", left, right)
            else:
                left = ("bin", {"==": "="}.get(op, op), left, self.binary(lvl + 1))

    def unary(self):
        tok = self.peek()
        if tok == ("op", "-"):
            self.take()
            return ("neg", self.unary())
        if tok == ("op", "+"):
            self.take()
            return self.unary()
        if tok[0] == "ident" and tok[1].upper() == "NOT" and self.peek(1) != ("op", "("):
            self.take()
            return ("not", self.unary())
        return self.primary()

    def primary(self):
        tok = self.take()
        k, v = tok
        if k == "num":
            return ("num", v)
        if k == "str":
            return ("str", v)
        if k == "op" and v == "(":
            e = self.expr()
            self.expect("op", ")")
            return e
        if k == "op" and v == "{":
            items = []
            if self.peek() != ("op", "}"):
                items.append(self.expr())
                while self.peek() == ("op", ","):
                    self.take()
                    items.append(self.expr())
            self.expect("op", "}")
            return ("set", items)
        if k == "bracket":
            return ("mref", v)
        if k == "table":
            if self.peek()[0] == "bracket":
                return ("col", v, self.take()[1])
            return ("tableref", v)
        if k == "ident":
            if self.peek() == ("op", "("):
                self.take()
                args = []
                if self.peek() != ("op", ")"):
                    args.append(self.expr())
                    while self.peek() == ("op", ","):
                        self.take()
                        args.append(self.expr())
                self.expect("op", ")")
                return ("call", v.upper(), args)
            if self.peek()[0] == "bracket":
                return ("col", v, self.take()[1])
            if v.upper() in ("TRUE", "FALSE"):
                return ("bool", v.upper() == "TRUE")
            return ("name", v)       # VAR name or bare table / unit keyword
        raise Unsupported(f"unexpected {v!r} in DAX")


def parse(src):
    return _P(src or "").parse()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _bad_name(s):
    return "}}" in s or "::" in s or "{{" in s


def col_token(table, column):
    if _bad_name(table) or _bad_name(column):
        raise Unsupported(f"name {table}[{column}] contains reserved characters")
    return "{{c:" + table + "::" + column + "}}"


def sql_str(v):
    return "'" + str(v).replace("'", "''") + "'"


def _lit_sql(node):
    if node[0] == "num":
        v = node[1]
        return repr(int(v)) if float(v).is_integer() else repr(v)
    if node[0] == "str":
        return sql_str(node[1])
    if node[0] == "bool":
        return "TRUE" if node[1] else "FALSE"
    if node[0] == "neg" and node[1][0] == "num":
        return "-" + _lit_sql(node[1])
    raise Unsupported("filter value must be a literal")


def _walk(node):
    yield node
    if isinstance(node, tuple):
        for x in node[1:]:
            if isinstance(x, tuple):
                yield from _walk(x)
            elif isinstance(x, list):
                for y in x:
                    if isinstance(y, tuple):
                        yield from _walk(y)
                    elif isinstance(y, (list, tuple)):
                        for z in y:
                            if isinstance(z, tuple):
                                yield from _walk(z)


def _subst(node, env):
    """Replace VAR names by their bound expressions."""
    if not isinstance(node, tuple):
        return node
    if node[0] == "name" and node[1] in env:
        return env[node[1]]
    if node[0] == "let":
        inner = dict(env)
        for n, e in node[1]:
            inner[n] = _subst(e, inner)
        return _subst(node[2], inner)
    out = [node[0]]
    for x in node[1:]:
        if isinstance(x, tuple):
            out.append(_subst(x, env))
        elif isinstance(x, list):
            out.append([_subst(y, env) if isinstance(y, tuple) else y for y in x])
        else:
            out.append(x)
    return tuple(out)


_CONTEXT_CHANGERS = {"CALCULATE", "CALCULATETABLE", "SUMX", "AVERAGEX", "MINX",
                     "MAXX", "COUNTX", "FILTER", "ALL", "ALLEXCEPT"}


def _resolve_lets(ast):
    """VAR/RETURN -> substitution, only when it cannot change semantics."""
    if not any(n[0] == "let" for n in _walk(ast)):
        return ast, []
    if any(n[0] == "call" and n[1] in _CONTEXT_CHANGERS for n in _walk(ast)):
        raise Unsupported("VAR used together with CALCULATE or an iterator "
                          "(evaluation context could change)")
    return _subst(ast, {}), ["VAR/RETURN substituted (no context transition)"]


# --------------------------------------------------------------------------- #
# row-level translation (calculated columns, iterator bodies)
# --------------------------------------------------------------------------- #

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
           "Oct", "Nov", "Dec"]
_MONTHS_LONG = ["January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December"]
_DATEDIFF_UNITS = {"DAY": "day", "MONTH": "month", "QUARTER": "quarter", "YEAR": "year"}


class RowContext:
    """Which columns a row expression may use.

    `home` is the table the row belongs to; `col_ok(table, column)` says
    whether a column exists; `alias_table` lets a virtual table (e.g. a
    calculated Date table) be addressed under its own name."""

    def __init__(self, home, col_ok):
        self.home = home
        self.col_ok = col_ok


def row_sql(node, ctx, notes):
    k = node[0]
    if k == "num" or k == "str" or k == "bool":
        return _lit_sql(node)
    if k == "col":
        t, c = node[1], node[2]
        if t != ctx.home:
            raise Unsupported(f"row expression uses {t}[{c}] from another table "
                              "(RELATED/lookup not in the MVP subset)")
        if not ctx.col_ok(t, c):
            raise Unsupported(f"unknown column {t}[{c}]")
        return col_token(t, c)
    if k == "mref":
        if ctx.col_ok(ctx.home, node[1]):
            return col_token(ctx.home, node[1])
        raise Unsupported(f"measure [{node[1]}] used in a row expression")
    if k == "neg":
        return f"(-{row_sql(node[1], ctx, notes)})"
    if k == "not":
        return f"(NOT {row_sql(node[1], ctx, notes)})"
    if k == "in":
        left = row_sql(node[1], ctx, notes)
        if node[2][0] != "set":
            raise Unsupported("IN needs a {…} list")
        return f"({left} IN ({', '.join(_lit_sql(x) for x in node[2][1])}))"
    if k == "bin":
        return _bin_sql(node[1], node[2], node[3], lambda n: row_sql(n, ctx, notes), notes)
    if k == "call":
        return _row_call(node[1], node[2], ctx, notes)
    raise Unsupported(f"{k} not supported in a row expression")


def _bin_sql(op, l, r, rec, notes):
    a, b = rec(l), rec(r)
    if op in ("+", "-"):
        return (f"(CASE WHEN {a} IS NULL AND {b} IS NULL THEN NULL "
                f"ELSE COALESCE({a}, 0) {op} COALESCE({b}, 0) END)")
    if op == "*":
        return f"({a} * {b})"
    if op == "/":
        notes.append("'/' by zero returns BLANK here; Power BI returns Infinity. "
                     "Use DIVIDE() in the source for exact behavior.")
        return f"({a} / NULLIF({b}, 0))"
    if op == "^":
        return f"POWER({a}, {b})"
    if op == "&":
        return f"(COALESCE(CAST({a} AS VARCHAR), '') || COALESCE(CAST({b} AS VARCHAR), ''))"
    if op == "&&":
        return f"({a} AND {b})"
    if op == "||":
        return f"({a} OR {b})"
    if op in ("=", "<>"):
        # DAX: BLANK = 0 and BLANK = "" are TRUE
        if r[0] == "num" and r[1] == 0:
            a = f"COALESCE({a}, 0)"
        elif r[0] == "str" and r[1] == "":
            a = f"COALESCE({a}, '')"
        return f"({a} {op} {b})"
    if op in ("<", ">", "<=", ">="):
        return f"({a} {op} {b})"
    raise Unsupported(f"operator {op}")


def _row_call(fn, args, ctx, notes):
    r = lambda n: row_sql(n, ctx, notes)   # noqa: E731
    if fn == "IF":
        if len(args) not in (2, 3):
            raise Unsupported("IF needs 2 or 3 arguments")
        els = r(args[2]) if len(args) == 3 else "NULL"
        return f"(CASE WHEN {r(args[0])} THEN {r(args[1])} ELSE {els} END)"
    if fn in ("AND", "OR") and len(args) == 2:
        return f"({r(args[0])} {fn} {r(args[1])})"
    if fn == "NOT" and len(args) == 1:
        return f"(NOT {r(args[0])})"
    if fn == "BLANK" and not args:
        return "NULL"
    if fn in ("TRUE", "FALSE") and not args:
        return fn
    if fn in ("YEAR", "MONTH", "DAY", "QUARTER") and len(args) == 1:
        return f"{fn}({r(args[0])})"
    if fn == "DATEDIFF" and len(args) == 3:
        unit = args[2][1].upper() if args[2][0] == "name" else None
        if unit not in _DATEDIFF_UNITS:
            raise Unsupported(f"DATEDIFF unit {unit!r} not in the MVP subset")
        return f"DATEDIFF('{_DATEDIFF_UNITS[unit]}', {r(args[0])}, {r(args[1])})"
    if fn == "ABS" and len(args) == 1:
        return f"ABS({r(args[0])})"
    if fn == "ROUND" and len(args) == 2 and args[1][0] == "num":
        return f"ROUND({r(args[0])}, {int(args[1][1])})"
    if fn == "FORMAT" and len(args) == 2 and args[1][0] == "str":
        fmt = args[1][1]
        x = r(args[0])
        if fmt in ("MMM", "MMMM"):
            names = _MONTHS if fmt == "MMM" else _MONTHS_LONG
            whens = " ".join(f"WHEN {i + 1} THEN '{m}'" for i, m in enumerate(names))
            notes.append(f"FORMAT(…, \"{fmt}\") assumes English month names")
            return f"(CASE MONTH({x}) {whens} END)"
        if fmt in ("YYYY", "yyyy"):
            return f"CAST(YEAR({x}) AS VARCHAR)"
        raise Unsupported(f"FORMAT pattern {fmt!r} not in the MVP subset")
    raise Unsupported(f"function {fn} not in the row-expression subset")


def translate_column(expression, table, col_ok):
    """Calculated column -> {sql, support, notes}. Raises Unsupported."""
    notes = []
    ast, n2 = _resolve_lets(parse(expression))
    notes += n2
    sql = row_sql(ast, RowContext(table, col_ok), notes)
    return {"sql": sql, "support": PARTIAL, "notes": notes}


# --------------------------------------------------------------------------- #
# measure translation
# --------------------------------------------------------------------------- #

class MeasureEnv:
    """Model lookups the measure translator needs.

    col_ok(table, column) -> bool
    measure_expr(name)    -> DAX text of a measure, or None
    base_tables           -> tables an aggregate may run over (fact tables)
    """

    def __init__(self, col_ok, measure_expr, table_exists):
        self.col_ok = col_ok
        self.measure_expr = measure_expr
        self.table_exists = table_exists


class _Ctx:
    def __init__(self, env, filters=None, stack=()):
        self.env = env
        self.filters = dict(filters or {})   # (table, column) -> SQL condition
        self.stack = stack
        self.leaves = []
        self.notes = []
        self.support = AUTO
        self.deps = []

    def bump(self, level, note=None):
        if _RANK[level] > _RANK[self.support]:
            self.support = level
        if note and note not in self.notes:
            self.notes.append(note)


def _cond_sql(filters):
    return " AND ".join(f"({c})" for _, c in sorted(filters.items()))


def _leaf(ctx, sql, table):
    ctx.leaves.append({"sql": sql, "overrides": [list(k) for k in sorted(ctx.filters)],
                       "tables": [table]})
    return "{{a:" + str(len(ctx.leaves) - 1) + "}}"


def _col_arg(node, ctx, fn):
    if node[0] != "col":
        raise Unsupported(f"{fn} needs a Table[Column] argument")
    t, c = node[1], node[2]
    if not ctx.env.col_ok(t, c):
        raise Unsupported(f"{fn} over unknown column {t}[{c}]")
    return t, col_token(t, c)


def _when(ctx, x):
    if not ctx.filters:
        return x
    return f"CASE WHEN {_cond_sql(ctx.filters)} THEN {x} END"


def _measure_node(node, ctx):
    k = node[0]
    if k in ("num", "str", "bool"):
        return _lit_sql(node)
    if k == "neg":
        return f"(-{_measure_node(node[1], ctx)})"
    if k == "mref":
        return _measure_ref(node[1], ctx)
    if k == "bin":
        if node[1] not in ("+", "-", "*", "/"):
            raise Unsupported(f"operator {node[1]} in a measure")
        return _bin_sql(node[1], node[2], node[3], lambda n: _measure_node(n, ctx),
                        _NoteSink(ctx))
    if k == "call":
        return _measure_call(node[1], node[2], ctx)
    if k == "col":
        raise Unsupported(f"bare column {node[1]}[{node[2]}] in a measure "
                          "(needs an aggregation)")
    raise Unsupported(f"{k} not supported in a measure")


class _NoteSink(list):
    """Lets _bin_sql's notes raise the measure to PARTIAL."""

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx

    def append(self, note):
        self.ctx.bump(PARTIAL, note)


def _measure_ref(name, ctx):
    if name in ctx.stack:
        raise Unsupported(f"circular measure reference via [{name}]")
    expr = ctx.env.measure_expr(name)
    if expr is None:
        raise Unsupported(f"[{name}] is not a measure of this model")
    if name not in ctx.deps:
        ctx.deps.append(name)
    ast, n2 = _resolve_lets(parse(expr))
    for n in n2:
        ctx.bump(AUTO, n)
    saved = ctx.stack
    ctx.stack = saved + (name,)
    try:
        return "(" + _measure_node(ast, ctx) + ")"
    except Unsupported as e:
        raise Unsupported(f"[{name}]: {e}") from None
    finally:
        ctx.stack = saved


_SIMPLE_AGG = {"SUM": "SUM", "AVERAGE": "AVG", "MIN": "MIN", "MAX": "MAX"}
_ITERATORS = {"SUMX": "SUM", "AVERAGEX": "AVG", "MINX": "MIN", "MAXX": "MAX"}


def _measure_call(fn, args, ctx):
    if fn in _SIMPLE_AGG and len(args) == 1:
        t, col = _col_arg(args[0], ctx, fn)
        return _leaf(ctx, f"{_SIMPLE_AGG[fn]}({_when(ctx, col)})", t)
    if fn in ("COUNT", "COUNTA") and len(args) == 1:
        t, col = _col_arg(args[0], ctx, fn)
        return _leaf(ctx, f"NULLIF(COUNT({_when(ctx, col)}), 0)", t)
    if fn == "DISTINCTCOUNTNOBLANK" and len(args) == 1:
        t, col = _col_arg(args[0], ctx, fn)
        return _leaf(ctx, f"NULLIF(COUNT(DISTINCT {_when(ctx, col)}), 0)", t)
    if fn == "DISTINCTCOUNT" and len(args) == 1:
        t, col = _col_arg(args[0], ctx, fn)
        nullhit = (f"CASE WHEN {_cond_sql(ctx.filters)} AND {col} IS NULL THEN 1 ELSE 0 END"
                   if ctx.filters else f"CASE WHEN {col} IS NULL THEN 1 ELSE 0 END")
        return _leaf(ctx, f"NULLIF(COUNT(DISTINCT {_when(ctx, col)}) + "
                          f"MAX({nullhit}), 0)", t)
    if fn == "COUNTROWS" and len(args) == 1:
        a = args[0]
        if a[0] not in ("tableref", "name") or not ctx.env.table_exists(a[1]):
            raise Unsupported("COUNTROWS needs a plain table argument")
        return _leaf(ctx, f"NULLIF(COUNT({_when(ctx, '1')}), 0)", a[1])
    if fn == "DIVIDE" and len(args) in (2, 3):
        a = _measure_node(args[0], ctx)
        b = _measure_node(args[1], ctx)
        alt = _measure_node(args[2], ctx) if len(args) == 3 else "NULL"
        return f"(CASE WHEN {b} IS NULL OR {b} = 0 THEN {alt} ELSE {a} / {b} END)"
    if fn == "BLANK" and not args:
        return "NULL"
    if fn in _ITERATORS and len(args) == 2:
        tab = args[0]
        if tab[0] not in ("tableref", "name") or not ctx.env.table_exists(tab[1]):
            raise Unsupported(f"{fn} over a table expression (only a plain table is supported)")
        notes = []
        body = row_sql(args[1], RowContext(tab[1], ctx.env.col_ok), notes)
        for n in notes:
            ctx.bump(PARTIAL, n)
        ctx.bump(PARTIAL, f"{fn} over the whole table translated as a row-level "
                          "aggregate; verify against Power BI")
        return _leaf(ctx, f"{_ITERATORS[fn]}({_when(ctx, body)})", tab[1])
    if fn == "CALCULATE" and args:
        new = dict(ctx.filters)
        for f in args[1:]:
            col, cond = _calc_filter(f, ctx)
            new[col] = cond                         # inner filter replaces outer
        ctx.bump(PARTIAL, "CALCULATE with a constant column filter: the filter "
                          "replaces slicer/visual filters on that column")
        saved = ctx.filters
        ctx.filters = new
        try:
            return _measure_node(args[0], ctx)
        finally:
            ctx.filters = saved
    raise Unsupported(f"{fn}({len(args)} args) is not in the MVP DAX subset")


def _calc_filter(node, ctx):
    if node[0] == "bin" and node[1] == "=" and node[2][0] == "col":
        t, c = node[2][1], node[2][2]
        if not ctx.env.col_ok(t, c):
            raise Unsupported(f"CALCULATE filter on unknown column {t}[{c}]")
        return (t, c), f"{col_token(t, c)} = {_lit_sql(node[3])}"
    if node[0] == "in" and node[1][0] == "col" and node[2][0] == "set":
        t, c = node[1][1], node[1][2]
        if not ctx.env.col_ok(t, c):
            raise Unsupported(f"CALCULATE filter on unknown column {t}[{c}]")
        vals = ", ".join(_lit_sql(x) for x in node[2][1])
        return (t, c), f"{col_token(t, c)} IN ({vals})"
    what = node[1] if node[0] == "call" else node[0]
    raise Unsupported(f"CALCULATE filter argument {what!s} is not a constant "
                      "column filter (time intelligence, FILTER, ALL… are not in "
                      "the MVP subset)")


def translate_measure(name, env):
    """Measure name -> translation dict (see module doc). Raises Unsupported."""
    expr = env.measure_expr(name)
    if expr is None:
        raise Unsupported(f"no measure named {name}")
    ctx = _Ctx(env, stack=(name,))
    ast, n2 = _resolve_lets(parse(expr))
    for n in n2:
        ctx.bump(AUTO, n)
    sql = _measure_node(ast, ctx)
    return {"expr": sql, "leaves": ctx.leaves, "support": ctx.support,
            "notes": ctx.notes, "depends_on": ctx.deps}


def implicit_measure(table, column, agg):
    """A visual's implicit aggregation (Sum of Sales) -> translation dict."""
    col = col_token(table, column)
    fn = {"sum": "SUM({})", "avg": "AVG({})", "min": "MIN({})", "max": "MAX({})",
          "median": "MEDIAN({})", "stdev": "STDDEV({})", "var": "VARIANCE({})",
          "count_non_null": "NULLIF(COUNT({}), 0)",
          "count": "NULLIF(COUNT({}), 0)"}.get(agg)
    if fn is None:
        raise Unsupported(f"implicit aggregation {agg!r}")
    support, notes = AUTO, []
    if agg in ("count", "count_non_null", "stdev", "var"):
        support = PARTIAL
        notes.append(f"implicit '{agg}' aggregation mapped from the PBIR function "
                     "code; confirm it matches the Power BI visual")
    return {"expr": "{{a:0}}", "leaves": [{"sql": fn.format(col), "overrides": [],
                                           "tables": [table]}],
            "support": support, "notes": notes, "depends_on": []}
