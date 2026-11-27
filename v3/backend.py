"""
backend.py  --  data-access layer with a local/Snowflake switch (MULTI-table).

run_sql(sql) executes the SAME SQL string in either environment:
  * In Snowflake (SiS): runs via the active Snowpark session.
  * Locally: loads EVERY datasource file from config.DATASOURCES into DuckDB
    and rewrites each fully-qualified table name to its local table.

The SQL the app emits (SUM, GROUP BY, DATE_TRUNC, NULLIF, window functions)
is valid in both DuckDB and Snowflake, so results verify locally before deploy.
"""

import os
import re
import pandas as pd
import config

_LOCAL_CON = None          # cached DuckDB connection
_EXTERNAL_SESSION = None   # a Snowpark session opened OUTSIDE Snowflake -- e.g.
                           # pipeline_app.py's "Push to Snowflake on upload"
                           # checkbox, run on a laptop. Without this, a session
                           # opened that way was invisible to backend.py (it only
                           # ever checked get_active_session(), which is True
                           # ONLY when the process is deployed inside SiS) -- so
                           # a locally-run-but-Snowflake-connected migrator would
                           # push tables + the semantic view for real, then
                           # silently render every chart from local DuckDB
                           # anyway. That defeats the entire "runs on Snowflake,
                           # Cortex is native" pitch, which is the whole point.
LOCAL_TABLE = "ORDERS_LOCAL"   # legacy alias for the default datasource


def _local_name(ds_caption):
    return re.sub(r"[^0-9A-Za-z]+", "_", ds_caption).strip("_").upper() + "_LOCAL"


def set_session(session):
    """Register an externally-opened Snowpark session so EVERY query this
    process makes runs as real Snowflake compute, even though the process
    itself is running on a laptop. Pass None to clear (revert to local DuckDB).
    Also drops the cached DuckDB connection so a session switch can never
    silently mix data from the two sources."""
    global _EXTERNAL_SESSION, _LOCAL_CON
    _EXTERNAL_SESSION = session
    _LOCAL_CON = None


def _active_session():
    """The Snowpark session to query against, if any: either one this process
    deliberately opened (set_session) or the one Snowflake itself hands a
    deployed SiS app (get_active_session). None -> caller uses local DuckDB."""
    if _EXTERNAL_SESSION is not None:
        return _EXTERNAL_SESSION
    try:
        from snowflake.snowpark.context import get_active_session
        return get_active_session()
    except Exception:
        return None


def _running_in_snowflake():
    """True if queries should run as real Snowflake compute -- either because
    this process is deployed inside Snowflake, or because it opened its own
    session on purpose (the local "push to Snowflake" path)."""
    return _active_session() is not None


_TEXT = {"dtype": str, "keep_default_na": False, "na_values": [""]}


def _read_source_file(path, text=False):
    """text=True (DM6, E03): read every cell as written -- only an empty cell is NULL; types come from the workbook
    afterwards (_normalize_columns types=...). Without it, pandas inference as before."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xls", ".xlsx"):
        # Excel cells are typed already; only stop 'NA' / 'null' text becoming NULL
        return pd.read_excel(path, **({"keep_default_na": False, "na_values": [""]} if text else {}))
    try:
        return pd.read_csv(path, encoding="utf-8-sig", **(_TEXT if text else {}))
    except UnicodeDecodeError:
        return pd.read_csv(path, encoding="latin-1", **(_TEXT if text else {}))


def _declared(s, kind):
    """One column -> the workbook's declared type (DM6). string: verbatim text (a whole-number float from an Excel
    numeric cell is written without '.0'); numbers: Tableau's reading of thousands separators; dates parsed."""
    if kind == "string":
        whole = str(s.dtype).startswith("float") and (s.dropna() % 1 == 0).all()
        return pd.Series([None if (v is None or (isinstance(v, float) and v != v)) else
                          (str(int(v)) if whole else str(v)) for v in s], index=s.index, dtype=object)
    if kind in ("integer", "real"):
        if pd.api.types.is_numeric_dtype(s):
            return s
        txt = s.astype(object).where(s.notna(), None)
        return pd.to_numeric(pd.Series([None if v is None else str(v).replace(",", "").strip() or None
                                        for v in txt], index=s.index), errors="coerce")
    if kind in ("date", "datetime"):
        return _parse_dates(s)
    if kind == "boolean":
        return s.map(lambda v: None if v is None or (isinstance(v, float) and v != v)
                     else str(v).strip().lower() in ("true", "1", "yes", "t"))
    return None


