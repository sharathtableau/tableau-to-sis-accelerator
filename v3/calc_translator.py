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
import re as _re_mod
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


# 6.3: Snowflake reserved keywords (and DuckDB's, for the local preview) cannot be unquoted column names; a field
# whose name is one gets a trailing underscore (ORDER -> ORDER_), everywhere a physical name is made
RESERVED = frozenset("""
ACCOUNT ALL ALTER AND ANY AS ASC BETWEEN BOTH BY CASE CAST CHECK COLLATE COLUMN CONNECT CONNECTION CONSTRAINT CREATE
CROSS CURRENT CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP CURRENT_USER DATABASE DEFAULT DELETE DESC DISTINCT DO DROP
ELSE END EXCEPT EXISTS FALSE FETCH FOLLOWING FOR FOREIGN FROM FULL GRANT GROUP GSCLUSTER HAVING ILIKE IN INCREMENT
INNER INSERT INTERSECT INTO IS ISSUE JOIN LATERAL LEADING LEFT LIKE LIMIT LOCALTIME LOCALTIMESTAMP MINUS NATURAL NOT
NULL OF OFFSET ON ONLY OR ORDER ORGANIZATION PIVOT PRIMARY QUALIFY REFERENCES REGEXP RETURNING REVOKE RIGHT RLIKE ROW
ROWS SAMPLE SCHEMA SELECT SET SOME START TABLE TABLESAMPLE THEN TO TRAILING TRIGGER TRUE TRY_CAST UNION UNIQUE
UNPIVOT UPDATE USER USING VALUES VIEW WHEN WHENEVER WHERE WINDOW WITH
""".split())


def to_phys(caption):
    """Caption -> UPPER_SNAKE physical column (deterministic, workbook-agnostic); never a reserved keyword."""
    s = re.sub(r"[^0-9a-zA-Z]+", "_", str(caption)).strip("_").upper()
    if s in RESERVED:
        s += "_"
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
# C6: LOOKUP / FIRST / LAST / RUNNING_SUM|AVG|MIN|MAX|COUNT / TOTAL(SUM|MIN|MAX|COUNT) ARE translated
# (_rewrite_table_functions) -- windows the plan fills from the sheet's addressing. Still refused:
_UNSUPPORTED = _re.compile(r"\b(RUNNING_(?!(?:SUM|AVG|MIN|MAX|COUNT)\s*\()\w+|"
                           r"PREVIOUS_VALUE|WINDOW_(?!SUM|AVG|MIN|MAX|"
                           r"COUNT|MEDIAN|STDEVP|STDEV|VARP|VAR|PERCENTILE|CORR|COVARP|COVAR)\w+)\s*\(", _re.I)
# The order-dependent table functions use their OWN placeholder: the exact plan fills it from the sheet's addressing
# (v3_sheet_plan.window_sql); any other path refuses (engine.q) -- never the arbitrary ORDER BY 1 INDEX() falls
# back to, which would silently compute LOOKUP / RUNNING_* over a guessed order.
TC_ORDER = "__TC_ORDER__"

# EN26 / V8: nested table calculations. Tableau lets every calculation nested inside a table calculation keep its
# OWN addressing (<table-calc field='[Inner]' ...> on the pill). A second translation pass tags each nested
# calculation's windows with its internal name; the sheet plan then fills each tag with that calculation's
# addressing. Off by default: every other translation is unchanged.
import contextvars as _cv
_TAG_NESTED = _cv.ContextVar('tag_nested', default=False)


def tag_name(name):
    """A calculation's internal name as a window tag (alphanumerics and '_'; anything else as ~hex~)."""
    return ''.join(c if c.isalnum() or c == '_' else '~%x~' % ord(c) for c in str(name).strip('[]'))


def tag_windows(sql, name):
    """Tag the still-untagged windows of a nested calculation's SQL with its name."""
    t = tag_name(name)
    sql = sql.replace('__TC_ORDER_DESC__', f'__TC_ORDER_DESC@{t}__')
    sql = sql.replace('__TC_ORDER__', f'__TC_ORDER@{t}__').replace('__WIN_ORDER__', f'__WIN_ORDER@{t}__')
    return _re_mod.sub(r'OVER \(\s*\)', f'OVER (__TABLE@{t}__)', sql)


def translate_tagged(formula, *args, **kwargs):
    """translate_formula with every nested calculation's windows tagged (see _TAG_NESTED)."""
    token = _TAG_NESTED.set(True)
    try:
        return translate_formula(formula, *args, **kwargs)
    finally:
        _TAG_NESTED.reset(token)
TC_ORDER_DESC = "__TC_ORDER_DESC__"       # the addressing order reversed (LAST())
_FULL_FRAME = "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING"
_RUN_FRAME = "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW"
_AGG_RE = _re.compile(r"\b(SUM|AVG|MIN|MAX|COUNT|MEDIAN|STDEV|VAR|STDDEV_SAMP|STDDEV_POP|VAR_SAMP|VAR_POP|"
                     r"COVAR_SAMP|COVAR_POP|CORR|COVAR|COVARP|PERCENTILE_CONT|PERCENTILE)\s*\(", _re.I)


def param_sql_literal(v):
    """A parameter VALUE -> SQL literal (float repr for numbers so DECIMAL
    doesn't overflow; strip the embedded quotes XML string values carry)."""
    if v is None:
        return "0"
    try:
        return str(float(v))
    except (TypeError, ValueError):
        pass
    s = str(v).strip().strip('"')
    # Tableau's OWN date/datetime literal syntax is #...# (e.g. #2026-08-26#,
    # #2026-08-26 00:00:00#) -- a date/datetime parameter's raw declared or
    # current value carries this verbatim. Quoted as-is ('#2026-08-26#') the
    # STRING CONTENT still has the hash delimiters, and Snowflake -- asked to
    # implicitly cast it where the parameter is compared against a
    # DATE/TIMESTAMP column, the common case -- fails: "Timestamp
    # '#2026-08-26#' is not recognized". Found live 2026-09-10 across the
    # real corpus (GOLD KR Dashboard family, Customer Opt-In/Out Analysis,
    # and others) -- every date-parameter-driven KPI/filter failed
    # identically. Stripping the delimiters leaves plain ISO-ish date/
    # timestamp text, which Snowflake casts implicitly without issue.
    if s.startswith("#") and s.endswith("#") and len(s) > 1:
        s = s[1:-1]
        # Make the literal's type explicit.  A bare quoted value works in a
        # comparison with a DATE column, but is ambiguous as an argument to
        # DATE_TRUNC in both DuckDB and Snowflake (DATE/TIMESTAMP overloads).
        # Tableau's hash delimiters tell us this is a temporal literal, so we
        # can preserve that information instead of degrading it to VARCHAR.
        typ = "TIMESTAMP" if (" " in s or "T" in s) else "DATE"
        return f"{typ} '" + s.replace("'", "''") + "'"
    return "'" + s.replace("'", "''") + "'"


def param_token(caption):
    """Runtime-substituted placeholder for a parameter (see engine.sub_params)."""
    return f"__PARAM_{to_phys(caption)}__"


_DATE_LIT = _re.compile(r"#(\d{4}-\d{2}-\d{2})(?:[ T](\d{1,2}:\d{2}(?::\d{2})?))?#|#(\d{1,2}:\d{2}(?::\d{2})?)#")


def _date_literals(s):
    """C14: Tableau date / datetime / time literals inside a formula -> SQL literals (string contents untouched)."""
    out, i = [], 0
    for m in _DATE_LIT.finditer(s):
        quote = None
        for ch in s[:m.start()]:
            if quote:
                quote = None if ch == quote else quote
            elif ch in "'\"":
                quote = ch
        if quote:
            continue
        if m.group(3):
            lit = f"TIME '{m.group(3)}'"
        elif m.group(2):
            t = m.group(2) if m.group(2).count(":") == 2 else m.group(2) + ":00"
            lit = f"TIMESTAMP '{m.group(1)} {t}'"
        else:
            lit = f"DATE '{m.group(1)}'"
        out.append(s[i:m.start()] + lit)
        i = m.end()
    out.append(s[i:])
    return "".join(out)


class TranslationFailure(ValueError):
    """A required dependency could not be translated without inventing a value."""


def _param_lit(params, name, param_alias=None):
    v = params.get(name)
    if v is None:
        raise TranslationFailure(f"Unknown parameter: {name}")
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


_LOD_AGG = _re.compile(r"\b(SUM|AVG|MIN|MAX|COUNTD|COUNT|MEDIAN|STDEVP|STDEV|VARP|VAR)\s*\(", _re.I)


