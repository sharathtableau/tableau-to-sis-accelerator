"""One-time setup + the MINIMAL TASK TEST for the task-driven capture path.

    python setup_task_capture.py setup     # create queue table, stage, notebook, task
    python setup_task_capture.py test      # run the minimal end-to-end chain test
    python setup_task_capture.py teardown  # suspend the task (leaves history intact)

THE MINIMAL TEST (what `test` proves, and why each step is there)

    1. INSERT one QUEUED row              -- exactly what the SiS button does
    2. EXECUTE TASK                       -- fires the task immediately instead
                                             of waiting for its 1-minute tick
    3. task runs EXECUTE NOTEBOOK         -- THE step that fails from SiS; this
                                             is the whole point of the test
    4. notebook claims the row atomically -- proves the queue contract
    5. notebook writes capture_manifest   -- proves stage write from the task
    6. read the row + the manifest back   -- proves the SiS-visible round trip

It deliberately does NOT capture screenshots. A 160-second Playwright run in
the middle would make a plumbing failure and a capture failure look the same,
and the plumbing is the only thing in question at this stage.

REQUIRES the one-time account grant (ACCOUNTADMIN):
    GRANT EXECUTE TASK ON ACCOUNT TO ROLE WBR_OWNER;
Without it, step 2 fails with:
    091089 (23001): Cannot execute task, EXECUTE TASK privilege must be
    granted to owner role
"""

import sys
import time

from snowflake.snowpark import Session

import task_capture as TC
import validation_jobs as VJ

DDL = [
    ("queue table", f"""
        CREATE TABLE IF NOT EXISTS {TC.TABLE} (
            RUN_ID STRING NOT NULL PRIMARY KEY,
            WORKBOOK_ID STRING,
            STATUS STRING NOT NULL,
            CREATED_AT TIMESTAMP_LTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
            STARTED_AT TIMESTAMP_LTZ,
            COMPLETED_AT TIMESTAMP_LTZ,
            HEARTBEAT_AT TIMESTAMP_LTZ,
            ATTEMPT NUMBER NOT NULL DEFAULT 0,
            APP_ARTIFACT_PATH STRING,
            INPUT_MANIFEST_PATH STRING,
            RESULT_MANIFEST_PATH STRING,
            REPORT_PATH STRING,
            ERROR_CODE STRING,
            ERROR_MESSAGE STRING,
            WORKER_ID STRING,
            PROGRESS_LABEL STRING,
            PROGRESS_DONE NUMBER,
            PROGRESS_TOTAL NUMBER,
            CANCEL_REQUESTED BOOLEAN NOT NULL DEFAULT FALSE
        )"""),
    ("stage", f"""
        CREATE STAGE IF NOT EXISTS {TC.STAGE.lstrip('@')}
            ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')
            DIRECTORY = (ENABLE = TRUE)"""),
]


def _session():
    return Session.builder.config("connection_name", "wbr").create()


def setup():
    s = _session()
    for label, stmt in DDL:
        s.sql(stmt).collect()
        print(f"  ok  {label}")
    TC.deploy_worker(s, mode="selftest", log=lambda m: print(f"  ok  {m}"))
    try:
        TC.resume_worker(s)
        print(f"  ok  task resumed")
    except Exception as e:
        print(f"  !!  could not RESUME the task: {str(e)[:200]}")
        print("      (needs: GRANT EXECUTE TASK ON ACCOUNT TO ROLE WBR_OWNER)")
    s.close()
    print("\nsetup complete")


def test():
    s = _session()
    run_id = f"TASKTEST-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}"
    print(f"run_id = {run_id}\n")

    print("1. INSERT one QUEUED row (what the SiS button does)")
    TC.submit(s, run_id=run_id, workbook_id="minimal-task-test")
    st = TC.poll(s, run_id)
    print(f"   -> status={st['status']}  label={st['label']}")
    assert st["status"] == VJ.QUEUED, st

    print("2. EXECUTE TASK (fire it now instead of waiting for the tick)")
    try:
        s.sql(f"EXECUTE TASK {TC.WORKER_TASK}").collect()
        print("   -> submitted")
    except Exception as e:
        msg = str(e)
        print(f"   -> FAILED: {msg[:300]}")
        if "EXECUTE TASK privilege" in msg:
            print("\n   BLOCKED ON THE GRANT. Ask an ACCOUNTADMIN to run:")
            print("     USE ROLE ACCOUNTADMIN;")
            print("     GRANT EXECUTE TASK ON ACCOUNT TO ROLE WBR_OWNER;")
        s.close()
        return 1

    print("3-5. waiting for the task -> notebook -> stage chain")
    deadline = time.time() + 600
    final = None
    while time.time() < deadline:
        time.sleep(15)
        st = TC.poll(s, run_id)
        print(f"   status={st['status']:<12} label={st['label']}")
        if st["terminal"]:
            final = st
            break
    if final is None:
        print("\n   TIMED OUT waiting for a terminal status")
        _dump_task_history(s)
        s.close()
        return 1

    print("6. reading the manifest back from the stage")
    ok = final["status"] == VJ.COMPLETED
    if ok:
        import os, tempfile, json
        d = tempfile.mkdtemp()
        try:
            s.file.get(f"{TC.STAGE}/{run_id}/output/", d)
            p = os.path.join(d, "capture_manifest.json")
            if os.path.exists(p):
                print("   manifest:", json.dumps(json.load(open(p)), indent=1)[:500])
            else:
                print("   !! manifest not found on the stage")
                ok = False
        except Exception as e:
            print(f"   !! could not download: {str(e)[:200]}")
            ok = False

    print(f"\n=== MINIMAL TASK TEST: {'PASSED' if ok else 'FAILED'} ===")
    if not ok:
        print(f"    status={final['status']} code={final['error_code']}")
        print(f"    message={final['error_message']}")
        _dump_task_history(s)
    s.close()
    return 0 if ok else 1


def _dump_task_history(s):
    print("\n   task history:")
    try:
        rows = s.sql(f"""
            SELECT STATE, ERROR_CODE, ERROR_MESSAGE, SCHEDULED_TIME
              FROM TABLE(WBR_DB.INFORMATION_SCHEMA.TASK_HISTORY(
                   TASK_NAME => '{TC.WORKER_TASK.split('.')[-1]}'))
             ORDER BY SCHEDULED_TIME DESC LIMIT 5
        """).collect()
        for r in rows:
            print(f"     {r['STATE']:<12} {r['ERROR_CODE']} "
                  f"{(r['ERROR_MESSAGE'] or '')[:180]}")
    except Exception as e:
        print("     (unavailable)", str(e)[:150])


def teardown():
    s = _session()
    try:
        TC.suspend_worker(s)
        print("task suspended")
    except Exception as e:
        print("suspend warn:", str(e)[:150])
    s.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "test"
    sys.exit({"setup": setup, "test": test, "teardown": teardown}[cmd]() or 0)