def _parse_dates(s):
    """Parse dates robustly: auto-detect ISO (YYYY-MM-DD) vs day-first (DD-MM-YYYY)
    by trying both and keeping whichever yields fewer unparsed values."""
    a = pd.to_datetime(s, errors="coerce")
    b = pd.to_datetime(s, errors="coerce", dayfirst=True)
    return a if a.isna().sum() <= b.isna().sum() else b


def _normalize_columns(df, types=None):
    """Normalize EVERY source column to UPPER_SNAKE (to_phys), so any Tableau
    field is queryable. Parse date columns, coerce numeric-looking text
    (including thousands-separated exports like "74,814").
    types: {UPPER_SNAKE column: Tableau datatype} -- a DECLARED column takes the workbook's type (DM6: a string
    code '001' stays '001'); undeclared columns keep the inference below."""
    from calc_translator import to_phys
    normalized = [to_phys(c) for c in df.columns]
    collisions = sorted({c for c in normalized if normalized.count(c) > 1})
    if collisions:
        raise ValueError("Ambiguous physical column normalization: " + ", ".join(collisions)
                         + ". Explicit column bindings are required; no columns were dropped.")
    df = df.rename(columns={c: to_phys(c) for c in df.columns}).copy()
    for c in df.columns:
        declared = _declared(df[c], (types or {}).get(c)) if (types or {}).get(c) else None
        if declared is not None:
            df[c] = declared
        elif types and (pd.api.types.is_object_dtype(df[c]) or pd.api.types.is_string_dtype(df[c])) and \
                df[c].map(lambda v: isinstance(v, str)).all() and "DATE" not in c:
            # read as text (DM6) but not declared: the previous inference for this column (pandas would have
            # read whole numbers as integers)
            cleaned = df[c].astype(str).str.replace(",", "", regex=False).str.strip()
            conv = pd.to_numeric(cleaned, errors="coerce")
            if conv.notna().all() and len(conv):
                df[c] = conv
            elif _looks_like_dates(cleaned):
                df[c] = _parse_dates(df[c])
        elif "DATE" in c:
            df[c] = _parse_dates(df[c])
        elif pd.api.types.is_object_dtype(df[c]) or pd.api.types.is_string_dtype(df[c]):
            # pandas 3 reads text as the dedicated string dtype, not object: both are text here (E03)
            cleaned = df[c].astype(str).str.replace(",", "", regex=False).str.strip()
            conv = pd.to_numeric(cleaned, errors="coerce")
            if conv.notna().mean() > 0.95:   # mostly numeric -> treat as number
                df[c] = conv
            elif _looks_like_dates(cleaned):  # date content under a non-DATE name
                df[c] = _parse_dates(df[c])   # (e.g. World Indicators "Year")
    return df


_DATE_SHAPE = re.compile(
    r"^\d{4}-\d{1,2}-\d{1,2}([ T]\d{2}:\d{2}(:\d{2})?)?$"   # ISO
    r"|^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$")                     # d/m/y or m/d/y

def _looks_like_dates(s):
    """True when a text column's non-null values are (almost all) date-shaped.
    Content check, not name check — Tableau extracts often carry date columns
    under captions like 'Year'. Shape-gated so numeric/ID strings never get
    fed to the date parser."""
    s = s.dropna()                         # pandas 3 keeps a missing value missing after astype(str)
    vals = s[(s != "") & (~s.str.lower().isin(("nan", "<na>", "none")))]   # pandas 2: it becomes the text 'nan'
    if len(vals) == 0:
        return False
    sample = vals.head(500)
    return sample.str.match(_DATE_SHAPE).mean() > 0.95