def _window_each_agg(body, phys):
    """C7: a FIXED body that combines aggregates ({FIXED k: SUM(a)/SUM(b)}) -> each aggregate OVER (PARTITION BY k):
    the body evaluated over the partition's rows, as Tableau does. None (refuse) when an aggregate nests another or
    a field is referenced outside every aggregate (not an aggregate expression; Tableau rejects it too)."""
    out, pos, outside = [], 0, []
    while True:
        m = _LOD_AGG.search(body, pos)
        if not m:
            outside.append(body[pos:])
            out.append(body[pos:])
            break
        op = body.index("(", m.end() - 1)
        end = _match_paren(body, op)
        if end < 0 or _LOD_AGG.search(body[op + 1:end - 1]):
            return None
        outside.append(body[pos:m.start()])
        out.append(body[pos:m.start()] + body[m.start():end] + f" OVER (PARTITION BY {phys})")
        pos = end
    rest = "".join(outside)
    if not out or len(out) == 1 and not _LOD_AGG.search(body):
        return None
    if _re.search(r"\[(?!Parameters\])[^\]]+\]", _re.sub(r"\[Parameters\]\.\[[^\]]+\]", "", rest)):
        return None
    return "".join(out)


def _inline_lod_bodies(s, calc_defs):
    """C8: a calculation referenced in an LOD BODY is that calculation's formula ({FIXED k: [Users]} with
    Users = SUM([x]) is {FIXED k: SUM([x])}), so the body's aggregates are visible to the LOD rewrite. Keys are left
    as they are (resolved by _key). Chains are followed a few levels; a row-level reference still refuses later."""
    if "{" not in s or not calc_defs:
        return s
    def body_refs(text):
        def sub(m):
            f = calc_defs.get(m.group(1))
            if f is None or "{" in str(f):
                return m.group(0)       # an LOD of its own resolves later as its own value (window / subquery)
            f = "\n".join(l for l in str(f).splitlines() if not l.strip().startswith("//")).strip()
            return "(" + f + ")"
        return _re.sub(r"(?<!\.)\[(?!Parameters\])([^\[\]]+)\](?!\s*\.)", sub, text)
    out, i = [], 0
    while True:
        j = s.find("{", i)
        if j < 0:
            out.append(s[i:])
            return "".join(out)
        depth, k = 0, j
        while k < len(s):
            depth += {"{": 1, "}": -1}.get(s[k], 0)
            if depth == 0:
                break
            k += 1
        if depth:
            out.append(s[i:])
            return "".join(out)
        block = s[j + 1:k]
        m = _re.match(r"\s*(fixed|include|exclude)\b", block, _re.I)
        if m:
            d, cut = 0, -1
            for n, ch in enumerate(block):
                d += 1 if ch in "([" else -1 if ch in ")]" else 0
                if ch == ":" and d == 0:
                    cut = n
                    break
            head, body = (block[:cut + 1], block[cut + 1:]) if cut >= 0 else (block, "")
        else:
            head, body = "", block
        for _ in range(4):
            new = body_refs(body)
            if new == body:
                break
            body = new
        stripped = body.strip()
        while stripped.startswith("(") and _match_paren(stripped, 0) == len(stripped):
            stripped = stripped[1:-1].strip()       # one aggregate wrapped in parens is still one aggregate
        out.append(s[i:j] + "{" + head + " " + stripped + "}")
        i = k + 1


def _rewrite_lod(inner, px):
    """Rewrite ONE LOD body (contents between { }) to SQL. Returns None if it
    cannot be safely translated (INCLUDE/EXCLUDE, or a non-single-agg
    partitioned body) -- caller drops+reports rather than guessing."""
    m = _re.match(r"\s*(fixed|include|exclude)\b(.*)", inner, _re.I | _re.S)
    if m:
        kw = m.group(1).lower()
        rest = m.group(2)
        if kw in ("include", "exclude"):
            return _include_exclude(kw, rest)   # C11: filled from the sheet's own dimensions by the plan
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
        # NESTED LODs ({FIXED a: AVG({FIXED a,b: SUM(x)})}): a window inside a
        # window aggregate is illegal SQL, so the one Tableau-documented shape
        # is rewritten as a grouped subquery (C1); anything else refuses.
        if "{" in body:
            return _nested_fixed(keys, body, px) if kw == "fixed" else None
        if keys:
            phys = ", ".join(px(k) for k in keys)
            if not _is_single_agg(body):
                # C7: any aggregate expression -- every aggregate over the same FIXED partition
                return _window_each_agg(body, phys)
            return f"{body} OVER (PARTITION BY {phys})"
        return f"(SELECT {body} FROM {TBL})"   # table-scoped (no partition)
    if "{" in inner:
        return None
    return f"(SELECT {inner.strip()} FROM {TBL})"   # bare aggregate LOD


INCLUDE_AGGS = ("SUM", "COUNT", "MIN", "MAX")


def _include_exclude(kw, rest):
    """C11: {INCLUDE dims: AGG(x)} -> __INCLUDE__(AGG(x), __KEYS__(dims)); {EXCLUDE dims: AGG(x)} ->
    __EXCLUDE__(AGG(x), __KEYS__(dims)). Both depend on the sheet's dimensions (Tableau computes them after the
    dimension filters, at the view's grain plus / minus the listed dimensions), so the plan writes the SQL
    (v3_sheet_plan.lod_view_sql); any other use of the marker refuses there. One aggregate of SUM / COUNT / MIN /
    MAX only (the shapes that decompose exactly in one grouped query); anything else stays unsupported."""
    depth, cut = 0, -1
    for i, ch in enumerate(rest):
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == ":" and depth == 0:
            cut = i
            break
    if cut < 0:
        return None
    keypart, body = rest[:cut].strip(), rest[cut + 1:].strip()
    if not keypart or "{" in body:
        return None
    if not _is_single_agg(body) and _window_each_agg(body, "K") is None:
        return None                        # C14: any aggregate expression (no row-level field outside an aggregate)
    return f"__{kw.upper()}__({body}, __KEYS__({keypart}))"


def _strip_scalar_subqueries(s):
    """Every '(SELECT ...)' -- at any paren depth (C1 nested LODs) -- replaced by SCALARSUB."""
    out, i = [], 0
    while True:
        m = _re.search(r"\(\s*SELECT\b", s[i:], _re.I)
        if not m:
            return "".join(out) + s[i:]
        start = i + m.start()
        end = _match_paren(s, start)
        if end < 0:
            return "".join(out) + s[i:]
        out.append(s[i:start] + "SCALARSUB")
        i = end


_NULL_KEY = "'~^NULL^~'"


def _nested_fixed(outer_keys, body, px):
    """C1: {FIXED a: AGG({FIXED a,b: AGG2(x)})} -> the inner LOD evaluated at ITS level (one value per (a, b), a
    grouped subquery over the whole table, as a FIXED LOD ignores non-context filters) and the outer aggregate over
    those values for the current row's outer keys (an equality-correlated scalar subquery; NULL keys match each
    other, as in Tableau). Row-level, like every FIXED LOD. Only one nesting level, outer keys a subset of the inner
    keys, the outer body exactly one aggregate of the inner LOD; anything else -> None (not translated)."""
    m = _re.fullmatch(r"\s*(SUM|AVG|MIN|MAX|COUNT)\s*\((.*)\)\s*", body, _re.I | _re.S)
    if not m:
        return None
    agg, arg = m.group(1).upper(), m.group(2).strip()
    while arg.startswith("(") and _match_paren(arg, 0) == len(arg):
        arg = arg[1:-1].strip()
    if not (arg.startswith("{") and arg.endswith("}")) or arg.count("{") != 1 or arg.count("}") != 1:
        return None
    im = _re.match(r"\s*fixed\b(.*)", arg[1:-1], _re.I | _re.S)
    if not im:
        return None
    rest, depth, cut = im.group(1), 0, -1
    for i, ch in enumerate(rest):
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == ":" and depth == 0:
            cut = i; break
    if cut < 0:
        return None
    inner_keys = _re.findall(r"\[([^\]]+)\]", rest[:cut])
    inner_body = rest[cut + 1:].strip()
    low = [k.lower() for k in inner_keys]
    if not inner_keys or not _is_single_agg(inner_body) or any(k.lower() not in low for k in outer_keys):
        return None
    cols = [px(k) for k in inner_keys]
    inner = (f"SELECT {', '.join(f'{c} AS __K{i}' for i, c in enumerate(cols))}, {inner_body} AS __S "
             f"FROM {TBL} GROUP BY {', '.join(cols)}")
    conds = [f"COALESCE(CAST(__g.__K{low.index(k.lower())} AS VARCHAR), {_NULL_KEY}) = "
             # unqualified: the subquery only exposes __K*/__S, so the name resolves to the OUTER row whatever
             # alias the outer query gives its table (the dashboard renderers alias it)
             f"COALESCE(CAST({px(k)} AS VARCHAR), {_NULL_KEY})" for k in outer_keys]
    return (f"(SELECT {agg}(__g.__S) FROM ({inner}) __g" + (f" WHERE {' AND '.join(conds)}" if conds else "") + ")")


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


_WINFN = _re.compile(r"\b(WINDOW_(SUM|AVG|MIN|MAX|COUNT|MEDIAN|STDEVP|STDEV|VARP|VAR|PERCENTILE|CORR|COVARP|COVAR)|"
                     r"RANK_DENSE|RANK_UNIQUE|RANK_MODIFIED|RANK_PERCENTILE|RANK|SIZE)\s*\(", _re.I)
_WIN_AGG = {"STDEV": "STDDEV_SAMP", "STDEVP": "STDDEV_POP", "VAR": "VAR_SAMP", "VARP": "VAR_POP",
            "COVAR": "COVAR_SAMP", "COVARP": "COVAR_POP"}


