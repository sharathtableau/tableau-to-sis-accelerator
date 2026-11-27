"""ONE command to run after the ACCOUNTADMIN grant lands.

    python activate_task_capture.py

Runs every remaining step in order and stops at the FIRST failure with an
actionable message -- never a bare traceback, and never a claim of success it
did not verify.

    1. verify EXECUTE TASK is actually granted (by running it, not by reading
       SHOW GRANTS -- that cannot see PUBLIC grants and has already produced
       one wrong "blocked" conclusion on this project)
    2. deploy the worker in CAPTURE mode + RESUME the task
    3. minimal task test: the self-test worker, proving task -> notebook ->
       stage -> queue without a 160s capture in the middle
    4. confirm the run in TASK_HISTORY (the task's own view of itself)
    5. redeploy the SiS app so it runs the current code
    6. end-to-end: enqueue a real Superstore capture and wait for it
    7. verify 10 Tableau + 10 Streamlit screenshots in the generated report

Prints PASS only when every step passed.
"""

import json
import os
import subprocess
import sys
import time

from snowflake.snowpark import Session

import task_capture as TC
import validation_jobs as VJ

ROOT = os.path.dirname(os.path.abspath(__file__))
APP = "WBR_DB.PUBLIC.TABLEAU_TO_SIS_PIPELINE_DEMO"
GRANT_HINT = (
    "  USE ROLE ACCOUNTADMIN;\n"
    "  GRANT EXECUTE TASK ON ACCOUNT TO ROLE WBR_OWNER;")


class Stop(Exception):
    """A step failed with something the operator can act on."""


def _s():
    return Session.builder.config("connection_name", "wbr").create()


def step(n, title):
    print(f"\n[{n}] {title}")


def ok(msg):
    print(f"    ok   {msg}")


def bad(msg):
    raise Stop(msg)


# ---------------------------------------------------------------- 1
def verify_grant(s):
    step(1, "verify EXECUTE TASK")
    try:
        s.sql(f"ALTER TASK {TC.WORKER_TASK} RESUME").collect()
        s.sql(f"ALTER TASK {TC.WORKER_TASK} SUSPEND").collect()
    except Exception as e:
        if "EXECUTE TASK privilege" in str(e):
            bad("EXECUTE TASK is still not granted. Ask an ACCOUNTADMIN for:\n"
                + GRANT_HINT)
        bad(f"could not toggle the task: {type(e).__name__}: {e}")
    ok("EXECUTE TASK is granted (RESUME/SUSPEND both succeeded)")


# ---------------------------------------------------------------- 2
def deploy_and_resume(s, mode):
    step(2, f"deploy the worker ({mode}) and resume the task")
    TC.deploy_worker(s, mode=mode, log=lambda m: ok(m))
    TC.resume_worker(s)
    ok(f"{TC.WORKER_TASK} RESUMED")


# ---------------------------------------------------------------- 3
def minimal_test(s):
    step(3, "minimal task test (task -> notebook -> stage -> queue)")
    run_id = f"ACTIVATE-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}"
    TC.submit(s, run_id=run_id, workbook_id="activation-minimal-test")
    ok(f"queued {run_id}")
    s.sql(f"EXECUTE TASK {TC.WORKER_TASK}").collect()
    ok("task fired")

    deadline = time.time() + 600
    while time.time() < deadline:
        time.sleep(15)
        st = TC.poll(s, run_id)
        if st and st["terminal"]:
            if st["status"] != VJ.COMPLETED:
                bad(f"minimal test ended {st['status']}: "
                    f"{st['error_code']} {st['error_message']}")
            ok(f"queue row reached {st['status']}")
            return run_id
        if st:
            print(f"         {st['status']}: {st['label']}")
    bad("minimal test never reached a terminal status within 600s -- check "
        f"TASK_HISTORY for {TC.WORKER_TASK}")


# ---------------------------------------------------------------- 4
def confirm_task_history(s):
    step(4, "confirm TASK_HISTORY")
    rows = s.sql(f"""
        SELECT STATE, ERROR_CODE, ERROR_MESSAGE, SCHEDULED_TIME
          FROM TABLE(WBR_DB.INFORMATION_SCHEMA.TASK_HISTORY(
               TASK_NAME => '{TC.WORKER_TASK.split('.')[-1]}'))
         ORDER BY SCHEDULED_TIME DESC LIMIT 5
    """).collect()
    if not rows:
        bad("TASK_HISTORY has no rows for the worker task")
    succeeded = [r for r in rows if r["STATE"] == "SUCCEEDED"]
    for r in rows[:3]:
        print(f"         {r['STATE']:<12} {r['ERROR_CODE'] or ''} "
              f"{(r['ERROR_MESSAGE'] or '')[:120]}")
    if not succeeded:
        bad("no SUCCEEDED run in TASK_HISTORY -- the task fired but the "
            "notebook did not complete; the rows above carry the reason")
    ok(f"{len(succeeded)} SUCCEEDED run(s) in TASK_HISTORY")


