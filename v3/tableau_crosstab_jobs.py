"""tableau_crosstab_jobs.py -- job queue for R18's browser-automation
crosstab fetch, the same "SiS enqueues, a worker with a real browser
executes" split R15 (validation_jobs.py) already proved necessary and
already proved works, kept as its OWN table/task/worker rather than folded
into R15's -- so iterating on this (still a newer, less-hardened path than
the screenshot capture) can never put the already-working screenshot
capture at risk. See ARCHITECTURE.md S28 for why this exists and how the
underlying fetch (tableau_zone_crosstab.py) was proven, both on a
workstation and inside Snowflake's own compute.

WHY A SEPARATE QUEUE, NOT ONE MORE JOB "KIND" IN VALIDATION_CAPTURE_JOBS:
validation_jobs.py's queue is intentionally generic (RUN_ID/STATUS/
heartbeat/atomic-claim), but its WORKER notebook body
(task_capture.py's _WORKER_CAPTURE) is written specifically around "capture
every dashboard's screenshot for one whole workbook run" -- a materially
different shape from "fetch one specific sheet's crosstab." Sharing the
table would mean either an ALTER TABLE on a live production table plus a
kind-dispatch rewrite of the PROVEN worker body, or two independent bodies
racing to interpret the same row. A second, isolated table/task/worker
costs one more lightweight scheduled task; it does not cost any risk to
R15.

WHY NO BUNDLE/HASH SYSTEM (unlike validation_jobs.Bundle): a screenshot
capture must prove it photographed the EXACT app code that was validated,
because a screenshot of different code is not evidence about a migration.
A crosstab fetch has no such requirement -- it is a live query against
Tableau's own server, freshly re-fetched every time, so there is nothing to
hash against. What DOES need care: credentials must never be persisted in
a queryable table column, AND -- found live 2026-08-21, not assumed --
must not be a borrowed SESSION TOKEN whose validity is tied to the
caller's own sign-in lifetime. A job can sit QUEUED for however long it
takes the scheduled task to next fire (plus however many other jobs are
ahead of it), and a REST session token is server-tracked and invalidated
by ANY sign_out() anywhere using it, including the enqueuing caller's own
sign_out after it finishes an unrelated call. Passing the PAT NAME+SECRET
instead (still staged as a per-run file, never a table column) lets the
WORKER sign in with its OWN independent, disposable session immediately
before use, immune to the caller's lifecycle entirely.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid

QUEUED = "QUEUED"
CLAIMED = "CLAIMED"
FETCHING = "FETCHING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"

ACTIVE_STATUSES = (CLAIMED, FETCHING)
TERMINAL_STATUSES = (COMPLETED, FAILED)

TABLE = "WBR_DB.PIPELINE_DEMO.TABLEAU_CROSSTAB_JOBS"
STAGE = "@WBR_DB.PIPELINE_DEMO.VALIDATION_CAPTURE_STAGE"
STAGE_SUBDIR = "_crosstab"  # keeps this job kind's files out of R15's own run-id folders

STALE_HEARTBEAT_SECONDS = 90  # a single-sheet fetch is a ~30-90s operation, not minutes
HEARTBEAT_SECONDS = 15
MAX_ATTEMPTS = 2  # a deterministic Tableau-side failure will not fix itself on a 3rd try

RETRYABLE_ERROR_CODES = frozenset({"WORKER_LOST", "SNOWFLAKE_UNAVAILABLE"})


def new_run_id() -> str:
    import datetime
    stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    return f"XTAB-{stamp}-{uuid.uuid4().hex[:8]}"


def _run_prefix(run_id: str) -> str:
    return f"{STAGE}/{STAGE_SUBDIR}/{run_id}"


def input_path(run_id: str) -> str:
    return f"{_run_prefix(run_id)}/input.json"


def output_path(run_id: str) -> str:
    return f"{_run_prefix(run_id)}/result.json"


def _sql(session, statement: str, params=None):
    return session.sql(statement, params or []).collect()


def ensure_table(session) -> None:
    """Idempotent -- create the table if this is the first run on a fresh
    account/schema. Never DROPs or ALTERs an existing one."""
    _sql(session, f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            RUN_ID                 VARCHAR NOT NULL,
            STATUS                 VARCHAR NOT NULL,
            CREATED_AT             TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP(),
            STARTED_AT             TIMESTAMP_LTZ,
            COMPLETED_AT           TIMESTAMP_LTZ,
            HEARTBEAT_AT           TIMESTAMP_LTZ,
            ATTEMPT                NUMBER DEFAULT 0,
            WORKER_ID              VARCHAR,
            SERVER_URL             VARCHAR,
            SITE_CONTENT_URL       VARCHAR,
            DASHBOARD_CONTENT_URL  VARCHAR,
            SHEET_NAME             VARCHAR,
            RESULT_PATH            VARCHAR,
            ERROR_CODE             VARCHAR,
            ERROR_MESSAGE          VARCHAR,
            PROGRESS_LABEL         VARCHAR,
            PRIMARY KEY (RUN_ID)
        )
    """)


