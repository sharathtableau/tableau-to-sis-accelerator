"""Task-driven visual capture: the SiS app enqueues, a Snowflake TASK captures.

WHY THIS EXISTS (read before changing the architecture)
-------------------------------------------------------
`EXECUTE NOTEBOOK` CANNOT be called from inside a Streamlit-in-Snowflake app
session. Measured 2026-08-16 across the full query history, a clean split with
no overlap:

    workstation Snowpark session   8 / 8 SUCCESS
    deployed SiS app              0 / 8 FAILED

every failure carrying the SAME code -- `300002:2250622648` -- and dying in
~0.6s, before pip, Chromium or the app ever start. The incident id changes per
attempt; the error code never does. It is a platform restriction on nested
execution, not a fault in our code.

A bounded-retry wrapper was built and shipped against this first
(`notebook_capture._capture_with_retry`, still present and still correct for
the workstation path). All three attempts failed identically -- which is what
PROVED the failure deterministic rather than transient. Do not re-diagnose
this as flaky and do not "fix" it with more retries.

So the trigger moves OUT of the SiS session entirely:

    SiS button
      -> INSERT one QUEUED row  (plain DML -- always allowed from SiS)
      -> return immediately, poll the row
    TASK (own execution context, owner rights, warehouse-backed)
      -> EXECUTE NOTEBOOK on the container runtime
      -> notebook claims the oldest QUEUED row ATOMICALLY
      -> captures, writes PNGs + manifest to the run's output prefix
      -> updates the row to DONE / FAILED
    SiS
      -> sees a terminal status, downloads the evidence, renders the report

The task is warehouse-backed (POWERHOUSE) and carries NO `EXECUTE AS USER`
clause -- it runs as its owner role, WBR_OWNER. Snowflake does not support
EXECUTE NOTEBOOK inside a task configured to run as a user.

STATUS VOCABULARY
-----------------
`validation_jobs.py` already defines the queue statuses this project uses, and
`test_automated_capture_queue_and_bundle` locks their semantics -- notably the
BLOCKED-vs-FAILED distinction, which is a core trust invariant here ("a
missing proof must never look like a proven-wrong"). That existing set is a
SUPERSET of the six requested for this change:

    requested   ->  existing
    QUEUED      ->  QUEUED
    CLAIMED     ->  CLAIMED
    RUNNING     ->  STARTING_APP / CAPTURING / GENERATING_REPORT  (finer)
    DONE        ->  COMPLETED
    FAILED      ->  FAILED
    TIMED_OUT   ->  TIMED_OUT   (added here -- genuinely new)

Renaming COMPLETED to DONE would break that gate and buy nothing, so the
existing names are kept and only the genuinely-missing TIMED_OUT is added.
"""

from __future__ import annotations

import json
import os
import tempfile
import time

import validation_jobs as VJ

TABLE = VJ.TABLE
STAGE = VJ.STAGE
NOTEBOOK_SCHEMA = "WBR_DB.PIPELINE_DEMO"
WORKER_NOTEBOOK = f"{NOTEBOOK_SCHEMA}.CAPTURE_WORKER"
WORKER_TASK = f"{NOTEBOOK_SCHEMA}.CAPTURE_WORKER_TASK"
COMPUTE_POOL = "SYSTEM_COMPUTE_POOL_CPU"
WAREHOUSE = "POWERHOUSE"
EAI = "PLAYWRIGHT_EAI"

# The generated app's own runtime, listed explicitly rather than shipping the
# repo so an unrelated file cannot silently become a capture dependency --
# the same discipline snowflake.yml's artifact list follows.
RUNTIME_MODULES = [
    "engine.py", "backend.py", "config.py", "calc_translator.py",
    "findings.py", "profile_superstore.py", "profile_default.py",
    "app_screenshot.py",
    # The worker imports this to validate its own evidence -- the SAME
    # implementation the test suite imports, never a container-side copy.
    "capture_validation.py",
]

# A job that never reaches a terminal status is TIMED_OUT rather than left
# CLAIMED forever -- a stuck row that still reads "in progress" is
# indistinguishable from a slow one, and the SiS poller would wait on it
# forever. Comfortably longer than the observed ~160s capture plus the
# container's own cold start.
JOB_TIMEOUT_SECONDS = 900
TIMED_OUT = "TIMED_OUT"

MAX_CAPTURE_ATTEMPTS = 3

# Structural floor for an accepted screenshot -- NOT a content/blank check.
# MIN_WIDTH is enforced because every real dashboard renders at the app's full
# viewport width regardless of content, so a narrow image is corruption.
# Height is deliberately NOT enforced: Sheet 22 is a genuinely empty
# placeholder sheet (confirmed against the IR -- non_data=True, no
# dims/measures -- and against Tableau's own screenshot of it, also blank) and
# captures as a real full-width, ~66px image. Rejecting it would fail every
# run forever on a dashboard that can never look any different.
MIN_WIDTH = 640


