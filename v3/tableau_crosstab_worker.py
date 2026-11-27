"""tableau_crosstab_worker.py -- deploys and runs the crosstab-fetch worker
notebook (R18). Mirrors task_capture.py's deploy_worker shape closely (same
CREATE NOTEBOOK -> ADD LIVE VERSION -> ALTER ... EAI -> CREATE TASK
sequence, same reasons for each step -- see that file's own comments for
why EAI must be attached via ALTER, not inline on CREATE), but for its own
isolated table/task/notebook (tableau_crosstab_jobs.py) so this newer, less
-hardened path can never put R15's already-proven screenshot capture at
risk.

PROVEN LIVE 2026-08-21 (see ARCHITECTURE.md S28): the underlying fetch
(tableau_zone_crosstab.fetch_sheet_crosstab_csv) already ran successfully
inside a standalone Snowflake notebook via this exact
pip-install/EAI/egress recipe -- this module wires that proven recipe into
the real, queue-driven worker rather than a one-off test harness.
"""

from __future__ import annotations

import json
import os
import tempfile

import tableau_crosstab_jobs as XJ

NOTEBOOK_SCHEMA = "WBR_DB.PIPELINE_DEMO"
WORKER_NOTEBOOK = f"{NOTEBOOK_SCHEMA}.CROSSTAB_WORKER"
WORKER_TASK = f"{NOTEBOOK_SCHEMA}.CROSSTAB_WORKER_TASK"
COMPUTE_POOL = "SYSTEM_COMPUTE_POOL_CPU"
WAREHOUSE = "POWERHOUSE"
EAI = "PLAYWRIGHT_EAI"

# The exact domains proven necessary live 2026-08-21, in the order they were
# found: base Tableau Cloud connectivity, Tableau's own vizql/vizportal
# asset CDN (without which the toolbar/dialog never render), Tableau's own
# telemetry beacon. Kept here as documentation of what this worker needs;
# the actual network rule is a Snowflake object (WBR_DB.DBX.
# PLAYWRIGHT_EGRESS_RULE) already extended to include them -- this list is
# not applied automatically, it is the record of what was required.
REQUIRED_EGRESS_DOMAINS = (
    "prod-useast-b.online.tableau.com:443",
    "d6lttq8hqy2mc.cloudfront.net:443",
    "prod.telemetry.tableausoftware.com:443",
)


_WORKER_SOURCE = '''
import sys, os, json, subprocess, traceback, time
from snowflake.snowpark.context import get_active_session
session = get_active_session()

sys.path.insert(0, "/tmp/xtab_src")
os.makedirs("/tmp/xtab_src", exist_ok=True)
session.file.get("__CODE_STAGE__/", "/tmp/xtab_src")

import tableau_crosstab_jobs as XJ

WORKER = "nb-" + str(os.getpid()) + "-" + str(int(time.time()))

def beat(run_id, **kw):
    try:
        XJ.heartbeat(session, run_id, WORKER, **kw)
    except Exception:
        pass

XJ.ensure_table(session)
recovered = XJ.recover_stale(session)

# Process EVERY currently-queued job in this one invocation, not just one --
# the scheduling task only fires once a minute, and a workbook can surface
# several unresolved sheets at once (Commission Model alone has 4). Waiting
# one minute PER SHEET would make a single "Run Validation" click take
# 10+ minutes for a workbook with that many gaps. A bounded time budget
# (not an unbounded loop) keeps one runaway invocation from blocking the
# next scheduled tick indefinitely.
DEADLINE = time.time() + 8 * 60
processed = []

env = dict(os.environ)
env["PLAYWRIGHT_BROWSERS_PATH"] = "/tmp/pw"
first_run_id = XJ.claim_next(session, WORKER)
if first_run_id is None:
    print(json.dumps({"result": "no queued job", "worker": WORKER,
                      "recovered_stale": recovered}))
else:
    # Install the browser runtime ONCE per invocation, reused across every
    # job this invocation processes -- not once per job.
    beat(first_run_id, status=XJ.FETCHING, label="Installing browser runtime")
    subprocess.run([sys.executable, "-m", "pip", "install",
                   "playwright==1.47.0", "openpyxl", "requests"], check=True)
    subprocess.run([sys.executable, "-m", "playwright", "install-deps",
                    "chromium"], check=True)
    subprocess.run([sys.executable, "-m", "playwright", "install",
                    "chromium"], check=True, env=env)
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/tmp/pw"
    import tableau_zone_crosstab as TZC
    import tableau_server as TS

    run_id = first_run_id
    while run_id is not None and time.time() < DEADLINE:
        try:
            beat(run_id, label="Fetching job input")
            work = "/tmp/" + run_id
            os.makedirs(work, exist_ok=True)
            session.file.get(XJ.input_path(run_id).rsplit("/", 1)[0] + "/", work)
            with open(os.path.join(work, "input.json"), encoding="utf-8") as f:
                cfg = json.load(f)

            # A fresh, independent, disposable sign-in -- NOT a borrowed
            # session token from whoever enqueued this job. A session token
            # is server-tracked and invalidated by ANY sign_out() anywhere
            # using it; by the time this job is claimed the enqueuing
            # caller may already have signed out of its own, unrelated
            # session. Found live 2026-08-21: a batch of 3 jobs all failed
            # with a browser-level navigation error because exactly that
            # had happened.
            rest_client = TS.TableauRestClient(cfg["server_url"])
            rest_client.sign_in_with_pat(cfg["token_name"], cfg["token_secret"],
                                         cfg["site_content_url"])
            try:
                beat(run_id, label="Fetching crosstab from Tableau")
                csv_text = TZC.fetch_sheet_crosstab_csv(
                    server_url=cfg["server_url"],
                    site_content_url=cfg["site_content_url"],
                    token=rest_client.auth_token,
                    dashboard_content_url=cfg["dashboard_content_url"],
                    sheet_name=cfg["sheet_name"],
                    headless=True,
                    timeout_s=60,
                )
            finally:
                rest_client.sign_out()

            result = {"success": True, "csv_text": csv_text}
            out_path = os.path.join(work, "result.json")
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(result, f)
            session.file.put(out_path, XJ.output_path(run_id).rsplit("/", 1)[0],
                             auto_compress=False, overwrite=True)
            XJ.complete(session, run_id, XJ.output_path(run_id))
            processed.append({"run_id": run_id, "result": "completed"})
        except Exception as exc:
            result = {"success": False, "error_type": type(exc).__name__,
                      "error": str(exc), "traceback": traceback.format_exc()[-3000:]}
            try:
                work = "/tmp/" + run_id
                os.makedirs(work, exist_ok=True)
                out_path = os.path.join(work, "result.json")
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(result, f)
                session.file.put(out_path, XJ.output_path(run_id).rsplit("/", 1)[0],
                                 auto_compress=False, overwrite=True)
            except Exception:
                pass
            XJ.requeue_or_fail(session, run_id, type(exc).__name__, str(exc))
            processed.append({"run_id": run_id, "result": "failed",
                              "error": str(exc)})
        run_id = XJ.claim_next(session, WORKER)
    print(json.dumps({"result": "batch done", "worker": WORKER,
                      "processed": processed, "recovered_stale": recovered}))
'''