def _frame_bound(arg, end):
    """A WINDOW_* start / end argument -> a ROWS frame bound (FIRST() / LAST() / an integer offset)."""
    a = arg.replace(" ", "")
    if _re.fullmatch(r"FIRST\(\)", a, _re.I):
        return "UNBOUNDED PRECEDING"
    if _re.fullmatch(r"LAST\(\)", a, _re.I):
        return "UNBOUNDED FOLLOWING"
    if _re.fullmatch(r"[+-]?\d+", a):
        n = int(a)
        return "CURRENT ROW" if n == 0 else f"{-n} PRECEDING" if n < 0 else f"{n} FOLLOWING"
    raise TranslationFailure(f"window offset '{arg}' is not translated (FIRST(), LAST() or a whole number only)")


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


# C6: also the value functions a ratio's denominator is built from (LOOKUP -> LAG/LEAD, ZN -> COALESCE, ABS):
# Snowflake raises on division by zero where Tableau returns NULL
_RATIO_PAREN_RE = _re.compile(r"/\s*\(")
_RATIO_AGG_RE = _re.compile(r"/\s*(SUM|AVG|COUNT|MIN|MAX|ABS|LAG|LEAD|FIRST_VALUE|LAST_VALUE|COALESCE)\s*\(", _re.I)


def _protect_ratio_divisions(s):
    """Wrap every '/ AGG(...)' with NULLIF(AGG(...), 0) so a ratio calc never
    divides by a real zero.

    REPLACES two regexes that used to do this in two passes: one opened
    "NULLIF(" on a bare "/ AGG(", the other tried to close it by matching
    the aggregate's argument as a run of non-parenthesis characters --
    which REQUIRES that argument to contain NO parentheses at all.
    SUM(CASE WHEN ... THEN x END) -- an
    extremely common conditional-measure shape (month-over-month deltas,
    filtered totals) -- has nested parens, so that regex simply never
    matched, and NULLIF( was left open forever: real Snowflake syntax
    errors ("syntax error at end of input") on the real "BDO
    Pre-Optimization Dashboard" / "Email Campaign Analytics" workbooks,
    found 2026-09-09 sweeping the real Tableau Cloud corpus after the
    user reported every migrated report failing in Preview.

    This uses REAL paren matching (_match_paren) instead, so the
    aggregate's true closing paren is found regardless of what is nested
    inside it. The trigger --  '/' directly followed by an aggregate name --
    also makes this naturally idempotent: a division a NESTED calc already
    protected (spliced in by _field() before this runs) reads as
    '/ NULLIF(...)' at this point, and NULLIF is not in the trigger
    alternation, so it is never re-matched and never double-wrapped
    (the earlier, narrower bug this replaces: 2026-09-09, "Sales Dashboard
    Ver 01" -- a swap CASE referencing an existing ratio calc by bracket
    name produced ', 0), 0))')."""
    out = []
    i, n = 0, len(s)
    while i < n:
        m = _RATIO_AGG_RE.match(s, i)
        if not m:
            pm = _RATIO_PAREN_RE.match(s, i)     # C8: '/ (expr)' -- a bracketed denominator, same NULL rule
            if pm:
                close = _match_paren(s, pm.end() - 1)
                inner = s[pm.end():close - 1].strip() if close != -1 else ""
                if close != -1 and not _re.match(r"NULLIF\s*\(", inner, _re.I) and not _re.fullmatch(r"[\d.\s]+", inner):
                    out.append(s[i:pm.end() - 1] + "NULLIF(" + s[pm.end() - 1:close] + ", 0)")
                    i = close
                    continue
            out.append(s[i])
            i += 1
            continue
        agg_open = m.end() - 1                 # index of the '(' just matched
        agg_close = _match_paren(s, agg_open)  # just past its REAL close
        if agg_close == -1:                    # unbalanced -- leave untouched
            out.append(s[i])
            i += 1
            continue
        # C6: a window over the aggregate (SUM(SUM(x)) OVER (...), TOTAL / WINDOW_* / LOOKUP) is ONE operand:
        # NULLIF(SUM(..), 0) OVER () is not SQL
        om = _re.match(r"\s*OVER\s*\(", s[agg_close:], _re.I)
        if om:
            over_close = _match_paren(s, agg_close + om.end() - 1)
            if over_close != -1:
                agg_close = over_close
        out.append(s[m.start():m.start(1)])    # '/' + whitespace
        out.append("NULLIF(")
        out.append(s[m.start(1):agg_close])    # 'SUM(...)' complete, balanced
        out.append(", 0)")
        i = agg_close
    return "".join(out)


_TYPE_CAST_RE = _re.compile(r"\b(INT|FLOAT|STR)\s*\(", _re.I)
_TYPE_CAST_SQL = {"INT": "INTEGER", "FLOAT": "DOUBLE", "STR": "VARCHAR"}


def _rewrite_type_casts(s):
    """Tableau's INT(x) / FLOAT(x) / STR(x) type-conversion functions ->
    CAST(x AS INTEGER/DOUBLE/VARCHAR). Never translated before -- they passed
    straight through as a literal function call. STR(x)/FLOAT(x) merely fail
    later as an unknown function (a Binder/Catalog error, caught elsewhere);
    INT(x) is worse: INT is a reserved TYPE name in both DuckDB and
    Snowflake, not a callable identifier, so "INT(" is a genuine SYNTAX
    error, not just a missing function. Found live 2026-09-09 sweeping the
    real Tableau Cloud corpus: every one of 47 calcs on "Hospitality - Rooms"
    that used INT() to shift a year parameter (STR(INT([Year Select]) - 1))
    failed identically with "syntax error at or near '('".

    Uses real paren matching (_match_paren), like _protect_ratio_divisions,
    so a nested/parenthesized argument (a formula, not a bare field) is
    wrapped correctly rather than truncated at the first inner paren."""
    out = []
    i, n = 0, len(s)
    while i < n:
        m = _TYPE_CAST_RE.match(s, i)
        if not m:
            out.append(s[i])
            i += 1
            continue
        open_paren = m.end() - 1
        close = _match_paren(s, open_paren)
        if close == -1:                        # unbalanced -- leave untouched
            out.append(s[i])
            i += 1
            continue
        sql_type = _TYPE_CAST_SQL[m.group(1).upper()]
        body = s[open_paren + 1:close - 1]     # inside the call's own parens
        # RECURSE on the body before wrapping it: a single left-to-right scan
        # that jumps straight to `close` after one match would skip over a
        # NESTED type-cast call untouched -- e.g. STR(INT([Year Select]) - 1)
        # left INT(YEAR_SELECT) untranslated inside the wrapped CAST, still a
        # real "INT(" syntax error just one level deeper. Found immediately
        # after writing the non-recursive version, verifying it against the
        # actual failing Hospitality-Rooms formula.
        if m.group(1).upper() == "INT":       # C14: Tableau INT truncates toward zero (a CAST rounds)
            out.append(f"CAST(TRUNC(CAST({_rewrite_type_casts(body)} AS DOUBLE)) AS INTEGER)")
        else:
            out.append(f"CAST({_rewrite_type_casts(body)} AS {sql_type})")
        i = close
    return "".join(out)


_QUOTE_CHARS = ("'", '"')


