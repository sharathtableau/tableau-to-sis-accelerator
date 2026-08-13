"""
calc_translator.py  --  Stage 2: Tableau fields/calcs -> Snowflake SQL.

Generic, workbook-agnostic:
  * to_phys()       : Tableau caption -> physical UPPER_SNAKE column name.
  * agg_sql()       : Tableau aggregation prefix -> SQL aggregate expression.
  * measure_sql()   : resolve a measure (caption [+agg]) to {sql, fmt} generically.
  * translate_formula(): generic Tableau formula -> SQL, incl. INDEX() as a
    post-aggregation window function (marked with WIN_ORDER for the engine).

Client-specific measure SQL/formats live in the profile (config.PROFILE), NOT here.
"""

import re
import config

# Tableau date-part / aggregation shelf prefixes.
DATE_PARTS = {"yr", "qr", "mn", "tmn", "tyr", "tqr", "twk", "wk", "tdy", "dy",
              "mdy", "md", "wd", "hr"}
AGGS = {"sum", "avg", "min", "max", "cnt", "ctd", "med", "stdev", "var"}


# COPIED (not aliased) from the default profile at import time -- set_profile()
# below mutates these IN PLACE (.clear()/.update(), never rebinds the name) so
# every module that did `from calc_translator import MEASURE_LIBRARY` (engine.py)
# keeps seeing live updates through the SAME object. Copying here means
# switching profiles can never corrupt a profile module's own original dict.
MEASURE_LIBRARY = dict(config.PROFILE.MEASURE_LIBRARY)
CAPTION_ALIASES = dict(config.PROFILE.CAPTION_ALIASES)
KPI_ORDER = list(config.PROFILE.KPI_ORDER)


def set_profile(profile):
    """Re-sync the module-level measure library/aliases/KPI order to a
    different client profile (per-workbook -- see config.set_profile()).
    Mutates in place so existing `from calc_translator import MEASURE_LIBRARY`
    bindings elsewhere (engine.py) observe the change without re-importing."""
    MEASURE_LIBRARY.clear()
    MEASURE_LIBRARY.update(profile.MEASURE_LIBRARY)
    CAPTION_ALIASES.clear()
    CAPTION_ALIASES.update(profile.CAPTION_ALIASES)
    KPI_ORDER[:] = profile.KPI_ORDER

# Placeholder the engine replaces with a real ORDER BY when it wraps a window
# (table-calc) measure around the grouped subquery.
WIN_ORDER = "__WIN_ORDER__"
# Placeholder the engine replaces with the sheet's physical table (used by
# table-scoped scalar LODs like {MAX([Order Date])} -> scalar subquery).
TBL = "__TBL__"


def to_phys(caption):
    """Caption -> UPPER_SNAKE physical column (deterministic, workbook-agnostic)."""
    s = re.sub(r"[^0-9a-zA-Z]+", "_", str(caption)).strip("_").upper()
    return s or "COL"


def field_to_phys(caption):
    # Deterministic: Tableau caption == source CSV header -> same UPPER_SNAKE.
    return to_phys(caption)


def agg_sql(agg, caption):
    """Tableau agg prefix + field -> SQL aggregate expression."""
    col = field_to_phys(caption)
    a = (agg or "").lower()
    if a == "sum":  return f"SUM({col})"
    if a == "avg":  return f"AVG({col})"
    if a == "min":  return f"MIN({col})"
    if a == "max":  return f"MAX({col})"
    if a == "med":  return f"MEDIAN({col})"
    if a == "cnt":  return f"COUNT({col})"
    if a == "ctd":  return f"COUNT(DISTINCT {col})"
    return f"SUM({col})"   # sensible default for a measure


def date_trunc_sql(part, caption):
    """Tableau date part -> Snowflake DATE_TRUNC / part expression."""
    col = field_to_phys(caption)
    p = (part or "").lower()
    if p in ("yr", "tyr"):  return f"DATE_TRUNC('YEAR', {col})"
    if p in ("qr", "tqr"):  return f"DATE_TRUNC('QUARTER', {col})"
    if p in ("mn", "tmn"):  return f"DATE_TRUNC('MONTH', {col})"
    if p in ("twk", "wk"):  return f"DATE_TRUNC('WEEK', {col})"
    if p in ("dy", "mdy"):  return f"DATE_TRUNC('DAY', {col})"
    return col


