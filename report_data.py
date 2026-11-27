"""
report_data.py  --  where report-ir apps get their rows.

* In Streamlit in Snowflake (or with a Snowpark session registered through
  set_session): the SQL runs in Snowflake as written. Nothing is rewritten.
* Locally: an in-memory DuckDB catalog is built so the SAME fully-qualified
  names ("WBR_DB"."PUBLIC"."SAMPLE_SUPERSTORE") resolve to local files. Used by
  tests and local preview only.

Results go through query_cache (viewer-scoped; see that module).
"""
import re

import pandas as pd

import query_cache

_SESSION = None
_LOCAL = None
_LOCAL_SOURCES = {}


def set_session(session):
    global _SESSION
    _SESSION = session


def _active_session():
    if _SESSION is not None:
        return _SESSION
    try:
        from snowflake.snowpark.context import get_active_session
        return get_active_session()
    except Exception:
        return None


def in_snowflake():
    return _active_session() is not None


def configure_local(sources):
    """sources: {"DB.SCHEMA.OBJECT": "path/to/file.csv|.xlsx"}.

    Columns are normalized to UPPER_SNAKE exactly like backend.py does, which
    is how the Snowflake loads of these files were named."""
    global _LOCAL, _LOCAL_SOURCES
    _LOCAL_SOURCES = dict(sources)
    _LOCAL = None
    query_cache.clear()


def _local():
    global _LOCAL
    if _LOCAL is not None:
        return _LOCAL
    import duckdb
    import backend
    con = duckdb.connect()
    made = set()
    for fq, path in _LOCAL_SOURCES.items():
        db, sch, obj = fq.split(".")
        if db not in made:
            con.execute(f'ATTACH \':memory:\' AS "{db}"')
            made.add(db)
        con.execute(f'CREATE SCHEMA IF NOT EXISTS "{db}"."{sch}"')
        df = backend._normalize_columns(backend._read_source_file(path))
        con.register("_src", df)
        con.execute(f'CREATE OR REPLACE TABLE "{db}"."{sch}"."{obj}" AS SELECT * FROM _src')
        con.unregister("_src")
    _LOCAL = con
    return con


def _numeric(df):
    for c in df.columns:
        if df[c].dtype == object:
            sample = df[c].dropna().head(20)
            if len(sample) and all(type(x).__name__ == "Decimal" for x in sample):
                df[c] = df[c].astype(float)
    return df


def _run(sql):
    s = _active_session()
    if s is not None:
        return _numeric(s.sql(sql).to_pandas())
    return _numeric(_local().execute(sql).df())


def run_sql(sql):
    return query_cache.cached_query(sql, _run, runner_id="report",
                                    in_snowflake=in_snowflake())