def _rewrite_string_concat(s):
    """Tableau's '+' concatenates when either operand is a string; Snowflake's
    '+' is strictly numeric, '||' concatenates. Telling every string operand
    apart from a numeric one in general needs full schema type inference.
    Two cases are nevertheless certain without schema access: a quoted
    literal, and an expression produced by a SQL string function / VARCHAR
    cast. Both are safe to rewrite. This covers STR(x) + " suffix" as well
    as UPPER(LEFT(x,1)) + LOWER(RIGHT(x,...)); the latter was found on the
    real BDO workbook and broke its shared Channel filter across dashboards.

    THE REAL BUG (found live 2026-09-10, the real 'Sales Dashboard Ver 01'
    workbook's `Review` calc: `STR([ReviewRating]) + " Stars"`, itself
    nested inside the 'Type Selected' parameter-swap dimension used to color
    the Scater sheet): translated to `CAST(REVIEWRATING AS VARCHAR) + '
    Stars'`, valid SQL syntax that Snowflake accepted at COMPILE time -- but
    Snowflake infers a CASE expression's result type from ALL branches
    together, and a '+' makes this branch look NUMERIC. That forces every
    OTHER branch of the same CASE (Country/Category/PaymentMethod/
    ReturnStatus/ShippingMethod, all genuinely VARCHAR) to implicitly
    convert to NUMBER too -- which only fails at RUNTIME, on the first row
    whose value isn't numeric-looking (a real PaymentMethod value, e.g.
    'Credit Card'): 'Numeric value 'Credit Card' is not recognized'. The
    error pointed at a value with no visible connection to the broken
    calc, which is why this class of bug hides well."""
    spans = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c in _QUOTE_CHARS:
            j = i + 1
            while j < n:
                if s[j] == "\\":
                    j += 2
                    continue
                if s[j] == c:
                    j += 1
                    break
                j += 1
            spans.append((i, j))
            i = j
        else:
            i += 1
    lit_starts = {a for a, _ in spans}
    lit_ends = {b for _, b in spans}
    in_lit = [False] * n
    for a, b in spans:
        for k in range(a, b):
            in_lit[k] = True
    def _left_operand(pos):
        end = pos
        while end >= 0 and s[end].isspace():
            end -= 1
        if end < 0:
            return ""
        start = end
        if s[end] == ")":
            depth = 1
            start -= 1
            while start >= 0 and depth:
                if not in_lit[start]:
                    if s[start] == ")": depth += 1
                    elif s[start] == "(": depth -= 1
                start -= 1
            while start >= 0 and (s[start].isalnum() or s[start] == "_"):
                start -= 1
            start += 1
        else:
            while start >= 0 and (s[start].isalnum() or s[start] in "_.'\""):
                start -= 1
            start += 1
        return s[start:end + 1].strip()

    def _right_operand(pos):
        start = pos
        while start < n and s[start].isspace():
            start += 1
        if start >= n:
            return ""
        # function call / CAST: find its balanced outer close
        name = _re.match(r"[A-Za-z_][A-Za-z0-9_]*\s*\(", s[start:])
        if name:
            op = start + name.group(0).rfind("(")
            close = _match_paren(s, op)
            return s[start:close].strip() if close > op else s[start:].strip()
        end = start
        while end < n and (s[end].isalnum() or s[end] in "_.'\""):
            end += 1
        return s[start:end].strip()

    string_fn = _re.compile(
        r"^(?:UPPER|LOWER|LEFT|RIGHT|TRIM|LTRIM|RTRIM|REPLACE|SUBSTR|SUBSTRING|CONCAT)\s*\(",
        _re.I)

    def _is_known_string(expr):
        e = expr.strip()
        return (bool(e and e[0] in _QUOTE_CHARS)
                or bool(string_fn.match(e))
                or bool(_re.match(r"^CAST\s*\(.*\bAS\s+VARCHAR\s*\)$", e,
                                  _re.I | _re.S))
                or "||" in e)

    # C9: an IF / CASE block whose results are string literals is a string operand ("..." + IF .. THEN " x" END)
    toks = [(mt.start(), mt.end(), mt.group(1).upper()) for mt in _re.finditer(r"\b(IF|CASE|END)\b", s, _re.I)
            if not in_lit[mt.start()]]

    def _yields_string(a, b):
        return bool(_re.search(r"\b(?:THEN|ELSE)\s*['\"]", s[a:b], _re.I))

    def _string_block_left(pos):
        if pos < 2 or s[pos - 2:pos + 1].upper() != "END" or (pos - 3 >= 0 and (s[pos - 3].isalnum() or s[pos - 3] == "_")):
            return False
        depth = 0
        for a, b, t in reversed([x for x in toks if x[1] <= pos + 1]):
            depth += 1 if t == "END" else -1
            if depth == 0:
                return _yields_string(a, pos + 1)
        return False

    def _string_block_right(pos):
        mt = _re.match(r"(IF|CASE)\b", s[pos:], _re.I)
        if not mt:
            return False
        depth = 0
        for a, b, t in [x for x in toks if x[0] >= pos]:
            depth += -1 if t == "END" else 1
            if depth == 0:
                return _yields_string(pos, b)
        return False

    convert = set()
    for idx, ch in enumerate(s):
        if ch != "+" or in_lit[idx]:
            continue
        left = idx - 1
        while left >= 0 and s[left].isspace():
            left -= 1
        right = idx + 1
        while right < n and s[right].isspace():
            right += 1
        if ((left + 1) in lit_ends or right in lit_starts
                or _is_known_string(_left_operand(left))
                or _is_known_string(_right_operand(right))
                or _string_block_left(left) or _string_block_right(right)):
            convert.add(idx)
    if not convert:
        return s
    return "".join("||" if i in convert else ch for i, ch in enumerate(s))


def is_window_sql(sql):
    """C6: a translated calculation that is a table function -- a window over the aggregated view the plan fills
    from the sheet's addressing: INDEX (__WIN_ORDER__), LOOKUP / RUNNING_* / FIRST / LAST (__TC_ORDER__),
    WINDOW_* / TOTAL (OVER ()). The single rule for the parser and the scoped field catalogue."""
    sql = sql or ""
    return (WIN_ORDER in sql or "__TC_ORDER" in sql or bool(_re.search(r"\)\s*OVER\s*\(\s*\)", sql))
            or bool(_re.search(r"\bOVER\s*\(\s*ORDER\s+BY\b", sql, _re.I)))      # C14: RANK* over the view


INTEGER_PARAMS = set()       # C9: integer-typed parameters (caption and internal name), set by the parser
DATE_FIELDS = set()          # C9: physical names of the workbook's date / datetime fields (set by the parser)


def set_date_fields(names):
    DATE_FIELDS.clear()
    DATE_FIELDS.update(n.upper() for n in names or ())


_DATE_VALUE = _re.compile(r"^(?:\(\s*SELECT\s+)?(?:MIN|MAX)\s*\(\s*([A-Z_][A-Z0-9_]*)\s*\)|"
                          r"^(?:DATEADD|DATE_TRUNC|DATEPARSE|MAKEDATE|TO_DATE)\s*\(|^(?:CURRENT_DATE|CURRENT_TIMESTAMP)$",
                          _re.I)


def _date_arithmetic(s):
    """C9: Tableau's date +/- n (n days later / earlier) -> DATEADD('day', +/-n, date). Only when the left operand is
    a date: a date field, MIN/MAX of one (a no-key FIXED included), or a date function -- never a number."""
    if not DATE_FIELDS:
        return s
    out, i = s, 0
    while True:
        m = _re.compile(r"\s*([-+])\s*(\d+)(?![\d.])").search(out, i)
        if not m:
            return out
        end = m.start()
        while end > 0 and out[end - 1] == " ":
            end -= 1
        start = None
        if end > 0 and out[end - 1] == ")":
            depth, k = 0, end - 1
            while k >= 0:
                depth += {")": 1, "(": -1}.get(out[k], 0)
                if depth == 0:
                    break
                k -= 1
            j = k
            while j > 0 and (out[j - 1].isalnum() or out[j - 1] == "_"):
                j -= 1
            start = j if k >= 0 else None
        else:
            j = end
            while j > 0 and (out[j - 1].isalnum() or out[j - 1] == "_"):
                j -= 1
            start = j if j < end else None
        operand = out[start:end] if start is not None else ""
        dm = _DATE_VALUE.match(operand.strip())
        is_date = (operand.upper() in DATE_FIELDS or
                   (dm and (dm.group(1) is None or dm.group(1).upper() in DATE_FIELDS)))
        if operand and is_date and (start == 0 or not (out[start - 1].isalnum() or out[start - 1] == "_")):
            n = int(m.group(2)) * (-1 if m.group(1) == "-" else 1)
            repl = f"DATEADD('day', {n}, {operand})"
            out = out[:start] + repl + out[m.end():]
            i = start + len(repl)
        else:
            i = m.end()


_MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October',
           'November', 'December']
_WEEKDAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']


def _rewrite_names_and_attr(s):
    """C9: DATENAME('part', d) -> Tableau's text, portable SQL (Snowflake's MONTHNAME is 'Mar', Tableau's 'March');
    ATTR(x) -> the value when every row agrees, else NULL. None for a DATENAME part not translated (refused)."""
    out, pos = s, 0
    rx = _re.compile(r"\b(DATENAME|ATTR)\s*\(", _re.I)
    while True:
        m = rx.search(out, pos)
        if not m:
            return out
        op = out.index("(", m.end() - 1)
        end = _match_paren(out, op)
        if end < 0:
            return None
        args = _split_args(out[op + 1:end - 1])
        if m.group(1).upper() == "ATTR":
            if len(args) != 1:
                return None
            x = args[0]
            repl = f"CASE WHEN MIN({x}) = MAX({x}) THEN MIN({x}) END"
        else:
            part = args[0].strip().strip("'\"").lower() if args else ""
            d = args[1] if len(args) >= 2 else None
            if d is None:
                return None
            if part in ("year", "quarter", "day"):
                repl = f"CAST({part.upper()}({d}) AS VARCHAR)"
            elif part == "month":
                repl = f"CASE MONTH({d}) " + " ".join(f"WHEN {i} THEN '{n}'" for i, n in enumerate(_MONTHS, 1)) + " END"
            elif part == "weekday":
                # days since a known Sunday (1970-01-04) mod 7: independent of any WEEK_START session setting
                repl = (f"CASE ((DATEDIFF('day', DATE '1970-01-04', {d}) % 7) + 7) % 7 "
                        + " ".join(f"WHEN {i} THEN '{n}'" for i, n in enumerate(_WEEKDAYS)) + " END")
            else:
                return None
        out = out[:m.start()] + repl + out[end:]
        pos = m.start() + len(repl)


class _WinMatch:
    def __init__(self, text):
        self._t = text

    def group(self, i=0):
        return self._t