# ---------------------------------------------------------------- SiS side
def upload_capture_bundle(session, run_id, ir, dashboards, routing=None,
                          runtime_modules=None, log=None):
    """Stage everything the worker needs: the generated app, the IR, the
    dashboards it must capture (by title), the app's runtime modules, and the
    LIVE table routing.

    Routing is shipped from the CALLER's live config.DATASOURCES rather than a
    file on disk. Two reasons, one of which cost a full run to learn: (1) data
    fidelity -- the captured app must query the same tables the validation
    queried, or the screenshot shows different numbers than the comparison;
    (2) the repo's own datasources.json is the LOCAL DEV map pointing at a
    SUPERSTORE database that does not exist in Snowflake, and shipping it
    produced ten flawless screenshots of ten "database does not exist" errors.
    """
    import codegen

    def _log(m):
        if log:
            log(m)

    here = os.path.dirname(os.path.abspath(__file__))
    tmp = tempfile.mkdtemp(prefix="taskcap")
    prefix = f"{STAGE}/{run_id}/input"

    with open(os.path.join(tmp, "app_generated.py"), "w", encoding="utf-8") as fh:
        fh.write(codegen.build(ir))
    with open(os.path.join(tmp, "workbook_ir.json"), "w", encoding="utf-8") as fh:
        json.dump(ir, fh)
    with open(os.path.join(tmp, "dashboards.json"), "w", encoding="utf-8") as fh:
        json.dump(dashboards, fh, indent=1)
    staged = ["app_generated.py", "workbook_ir.json", "dashboards.json"]

    for name in (runtime_modules or RUNTIME_MODULES):
        src = os.path.join(here, name)
        if os.path.exists(src):
            with open(src, "rb") as r, open(os.path.join(tmp, name), "wb") as w:
                w.write(r.read())
            staged.append(name)

    if routing is None:
        try:
            import config
            routing = {k: dict(v) for k, v in (config.DATASOURCES or {}).items()}
        except Exception:                                       # noqa: BLE001
            routing = {}
        if not routing:
            deploy_map = os.path.join(here, "datasources.deploy.json")
            if os.path.exists(deploy_map):
                with open(deploy_map, encoding="utf-8") as fh:
                    routing = json.load(fh)
    if routing:
        with open(os.path.join(tmp, "datasources.json"), "w", encoding="utf-8") as fh:
            json.dump(routing, fh, indent=1)
        staged.append("datasources.json")

    for name in staged:
        session.file.put(os.path.join(tmp, name).replace("\\", "/"), prefix,
                         auto_compress=False, overwrite=True)
    _log(f"staged {len(staged)} file(s) -> {prefix}")
    return prefix, staged


def submit(session, run_id=None, workbook_id=None):
    """Enqueue one capture request. Plain INSERT -- the ONLY thing the SiS app
    does, and the one thing a SiS session is definitely allowed to do."""
    run_id = run_id or VJ.new_run_id()
    VJ.enqueue(session, run_id, workbook_id=workbook_id)
    return run_id


def fetch_results(session, run_id, dest_dir):
    """Bring the WINNING attempt's screenshots home after COMPLETED.

    Reads the run-level manifest to learn which attempt won, then downloads
    ONLY that attempt's prefix -- so a partial result from a failed earlier
    attempt can never be mixed into the evidence. Returns
    (shots, diagnostics) where `shots` is {title: png_bytes}."""
    os.makedirs(dest_dir, exist_ok=True)
    try:
        session.file.get(f"{STAGE}/{run_id}/output/", dest_dir)
    except Exception as exc:                                    # noqa: BLE001
        return {}, {"error": f"could not download results: "
                             f"{type(exc).__name__}: {exc}"}

    manifest_path = os.path.join(dest_dir, "capture_manifest.json")
    if not os.path.exists(manifest_path):
        return {}, {"error": "capture produced no manifest"}
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            diag = json.load(fh)
    except Exception as exc:                                    # noqa: BLE001
        return {}, {"error": f"manifest malformed: {type(exc).__name__}: {exc}"}

    winning = diag.get("winning_prefix")
    files = diag.get("files") or {}
    if not winning or not files:
        return {}, diag

    child = winning.rstrip("/").split("/")[-1]
    shots = {}
    for title, info in files.items():
        for candidate in (os.path.join(dest_dir, child, info["file"]),
                          os.path.join(dest_dir, info["file"])):
            if os.path.exists(candidate):
                with open(candidate, "rb") as fh:
                    shots[title] = fh.read()
                break
    return shots, diag


def poll(session, run_id):
    """Current queue state for the SiS progress UI, plus a computed
    `terminal` flag so the caller never has to know the status vocabulary."""
    state = VJ.status_of(session, run_id)
    if state is None:
        return None
    state["terminal"] = state["status"] in (
        VJ.COMPLETED, VJ.FAILED, VJ.BLOCKED, TIMED_OUT)
    return state