def enqueue(session, token_name: str, token_secret: str, server_url: str,
            site_content_url: str, dashboard_content_url: str,
            sheet_name: str) -> str:
    """Write the PAT name+secret (NOT a borrowed session token -- see the
    module docstring for why) and everything else the worker needs to a
    per-run stage file, then insert one QUEUED row. Returns the run_id."""
    run_id = new_run_id()
    tmp = tempfile.mkdtemp(prefix="xtabjob_")
    path = os.path.join(tmp, "input.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({
            "token_name": token_name, "token_secret": token_secret,
            "server_url": server_url,
            "site_content_url": site_content_url,
            "dashboard_content_url": dashboard_content_url,
            "sheet_name": sheet_name,
        }, fh)
    session.file.put(path.replace("\\", "/"), f"{_run_prefix(run_id)}",
                     auto_compress=False, overwrite=True)
    _sql(session, f"""
        INSERT INTO {TABLE}
            (RUN_ID, STATUS, SERVER_URL, SITE_CONTENT_URL,
             DASHBOARD_CONTENT_URL, SHEET_NAME, PROGRESS_LABEL)
        VALUES (?, '{QUEUED}', ?, ?, ?, ?, 'Queued for Tableau crosstab fetch')
    """, [run_id, server_url, site_content_url, dashboard_content_url, sheet_name])
    return run_id


def claim_next(session, worker_id: str):
    """Atomically take the oldest QUEUED job -- same UPDATE...WHERE pattern
    validation_jobs.claim_next uses, for the same reason: two workers racing
    for the same row must never both win."""
    rows = _sql(session, f"""
        UPDATE {TABLE}
           SET STATUS = '{CLAIMED}', WORKER_ID = ?,
               STARTED_AT = CURRENT_TIMESTAMP(), HEARTBEAT_AT = CURRENT_TIMESTAMP(),
               ATTEMPT = ATTEMPT + 1, PROGRESS_LABEL = 'Claimed by worker'
         WHERE RUN_ID = (SELECT RUN_ID FROM {TABLE}
                          WHERE STATUS = '{QUEUED}' ORDER BY CREATED_AT LIMIT 1)
           AND STATUS = '{QUEUED}'
    """, [worker_id])
    if not (rows and rows[0][0]):
        return None
    got = _sql(session, f"""
        SELECT RUN_ID FROM {TABLE}
         WHERE WORKER_ID = ? AND STATUS = '{CLAIMED}'
         ORDER BY STARTED_AT DESC LIMIT 1
    """, [worker_id])
    return got[0][0] if got else None


def heartbeat(session, run_id, worker_id, status=None, label=None):
    sets = ["HEARTBEAT_AT = CURRENT_TIMESTAMP()"]
    params = []
    if status:
        sets.append("STATUS = ?")
        params.append(status)
    if label is not None:
        sets.append("PROGRESS_LABEL = ?")
        params.append(label)
    params.extend([run_id, worker_id])
    _sql(session, f"UPDATE {TABLE} SET {', '.join(sets)} "
                  f"WHERE RUN_ID = ? AND WORKER_ID = ?", params)


def complete(session, run_id, result_path):
    _sql(session, f"""
        UPDATE {TABLE}
           SET STATUS = '{COMPLETED}', COMPLETED_AT = CURRENT_TIMESTAMP(),
               HEARTBEAT_AT = CURRENT_TIMESTAMP(), RESULT_PATH = ?,
               PROGRESS_LABEL = 'Crosstab fetched', ERROR_CODE = NULL,
               ERROR_MESSAGE = NULL
         WHERE RUN_ID = ?
    """, [result_path, run_id])


