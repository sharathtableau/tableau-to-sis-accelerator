"""
query_cache.py  --  viewer-scoped query result cache for generated apps.

WHY THIS EXISTS (security fix, 2026-11-27)
------------------------------------------
engine._q_exec used to be `@st.cache_data(ttl=600) def _q_exec(sql)`, i.e. the
cache key was the SQL text ALONE. st.cache_data is process-wide: every browser
session served by the same Streamlit process shares it. On Snowflake's container
runtime (the default for newly created Streamlit objects) one process serves
many viewers, so viewer B could be handed the rows that were fetched for viewer
A. Once a table has a row access policy (the Snowflake equivalent of Power BI
row-level security) that is a data leak.

THE RULE
--------
* Locally (no Snowpark session, DuckDB over files) there is one user and no
  row-level security, so results are shared under the constant scope "local".
* In Snowflake, results are shared ONLY between sessions of the SAME viewer:
  the cache key is (viewer identity, runner, sql).
* In Snowflake with NO resolvable viewer identity, nothing is shared across
  sessions: results are cached per browser session in st.session_state only.
  Fail closed, never open.

This module never decides WHAT a viewer may see -- Snowflake does that. It only
guarantees the cache cannot hand one viewer's result to another viewer.
"""

import streamlit as st

TTL_SECONDS = 600
_SESSION_KEY = "__viewer_scoped_query_cache__"
_MAX_SESSION_ENTRIES = 256


def _streamlit_viewer():
    """The viewer's identity as Streamlit reports it, or None.

    Streamlit in Snowflake exposes the viewer through `st.user` (older
    releases: `st.experimental_user`) with user_name / login_name / email.
    Every access is guarded: outside a running app these raise or are empty.
    """
    for obj_name in ("user", "experimental_user"):
        try:
            obj = getattr(st, obj_name)
        except Exception:
            continue
        for attr in ("user_name", "login_name", "email"):
            try:
                if hasattr(obj, "get"):
                    v = obj.get(attr)
                else:
                    v = getattr(obj, attr, None)
            except Exception:
                v = None
            if v:
                return str(v)
    return None


def viewer_scope(in_snowflake):
    """Cache scope for the current viewer.

    "local" outside Snowflake; "user:<name>" in Snowflake when the viewer is
    known; None in Snowflake when the viewer is unknown (=> do not share)."""
    if not in_snowflake:
        return "local"
    who = _streamlit_viewer()
    return ("user:" + who) if who else None


@st.cache_data(ttl=TTL_SECONDS, show_spinner=False)
def _shared(scope, runner_id, sql, _runner):
    # `scope` and `runner_id` are part of the cache key on purpose; `_runner`
    # (leading underscore) is excluded from hashing by Streamlit.
    return _runner(sql)


def _session_cache():
    try:
        return st.session_state.setdefault(_SESSION_KEY, {})
    except Exception:
        return None            # no Streamlit runtime -> no session cache


def cached_query(sql, runner, *, runner_id, in_snowflake):
    """Run `runner(sql)` through the viewer-scoped cache and return its result."""
    scope = viewer_scope(in_snowflake)
    if scope is not None:
        return _shared(scope, runner_id, sql, runner)
    cache = _session_cache()
    key = (runner_id, sql)
    if cache is not None and key in cache:
        return cache[key].copy()
    res = runner(sql)
    if cache is not None:
        if len(cache) >= _MAX_SESSION_ENTRIES:
            cache.pop(next(iter(cache)))
        cache[key] = res
        return res.copy()
    return res


def clear():
    """Drop every cached result (shared and this session's)."""
    try:
        _shared.clear()
    except Exception:
        pass
    cache = _session_cache()
    if cache is not None:
        cache.clear()