def _replace_windows(s, repl):
    """Every AGG(...) OVER (...) call (bracket-matched, any nesting) replaced by repl(match-like)."""
    out, i = [], 0
    rx = _re.compile(r"\b(SUM|AVG|MIN|MAX|COUNT|MEDIAN|STDEV|VAR)\s*\(", _re.I)
    while True:
        m = rx.search(s, i)
        if not m:
            out.append(s[i:])
            return "".join(out)
        op = s.index("(", m.end() - 1)
        end = _match_paren(s, op)
        om = _re.match(r"\s*OVER\s*\(", s[end:], _re.I) if end > 0 else None
        if not om:
            out.append(s[i:m.end()])
            i = m.end()                         # step into the call: a window may be nested inside it
            continue
        oend = _match_paren(s, end + om.end() - 1)
        if oend < 0:
            out.append(s[i:])
            return "".join(out)
        out.append(s[i:m.start()] + repl(_WinMatch(s[m.start():oend])))
        i = oend


def _split_args(body):
    """Top-level comma-separated arguments of a call body (parens and quotes respected)."""
    args, depth, cur, quote = [], 0, [], None
    for ch in body:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            args.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    args.append("".join(cur).strip())
    return args


_TABLE_FN = _re.compile(r"\b(LOOKUP|RUNNING_(?:SUM|AVG|MIN|MAX|COUNT)|TOTAL|ZN)\s*\(", _re.I)
_SINGLE_AGG = _re.compile(r"^(SUM|MIN|MAX|COUNT|COUNTD|AVG|MEDIAN|ATTR)\s*\(", _re.I)


def _rewrite_table_functions(s, _top=True):
    """C6: Tableau table functions -> windows over the aggregated view (filled by the plan from the sheet's
    addressing via __WIN_ORDER__ / OVER ()):
      LOOKUP(e, -n|n|0|FIRST()|LAST()) -> LAG / LEAD / e / FIRST_VALUE / LAST_VALUE
      RUNNING_SUM|AVG|MIN|MAX|COUNT(e) -> AGG(e) OVER (order, rows from the first to the current)
      TOTAL(SUM|MIN|MAX|COUNT(x))      -> SUM|MIN|MAX|SUM(AGG(x)) OVER ()   (TOTAL(AVG..) refused)
      ZN(e)                            -> COALESCE((e), 0)
      FIRST() / LAST()                 -> offsets from the first / last row of the partition
    Returns None for a call it cannot translate exactly (non-constant LOOKUP offset, TOTAL of AVG ...)."""
    out, pos = s, 0
    while True:
        m = _TABLE_FN.search(out, pos)
        if not m:
            break
        fn = m.group(1).upper()
        op = out.index("(", m.end() - 1)
        end = _match_paren(out, op)
        if end < 0:
            return None
        inner = _rewrite_table_functions(out[op + 1:end - 1], _top=False)   # innermost first (ZN in LOOKUP)
        if inner is None:
            return None
        args = _split_args(inner)
        if fn == "ZN":
            if len(args) != 1:
                return None
            repl = f"COALESCE(({args[0]}), 0)"
        elif fn == "LOOKUP":
            if len(args) != 2:
                return None
            e, off = args[0], args[1].replace(" ", "")
            if _re.fullmatch(r"FIRST\(\)", off, _re.I):
                repl = f"FIRST_VALUE({e}) OVER ({TC_ORDER} {_FULL_FRAME})"
            elif _re.fullmatch(r"LAST\(\)", off, _re.I):
                repl = f"LAST_VALUE({e}) OVER ({TC_ORDER} {_FULL_FRAME})"
            elif _re.fullmatch(r"[+-]?\d+", off):
                n = int(off)
                repl = (f"({e})" if n == 0 else f"LAG({e}, {-n}) OVER ({TC_ORDER})" if n < 0
                        else f"LEAD({e}, {n}) OVER ({TC_ORDER})")
            else:
                return None                    # a computed offset is not translated
        elif fn.startswith("RUNNING_"):
            if len(args) != 1:
                return None
            repl = f"{fn[8:]}({args[0]}) OVER ({TC_ORDER} {_RUN_FRAME})"
        else:                                  # TOTAL
            am = _SINGLE_AGG.match(args[0]) if len(args) == 1 else None
            if not am or _match_paren(args[0], args[0].index("(")) != len(args[0]):
                return None                    # TOTAL of one aggregate call only
            outer = {"SUM": "SUM", "MIN": "MIN", "MAX": "MAX", "COUNT": "SUM"}.get(am.group(1).upper())
            if not outer:
                return None                    # TOTAL(AVG(x)) != AVG(AVG(x)): not re-aggregable
            repl = f"{outer}({args[0]}) OVER ()"
        out = out[:m.start()] + repl + out[end:]
        pos = m.start() + len(repl)
    if not _top:                               # FIRST() / LAST() as a LOOKUP offset are read by the caller
        return out
    out = _re.sub(r"\bFIRST\s*\(\s*\)", f"(1 - ROW_NUMBER() OVER ({TC_ORDER}))", out, flags=_re.I)
    out = _re.sub(r"\bLAST\s*\(\s*\)", f"(ROW_NUMBER() OVER ({TC_ORDER_DESC}) - 1)", out, flags=_re.I)
    return out


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
        args = _split_args(body) if body else []
        if fn == "SIZE":
            if args:
                return None
            repl = "COUNT(MIN(1)) OVER ()"      # marks in the partition (the plan fills the partition)
        elif fn.startswith("WINDOW_"):
            agg = fn[7:]
            nexpr = 2 if agg in ("PERCENTILE", "CORR", "COVAR", "COVARP") else 1
            if len(args) not in (nexpr, nexpr + 2):
                return None
            if agg == "PERCENTILE":
                core = f"PERCENTILE_CONT({args[1]}) WITHIN GROUP (ORDER BY {args[0]})"
            else:
                core = f"{_WIN_AGG.get(agg, agg)}({', '.join(args[:nexpr])})"
            if len(args) == nexpr:
                repl = f"{core} OVER ()"
            else:
                lo, hi = _frame_bound(args[nexpr], False), _frame_bound(args[nexpr + 1], True)
                if (lo, hi) == ("UNBOUNDED PRECEDING", "UNBOUNDED FOLLOWING"):
                    repl = f"{core} OVER ()"
                elif agg in ("MEDIAN", "PERCENTILE"):
                    raise TranslationFailure(f"{fn} over a moving window is not translated")
                else:                           # a moving window along the addressing (C14)
                    repl = f"{core} OVER ({TC_ORDER} ROWS BETWEEN {lo} AND {hi})"
        else:
            direc = "DESC"
            if len(args) == 2 and (args[1].strip().strip("'\"").lower() in ("asc", "desc")):
                direc = args[1].strip().strip("'\"").upper()
                args = args[:1]
            elif fn == "RANK_PERCENTILE" and len(args) == 1:
                direc = "ASC"                   # Tableau: RANK_PERCENTILE ranks ascending by default
            if len(args) != 1 or not args[0]:
                return None            # RANK() with no expr needs view context
            e = args[0]
            if fn == "RANK_MODIFIED":           # ties take the highest rank: 1, 3, 3, 4
                repl = f"COUNT(*) OVER (ORDER BY {e} {direc} RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)"
            elif fn == "RANK_PERCENTILE":       # (modified rank - 1) / (rows - 1)
                repl = (f"((COUNT(*) OVER (ORDER BY {e} {direc} RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) "
                        f"- 1) / NULLIF(COUNT(*) OVER () - 1, 0))")
            else:
                sqlfn = {"RANK": "RANK", "RANK_DENSE": "DENSE_RANK", "RANK_UNIQUE": "ROW_NUMBER"}[fn]
                repl = f"{sqlfn}() OVER (ORDER BY {e} {direc})"
        out = out[:m.start()] + repl + out[end:]
        pos = m.start() + len(repl)     # never re-match our own replacement



# ---------------------------------------------------------------------------------------------------------------------
# C14: the Tableau function catalogue -> Snowflake SQL with Tableau's own meaning, or a named refusal. Runs after the
# field references are resolved (arguments are SQL), before IF/CASE and string-literal conversion.
_REFUSE_FN = [
    (_re.compile(r"^SCRIPT_", _re.I), "external service (TabPy / R) calculation: not run in Snowflake (EN18)"),
    (_re.compile(r"^RAWSQL", _re.I), "pass-through SQL written for the original database: not run (EN18)"),
    (_re.compile(r"^MODEL_", _re.I), "Tableau predictive model function: not reproduced"),
    (_re.compile(r"^(MAKEPOINT|MAKELINE|DISTANCE|BUFFER|AREA|INTERSECTS|OUTLINE|SHAPETYPE|VALIDATE|HEXBINX|HEXBINY|"
                 r"COLLECT)$", _re.I), "spatial function: not translated yet"),
    (_re.compile(r"^(USERNAME|FULLNAME|USERDOMAIN|ISMEMBEROF|ISUSERNAME|ISFULLNAME|USERATTRIBUTE|"
                 r"USERATTRIBUTEINCLUDES)$", _re.I),
     "user function: becomes a Snowflake row access policy (EN27), not a calculation"),
]
_FN_CALL = _re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_SUNDAY = "DATE '1970-01-04'"            # a known Sunday: weekday maths independent of WEEK_START