def available(session) -> tuple[bool, str | None]:
    """(True, None) when a task-driven capture can be attempted from here.

    Cheap on purpose: it checks only what makes the whole path impossible, so
    a caller never waits minutes to discover the obvious. It deliberately does
    NOT check the EXECUTE TASK grant -- a suspended/ungranted task surfaces as
    a job that never leaves QUEUED, which `capture()` reports as a timeout
    naming the grant, rather than a guess made up front."""
    if session is None:
        return False, ("no Snowflake session -- the capture runs as a task "
                       "inside Snowflake and needs one to enqueue")
    try:
        rows = session.sql(f"SHOW TASKS LIKE '{WORKER_TASK.split('.')[-1]}' "
                           f"IN SCHEMA {NOTEBOOK_SCHEMA}").collect()
    except Exception as exc:                                    # noqa: BLE001
        return False, f"cannot read tasks: {type(exc).__name__}: {exc}"
    if not rows:
        return False, (f"{WORKER_TASK} does not exist -- run "
                       "`python setup_task_capture.py setup` once")
    return True, None


def capture(session, ir, stem, dashboards=None, log=None,
            timeout_seconds=JOB_TIMEOUT_SECONDS, poll_seconds=10,
            sleep_fn=time.sleep):
    """Enqueue a capture and wait for the task worker to finish it.

    Returns (shots, notes) in exactly the shape
    `deep_validation._render_app_screenshots` returns, so the caller cannot
    tell which path produced the images -- only that they are real
    screenshots. Never calls EXECUTE NOTEBOOK: that is precisely what a SiS
    session cannot do, and is why this path exists.

    `notes` carries the worker's full attempt history under "attempts" so the
    report can render ONE workbook-level diagnostics block, and a per-attempt
    dump never repeats under every dashboard."""
    def _log(m):
        if log:
            log(m)

    dashboards = dashboards or [
        {"title": d.get("title") or d.get("name"), "name": d.get("name")}
        for d in ir.get("dashboards", [])]

    run_id = VJ.new_run_id()
    _log(f"capture run {run_id}")
    try:
        upload_capture_bundle(session, run_id, ir, dashboards, log=_log)
    except Exception as exc:                                    # noqa: BLE001
        return {}, [{"dashboard": None, "captured": False, "run_id": run_id,
                     "reason": f"could not stage the capture bundle: "
                               f"{type(exc).__name__}: {exc}"}]

    submit(session, run_id=run_id, workbook_id=stem)
    _log("queued; waiting for the task worker")

    deadline = time.time() + timeout_seconds
    last_label = None
    while time.time() < deadline:
        sleep_fn(poll_seconds)
        state = poll(session, run_id)
        if state is None:
            continue
        if state["label"] != last_label:
            last_label = state["label"]
            _log(f"{state['status']}: {state['label']}")
        if state["terminal"]:
            break
    else:
        state = poll(session, run_id) or {}

    state = poll(session, run_id) or {}
    if not state.get("terminal"):
        expire_stale(session, timeout_seconds)
        stuck_in_queue = state.get("status") == VJ.QUEUED
        reason = (f"the capture job did not finish within {timeout_seconds}s "
                  f"(last status {state.get('status')})")
        if stuck_in_queue:
            # The single most likely cause, named explicitly rather than left
            # for someone to rediscover: nothing ever claimed the job.
            reason += (". The job was never claimed -- check that "
                       f"{WORKER_TASK} is RESUMED and that the account grant "
                       "`GRANT EXECUTE TASK ON ACCOUNT TO ROLE WBR_OWNER` "
                       "has been applied")
        return {}, [{"dashboard": None, "captured": False, "run_id": run_id,
                     "reason": reason, "summary": reason}]

    shots, diag = fetch_results(
        session, run_id, os.path.join("reports", f"{stem}_validation_input",
                                      "streamlit"))
    attempts = (diag or {}).get("attempts") or []

    if state["status"] == VJ.COMPLETED and shots:
        notes = []
        if len(attempts) > 1:
            summary = (f"{len(shots)}/{len(dashboards)} screenshot(s) captured "
                       f"on attempt {diag.get('winning_attempt')} of "
                       f"{MAX_CAPTURE_ATTEMPTS}")
            notes = [{"dashboard": None, "captured": True, "run_id": run_id,
                      "reason": summary, "summary": summary,
                      "attempts": attempts}]
        return shots, notes

    summary = (f"Streamlit screenshot unavailable because workbook capture "
               f"ended {state['status']} after {len(attempts) or 1} attempt(s). "
               "See capture diagnostics above.")
    return {}, [{"dashboard": None, "captured": False, "run_id": run_id,
                 "reason": summary, "summary": summary, "attempts": attempts,
                 "error_code": state.get("error_code"),
                 "error_message": state.get("error_message")}]


def expire_stale(session, timeout_seconds: int = JOB_TIMEOUT_SECONDS):
    """Move jobs that never finished to TIMED_OUT. Distinct from
    VJ.recover_stale (which REQUEUES a job whose worker died and is worth
    another attempt); this is the terminal give-up, so the poller stops."""
    active = "', '".join((VJ.QUEUED,) + VJ.ACTIVE_STATUSES)
    rows = session.sql(f"""
        UPDATE {TABLE}
           SET STATUS = '{TIMED_OUT}',
               COMPLETED_AT = CURRENT_TIMESTAMP(),
               ERROR_CODE = 'TIMED_OUT',
               ERROR_MESSAGE = 'no terminal status within {int(timeout_seconds)}s',
               PROGRESS_LABEL = 'Timed out'
         WHERE STATUS IN ('{active}')
           AND CREATED_AT < DATEADD('second', -{int(timeout_seconds)},
                                    CURRENT_TIMESTAMP())
    """).collect()
    return rows[0][0] if rows and rows[0] else 0