def measure_sql(caption, agg=None, count_records=False):
    """Resolve a measure to {sql, fmt}: count-of-records -> COUNT(*); profile
    library; else generic aggregate over the physical column."""
    if count_records:
        return {"sql": "COUNT(*)", "fmt": "num0"}
    key = CAPTION_ALIASES.get(caption, caption)
    if key in MEASURE_LIBRARY:
        return dict(MEASURE_LIBRARY[key])
    if agg:
        fmt = "pct" if (agg or "").lower() == "avg" and "ratio" in caption.lower() else "float"
        return {"sql": agg_sql(agg, caption), "fmt": fmt}
    return {"sql": f"SUM({field_to_phys(caption)})", "fmt": "float"}


# ===========================================================================
# Generic Tableau formula -> Snowflake SQL translator (for calculated fields).
# Returns (sql, agg_ready) or (None, False) if it contains unsupported
# table calcs (WINDOW_/LOOKUP/RUNNING_...). INDEX() IS supported: it becomes
# ROW_NUMBER() OVER (__WIN_ORDER__), computed by the engine post-aggregation.
# ===========================================================================
import re as _re

# Table calcs we CANNOT translate deterministically (need view-layout ordering
# we refuse to guess): LOOKUP/FIRST/LAST/RUNNING_*/PREVIOUS_VALUE/SIZE/TOTAL.
# TOTAL stays out because TOTAL(AVG(x)) != AVG(AVG(x)) -- re-aggregation is
# only sound per-function and none appear in the corpus to verify against.
# WINDOW_SUM/AVG/MIN/MAX/COUNT/MEDIAN and RANK* ARE translated (see
# _rewrite_window_calcs): they become window-over-aggregate expressions that
# are layout-independent at Tableau's default "Table" scope.
_UNSUPPORTED = _re.compile(r"\b(LOOKUP|FIRST|LAST|RUNNING_\w+|TOTAL|"
                           r"PREVIOUS_VALUE|SIZE|WINDOW_(?!SUM|AVG|MIN|MAX|"
                           r"COUNT|MEDIAN)\w+)\s*\(", _re.I)
_AGG_RE = _re.compile(r"\b(SUM|AVG|MIN|MAX|COUNT|MEDIAN|STDEV|VAR)\s*\(", _re.I)


def param_sql_literal(v):
    """A parameter VALUE -> SQL literal (float repr for numbers so DECIMAL
    doesn't overflow; strip the embedded quotes XML string values carry)."""
    if v is None:
        return "0"
    try:
        return str(float(v))
    except (TypeError, ValueError):
        return "'" + str(v).strip().strip('"').replace("'", "''") + "'"


def param_token(caption):
    """Runtime-substituted placeholder for a parameter (see engine.sub_params)."""
    return f"__PARAM_{to_phys(caption)}__"


def _param_lit(params, name, param_alias=None):
    v = params.get(name)
    if v is None:
        return "0"
    # emit a TOKEN, not the literal: the engine substitutes the CURRENT value
    # at query time, which is what makes runtime what-if controls possible.
    canonical = (param_alias or {}).get(name, name)
    return param_token(canonical)


_AGG_HEAD = _re.compile(r"^(sum|avg|min|max|count|countd|median|stdev|stdevp|"
                        r"var|varp|attr)\s*\(", _re.I)


def _is_single_agg(body):
    """True if `body` is exactly one aggregate call wrapping everything, e.g.
    MIN(IF ... END) -- safe to turn into a window aggregate. A compound body
    (SUM(x)/COUNT(y)) is NOT, so we refuse rather than emit a wrong OVER."""
    m = _AGG_HEAD.match(body.strip())
    if not m:
        return False
    b = body.strip()
    depth, start = 0, b.index("(", m.start())
    for idx in range(start, len(b)):
        if b[idx] == "(":
            depth += 1
        elif b[idx] == ")":
            depth -= 1
            if depth == 0:
                return idx == len(b) - 1
    return False


def _rewrite_lod(inner, px):
    """Rewrite ONE LOD body (contents between { }) to SQL. Returns None if it
    cannot be safely translated (INCLUDE/EXCLUDE, or a non-single-agg
    partitioned body) -- caller drops+reports rather than guessing."""
    m = _re.match(r"\s*(fixed|include|exclude)\b(.*)", inner, _re.I | _re.S)
    if m:
        kw = m.group(1).lower()
        rest = m.group(2)
        if kw in ("include", "exclude"):
            return None                    # needs pane context -> unsupported
        depth, cut = 0, -1                 # split on first top-level ':'
        for i, ch in enumerate(rest):
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
            elif ch == ":" and depth == 0:
                cut = i; break
        keypart, body = (rest[:cut], rest[cut + 1:]) if cut >= 0 else ("", rest)
        keys = _re.findall(r"\[([^\]]+)\]", keypart)
        body = body.strip()
        # NESTED LODs ({FIXED a: AVG({FIXED a,b: SUM(x)})}) would need
        # correlated subqueries -- a window inside a window aggregate is
        # illegal SQL. Refuse rather than emit broken/wrong SQL.
        if "{" in body:
            return None
        if keys:
            if not _is_single_agg(body):
                return None
            phys = ", ".join(px(k) for k in keys)
            return f"{body} OVER (PARTITION BY {phys})"
        return f"(SELECT {body} FROM {TBL})"   # table-scoped (no partition)
    if "{" in inner:
        return None
    return f"(SELECT {inner.strip()} FROM {TBL})"   # bare aggregate LOD