def _q(arg):
    """A Tableau string-literal argument's text ("x" or 'x'), else None."""
    a = arg.strip()
    if len(a) >= 2 and a[0] == a[-1] and a[0] in "'\"":
        return a[1:-1]
    return None


def _lit(text):
    return "'" + str(text).replace("'", "''") + "'"


def _dow0(d):
    """Weekday with Sunday = 0 (Tableau's default week start)."""
    return f"(((DATEDIFF('day', {_SUNDAY}, {d}) % 7) + 7) % 7)"


def _tableau_week(d):
    """Tableau WEEK / DATEPART('week'): week 1 holds 1 January, weeks start on Sunday."""
    return f"(FLOOR((EXTRACT(DOY FROM {d}) - 1 + {_dow0(f'DATE_TRUNC(' + chr(39) + 'year' + chr(39) + f', {d})')}) / 7) + 1)"


_ICU = [("yyyy", "YYYY"), ("yy", "YY"), ("MMMM", "MMMM"), ("MMM", "MON"), ("MM", "MM"), ("dd", "DD"),
        ("HH", "HH24"), ("hh", "HH12"), ("mm", "MI"), ("ss", "SS"), ("a", "AM")]


def _snowflake_format(icu):
    out, i = [], 0
    while i < len(icu):
        for a, b in _ICU:
            if icu.startswith(a, i):
                out.append(b)
                i += len(a)
                break
        else:
            if icu[i].isalpha():
                raise TranslationFailure(f"DATEPARSE format '{icu}': token '{icu[i]}' not translated")
            out.append(icu[i])
            i += 1
    return "".join(out)


def _fn_sql(name, args):
    """SQL for one Tableau call (arguments already translated), or None to leave it as written."""
    u, n = name.upper(), len(args)
    a = args
    if u == "DIV" and n == 2:
        return f"CAST(TRUNC(({a[0]}) / NULLIF({a[1]}, 0)) AS INTEGER)"
    if u == "SQUARE" and n == 1:
        return f"POWER({a[0]}, 2)"
    if u == "LOG" and n in (1, 2):
        return f"LOG({a[1] if n == 2 else 10}, {a[0]})"
    if u == "LOG2" and n == 1:
        return f"LOG(2, {a[0]})"
    if u == "COT" and n == 1:
        return f"(1 / NULLIF(TAN({a[0]}), 0))"
    if u in ("MIN", "MAX") and n == 2:           # two-argument form: row-level, NULL when either is NULL
        op = "<=" if u == "MIN" else ">="
        return f"(CASE WHEN ({a[0]}) IS NULL OR ({a[1]}) IS NULL THEN NULL WHEN ({a[0]}) {op} ({a[1]}) THEN ({a[0]}) ELSE ({a[1]}) END)"
    if u == "CHAR" and n == 1:
        return f"CHR({a[0]})"
    if u == "STARTSWITH" and n == 2:
        return f"(LEFT({a[0]}, LENGTH({a[1]})) = {a[1]})"
    if u == "ENDSWITH" and n == 2:
        return f"(RIGHT({a[0]}, LENGTH({a[1]})) = {a[1]})"
    if u == "FIND" and n in (2, 3):
        if n == 2:
            return f"POSITION({a[1]} IN {a[0]})"
        sub = f"POSITION({a[1]} IN SUBSTR({a[0]}, {a[2]}))"
        return f"(CASE WHEN {sub} = 0 THEN 0 ELSE {sub} + ({a[2]}) - 1 END)"
    if u == "FINDNTH" and n == 3:
        try:
            k = int(str(a[2]).strip())
        except ValueError:
            raise TranslationFailure("FINDNTH with a computed occurrence is not translated")
        if not 1 <= k <= 10:
            raise TranslationFailure("FINDNTH occurrence outside 1-10 is not translated")
        pos = f"POSITION({a[1]} IN {a[0]})"
        for _ in range(k - 1):
            sub = f"POSITION({a[1]} IN SUBSTR({a[0]}, {pos} + 1))"
            pos = f"(CASE WHEN {pos} = 0 OR {sub} = 0 THEN 0 ELSE {pos} + {sub} END)"
        return pos
    if u == "MID" and n in (2, 3):
        return f"SUBSTR({', '.join(a)})"
    if u == "LEN" and n == 1:
        return f"LENGTH({a[0]})"
    if u == "PROPER" and n == 1:
        return f"INITCAP({a[0]})"
    if u == "SPACE" and n == 1:
        return f"REPEAT(' ', {a[0]})"
    if u == "SPLIT" and n == 3:
        return f"SPLIT_PART({a[0]}, {a[1]}, {a[2]})"
    if u == "REGEXP_MATCH" and n == 2:
        return f"(REGEXP_INSTR({a[0]}, {a[1]}) > 0)"
    if u == "REGEXP_EXTRACT" and n == 2:
        return f"REGEXP_SUBSTR({a[0]}, {a[1]}, 1, 1, 'e', 1)"
    if u == "REGEXP_EXTRACT_NTH" and n == 3:
        return f"REGEXP_SUBSTR({a[0]}, {a[1]}, 1, 1, 'e', {a[2]})"
    if u == "DATEPART" and n in (2, 3):
        part, d = (_q(a[0]) or "").lower(), a[1]
        if part in ("year", "quarter", "month", "day", "hour", "minute", "second"):
            return f"EXTRACT({part.upper()} FROM {d})"
        if part == "dayofyear":
            return f"EXTRACT(DOY FROM {d})"
        if part == "weekday":
            return f"({_dow0(d)} + 1)"
        if part == "week":
            return _tableau_week(d)
        if part == "iso-week":
            return f"WEEKISO({d})"
        if part == "iso-year":
            return f"YEAROFWEEKISO({d})"
        if part == "iso-weekday":
            return f"DAYOFWEEKISO({d})"
        raise TranslationFailure(f"DATEPART '{part}' is not translated")
    if u == "WEEK" and n == 1:
        return _tableau_week(a[0])
    if u == "DATEDIFF" and n in (3, 4) and (_q(a[0]) or "").lower() == "week":
        f = lambda d: f"FLOOR(DATEDIFF('day', {_SUNDAY}, {d}) / 7)"
        return f"({f(a[2])} - {f(a[1])})"
    if u == "ISOWEEK" and n == 1:
        return f"WEEKISO({a[0]})"
    if u == "ISOYEAR" and n == 1:
        return f"YEAROFWEEKISO({a[0]})"
    if u == "ISOWEEKDAY" and n == 1:
        return f"DAYOFWEEKISO({a[0]})"
    if u == "ISOQUARTER" and n == 1:
        return f"LEAST(CEIL(WEEKISO({a[0]}) / 13), 4)"
    if u == "DATEPARSE" and n == 2:
        fmt = _q(a[0])
        if fmt is None:
            raise TranslationFailure("DATEPARSE with a computed format is not translated")
        return f"TRY_TO_TIMESTAMP({a[1]}, {_lit(_snowflake_format(fmt))})"
    if u == "ISDATE" and n == 1:
        return f"(TRY_CAST({a[0]} AS DATE) IS NOT NULL)"
    if u == "MAKEDATE" and n == 3:
        return f"DATE_FROM_PARTS({', '.join(a)})"
    if u == "MAKEDATETIME" and n == 2:
        return f"TIMESTAMP_FROM_PARTS({a[0]}, {a[1]})"
    if u == "MAKETIME" and n == 3:
        return f"TIME_FROM_PARTS({', '.join(a)})"
    if u == "DATETIME" and n == 1:
        return f"CAST({a[0]} AS TIMESTAMP)"
    if u == "IIF" and n in (3, 4):
        unknown = a[3] if n == 4 else "NULL"
        return f"(CASE WHEN {a[0]} THEN {a[1]} WHEN NOT ({a[0]}) THEN {a[2]} ELSE {unknown} END)"
    if u == "ISNULL" and n == 1:
        return f"(({a[0]}) IS NULL)"
    if u in ("STDEV", "STDEVP", "VAR", "VARP") and n == 1:
        return {"STDEV": "STDDEV_SAMP", "STDEVP": "STDDEV_POP", "VAR": "VAR_SAMP", "VARP": "VAR_POP"}[u] + f"({a[0]})"
    if u in ("COVAR", "COVARP") and n == 2:
        return ("COVAR_SAMP" if u == "COVAR" else "COVAR_POP") + f"({a[0]}, {a[1]})"
    if u == "PERCENTILE" and n == 2:
        return f"PERCENTILE_CONT({a[1]}) WITHIN GROUP (ORDER BY {a[0]})"
    return None