_DATEADD_RE = re.compile(r"\bDATEADD\s*\(", re.I)


def _rewrite_dateadd_for_duckdb(sql):
    """Snowflake DATEADD(part,n,date) -> DuckDB interval arithmetic.

    The generated SQL is Snowflake-native. Local verification previously
    claimed the same dialect worked unchanged, but BDO's previous-period KPI
    calculations proved DATEADD was the exception: every delta failed locally
    and the composed tile silently lost its value. Balanced parsing keeps
    nested date expressions intact and leaves an unparseable call untouched.
    """
    out, pos = sql, 0
    while True:
        match = _DATEADD_RE.search(out, pos)
        if not match:
            return out
        op = out.index("(", match.start())
        depth, quote, close = 1, None, None
        i = op + 1
        while i < len(out):
            ch = out[i]
            if quote:
                if ch == quote and (i == 0 or out[i - 1] != "\\"):
                    quote = None
            elif ch in ("'", '"'):
                quote = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    close = i
                    break
            i += 1
        if close is None:
            return out
        body = out[op + 1:close]
        args, start, depth, quote = [], 0, 0, None
        for j, ch in enumerate(body):
            if quote:
                if ch == quote and (j == 0 or body[j - 1] != "\\"):
                    quote = None
            elif ch in ("'", '"'):
                quote = ch
            elif ch == "(": depth += 1
            elif ch == ")": depth -= 1
            elif ch == "," and depth == 0:
                args.append(body[start:j].strip()); start = j + 1
        args.append(body[start:].strip())
        if len(args) != 3:
            pos = close + 1
            continue
        part = args[0].strip().strip("'\"")
        if not re.match(r"^[A-Za-z]+$", part):
            pos = close + 1
            continue
        # Rewrite nested calls before replacing the outer call; advancing past
        # the replacement would otherwise leave DATEADD inside its date arg.
        date_arg = _rewrite_dateadd_for_duckdb(args[2])
        amount_arg = _rewrite_dateadd_for_duckdb(args[1])
        repl = (f"(CAST({date_arg} AS TIMESTAMP) + ({amount_arg}) "
                f"* INTERVAL '1 {part.lower()}')")
        out = out[:match.start()] + repl + out[close + 1:]
        pos = match.start() + len(repl)


# C14: the translator writes Snowflake SQL; the local DuckDB preview supplies the few Snowflake functions DuckDB lacks
# with the same meaning, so local results stand for Snowflake's.
_DUCKDB_COMPAT = [
    ("CREATE OR REPLACE MACRO initcap(s) AS array_to_string(list_transform(string_split(lower(s), ' '), "
     "lambda w: upper(w[1:1]) || w[2:]), ' ')",
     "CREATE OR REPLACE MACRO initcap(s) AS array_to_string(list_transform(string_split(lower(s), ' '), "
     "w -> upper(w[1:1]) || w[2:]), ' ')"),        # (new lambda syntax, then the older DuckDB arrow syntax)
    "CREATE OR REPLACE MACRO regexp_instr(s, p) AS CASE WHEN regexp_matches(s, p) THEN 1 ELSE 0 END",
    "CREATE OR REPLACE MACRO tableau_regexp_replace(s, p, r) AS regexp_replace(s, p, r, 'g')",
    "CREATE OR REPLACE MACRO date_from_parts(y, m, d) AS make_date(CAST(y AS BIGINT), CAST(m AS BIGINT), CAST(d AS BIGINT))",
    "CREATE OR REPLACE MACRO timestamp_from_parts(d, t) AS CAST(CAST(d AS DATE) + CAST(t AS TIME) AS TIMESTAMP)",
    "CREATE OR REPLACE MACRO time_from_parts(h, m, s) AS make_time(CAST(h AS BIGINT), CAST(m AS BIGINT), CAST(s AS DOUBLE))",
    "CREATE OR REPLACE MACRO weekiso(d) AS week(d)",
    "CREATE OR REPLACE MACRO yearofweekiso(d) AS isoyear(d)",
    "CREATE OR REPLACE MACRO dayofweekiso(d) AS isodow(d)",
    "CREATE OR REPLACE MACRO equal_null(a, b) AS (a IS NOT DISTINCT FROM b)",
]