def _ipynb(source: str) -> str:
    return json.dumps({
        "cells": [{"cell_type": "code", "execution_count": None, "id": "worker",
                   "metadata": {"language": "python"}, "outputs": [],
                   "source": [line + "\n" for line in source.strip().split("\n")]}],
        "metadata": {"kernelspec": {"display_name": "Streamlit Notebook",
                                    "name": "streamlit"},
                     "language_info": {"name": "python"}},
        "nbformat": 4, "nbformat_minor": 5}, indent=1)


def deploy_worker(session, log=print):
    """(Re)create the crosstab worker notebook and its task. Idempotent --
    safe to re-run after editing the worker body. Stages BOTH this
    project's tableau_zone_crosstab.py/tableau_crosstab_jobs.py modules
    (the worker imports them) and the notebook source itself."""
    code_stage = f"{XJ.STAGE}/{XJ.STAGE_SUBDIR}/_code"
    tmp = tempfile.mkdtemp(prefix="xtabworker")
    root = os.path.dirname(os.path.abspath(__file__))
    for mod in ("tableau_zone_crosstab.py", "tableau_crosstab_jobs.py",
                "tableau_server.py"):
        session.file.put(os.path.join(root, mod).replace("\\", "/"), code_stage,
                         auto_compress=False, overwrite=True)
    log(f"staged worker modules -> {code_stage}")

    source = _WORKER_SOURCE.replace("__CODE_STAGE__", code_stage)
    nb_path = os.path.join(tmp, "crosstab_worker.ipynb")
    with open(nb_path, "w", encoding="utf-8") as fh:
        fh.write(_ipynb(source))
    nb_stage = f"{code_stage}/_nb"
    session.file.put(nb_path.replace("\\", "/"), nb_stage,
                     auto_compress=False, overwrite=True)
    log(f"staged worker notebook -> {nb_stage}")

    session.sql(f"""
        CREATE OR REPLACE NOTEBOOK {WORKER_NOTEBOOK}
          FROM '{nb_stage}'
          MAIN_FILE = 'crosstab_worker.ipynb'
          QUERY_WAREHOUSE = {WAREHOUSE}
          RUNTIME_NAME = 'SYSTEM$BASIC_RUNTIME'
          COMPUTE_POOL = '{COMPUTE_POOL}'
    """).collect()
    session.sql(f"ALTER NOTEBOOK {WORKER_NOTEBOOK} ADD LIVE VERSION FROM LAST").collect()
    # Same two-step reason as task_capture.py's deploy_worker: CREATE
    # NOTEBOOK does not accept EXTERNAL_ACCESS_INTEGRATIONS inline.
    try:
        session.sql(f"ALTER NOTEBOOK {WORKER_NOTEBOOK} SET "
                    f"EXTERNAL_ACCESS_INTEGRATIONS = ({EAI})").collect()
    except Exception as exc:
        raise RuntimeError(
            f"could not attach {EAI} to {WORKER_NOTEBOOK}: "
            f"{type(exc).__name__}: {exc}") from exc
    log(f"created notebook {WORKER_NOTEBOOK}")

    session.sql(f"""
        CREATE OR REPLACE TASK {WORKER_TASK}
          WAREHOUSE = {WAREHOUSE}
          SCHEDULE = '1 MINUTE'
        AS
          EXECUTE NOTEBOOK {WORKER_NOTEBOOK}()
    """).collect()
    log(f"created task {WORKER_TASK} (suspended until resumed)")
    return WORKER_NOTEBOOK, WORKER_TASK


def resume_worker(session):
    session.sql(f"ALTER TASK {WORKER_TASK} RESUME").collect()


def suspend_worker(session):
    session.sql(f"ALTER TASK {WORKER_TASK} SUSPEND").collect()


__all__ = ["WORKER_NOTEBOOK", "WORKER_TASK", "REQUIRED_EGRESS_DOMAINS",
           "deploy_worker", "resume_worker", "suspend_worker"]