def _rewrite_power(s):
    """C14: Tableau's power operator a ^ b -> POWER(a, b) (right-associative; Tableau's negate binds tighter than
    ^, so -2^2 = 4 while 3 - 2^2 = -1; the exponent may carry its own sign)."""
    def atom_left(t, k):                    # start index of the operand ending at t[k-1]
        j = k
        while j > 0 and t[j - 1] == " ":
            j -= 1
        if j > 0 and t[j - 1] == ")":
            depth = 0
            for p in range(j - 1, -1, -1):
                depth += {")": 1, "(": -1}.get(t[p], 0)
                if depth == 0:
                    q = p
                    while q > 0 and (t[q - 1].isalnum() or t[q - 1] == "_"):
                        q -= 1
                    return q
            return None
        q = j
        while q > 0 and (t[q - 1].isalnum() or t[q - 1] in "_.'"):
            q -= 1
        return q if q < j else None

    def atom_right(t, k):                   # end index of the operand starting at t[k]
        j = k
        while j < len(t) and t[j] == " ":
            j += 1
        if j < len(t) and t[j] in "+-":
            j += 1
        if j < len(t) and (t[j].isalnum() or t[j] == "_"):
            q = j
            while q < len(t) and (t[q].isalnum() or t[q] in "_."):
                q += 1
            if q < len(t) and t[q] == "(":
                q = _match_paren(t, q)
            return q
        if j < len(t) and t[j] == "(":
            return _match_paren(t, j)
        return None
    while True:
        k = max((i for i, ch in enumerate(s) if ch == "^" and not _in_string(s, i)), default=-1)
        if k < 0:
            return s
        lo, hi = atom_left(s, k), atom_right(s, k + 1)
        if lo is not None and lo > 0:           # a negate on the base (start, an operator or '(' before it)
            p = lo - 1
            while p >= 0 and s[p] == " ":
                p -= 1
            if p >= 0 and s[p] == "-":
                q = p - 1
                while q >= 0 and s[q] == " ":
                    q -= 1
                if q < 0 or s[q] in "(,+-*/%=<>^" or s[max(0, q - 3):q + 1].upper().endswith(("THEN", "ELSE", " AND")):
                    lo = p
        if lo is None or hi is None or hi < 0:
            raise TranslationFailure("'^' with an operand that is not translated")
        s = s[:lo] + f"POWER({s[lo:k].strip()}, {s[k + 1:hi].strip()})" + s[hi:]


def _rewrite_functions(s):
    """C14: every Tableau call rewritten (inner calls first); a refused function raises with its reason."""
    out, i = [], 0
    while True:
        m = _FN_CALL.search(s, i)
        if not m:
            out.append(s[i:])
            return "".join(out)
        name = m.group(1)
        if _in_string(s, m.start()):
            out.append(s[i:m.end()])
            i = m.end()
            continue
        for rx, why in _REFUSE_FN:
            if rx.search(name):
                raise TranslationFailure(f"{name.upper()}: {why}")
        op = m.end() - 1
        end = _match_paren(s, op)
        if end < 0:
            out.append(s[i:])
            return "".join(out)
        args = [_rewrite_functions(x) for x in _split_args(s[op + 1:end - 1])] if s[op + 1:end - 1].strip() else []
        repl = _fn_sql(name, args)
        if repl is None:
            out.append(s[i:op + 1] + ", ".join(args) + ")")
        else:
            out.append(s[i:m.start()] + repl)
        i = end


def _in_string(s, pos):
    """True when s[pos] sits inside a quoted literal."""
    quote = None
    for ch in s[:pos]:
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
    return quote is not None


# names a translated calculation may call: Snowflake functions the translator emits or that Tableau shares as is.
# Anything else is a Tableau function nobody translated -- refused by name instead of failing at query time.
_KNOWN_FN = set("""ABS ACOS ASIN ATAN ATAN2 COS SIN TAN CEIL CEILING FLOOR ROUND SQRT POWER EXP LN LOG SIGN PI DEGREES
RADIANS TRUNC MOD GREATEST LEAST COALESCE NULLIF IFNULL NVL CAST TRY_CAST ASCII CHR CONTAINS LEFT RIGHT SUBSTR SUBSTRING
LENGTH LOWER UPPER INITCAP REPLACE REPEAT SPLIT_PART TRIM LTRIM RTRIM POSITION REGEXP_INSTR REGEXP_SUBSTR REGEXP_REPLACE
CONCAT DATE TO_DATE DATE_TRUNC DATEADD DATEDIFF EXTRACT YEAR QUARTER MONTH DAY DAYOFYEAR HOUR MINUTE SECOND WEEKISO
YEAROFWEEKISO DAYOFWEEKISO DATE_FROM_PARTS TIMESTAMP_FROM_PARTS TIME_FROM_PARTS TRY_TO_TIMESTAMP SUM AVG MIN MAX COUNT
MEDIAN STDDEV_SAMP STDDEV_POP VAR_SAMP VAR_POP COVAR_SAMP COVAR_POP CORR PERCENTILE_CONT ROW_NUMBER RANK DENSE_RANK LAG
LEAD FIRST_VALUE LAST_VALUE NTH_VALUE PERCENT_RANK CUME_DIST NTILE HASH STR INT FLOAT ZN
AGGWIN WINEXPR""".split())
_SQL_WORDS_BEFORE_PAREN = set("""CASE WHEN THEN ELSE END AND OR NOT IN IS AS OVER BY FROM WHERE SELECT EXISTS WITHIN
GROUP PARTITION ORDER ON VALUES NULL DISTINCT BETWEEN LIKE INTERVAL ROWS RANGE""".split())


def _unknown_function(s):
    """The first called name that is neither emitted SQL nor shared with Tableau (None when all are known)."""
    for m in _FN_CALL.finditer(s):
        name = m.group(1).upper()
        if name in _KNOWN_FN or name in _SQL_WORDS_BEFORE_PAREN or name.startswith("__") or _in_string(s, m.start()):
            continue
        return name
    return None


REFUSALS = {}       # formula -> why it was refused (the most recent translation of that text)


def refusal(formula):
    """The reason a formula was refused by translate_formula (or a generic one)."""
    return REFUSALS.get((formula or "").strip(), "not translated")


def translate_formula(formula, params=None, calc_defs=None, _depth=0, param_alias=None,
                      colmap=None):
    try:
        return _translate_formula_impl(formula, params, calc_defs, _depth, param_alias, colmap)
    except TranslationFailure as exc:
        # Existing callers represent refusal as (None, False); preserve that API. C14: the reason is kept.
        REFUSALS[(formula or "").strip()] = str(exc)
        return None, False


