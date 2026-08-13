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


def _read_source_file(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xls", ".xlsx"):
        return pd.read_excel(path)          # needs xlrd (.xls) or openpyxl (.xlsx)
    try:
        return pd.read_csv(path, encoding="utf-8-sig")
    except UnicodeDecodeError:
        return pd.read_csv(path, encoding="latin-1")


def _parse_dates(s):
    """Parse dates robustly: auto-detect ISO (YYYY-MM-DD) vs day-first (DD-MM-YYYY)
    by trying both and keeping whichever yields fewer unparsed values."""
    a = pd.to_datetime(s, errors="coerce")
    b = pd.to_datetime(s, errors="coerce", dayfirst=True)
    return a if a.isna().sum() <= b.isna().sum() else b


def _normalize_columns(df):
    """Normalize EVERY source column to UPPER_SNAKE (to_phys), so any Tableau
    field is queryable. Parse date columns, coerce numeric-looking text
    (including thousands-separated exports like "74,814")."""
    from calc_translator import to_phys
    df = df.rename(columns={c: to_phys(c) for c in df.columns})
    df = df.loc[:, ~df.columns.duplicated()].copy()
    for c in df.columns:
        if "DATE" in c:
            df[c] = _parse_dates(df[c])
        elif df[c].dtype == object:
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
    vals = s[(s != "") & (s.str.lower() != "nan")]
    if len(vals) == 0:
        return False
    sample = vals.head(500)
    return sample.str.match(_DATE_SHAPE).mean() > 0.95


def _get_local_con():
    """Build (once) a DuckDB connection with EVERY datasource registered."""
    global _LOCAL_CON
    if _LOCAL_CON is not None:
        return _LOCAL_CON
    import duckdb

    con = duckdb.connect(database=":memory:")
    missing = []
    for caption, spec in config.DATASOURCES.items():
        path = spec.get("local_file")
        if not path or not os.path.exists(path):
            missing.append((caption, path))
            continue
        df = _normalize_columns(_read_source_file(path))
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


def run_sql(sql):
    """Execute SQL against Snowflake (if deployed OR a session was pushed in
    via set_session) or local DuckDB (pure local testing)."""
    sess = _active_session()
    if sess is not None:
        return sess.sql(sql).to_pandas()
    local_sql = sql
    for caption, spec in config.DATASOURCES.items():
        local_sql = local_sql.replace(spec["table"], _local_name(caption))
    return _get_local_con().execute(local_sql).fetchdf()