def duckdb_compat(con):
    """Install the Snowflake-function shims on a DuckDB connection (idempotent)."""
    for stmt in _DUCKDB_COMPAT:
        for variant in (stmt if isinstance(stmt, tuple) else (stmt,)):
            try:
                con.execute(variant)
                break
            except Exception:       # a DuckDB version without this form: the next one, else the shim is absent
                continue            # (only the preview of that one function is affected, never the connection)
    return con


_SF_TO_STRPTIME = [('YYYY', '%Y'), ('HH24', '%H'), ('HH12', '%I'), ('MMMM', '%B'), ('MON', '%b'), ('MM', '%m'),
                   ('DD', '%d'), ('MI', '%M'), ('SS', '%S'), ('YY', '%y'), ('AM', '%p')]


def _strptime_format(fmt):
    out, i = [], 0
    while i < len(fmt):
        for a, b in _SF_TO_STRPTIME:
            if fmt.startswith(a, i):
                out.append(b)
                i += len(a)
                break
        else:
            out.append(fmt[i])
            i += 1
    return ''.join(out)


def _rewrite_try_to_timestamp(sql):
    """TRY_TO_TIMESTAMP(x, 'Snowflake format') -> DuckDB try_strptime with the same format (a literal there)."""
    out, pos = sql, 0
    rx = re.compile(r"\bTRY_TO_TIMESTAMP\s*\(", re.I)
    while True:
        m = rx.search(out, pos)
        if not m:
            return out
        depth, quote, close = 0, None, None
        for k in range(m.end() - 1, len(out)):
            ch = out[k]
            if quote:
                quote = None if ch == quote else quote
            elif ch == "'":
                quote = ch
            elif ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    close = k
                    break
        if close is None:
            return out
        body = out[m.end():close]
        fm = re.search(r",\s*'((?:[^']|'')*)'\s*$", body)
        if not fm:
            pos = close
            continue
        repl = (f"try_strptime(CAST({body[:fm.start()]} AS VARCHAR), "
                f"'{_strptime_format(fm.group(1))}')")
        out = out[:m.start()] + repl + out[close + 1:]
        pos = m.start() + len(repl)


def local_sql(sql):
    """Snowflake SQL -> what the local DuckDB preview runs (DATEADD, REGEXP_REPLACE replaces every match,
    TRY_TO_TIMESTAMP with a Snowflake format)."""
    sql = re.sub(r"\bREGEXP_REPLACE\s*\(", "tableau_regexp_replace(", sql, flags=re.I)
    sql = _rewrite_try_to_timestamp(sql)
    sql = _rewrite_percentile_window(sql)
    # REGEXP_SUBSTR(s, p, 1, 1, 'e', n) (Snowflake: group n) -> DuckDB regexp_extract (its group must be a literal)
    sql = re.sub(r"\bREGEXP_SUBSTR\s*\(((?:[^()']|'(?:[^']|'')*'|\([^()]*\))*?),\s*1,\s*1,\s*'e',\s*(\d+)\s*\)",
                 lambda m: f"nullif(regexp_extract({m.group(1)}, {m.group(2)}), '')", sql, flags=re.I)
    return _rewrite_dateadd_for_duckdb(sql)