def _expand_lods(s, px):
    """Expand every {...} LOD block via brace matching. Returns the rewritten
    string, or None if any LOD is unsupported (so the whole calc is dropped)."""
    if "{" not in s:
        return s
    out, i, n = [], 0, len(s)
    while i < n:
        if s[i] == "{":
            depth, j = 0, i
            while j < n:
                if s[j] == "{":
                    depth += 1
                elif s[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if j >= n:
                return None
            rewritten = _rewrite_lod(s[i + 1:j], px)
            if rewritten is None:
                return None
            out.append(rewritten)
            i = j + 1
        else:
            out.append(s[i]); i += 1
    return "".join(out)


_WINFN = _re.compile(r"\b(WINDOW_(SUM|AVG|MIN|MAX|COUNT|MEDIAN)|"
                     r"RANK_DENSE|RANK_UNIQUE|RANK)\s*\(", _re.I)


def _match_paren(s, i):
    """Index just past the ')' closing the '(' at s[i], or -1."""
    depth = 0
    for j in range(i, len(s)):
        if s[j] == "(":
            depth += 1
        elif s[j] == ")":
            depth -= 1
            if depth == 0:
                return j + 1
    return -1


def _rewrite_window_calcs(s):
    """WINDOW_MAX(body) -> MAX(body) OVER ()   (Tableau default 'Table' scope:
    the whole query result is one partition, so this is layout-independent).
    RANK(body[, 'asc'|'desc']) -> RANK() OVER (ORDER BY body DESC/ASC);
    RANK_DENSE -> DENSE_RANK, RANK_UNIQUE -> ROW_NUMBER.
    Returns None when a call can't be rewritten (unbalanced parens)."""
    out, pos = s, 0
    while True:
        m = _WINFN.search(out, pos)
        if not m:
            return out
        fn = m.group(1).upper()
        op = out.index("(", m.end() - 1)
        end = _match_paren(out, op)
        if end < 0:
            return None
        body = out[op + 1:end - 1].strip()
        if fn.startswith("WINDOW_"):
            repl = f"{fn[7:]}({body}) OVER ()"
        else:
            sqlfn = {"RANK": "RANK", "RANK_DENSE": "DENSE_RANK",
                     "RANK_UNIQUE": "ROW_NUMBER"}[fn]
            direc = "ASC" if _re.search(r",\s*'asc'\s*$", body, _re.I) else "DESC"
            body = _re.sub(r",\s*'(asc|desc)'\s*$", "", body, flags=_re.I).strip()
            if not body:
                return None            # RANK() with no expr needs view context
            repl = f"{sqlfn}() OVER (ORDER BY {body} {direc})"
        out = out[:m.start()] + repl + out[end:]
        pos = m.start() + len(repl)     # never re-match our own replacement


def translate_formula(formula, params=None, calc_defs=None, _depth=0, param_alias=None,
                      colmap=None):
    params = params or {}
    calc_defs = calc_defs or {}
    colmap = colmap or {}

    def _px(name):
        # resolve workbook RENAMES (field 'ORDER_DATE' over column OrderDate)
        return to_phys(colmap.get(name, name))
    if formula is None or _depth > 6:
        return None, False
    s = "\n".join(l for l in formula.splitlines() if not l.strip().startswith("//")).strip()
    if not s or _UNSUPPORTED.search(s):
        return None, False
    # cross-datasource (blend) references cannot translate to single-table SQL
    if _re.search(r"\[federated\.\w+\]\s*\.", s):
        return None, False

    # INDEX() -> post-aggregation row number (engine substitutes the ordering).
    s = _re.sub(r"\bINDEX\s*\(\s*\)", f"ROW_NUMBER() OVER ({WIN_ORDER})", s, flags=_re.I)

    # WINDOW_MAX(agg) / RANK(agg) etc. -> window-over-aggregate (table calcs
    # at Tableau's default "Table" scope; valid inline in a grouped SELECT)
    s = _rewrite_window_calcs(s)
    if s is None:
        return None, False

    # parameter references -> runtime token (unknown params stay literal 0)
    s = _re.sub(r"\[Parameters\]\.\[([^\]]+)\]",
                lambda m: _param_lit(params, m.group(1), param_alias), s)

    # LOD expressions {FIXED [k]: <agg-body>} -> partitioned window aggregate;
    # {FIXED : <agg>} / {<agg>} (no dimension) -> scalar subquery. Handles
    # arbitrary aggregate bodies (IF/CASE, DATEDIFF, ...) via brace matching.
    s = _expand_lods(s, _px)
    if s is None:                          # unsupported LOD (INCLUDE/EXCLUDE,
        return None, False                 # or non-single-aggregate body)

    # Tableau date function names -> SQL
    s = _re.sub(r"\bDATETRUNC\s*\(", "DATE_TRUNC(", s, flags=_re.I)
    s = _re.sub(r"\bTODAY\s*\(\s*\)", "CURRENT_DATE", s, flags=_re.I)
    s = _re.sub(r"\bNOW\s*\(\s*\)", "CURRENT_TIMESTAMP", s, flags=_re.I)

    # COUNTD(<anything>) -> COUNT(DISTINCT <anything>).  COUNTD takes exactly
    # one argument, so the opening-paren swap balances for any body (a bare
    # [field], a CASE/IF expression, etc). Inner [field] refs resolve below.
    s = _re.sub(r"\bCOUNTD\s*\(", "COUNT(DISTINCT ", s, flags=_re.I)

    # remaining [Field] refs: inline nested calcs, else physical column
    def _field(m):
        name = m.group(1)
        if name in calc_defs:
            sub, _ = translate_formula(calc_defs[name], params, calc_defs,
                                       _depth + 1, param_alias, colmap)
            return "(" + sub + ")" if sub else "NULL"
        return _px(name)
    s = _re.sub(r"\[([^\]]+)\]", _field, s)

    # IF / ELSEIF / ELSE / END  ->  CASE WHEN ... END
    s = _re.sub(r"\bIF\b", "CASE WHEN", s, flags=_re.I)
    s = _re.sub(r"\bELSEIF\b", "WHEN", s, flags=_re.I)
    s = _re.sub(r"\bTHEN\b", "THEN", s, flags=_re.I)
    s = _re.sub(r"\bELSE\b", "ELSE", s, flags=_re.I)
    s = _re.sub(r"\bEND\b", "END", s, flags=_re.I)
    # CASE [field] WHEN ... already valid

    # protect ratios against divide-by-zero
    s = _re.sub(r"/\s*(SUM|AVG|COUNT|MIN|MAX)\(", r"/ NULLIF(\1(", s, flags=_re.I)
    # (best-effort: close the NULLIF) -- only when a simple single agg follows
    s = _re.sub(r"/ NULLIF\((SUM|AVG|COUNT|MIN|MAX)\(([^()]*)\)",
                r"/ NULLIF(\1(\2), 0)", s, flags=_re.I)

    # Tableau string literals use double quotes -> SQL single quotes
    s = _re.sub(r'"([^"]*)"', lambda m: "'" + m.group(1).replace("'", "''") + "'", s)
    # agg_ready means "already an aggregate expression". Window aggregates
    # (SUM(x) OVER (...), from FIXED LODs) are ROW-LEVEL -- strip them first
    # so {FIXED Order ID: SUM(Profit)}>0 is not mistaken for an aggregate.
    def _win_sentinel(m):
        # row-grain FIXED windows (AGG(rowexpr) OVER (PARTITION BY ..)) are NOT
        # agg-ready; table-calc windows (AGG(AGG(..)) OVER ()) ARE -- the
        # nested aggregate means the window runs over the grouped result
        return "AGGWIN(1)" if _AGG_RE.search(m.group(0)[m.group(0).index("(") + 1:]) \
               else "WINEXPR"
    no_win = _re.sub(r"\b(SUM|AVG|MIN|MAX|COUNT|MEDIAN|STDEV|VAR)\s*\((?:[^()]|\([^()]*\))*\)\s*OVER\s*\([^()]*\)",
                     _win_sentinel, s, flags=_re.I)
    # scalar subqueries ((SELECT MAX(x) FROM __TBL__)) are row-usable values,
    # NOT aggregations of the current query -- strip before the agg scan or
    # row-level period filters classify agg-level and never push
    no_sub = _re.sub(r"\(\s*SELECT\b(?:[^()]|\([^()]*\))*\)", "SCALARSUB",
                     no_win, flags=_re.I)
    agg_ready = (bool(_AGG_RE.search(no_sub)) or "AGGWIN(1)" in no_win
                 or WIN_ORDER in s)
    if "NULL" == s.strip():
        return None, False
    return s, agg_ready