# ------------------------------------------------------- worker notebook
# SELF-TEST MODE ONLY at this stage. It proves the whole chain --
# SiS INSERT -> task fires -> notebook runs on the container runtime ->
# notebook CLAIMS the row atomically -> writes a manifest to the run's output
# prefix -> marks the row COMPLETED -> SiS reads it back -- WITHOUT the
# 160-second Playwright capture in the middle. The capture is wired in only
# after this passes, so a failure here is unambiguously the plumbing.
_WORKER_SELFTEST = '''
import json, time, traceback
from snowflake.snowpark.context import get_active_session

TABLE   = "__TABLE__"
STAGE   = "__STAGE__"
WORKER  = "task-selftest-" + time.strftime("%Y%m%dT%H%M%S", time.gmtime())
session = get_active_session()

def sql(stmt, params=None):
    return session.sql(stmt, params or []).collect()

report = {"worker_id": WORKER, "mode": "SELFTEST",
          "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
          "steps": []}

def step(name, fn):
    t0 = time.time()
    try:
        out = fn()
        report["steps"].append({"step": name, "ok": True,
                                "seconds": round(time.time() - t0, 2),
                                "detail": out})
        return out
    except Exception as e:
        report["steps"].append({"step": name, "ok": False,
                                "seconds": round(time.time() - t0, 2),
                                "error": (type(e).__name__ + ": " + str(e))[:800],
                                "trace": traceback.format_exc()[-900:]})
        raise

# --- ATOMIC CLAIM. The `AND STATUS = 'QUEUED'` predicate is what makes this
# safe under two workers: Snowflake serialises conflicting DML on a row, so
# exactly one UPDATE matches and the other matches zero rows. A
# SELECT-then-UPDATE would let both win.
def claim():
    rows = sql("""
        UPDATE """ + TABLE + """
           SET STATUS = 'CLAIMED', WORKER_ID = ?,
               STARTED_AT = CURRENT_TIMESTAMP(),
               HEARTBEAT_AT = CURRENT_TIMESTAMP(),
               ATTEMPT = ATTEMPT + 1,
               PROGRESS_LABEL = 'Claimed by task worker'
         WHERE RUN_ID = (SELECT RUN_ID FROM """ + TABLE + """
                          WHERE STATUS = 'QUEUED' AND CANCEL_REQUESTED = FALSE
                          ORDER BY CREATED_AT LIMIT 1)
           AND STATUS = 'QUEUED'
    """, [WORKER])
    if not (rows and rows[0][0]):
        return None
    got = sql("SELECT RUN_ID FROM " + TABLE +
              " WHERE WORKER_ID = ? AND STATUS = 'CLAIMED'"
              " ORDER BY STARTED_AT DESC LIMIT 1", [WORKER])
    return got[0][0] if got else None

run_id = step("claim_job", claim)

if not run_id:
    report["result"] = "no queued job -- nothing to do"
    print(json.dumps(report, indent=1, default=str)[:2000])
else:
    report["run_id"] = run_id
    try:
        step("mark_running", lambda: sql(
            "UPDATE " + TABLE + " SET STATUS = 'CAPTURING',"
            " HEARTBEAT_AT = CURRENT_TIMESTAMP(),"
            " PROGRESS_LABEL = 'Self-test: proving the task->notebook path'"
            " WHERE RUN_ID = ?", [run_id]) and "ok")

        # The manifest SiS reads back to prove the chain end to end.
        def write_manifest():
            import os
            out = "/tmp/" + run_id
            os.makedirs(out, exist_ok=True)
            path = os.path.join(out, "capture_manifest.json")
            payload = {
                "run_id": run_id, "worker_id": WORKER, "mode": "SELFTEST",
                "status": "DONE",
                "completed_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "note": "task -> notebook -> stage chain proven; no capture yet",
            }
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=1)
            session.file.put(path, STAGE + "/" + run_id + "/output",
                             auto_compress=False, overwrite=True)
            return payload
        step("write_manifest", write_manifest)

        step("mark_done", lambda: sql(
            "UPDATE " + TABLE + " SET STATUS = 'COMPLETED',"
            " COMPLETED_AT = CURRENT_TIMESTAMP(),"
            " HEARTBEAT_AT = CURRENT_TIMESTAMP(),"
            " RESULT_MANIFEST_PATH = ?,"
            " PROGRESS_LABEL = 'Self-test complete',"
            " ERROR_CODE = NULL, ERROR_MESSAGE = NULL"
            " WHERE RUN_ID = ?",
            [STAGE + "/" + run_id + "/output/capture_manifest.json", run_id])
            and "ok")
        report["result"] = "SELFTEST OK"
    except Exception as e:
        sql("UPDATE " + TABLE + " SET STATUS = 'FAILED',"
            " COMPLETED_AT = CURRENT_TIMESTAMP(),"
            " ERROR_CODE = 'SELFTEST', ERROR_MESSAGE = ?,"
            " PROGRESS_LABEL = 'Self-test failed'"
            " WHERE RUN_ID = ?", [str(e)[:2000], run_id])
        report["result"] = "SELFTEST FAILED"

    print(json.dumps(report, indent=1, default=str)[:3000])
'''