def _rewrite_percentile_window(sql):
    """PERCENTILE_CONT(p) WITHIN GROUP (ORDER BY e) OVER (..) -> quantile_cont(e, p) OVER (..): DuckDB has the
    ordered-set form only as an aggregate."""
    rx = re.compile(r"\bPERCENTILE_CONT\s*\(([^()]*)\)\s*WITHIN\s+GROUP\s*\(\s*ORDER\s+BY\s+", re.I)
    out, pos = sql, 0
    while True:
        m = rx.search(out, pos)
        if not m:
            return out
        depth, close = 1, None
        for k in range(m.end(), len(out)):
            if out[k] == '(':
                depth += 1
            elif out[k] == ')':
                depth -= 1
                if depth == 0:
                    close = k
                    break
        if close is None or not re.match(r"\s*OVER\s*\(", out[close + 1:], re.I):
            pos = m.end()
            continue
        repl = f"quantile_cont({out[m.end():close]}, {m.group(1)})"
        out = out[:m.start()] + repl + out[close + 1:]
        pos = m.start() + len(repl)


def _get_local_con():
    """Build (once) a DuckDB connection with EVERY datasource registered."""
    global _LOCAL_CON
    if _LOCAL_CON is not None:
        return _LOCAL_CON
    import duckdb

    con = duckdb.connect(database=":memory:")
    duckdb_compat(con)
    missing = []
    for caption, spec in config.DATASOURCES.items():
        path = spec.get("local_file")
        if not path or not os.path.exists(path):
            missing.append((caption, path))
            continue
        types = spec.get("types")                    # DM6: the workbook's declared column types, when known
        df = _normalize_columns(_read_source_file(path, text=bool(types)), types=types)
        view = "src_" + _local_name(caption).lower()
        con.register(view, df)
        con.execute('CREATE OR REPLACE TABLE %s AS SELECT * FROM %s'
                    % (_local_name(caption), view))
    # legacy alias for the default datasource
    dflt = config.DEFAULT_DATASOURCE
    if not any(c == dflt for c, _ in missing):
        con.execute("CREATE OR REPLACE VIEW %s AS SELECT * FROM %s"
                    % (LOCAL_TABLE, _local_name(dflt)))
    else:
        raise FileNotFoundError(
            "Local data file not found for default datasource '%s': %s\n"
            "Export it to that path (CSV or Excel), or edit DATASOURCES in config.py."
            % (dflt, config.DATASOURCES[dflt].get("local_file")))
    _LOCAL_CON = con
    return con


def result_frame(df):
    """Query results with pandas-2 semantics on any pandas: a missing value in a text column is None (pandas 3
    returns NaN / pd.NA in its string dtype, which the engine's None checks would miss)."""
    for c in df.columns:
        if pd.api.types.is_string_dtype(df[c]) and not pd.api.types.is_object_dtype(df[c]):
            df[c] = df[c].astype(object).where(df[c].notna(), None)
    return df


def run_sql(sql):
    """Execute SQL against Snowflake (if deployed OR a session was pushed in
    via set_session) or local DuckDB (pure local testing)."""
    sess = _active_session()
    if sess is not None:
        if any(s.get('model_plan') for s in config.DATASOURCES.values()):
            from v3_model_binding import replace_bindings
            sql = replace_bindings(sql, config.DATASOURCES)
        return result_frame(sess.sql(sql).to_pandas())
    local_sql = sql
    # whole identifiers only, longest first: DB.S.SAMPLE_SUPERSTORE must not rewrite the start of
    # DB.S.SAMPLE_SUPERSTORE_RETURNS (DM4: a related table's name extends its datasource's)
    for caption, spec in sorted(config.DATASOURCES.items(), key=lambda kv: -len(kv[1]["table"])):
        local_sql = re.sub(r"(?<![A-Za-z0-9_$.])" + re.escape(spec["table"]) + r"(?![A-Za-z0-9_$])",
                           _local_name(caption), local_sql)
    return result_frame(_get_local_con().execute(globals()['local_sql'](local_sql)).fetchdf())
