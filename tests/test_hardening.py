"""
tests/test_hardening.py -- Phase 0 security/deploy hardening.

1. The query cache must never hand one viewer's rows to another viewer.
2. Deploys must pin the Streamlit runtime instead of inheriting the account
   default.

Run: pytest tests/test_hardening.py
"""
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import query_cache  # noqa: E402


def _counting_runner(calls):
    def run(sql):
        calls.append(sql)
        return pd.DataFrame({"V": [len(calls)]})
    return run


def test_local_scope_shares_results():
    query_cache.clear()
    calls = []
    r = _counting_runner(calls)
    a = query_cache.cached_query("SELECT 1", r, runner_id="t", in_snowflake=False)
    b = query_cache.cached_query("SELECT 1", r, runner_id="t", in_snowflake=False)
    assert len(calls) == 1 and a.equals(b)


def test_different_viewers_never_share(monkeypatch):
    query_cache.clear()
    calls = []
    r = _counting_runner(calls)
    monkeypatch.setattr(query_cache, "_streamlit_viewer", lambda: "ALICE")
    a = query_cache.cached_query("SELECT * FROM T", r, runner_id="t", in_snowflake=True)
    monkeypatch.setattr(query_cache, "_streamlit_viewer", lambda: "BOB")
    b = query_cache.cached_query("SELECT * FROM T", r, runner_id="t", in_snowflake=True)
    assert len(calls) == 2, "BOB must not be served ALICE's cached result"
    assert a.V[0] == 1 and b.V[0] == 2
    # same viewer again -> cache hit
    monkeypatch.setattr(query_cache, "_streamlit_viewer", lambda: "ALICE")
    c = query_cache.cached_query("SELECT * FROM T", r, runner_id="t", in_snowflake=True)
    assert len(calls) == 2 and c.V[0] == 1


def test_unknown_viewer_in_snowflake_is_not_shared(monkeypatch):
    query_cache.clear()
    monkeypatch.setattr(query_cache, "_streamlit_viewer", lambda: None)
    assert query_cache.viewer_scope(True) is None
    calls = []
    r = _counting_runner(calls)
    query_cache.cached_query("SELECT 2", r, runner_id="t", in_snowflake=True)
    # a NEW browser session (fresh session_state) must not see that result:
    # only the per-session cache was used, never the process-wide one
    sc = query_cache._session_cache()
    if sc is not None:
        sc.clear()
    query_cache.cached_query("SELECT 2", r, runner_id="t", in_snowflake=True)
    assert len(calls) == 2


def test_engine_uses_viewer_scoped_cache():
    import engine
    src = open(os.path.join(ROOT, "engine.py"), encoding="utf-8").read()
    assert "query_cache.cached_query" in src
    assert callable(getattr(engine._q_exec, "clear", None))


def test_deploy_pins_warehouse_runtime():
    import pipeline
    assert pipeline._resolve_runtime_name() == "SYSTEM$WAREHOUSE_RUNTIME"
    ddl = pipeline._pin_runtime_ddl("DB", "SC", "APP", "SYSTEM$WAREHOUSE_RUNTIME")
    assert ddl == ('ALTER STREAMLIT "DB"."SC"."APP" '
                   "SET RUNTIME_NAME = 'SYSTEM$WAREHOUSE_RUNTIME'")
    try:
        pipeline._resolve_runtime_name("SYSTEM$ST_CONTAINER_RUNTIME_PY3_11")
        assert False, "container runtime must be refused on the ROOT_LOCATION path"
    except RuntimeError:
        pass


def test_deploy_orchestration_runs_runtime_pin(tmp_path):
    import pipeline
    from test_regression import _StubSession
    app = tmp_path / "app_stub.py"
    app.write_text("from engine import run\nrun({})\n")
    stub = _StubSession()
    res = pipeline.deploy_streamlit_app(
        stub, "stub", str(app), root_dir=ROOT,
        datasources={"DS": {"table": "WBR_DB.X.DS"}})
    pins = [q for q in stub.sql_log if "SET RUNTIME_NAME" in q]
    assert len(pins) == 1 and res["runtime_name"] == "SYSTEM$WAREHOUSE_RUNTIME"
    # the pin runs AFTER the create
    i_create = next(i for i, q in enumerate(stub.sql_log)
                    if q.strip().upper().startswith("CREATE OR REPLACE STREAMLIT"))
    assert stub.sql_log.index(pins[0]) > i_create