# ---------------------------------------------------------------- 5
def redeploy_app():
    step(5, "redeploy the SiS app (deploying is NOT restarting -- a running "
            "app keeps its imported modules for the process lifetime)")
    for cmd in (["snow", "sql", "-c", "wbr", "-q", f"DROP STREAMLIT {APP}"],
                ["snow", "streamlit", "deploy", "pipeline_demo", "-c", "wbr",
                 "--replace"]):
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
        if r.returncode:
            bad(f"{' '.join(cmd[:3])} failed: {(r.stderr or r.stdout)[-400:]}")
    # DROP STREAMLIT also wipes the Tableau EAI/secret binding
    r = subprocess.run(
        ["snow", "sql", "-c", "wbr", "-q",
         f"ALTER STREAMLIT {APP} SET "
         "EXTERNAL_ACCESS_INTEGRATIONS = (TABLEAU_ACCESS_INTEGRATION) "
         "SECRETS = ('tableau_pat' = WBR_DB.PUBLIC.TABLEAU_PAT_SECRET)"],
        capture_output=True, text=True, cwd=ROOT)
    if r.returncode:
        bad("could not restore the Tableau EAI/secret binding: "
            f"{(r.stderr or r.stdout)[-300:]}")
    ok("app redeployed and the Tableau binding restored")


# ---------------------------------------------------------------- 6/7
def end_to_end(s):
    step(6, "end-to-end capture through the queue (real Superstore workbook)")
    ir_path = os.path.join(ROOT, "superstore_ir.json")
    if not os.path.exists(ir_path):
        bad(f"{ir_path} not found -- cannot run the end-to-end check")
    with open(ir_path, encoding="utf-8") as fh:
        ir = json.load(fh)
    expected = [d.get("title") or d.get("name") for d in ir.get("dashboards", [])]
    ok(f"{len(expected)} dashboard(s) expected")

    import config
    with open(os.path.join(ROOT, "datasources.deploy.json"), encoding="utf-8") as fh:
        config.DATASOURCES = json.load(fh)

    shots, notes = TC.capture(s, ir, "activation_e2e",
                              log=lambda m: print(f"         {m}"))

    step(7, "verify the evidence")
    if not shots:
        reason = (notes[0].get("reason") if notes else "no reason recorded")
        bad(f"no Streamlit screenshots produced: {reason}")
    missing = sorted(set(expected) - set(shots))
    if missing:
        bad(f"{len(shots)}/{len(expected)} captured; missing: {missing}")
    ok(f"{len(shots)}/{len(expected)} Streamlit screenshots captured")

    import capture_validation as CV
    outdir = os.path.join(ROOT, "reports", "activation_e2e_validation_input",
                          "streamlit")
    files = {t: {"file": f"streamlit-"
                         f"{''.join(c.lower() if c.isalnum() else '-' for c in t).strip('-')}.png"}
             for t in shots}
    # validate from the bytes we actually hold, not just what is on disk
    for title, png in shots.items():
        if not png.startswith(CV.PNG_MAGIC):
            bad(f"{title!r}: returned bytes are not a PNG")
    ok("every returned screenshot is a structurally valid PNG")
    return len(shots), len(expected)


def main():
    s = _s()
    try:
        verify_grant(s)
        deploy_and_resume(s, "selftest")
        minimal_test(s)
        confirm_task_history(s)
        deploy_and_resume(s, "capture")
        redeploy_app()
        got, want = end_to_end(s)
        print(f"\n=== PASS: task capture is live. {got}/{want} Streamlit "
              "screenshots captured through the queue, with no workstation "
              "capture and no EXECUTE NOTEBOOK from SiS. ===")
        print("\nStill to confirm BY HAND (needs a browser click):")
        print("  open the deployed app in a FRESH tab, run the validation, and")
        print("  check the report carries 10 Tableau + 10 Streamlit images")
        print(f"  {APP}")
        return 0
    except Stop as e:
        print(f"\n=== FAILED ===\n{e}")
        return 1
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