# The wrapper the generated app runs under inside the container, kept OUT of
# any f-string: it is full of braces, and escaping them all is how this broke
# the first time. Identical in intent to notebook_capture._WRAPPER -- the app
# gets a real Snowpark session from the container's own OAuth token, so it
# queries the SAME tables the validation queried.
_APP_WRAPPER = """
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from snowflake.snowpark import Session
with open("/snowflake/session/token") as fh:
    _tok = fh.read()
_s = Session.builder.configs({
    "host": os.environ["SNOWFLAKE_HOST"],
    "account": os.environ["SNOWFLAKE_ACCOUNT"],
    "token": _tok, "authenticator": "oauth",
    "database": os.environ.get("SNOWFLAKE_DATABASE", "WBR_DB"),
    "schema": os.environ.get("SNOWFLAKE_SCHEMA", "PIPELINE_DEMO"),
    "warehouse": os.environ.get("SNOWFLAKE_WAREHOUSE", "POWERHOUSE"),
}).create()
import backend
backend.set_session(_s)
_here = os.path.dirname(os.path.abspath(__file__))
exec(compile(open(os.path.join(_here, "app_generated.py"), encoding="utf-8").read(),
             "app_generated.py", "exec"))
"""


# The REAL worker. Runs on the container runtime, claims one job, and captures
# the generated app with the SAME app_screenshot.capture_app the workstation
# path uses -- never a second browser implementation, because a screenshot is
# evidence and two implementations drift.
#
# RETRIES LIVE HERE, not in SiS. The environment build (pip + apt + Chromium,
# ~20s) happens ONCE and is reused across attempts; only the capture itself is
# retried, each attempt into its OWN stage prefix so a partial result from one
# attempt can never be mixed with another's.
_WORKER_CAPTURE = '''
import json, os, subprocess, sys, time, traceback, glob, io
from snowflake.snowpark.context import get_active_session

TABLE       = "__TABLE__"
STAGE       = "__STAGE__"
MAX_ATT     = __MAX_ATTEMPTS__
MIN_WIDTH   = __MIN_WIDTH__
WORKER      = "task-worker-" + time.strftime("%Y%m%dT%H%M%S", time.gmtime())
session     = get_active_session()

def sql(stmt, params=None):
    return session.sql(stmt, params or []).collect()

DIAG = {"worker_id": WORKER, "mode": "CAPTURE",
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "attempts": [], "steps": []}

def step(name, fn):
    t0 = time.time()
    try:
        out = fn()
        DIAG["steps"].append({"step": name, "ok": True,
                              "seconds": round(time.time() - t0, 1),
                              "detail": str(out)[:300]})
        return out
    except Exception as e:
        DIAG["steps"].append({"step": name, "ok": False,
                              "seconds": round(time.time() - t0, 1),
                              "error": (type(e).__name__ + ": " + str(e))[:800],
                              "trace": traceback.format_exc()[-900:]})
        raise

def sh(cmd, timeout=2400, env=None):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    if r.returncode:
        raise RuntimeError("rc=" + str(r.returncode) + ": " + (r.stderr or r.stdout)[-700:])
    return (r.stdout or "")[-160:]

def beat(status=None, label=None, done=None, total=None):
    """Heartbeat + progress. Returns False once a cancel is requested, so a
    long capture stops cleanly instead of finishing work nobody wants."""
    sets, params = ["HEARTBEAT_AT = CURRENT_TIMESTAMP()"], []
    if status:
        sets.append("STATUS = ?"); params.append(status)
    if label is not None:
        sets.append("PROGRESS_LABEL = ?"); params.append(label)
    if done is not None:
        sets.append("PROGRESS_DONE = ?"); params.append(done)
    if total is not None:
        sets.append("PROGRESS_TOTAL = ?"); params.append(total)
    params.append(RUN_ID)
    sql("UPDATE " + TABLE + " SET " + ", ".join(sets) + " WHERE RUN_ID = ?", params)
    rows = sql("SELECT CANCEL_REQUESTED FROM " + TABLE + " WHERE RUN_ID = ?", [RUN_ID])
    return not (rows and rows[0][0])

# --- TRANSIENT CLASSIFICATION, worker-side. Whitelist, not blacklist: an
# error this project has never seen is NOT retried, because three
# identical-looking failures hide a real bug better than one clear one does.
TRANSIENT = ("300002", "xx000", "incident", "sql execution internal error",
             "service unavailable", "temporarily unavailable", "timed out",
             "timeout", "could not be started", "resource exhausted",
             "connection reset", "econnreset", "chromium", "browser closed",
             "target closed", "net::err")
DETERMINISTIC = ("insufficient privileges", "not authorized", "does not exist",
                 "sql compilation error", "invalid identifier", "syntaxerror",
                 "modulenotfounderror", "importerror", "no such file")

def is_transient(text):
    low = (text or "").lower()
    if any(m in low for m in DETERMINISTIC):
        return False
    return any(m in low for m in TRANSIENT)

# ---------------------------------------------------------------- claim
# ATOMIC. The trailing `AND STATUS = 'QUEUED'` is what makes this safe under
# two workers: Snowflake serialises conflicting DML on a row, so exactly one
# UPDATE matches a row and the other matches zero. SELECT-then-UPDATE would
# let both win and double-book the compute pool.
def claim():
    rows = sql("""
        UPDATE """ + TABLE + """
           SET STATUS = 'CLAIMED', WORKER_ID = ?,
               STARTED_AT = CURRENT_TIMESTAMP(),
               HEARTBEAT_AT = CURRENT_TIMESTAMP(),
               ATTEMPT = ATTEMPT + 1,
               PROGRESS_LABEL = 'Claimed by task worker'
         WHERE RUN_ID = (SELECT RUN_ID FROM """ + TABLE + """
                          WHERE STATUS = 'QUEUED' AND CANCEL_REQUESTED = FALSE
                          ORDER BY CREATED_AT LIMIT 1)
           AND STATUS = 'QUEUED'
    """, [WORKER])
    if not (rows and rows[0][0]):
        return None
    got = sql("SELECT RUN_ID FROM " + TABLE +
              " WHERE WORKER_ID = ? AND STATUS = 'CLAIMED'"
              " ORDER BY STARTED_AT DESC LIMIT 1", [WORKER])
    return got[0][0] if got else None

RUN_ID = claim()
if not RUN_ID:
    print(json.dumps({"result": "no queued job", "worker": WORKER}))
else:
    DIAG["run_id"] = RUN_ID
    IN   = STAGE + "/" + RUN_ID + "/input"
    OUT  = STAGE + "/" + RUN_ID + "/output"
    WORK = "/tmp/" + RUN_ID
    terminal = None

    def finish_blocked(code, message):
        """No visual evidence produced. BLOCKED, never FAILED -- 'could not
        measure' and 'measured and wrong' must never look the same."""
        sql("UPDATE " + TABLE + " SET STATUS = 'BLOCKED',"
            " COMPLETED_AT = CURRENT_TIMESTAMP(), HEARTBEAT_AT = CURRENT_TIMESTAMP(),"
            " ERROR_CODE = ?, ERROR_MESSAGE = ?, PROGRESS_LABEL = 'Capture blocked'"
            " WHERE RUN_ID = ?", [code, str(message)[:2000], RUN_ID])

    def finish_failed(code, message):
        """The JOB could not run at all (bad bundle, missing egress). Distinct
        from BLOCKED so the operator knows to fix infrastructure, not the app."""
        sql("UPDATE " + TABLE + " SET STATUS = 'FAILED',"
            " COMPLETED_AT = CURRENT_TIMESTAMP(), HEARTBEAT_AT = CURRENT_TIMESTAMP(),"
            " ERROR_CODE = ?, ERROR_MESSAGE = ?, PROGRESS_LABEL = 'Capture failed'"
            " WHERE RUN_ID = ?", [code, str(message)[:2000], RUN_ID])

    try:
        beat(status="STARTING_APP", label="Preparing the browser container")

        # ---- environment, built ONCE and reused by every attempt ---------
        PW = dict(os.environ); PW["PLAYWRIGHT_BROWSERS_PATH"] = "/tmp/pw"
        step("pip_install", lambda: sh([sys.executable, "-m", "pip", "install",
                                        "playwright==1.47.0", "pillow", "streamlit",
                                        "altair", "duckdb", "plotly"]))
        step("install_system_deps", lambda: sh([sys.executable, "-m", "playwright",
                                                "install-deps", "chromium"]))
        step("install_chromium", lambda: sh([sys.executable, "-m", "playwright",
                                             "install", "chromium"], env=PW))

        def fetch():
            os.makedirs(WORK, exist_ok=True)
            session.file.get(IN + "/", WORK)
            return sorted(os.path.basename(p) for p in glob.glob(WORK + "/*"))
        staged = step("fetch_bundle", fetch)
        if "app_generated.py" not in staged:
            raise RuntimeError("bundle is missing app_generated.py (no such file)")

        with open(os.path.join(WORK, "dashboards.json"), encoding="utf-8") as fh:
            EXPECTED = [d["title"] for d in json.load(fh)]
        DIAG["expected"] = EXPECTED

        app_path = os.path.join(WORK, "app_capture.py")
        with open(app_path, "w", encoding="utf-8") as fh:
            fh.write(__APP_WRAPPER__)

        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/tmp/pw"
        sys.path.insert(0, WORK)
        import app_screenshot as APS

        def slug(t):
            return "".join(c.lower() if c.isalnum() else "-" for c in str(t)).strip("-")

        # ---- STRUCTURAL validation, from the SHARED module the test suite
        # imports directly (capture_validation.py, staged with the runtime).
        # ONE implementation on purpose: a worker-side copy that drifts from
        # the tested one is how a validator quietly stops validating what you
        # think it does. See that module for why blankness is never a
        # rejection reason (Sheet 22).
        import capture_validation as CV

        def validate(files, outdir):
            ok, reason = CV.validate_capture(files, outdir, EXPECTED, MIN_WIDTH)
            return None if ok else reason

        # ---- attempts, each into its OWN prefix -------------------------
        for att in range(1, MAX_ATT + 1):
            child = "A" + str(att)
            outdir = os.path.join(WORK, "out", child)
            os.makedirs(outdir, exist_ok=True)
            rec = {"attempt": att, "child_id": RUN_ID + "-" + child,
                   "stage_prefix": OUT + "/" + child}
            t0 = time.time()
            if not beat(status="CAPTURING",
                        label="Capturing dashboards (attempt " + str(att) + " of " + str(MAX_ATT) + ")",
                        done=0, total=len(EXPECTED)):
                finish_blocked("CANCELLED", "cancel requested")
                terminal = "CANCELLED"
                break
            try:
                shots, notes = APS.capture_app(app_path, startup_timeout=600)
                files = {}
                for title, png in shots.items():
                    name = "streamlit-" + slug(title) + ".png"
                    with open(os.path.join(outdir, name), "wb") as fh:
                        fh.write(png)
                    files[title] = {"file": name, "bytes": len(png)}
                problem = validate(files, outdir)
                rec["captured"] = len(files)
                rec["elapsed_s"] = round(time.time() - t0, 1)
                if problem is None:
                    # upload ONLY this attempt's files
                    for p in sorted(glob.glob(outdir + "/*")):
                        session.file.put(p, OUT + "/" + child,
                                         auto_compress=False, overwrite=True)
                    rec["ok"] = True
                    rec["error"] = None
                    DIAG["attempts"].append(rec)
                    DIAG["winning_attempt"] = att
                    DIAG["winning_prefix"] = OUT + "/" + child
                    DIAG["files"] = files
                    terminal = "COMPLETED"
                    break
                rec["ok"] = False
                rec["error"] = "evidence incomplete: " + problem
                rec["retryable"] = True
            except Exception as e:
                rec["ok"] = False
                rec["elapsed_s"] = round(time.time() - t0, 1)
                rec["error"] = (type(e).__name__ + ": " + str(e))[:900]
                rec["retryable"] = is_transient(rec["error"])
            DIAG["attempts"].append(rec)
            if not rec.get("retryable") or att == MAX_ATT:
                break
            time.sleep(5)

        if terminal == "COMPLETED":
            manifest_path = OUT + "/" + DIAG["winning_prefix"].split("/")[-1] + "/capture_manifest.json"
            sql("UPDATE " + TABLE + " SET STATUS = 'COMPLETED',"
                " COMPLETED_AT = CURRENT_TIMESTAMP(), HEARTBEAT_AT = CURRENT_TIMESTAMP(),"
                " RESULT_MANIFEST_PATH = ?, PROGRESS_LABEL = 'Capture complete',"
                " PROGRESS_DONE = ?, ERROR_CODE = NULL, ERROR_MESSAGE = NULL"
                " WHERE RUN_ID = ?",
                [manifest_path, len(DIAG.get("files") or {}), RUN_ID])
        elif terminal != "CANCELLED":
            last = DIAG["attempts"][-1] if DIAG["attempts"] else {}
            finish_blocked("CAPTURE_INCOMPLETE",
                           "no valid evidence after " + str(len(DIAG["attempts"])) +
                           " attempt(s): " + str(last.get("error"))[:400])
    except Exception as e:
        DIAG["fatal"] = (type(e).__name__ + ": " + str(e))[:900]
        DIAG["fatal_trace"] = traceback.format_exc()[-1500:]
        # A bundle/egress/config problem is a JOB failure, not merely absent
        # evidence -- FAILED tells the operator to fix infrastructure.
        if is_transient(DIAG["fatal"]):
            finish_blocked("CAPTURE_ENV", DIAG["fatal"])
        else:
            finish_failed("WORKER_FATAL", DIAG["fatal"])
    finally:
        # The diagnostics manifest is written whatever happened -- a failed
        # run with no explanation is the one outcome this project forbids.
        DIAG["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            for i, lg in enumerate(sorted(glob.glob("/tmp/st_shot_*/server.log"))):
                try:
                    txt = open(lg, "r", errors="replace").read()[-40000:]
                    lp = os.path.join(WORK, "app_server_%d.log" % i)
                    open(lp, "w", encoding="utf-8").write(txt)
                    session.file.put(lp, OUT, auto_compress=False, overwrite=True)
                    DIAG.setdefault("app_log_tail", txt[-2000:])
                except Exception:
                    pass
            os.makedirs(WORK, exist_ok=True)
            mp = os.path.join(WORK, "capture_manifest.json")
            with open(mp, "w", encoding="utf-8") as fh:
                json.dump(DIAG, fh, indent=1, default=str)
            session.file.put(mp, OUT, auto_compress=False, overwrite=True)
            if DIAG.get("winning_prefix"):
                session.file.put(mp, DIAG["winning_prefix"],
                                 auto_compress=False, overwrite=True)
        except Exception as e:
            print("could not write diagnostics:", str(e)[:300])
    print(json.dumps(DIAG, indent=1, default=str)[:3000])
'''