def _translate_formula_impl(formula, params=None, calc_defs=None, _depth=0, param_alias=None,
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
    if not s:
        return None, False
    bad = _UNSUPPORTED.search(s)
    if bad:
        name = bad.group(1).upper()
        raise TranslationFailure(f"{name}: " + ("refers to its own previous result (recursive): not expressible in "
                                                "SQL" if name == "PREVIOUS_VALUE" else "table function not translated"))
    s = _date_literals(s)              # C14: #2024-03-20# / #2024-03-20 10:00:00# / #10:00:00#
    # cross-datasource (blend) references cannot translate to single-table SQL
    if _re.search(r"\[federated\.\w+\]\s*\.", s):
        return None, False
    # C8: a calculation referenced in an LOD body is its formula -- inlined FIRST, so its parameters, table
    # functions and fields go through every step below like the rest of the formula
    s = _inline_lod_bodies(s, calc_defs)
    if _UNSUPPORTED.search(s) or _re.search(r"\[federated\.\w+\]\s*\.", s):
        return None, False

    # INDEX() -> post-aggregation row number (engine substitutes the ordering).
    s = _re.sub(r"\bINDEX\s*\(\s*\)", f"ROW_NUMBER() OVER ({WIN_ORDER})", s, flags=_re.I)

    # WINDOW_MAX(agg) / RANK(agg) etc. -> window-over-aggregate (table calcs
    # at Tableau's default "Table" scope; valid inline in a grouped SELECT). C14: first, so a moving window's
    # FIRST() / LAST() bounds are read as bounds before FIRST() / LAST() become row offsets below
    s = _rewrite_window_calcs(s)
    if s is None:
        return None, False

    # C6: LOOKUP / FIRST / LAST / RUNNING_* / TOTAL / ZN -> windows over the aggregated view
    s = _rewrite_table_functions(s)
    if s is None:
        return None, False

    # C9: STR() of an integer parameter is its integer text ('2025'), as in Tableau -- the runtime value is a float
    s = _re.sub(r"\bSTR\s*\(\s*(\[Parameters\]\.\[([^\]]+)\])\s*\)",
                lambda m: f"STR(INT({m.group(1)}))" if m.group(2) in INTEGER_PARAMS else m.group(0), s, flags=_re.I)

    # parameter references -> runtime token (unknown params stay literal 0)
    s = _re.sub(r"\[Parameters\]\.\[([^\]]+)\]",
                lambda m: _param_lit(params, m.group(1), param_alias), s)

    # LOD expressions {FIXED [k]: <agg-body>} -> partitioned window aggregate;
    # {FIXED : <agg>} / {<agg>} (no dimension) -> scalar subquery. Handles
    # arbitrary aggregate bodies (IF/CASE, DATEDIFF, ...) via brace matching.
    def _key(name):
        # C7: a calculated FIXED key is its own row-level expression, never its internal name as a column
        if name in calc_defs:
            sub, agg = translate_formula(calc_defs[name], params, calc_defs, _depth + 1, param_alias, colmap)
            if sub is None or agg:
                raise TranslationFailure(f"FIXED key '{name}' is not a row-level dimension")
            return f"({sub})"
        return _px(name)
    s = _expand_lods(s, _key)
    if s is None:                          # unsupported LOD (INCLUDE/EXCLUDE,
        return None, False                 # or non-single-aggregate body)

    # C9: DATENAME / ATTR (Tableau's own text / agreement semantics)
    s = _rewrite_names_and_attr(s)
    if s is None:
        return None, False
    # Tableau date function names -> SQL
    s = _re.sub(r"\bDATETRUNC\s*\(", "DATE_TRUNC(", s, flags=_re.I)
    s = _re.sub(r"\bTODAY\s*\(\s*\)", "CURRENT_DATE", s, flags=_re.I)
    s = _re.sub(r"\bNOW\s*\(\s*\)", "CURRENT_TIMESTAMP", s, flags=_re.I)

    # Tableau type-conversion functions -> CAST (see _rewrite_type_casts)
    s = _rewrite_type_casts(s)

    # Tableau '+' concatenates when touching a string literal; SQL's is
    # numeric-only (see _rewrite_string_concat)
    s = _rewrite_string_concat(s)

    # COUNTD(<anything>) -> COUNT(DISTINCT <anything>).  COUNTD takes exactly
    # one argument, so the opening-paren swap balances for any body (a bare
    # [field], a CASE/IF expression, etc). Inner [field] refs resolve below.
    s = _re.sub(r"\bCOUNTD\s*\(", "COUNT(DISTINCT ", s, flags=_re.I)

    # remaining [Field] refs: inline nested calcs, else physical column
    def _resolve_field(name, before, after):
        if name in calc_defs:
            sub, _ = translate_formula(calc_defs[name], params, calc_defs,
                                       _depth + 1, param_alias, colmap)
            if sub is None:
                raise TranslationFailure(f"Untranslated calculation dependency: {name}")
            if _TAG_NESTED.get() and is_window_sql(sub):
                sub = tag_windows(sub, name)
            val = "(" + sub + ")"
        else:
            val = _px(name)
        # Guard a real, found-live bug: Tableau's own formula syntax needs no
        # whitespace around a bracket reference -- "]" and "[" are
        # unambiguous token boundaries there -- so "...[Unique Recipients]END"
        # is completely normal Tableau authoring. Once _px() replaces it with
        # the bare identifier UNIQUE_RECIPIENTS (no delimiter), a following
        # keyword with no original space FUSES onto it textually:
        # "UNIQUE_RECIPIENTSEND". \bEND\b (below) requires a word boundary on
        # both sides, and underscore/letters are all word characters, so it
        # can no longer see "END" as a keyword at all -- the CASE never
        # closes. Found live 2026-09-09 across the real "GOLD KR Dashboard"
        # family (identical calc definitions copied into 3 separate
        # workbooks, all 3 broken identically) -- "syntax error at or near
        # ')'" because a CASE that never got its END has nothing left to
        # legally close. The same fusion can happen on the LEADING side too
        # (THEN/ELSE immediately before a bracket ref with no space).
        #
        # Padding is applied ONLY where a collision would actually happen --
        # checked against val's own edges (a nested-calc substitution is
        # "(...)"" and can never fuse, since "(" and ")" are not word
        # characters) and the ORIGINAL text's adjacent character (a bracket
        # ref already surrounded by punctuation, the overwhelmingly common
        # case, needs nothing). Unconditional padding was tried first and
        # reverted: it passed every real-bug check here but cosmetically
        # changed already-correct output ("SUM(REVENUE)" -> "SUM( REVENUE )"),
        # breaking exact-string regression gates elsewhere for no semantic
        # reason.
        lead = " " if val[:1].isalnum() and (before.isalnum() or before == "_") else ""
        trail = " " if val[-1:].isalnum() and (after.isalnum() or after == "_") else ""
        return lead + val + trail

    def _substitute_field_refs(text):
        """Replace every [Field] reference with its resolved SQL, walking the
        string by hand instead of a single regex -- SKIPPING any '[' or ']'
        that sits inside a single-quoted STRING LITERAL.

        THE BUG (found live 2026-09-09, real "Campaign Incrementality vs
        Holdout Group" workbook): the old regex r"\\[([^\\]]+)\\]" has no
        concept of string quoting. A formula that manipulates strings
        CONTAINING literal bracket characters --
        REPLACE(REPLACE(LEFT([Name], FIND([Name],']')), '[', ''), ']', '') --
        has stray '[' / ']' characters that are STRING CONTENT, not field
        delimiters. The regex's first two matches (the real [Name] refs)
        resolved fine, but once it reached the stray '[' inside the '[' ' '
        string literal, it greedily matched everything up to the NEXT ']'
        ANYWHERE LATER in the formula -- past the real closing parens, past
        the second REPLACE's own arguments -- swallowing it all into one
        bogus "field name" (literally the text "],''),'" in the real case).
        to_phys() sanitizes a no-alphanumeric name down to nothing and falls
        back to its own literal default, "COL" -- which is exactly what
        silently replaced a real argument, corrupting the SQL and leaving
        the outer REPLACE's own closing paren stranded ("syntax error at end
        of input")."""
        out = []
        i, n = 0, len(text)
        # Track WHICH quote character opened the current string -- this
        # step runs BEFORE the later "Tableau string literals use double
        # quotes" conversion, so a formula can still be using either quote
        # style here (REPLACE(..., "[", "") is exactly as valid Tableau
        # authoring as the single-quoted form). Only the SAME character
        # that opened a string can close it.
        quote_char = None
        while i < n:
            c = text[i]
            if quote_char is not None:
                if c == quote_char:
                    if i + 1 < n and text[i + 1] == quote_char:  # doubled = escaped
                        out.append(c * 2)
                        i += 2
                        continue
                    quote_char = None
                out.append(c)
                i += 1
                continue
            if c in "'\"":
                quote_char = c
                out.append(c)
                i += 1
                continue
            if c == "[":
                j = text.find("]", i + 1)
                if j == -1:                    # unmatched -- leave untouched
                    out.append(c)
                    i += 1
                    continue
                name = text[i + 1:j]
                before = text[i - 1] if i > 0 else ""
                after = text[j + 1] if j + 1 < n else ""
                out.append(_resolve_field(name, before, after))
                i = j + 1
                continue
            out.append(c)
            i += 1
        return "".join(out)
    s = _substitute_field_refs(s)
    s = _date_arithmetic(s)            # C9: date +/- n days
    s = _rewrite_power(s)              # C14: a ^ b
    s = _rewrite_functions(s)          # C14: the Tableau function catalogue (or a named refusal)

    # IF / ELSEIF / ELSE / END  ->  CASE WHEN ... END
    s = _re.sub(r"\bIF\b", "CASE WHEN", s, flags=_re.I)
    s = _re.sub(r"\bELSEIF\b", "WHEN", s, flags=_re.I)
    s = _re.sub(r"\bTHEN\b", "THEN", s, flags=_re.I)
    s = _re.sub(r"\bELSE\b", "ELSE", s, flags=_re.I)
    s = _re.sub(r"\bEND\b", "END", s, flags=_re.I)
    # CASE [field] WHEN ... already valid

    # protect ratios against divide-by-zero -- see _protect_ratio_divisions
    # for the two real bugs this single paren-aware pass replaces (a nested
    # ratio calc's already-closed NULLIF getting double-closed; an aggregate
    # wrapping a CASE/nested call never getting closed at all).
    s = _protect_ratio_divisions(s)

    # Tableau string literals use double quotes -> SQL single quotes
    s = _re.sub(r'"([^"]*)"', lambda m: "'" + m.group(1).replace("'", "''") + "'", s)
    # agg_ready means "already an aggregate expression". Window aggregates
    # (SUM(x) OVER (...), from FIXED LODs) are ROW-LEVEL -- strip them first
    # so {FIXED Order ID: SUM(Profit)}>0 is not mistaken for an aggregate.
    def _win_sentinel(m):
        # row-grain FIXED windows (AGG(rowexpr) OVER (PARTITION BY ..)) are NOT
        # agg-ready; table-calc windows (AGG(AGG(..)) OVER ()) ARE -- the
        # nested aggregate means the window runs over the grouped result
        # a FIXED over a FIXED (AVG(SUM(x) OVER (..)) OVER (..)) is still row-level: inner windows first
        body = _replace_windows(m.group(0)[m.group(0).index("(") + 1:], lambda _m: "WINEXPR")
        return "AGGWIN(1)" if _AGG_RE.search(body) else "WINEXPR"
    no_win = _replace_windows(s, _win_sentinel)     # C10: bracket-matched (any nesting in body or OVER)
    # scalar subqueries ((SELECT MAX(x) FROM __TBL__)) are row-usable values,
    # NOT aggregations of the current query -- strip before the agg scan or
    # row-level period filters classify agg-level and never push
    no_sub = _strip_scalar_subqueries(no_win)
    agg_ready = (bool(_AGG_RE.search(no_sub)) or "AGGWIN(1)" in no_win
                 or WIN_ORDER in s or "__TC_ORDER" in s)
    if "NULL" == s.strip():
        return None, False
    unknown = _unknown_function(s)
    if unknown:
        raise TranslationFailure(f"{unknown}: Tableau function not translated yet")
    return s, agg_ready