def fail(session, run_id, code, message):
    _sql(session, f"""
        UPDATE {TABLE}
           SET STATUS = '{FAILED}', COMPLETED_AT = CURRENT_TIMESTAMP(),
               HEARTBEAT_AT = CURRENT_TIMESTAMP(), ERROR_CODE = ?,
               ERROR_MESSAGE = ?, PROGRESS_LABEL = ?
         WHERE RUN_ID = ?
    """, [code, str(message)[:2000], f"FAILED: {code}", run_id])


def requeue_or_fail(session, run_id, code, message):
    if code not in RETRYABLE_ERROR_CODES:
        fail(session, run_id, code, message)
        return False
    rows = _sql(session, f"SELECT ATTEMPT FROM {TABLE} WHERE RUN_ID = ?", [run_id])
    attempt = rows[0][0] if rows else MAX_ATTEMPTS
    if attempt >= MAX_ATTEMPTS:
        fail(session, run_id, code, f"{message} (giving up after {attempt} attempt(s))")
        return False
    _sql(session, f"""
        UPDATE {TABLE} SET STATUS = '{QUEUED}', WORKER_ID = NULL,
               ERROR_CODE = ?, ERROR_MESSAGE = ?,
               PROGRESS_LABEL = 'Retrying after an infrastructure failure'
         WHERE RUN_ID = ?
    """, [code, str(message)[:2000], run_id])
    return True


def recover_stale(session, stale_seconds: int = STALE_HEARTBEAT_SECONDS):
    active = "', '".join(ACTIVE_STATUSES)
    rows = _sql(session, f"""
        UPDATE {TABLE}
           SET STATUS = '{QUEUED}', WORKER_ID = NULL, ERROR_CODE = 'WORKER_LOST',
               ERROR_MESSAGE = 'no heartbeat within {stale_seconds}s; requeued',
               PROGRESS_LABEL = 'Requeued after worker loss'
         WHERE STATUS IN ('{active}')
           AND HEARTBEAT_AT < DATEADD('second', -{int(stale_seconds)}, CURRENT_TIMESTAMP())
           AND ATTEMPT < {MAX_ATTEMPTS}
    """)
    return rows[0][0] if rows and rows[0] else 0


def status_of(session, run_id):
    rows = _sql(session, f"""
        SELECT STATUS, PROGRESS_LABEL, ERROR_CODE, ERROR_MESSAGE, RESULT_PATH,
               WORKER_ID, ATTEMPT, DASHBOARD_CONTENT_URL, SHEET_NAME
          FROM {TABLE} WHERE RUN_ID = ?
    """, [run_id])
    if not rows:
        return None
    r = rows[0]
    return {"status": r[0], "label": r[1], "error_code": r[2], "error_message": r[3],
            "result_path": r[4], "worker_id": r[5], "attempt": r[6],
            "dashboard_content_url": r[7], "sheet_name": r[8]}


def fetch_result_csv(session, run_id, dest_dir):
    """Download and read the worker's result JSON. Returns (csv_text_or_None,
    error_message_or_None) -- never raises for a missing/malformed result,
    since "the worker never wrote one" is itself a real, reportable failure
    mode, not a bug in the caller."""
    os.makedirs(dest_dir, exist_ok=True)
    session.file.get(output_path(run_id), dest_dir)
    path = os.path.join(dest_dir, "result.json")
    if not os.path.exists(path):
        return None, "worker completed but wrote no result.json"
    with open(path, encoding="utf-8") as fh:
        result = json.load(fh)
    if not result.get("success"):
        return None, result.get("error") or "unknown failure"
    return result.get("csv_text"), None


__all__ = [
    "QUEUED", "CLAIMED", "FETCHING", "COMPLETED", "FAILED",
    "ACTIVE_STATUSES", "TERMINAL_STATUSES", "MAX_ATTEMPTS",
    "HEARTBEAT_SECONDS", "STALE_HEARTBEAT_SECONDS",
    "new_run_id", "input_path", "output_path", "ensure_table", "enqueue",
    "claim_next", "heartbeat", "complete", "fail", "requeue_or_fail",
    "recover_stale", "status_of", "fetch_result_csv",
]