def worker_notebook_source(mode: str = "capture") -> str:
    """`capture` is the production worker; `selftest` is the plumbing-only
    body used to prove the task->notebook->stage chain without spending 160s
    on Playwright (so a plumbing failure cannot be mistaken for a capture
    failure)."""
    if mode == "selftest":
        return (_WORKER_SELFTEST
                .replace("__TABLE__", TABLE)
                .replace("__STAGE__", STAGE))
    if mode == "capture":
        return (_WORKER_CAPTURE
                .replace("__TABLE__", TABLE)
                .replace("__STAGE__", STAGE)
                .replace("__MAX_ATTEMPTS__", str(MAX_CAPTURE_ATTEMPTS))
                .replace("__MIN_WIDTH__", str(MIN_WIDTH))
                .replace("__APP_WRAPPER__", repr(_APP_WRAPPER)))
    raise ValueError(f"unknown worker mode {mode!r}")


def _ipynb(source: str) -> str:
    return json.dumps({
        "cells": [{"cell_type": "code", "execution_count": None, "id": "worker",
                   "metadata": {"language": "python"}, "outputs": [],
                   "source": [line + "\n" for line in source.strip().split("\n")]}],
        "metadata": {"kernelspec": {"display_name": "Streamlit Notebook",
                                    "name": "streamlit"},
                     "language_info": {"name": "python"}},
        "nbformat": 4, "nbformat_minor": 5}, indent=1)


def deploy_worker(session, mode: str = "selftest", log=print):
    """(Re)create the worker notebook and the task that runs it.

    Idempotent -- safe to re-run after editing the worker body."""
    tmp = tempfile.mkdtemp(prefix="taskcap")
    nb_file = "capture_worker.ipynb"
    path = os.path.join(tmp, nb_file)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(_ipynb(worker_notebook_source(mode)))
    prefix = f"{STAGE}/_worker"
    session.file.put(path.replace("\\", "/"), prefix,
                     auto_compress=False, overwrite=True)
    log(f"staged worker notebook -> {prefix}/{nb_file}")

    session.sql(f"""
        CREATE OR REPLACE NOTEBOOK {WORKER_NOTEBOOK}
          FROM '{prefix}'
          MAIN_FILE = '{nb_file}'
          QUERY_WAREHOUSE = {WAREHOUSE}
          RUNTIME_NAME = 'SYSTEM$BASIC_RUNTIME'
          COMPUTE_POOL = '{COMPUTE_POOL}'
    """).collect()
    session.sql(f"ALTER NOTEBOOK {WORKER_NOTEBOOK} ADD LIVE VERSION FROM LAST").collect()
    # REAL BUG, found live on the first-ever end-to-end run through this task
    # path (2026-08-17, blocked until then on the EXECUTE TASK grant): this
    # notebook is created WITHOUT an external access integration, so pip has
    # no egress at all -- "Failed to establish a new connection: [Errno -2]
    # Name or service not known" installing playwright, every time. The
    # STANDALONE capture path (notebook_capture.py) already attaches this via
    # a separate ALTER right after creation; this one was simply never wired
    # the same way, because nothing could exercise it before the grant
    # landed. Same EAI, same two-step pattern (CREATE NOTEBOOK -- like most
    # object types -- accepts EXTERNAL_ACCESS_INTEGRATIONS only via ALTER,
    # not inline on CREATE).
    try:
        session.sql(f"ALTER NOTEBOOK {WORKER_NOTEBOOK} SET "
                    f"EXTERNAL_ACCESS_INTEGRATIONS = ({EAI})").collect()
    except Exception as exc:                                        # noqa: BLE001
        raise RuntimeError(
            f"could not attach {EAI} to {WORKER_NOTEBOOK} (needed for pip and "
            f"the Chromium download): {type(exc).__name__}: {exc}") from exc
    log(f"created notebook {WORKER_NOTEBOOK}")

    # Warehouse-backed, owner-rights. Deliberately NO `EXECUTE AS USER`:
    # EXECUTE NOTEBOOK is not supported inside a task configured that way.
    session.sql(f"""
        CREATE OR REPLACE TASK {WORKER_TASK}
          WAREHOUSE = {WAREHOUSE}
          SCHEDULE = '1 MINUTE'
        AS
          EXECUTE NOTEBOOK {WORKER_NOTEBOOK}()
    """).collect()
    log(f"created task {WORKER_TASK} (suspended until RESUME)")
    return WORKER_NOTEBOOK, WORKER_TASK


def resume_worker(session):
    session.sql(f"ALTER TASK {WORKER_TASK} RESUME").collect()


def suspend_worker(session):
    session.sql(f"ALTER TASK {WORKER_TASK} SUSPEND").collect()


__all__ = ["available", "capture", "submit", "poll", "expire_stale",
           "fetch_results", "upload_capture_bundle", "deploy_worker",
           "resume_worker", "suspend_worker", "worker_notebook_source",
           "TABLE", "STAGE", "WORKER_NOTEBOOK", "WORKER_TASK", "TIMED_OUT",
           "JOB_TIMEOUT_SECONDS", "MAX_CAPTURE_ATTEMPTS", "MIN_WIDTH",
           "RUNTIME_MODULES"]
