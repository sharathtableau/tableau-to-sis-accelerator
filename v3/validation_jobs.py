"""Job queue + artifact bundle for AUTOMATED visual validation.

ONE module, imported by BOTH sides -- the SiS accelerator that submits a job
and the containerised worker that runs it. Deliberately not two
implementations: the whole point of hashing the bundle is that both ends agree
byte-for-byte on what was validated, and two copies of the queue/bundle logic
would be the first thing to disagree.

WHY A QUEUE AT ALL
------------------
Streamlit in Snowflake cannot run Playwright, Chromium, or a second Streamlit
server, so the screenshot step must execute somewhere with a browser. The
observed Superstore capture takes ~151s, which is far too long to hold an
interactive SiS request open. So: SiS uploads a bundle, inserts one QUEUED
row, and returns immediately; a worker claims the row, captures, and writes
the results back. The user clicks once.

NOTHING HERE IMPORTS STREAMLIT OR PLAYWRIGHT. It runs identically in the SiS
sandbox (which has neither) and in the worker container.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import uuid

# The statuses the queue may hold. Ordered roughly by lifecycle so a UI can
# render progress; BLOCKED is terminal-but-not-a-pass, exactly as in the pack.
QUEUED = "QUEUED"
CLAIMED = "CLAIMED"
STARTING_APP = "STARTING_APP"
CAPTURING = "CAPTURING"
GENERATING_REPORT = "GENERATING_REPORT"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
BLOCKED = "BLOCKED"

ACTIVE_STATUSES = (CLAIMED, STARTING_APP, CAPTURING, GENERATING_REPORT)
TERMINAL_STATUSES = (COMPLETED, FAILED, BLOCKED)

TABLE = "WBR_DB.PIPELINE_DEMO.VALIDATION_CAPTURE_JOBS"
STAGE = "@WBR_DB.PIPELINE_DEMO.VALIDATION_CAPTURE_STAGE"

# A worker that stops heart-beating for this long is presumed dead and its job
# is recoverable. Comfortably longer than the ~151s capture plus startup, so a
# slow-but-alive worker is never stolen from.
STALE_HEARTBEAT_SECONDS = 180
HEARTBEAT_SECONDS = 15
MAX_ATTEMPTS = 3

# Failures worth retrying are INFRASTRUCTURE failures. A deterministic
# application failure (the generated app raises on every start, a dashboard
# never renders) will fail identically forever, and retrying it just burns a
# compute pool and delays the honest BLOCKED the user needs to see.
RETRYABLE_ERROR_CODES = frozenset({
    "WORKER_LOST", "SNOWFLAKE_UNAVAILABLE", "STAGE_IO", "CHROMIUM_STARTUP",
})


# --------------------------------------------------------------- hashing
def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def new_run_id() -> str:
    """A run id that sorts by time and is safe in a stage path and a filename."""
    import datetime
    stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    return f"VAL-{stamp}-{uuid.uuid4().hex[:8]}"


# --------------------------------------------------------------- bundle
def stage_prefix(run_id: str) -> str:
    return f"{STAGE}/{run_id}"


def input_prefix(run_id: str) -> str:
    return f"{stage_prefix(run_id)}/input"


def output_prefix(run_id: str) -> str:
    return f"{stage_prefix(run_id)}/output"


class Bundle:
    """The exact bytes the worker must reproduce a capture from.

    Every artifact is hashed and the manifest records the hashes, so the
    worker can prove it captured THE app that was validated -- not a
    regenerated near-copy. A hash mismatch is a hard failure, never a warning:
    a screenshot of different code is not evidence about this migration.
    """

    MANIFEST = "input_manifest.json"

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.meta: dict[str, object] = {}

    def add(self, name: str, data: bytes | str) -> None:
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.files[name] = data

    def add_json(self, name: str, obj) -> None:
        self.add(name, json.dumps(obj, indent=1, default=str))

    def manifest(self, run_id: str, workbook_id: str | None = None) -> dict:
        return {
            "run_id": run_id,
            "workbook_id": workbook_id,
            "created_utc": _utc_now(),
            "artifacts": {name: {"sha256": sha256_bytes(data),
                                 "bytes": len(data)}
                          for name, data in sorted(self.files.items())},
            **self.meta,
        }

    def finalize(self, run_id: str, workbook_id: str | None = None) -> dict:
        """Attach the manifest to the bundle and return it.

        The manifest is added LAST and is not itself hashed inside itself --
        it is the authority the worker verifies everything else against.
        """
        manifest = self.manifest(run_id, workbook_id)
        self.add_json(self.MANIFEST, manifest)
        return manifest

    def verify(self, root: str) -> list[str]:
        """Re-hash every artifact on disk against the manifest. Returns the
        list of problems -- empty means the worker holds exactly what SiS
        uploaded."""
        manifest_path = os.path.join(root, self.MANIFEST)
        if not os.path.exists(manifest_path):
            return [f"missing {self.MANIFEST}"]
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        problems = []
        for name, entry in (manifest.get("artifacts") or {}).items():
            path = os.path.join(root, name.replace("/", os.sep))
            if not os.path.exists(path):
                problems.append(f"missing artifact {name}")
                continue
            actual = sha256_file(path)
            if actual != entry.get("sha256"):
                problems.append(
                    f"hash mismatch for {name}: manifest {entry.get('sha256')[:12]}… "
                    f"!= actual {actual[:12]}…")
        return problems


def build_capture_bundle(run_id, ir, app_source, dashboards, spec=None,
                         tableau_shots=None, tableau_rows=None,
                         engine_version=None, data_snapshot=None,
                         workbook_id=None):
    """Everything the worker needs, hashed.

    `dashboards` carries the STABLE ids the worker selects tabs by -- matching
    on a display title alone breaks the moment a title is duplicated or
    renamed, and a screenshot attributed to the wrong dashboard is worse than
    a missing one.
    """
    bundle = Bundle()
    bundle.add("app.py", app_source)
    bundle.add_json("workbook_ir.json", ir)
    bundle.add_json("dashboards.json", dashboards)
    if spec is not None:
        bundle.add_json("validation_spec.json", spec)
    if tableau_rows:
        bundle.add_json("tableau_rows.json", tableau_rows)
    for title, png in (tableau_shots or {}).items():
        bundle.add(f"tableau/{_safe(title)}.png", png)
    bundle.meta["engine_version"] = engine_version
    # The data the app must query. Comparing a Tableau image against a
    # Streamlit image drawn from a different data state is not a comparison.
    bundle.meta["data_snapshot"] = data_snapshot or {}
    manifest = bundle.finalize(run_id, workbook_id)
    return bundle, manifest


def _safe(value: str) -> str:
    return "".join(c.lower() if c.isalnum() else "-" for c in str(value)).strip("-")


def _utc_now() -> str:
    import datetime
    return datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"


# --------------------------------------------------------------- stage I/O
def upload_bundle(session, run_id: str, bundle: Bundle, tmp_root: str) -> str:
    """Write the bundle to a temp dir and PUT it to the run's input prefix.

    A SPACE-FREE temp dir is used deliberately: Snowflake's PUT takes a URI,
    and a path containing spaces breaks it -- this project has hit that
    exact bug before on Windows paths.
    """
    local = os.path.join(tmp_root, run_id, "input")
    os.makedirs(local, exist_ok=True)
    for name, data in bundle.files.items():
        target = os.path.join(local, name.replace("/", os.sep))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(data)
    for name in bundle.files:
        target = os.path.join(local, name.replace("/", os.sep))
        sub = posixpath.dirname(name)
        dest = f"{input_prefix(run_id)}/{sub}" if sub else input_prefix(run_id)
        session.file.put(target.replace("\\", "/"), dest,
                         auto_compress=False, overwrite=True)
    return input_prefix(run_id)


def download_bundle(session, run_id: str, dest_root: str) -> str:
    os.makedirs(dest_root, exist_ok=True)
    session.file.get(f"{input_prefix(run_id)}/", dest_root)
    return dest_root


# --------------------------------------------------------------- queue ops
def _sql(session, statement: str, params=None):
    return session.sql(statement, params or []).collect()


def enqueue(session, run_id, workbook_id=None, app_artifact_path=None,
            input_manifest_path=None):
    """Insert exactly one QUEUED row. Idempotent on RUN_ID: re-submitting the
    same run must not create a second job (the SiS page can rerun on any
    widget interaction, and a duplicate row would double-book a worker)."""
    _sql(session, f"""
        INSERT INTO {TABLE}
            (RUN_ID, WORKBOOK_ID, STATUS, APP_ARTIFACT_PATH, INPUT_MANIFEST_PATH,
             PROGRESS_LABEL, PROGRESS_DONE, PROGRESS_TOTAL)
        SELECT ?, ?, '{QUEUED}', ?, ?, 'Queued for visual capture', 0, 0
        WHERE NOT EXISTS (SELECT 1 FROM {TABLE} WHERE RUN_ID = ?)
    """, [run_id, workbook_id, app_artifact_path, input_manifest_path, run_id])
    return run_id


def claim_next(session, worker_id: str):
    """Atomically take the oldest QUEUED job. Returns its RUN_ID or None.

    The `AND STATUS = QUEUED` predicate is what makes this safe: Snowflake
    serialises conflicting DML on the same row, so of two workers racing for
    the same job exactly one UPDATE matches a row and the other matches zero.
    Claiming by SELECT-then-UPDATE would let both win.
    """
    rows = _sql(session, f"""
        UPDATE {TABLE}
           SET STATUS = '{CLAIMED}',
               WORKER_ID = ?,
               STARTED_AT = CURRENT_TIMESTAMP(),
               HEARTBEAT_AT = CURRENT_TIMESTAMP(),
               ATTEMPT = ATTEMPT + 1,
               PROGRESS_LABEL = 'Claimed by worker'
         WHERE RUN_ID = (
                 SELECT RUN_ID FROM {TABLE}
                  WHERE STATUS = '{QUEUED}' AND CANCEL_REQUESTED = FALSE
                  ORDER BY CREATED_AT
                  LIMIT 1)
           AND STATUS = '{QUEUED}'
    """, [worker_id])
    updated = rows[0][0] if rows and rows[0] else 0
    if not updated:
        return None
    got = _sql(session, f"""
        SELECT RUN_ID FROM {TABLE}
         WHERE WORKER_ID = ? AND STATUS = '{CLAIMED}'
         ORDER BY STARTED_AT DESC LIMIT 1
    """, [worker_id])
    return got[0][0] if got else None


def heartbeat(session, run_id, worker_id, status=None, label=None,
              done=None, total=None):
    """Prove the worker is alive and report progress. Returns True while the
    job should keep going, False once a cancel has been requested."""
    sets = ["HEARTBEAT_AT = CURRENT_TIMESTAMP()"]
    params = []
    if status:
        sets.append("STATUS = ?")
        params.append(status)
    if label is not None:
        sets.append("PROGRESS_LABEL = ?")
        params.append(label)
    if done is not None:
        sets.append("PROGRESS_DONE = ?")
        params.append(done)
    if total is not None:
        sets.append("PROGRESS_TOTAL = ?")
        params.append(total)
    params.extend([run_id, worker_id])
    _sql(session, f"UPDATE {TABLE} SET {', '.join(sets)} "
                  f"WHERE RUN_ID = ? AND WORKER_ID = ?", params)
    rows = _sql(session, f"SELECT CANCEL_REQUESTED FROM {TABLE} WHERE RUN_ID = ?",
                [run_id])
    return not (rows and rows[0][0])


def complete(session, run_id, report_path, result_manifest_path):
    _sql(session, f"""
        UPDATE {TABLE}
           SET STATUS = '{COMPLETED}', COMPLETED_AT = CURRENT_TIMESTAMP(),
               HEARTBEAT_AT = CURRENT_TIMESTAMP(), REPORT_PATH = ?,
               RESULT_MANIFEST_PATH = ?, PROGRESS_LABEL = 'Validation complete',
               ERROR_CODE = NULL, ERROR_MESSAGE = NULL
         WHERE RUN_ID = ?
    """, [report_path, result_manifest_path, run_id])


def fail(session, run_id, code, message, blocked=False):
    """Terminal failure. BLOCKED is used when evidence could not be produced
    -- never silently upgraded, and never confused with a proven wrong."""
    status = BLOCKED if blocked else FAILED
    _sql(session, f"""
        UPDATE {TABLE}
           SET STATUS = '{status}', COMPLETED_AT = CURRENT_TIMESTAMP(),
               HEARTBEAT_AT = CURRENT_TIMESTAMP(),
               ERROR_CODE = ?, ERROR_MESSAGE = ?,
               PROGRESS_LABEL = ?
         WHERE RUN_ID = ?
    """, [code, str(message)[:2000], f"{status}: {code}", run_id])


def requeue_or_fail(session, run_id, code, message):
    """An infrastructure failure goes back on the queue until MAX_ATTEMPTS; a
    deterministic one fails immediately. Retrying a deterministic failure just
    burns the pool and delays the honest answer."""
    if code not in RETRYABLE_ERROR_CODES:
        fail(session, run_id, code, message)
        return False
    rows = _sql(session, f"SELECT ATTEMPT FROM {TABLE} WHERE RUN_ID = ?", [run_id])
    attempt = rows[0][0] if rows else MAX_ATTEMPTS
    if attempt >= MAX_ATTEMPTS:
        fail(session, run_id, code,
             f"{message} (giving up after {attempt} attempt(s))")
        return False
    _sql(session, f"""
        UPDATE {TABLE}
           SET STATUS = '{QUEUED}', WORKER_ID = NULL,
               ERROR_CODE = ?, ERROR_MESSAGE = ?,
               PROGRESS_LABEL = 'Retrying after an infrastructure failure'
         WHERE RUN_ID = ?
    """, [code, str(message)[:2000], run_id])
    return True


def recover_stale(session, stale_seconds: int = STALE_HEARTBEAT_SECONDS):
    """Return jobs whose worker died to the queue. Bounded by MAX_ATTEMPTS so
    a job that kills every worker it touches cannot loop forever."""
    active = "', '".join(ACTIVE_STATUSES)
    rows = _sql(session, f"""
        UPDATE {TABLE}
           SET STATUS = '{QUEUED}', WORKER_ID = NULL,
               ERROR_CODE = 'WORKER_LOST',
               ERROR_MESSAGE = 'no heartbeat within {stale_seconds}s; requeued',
               PROGRESS_LABEL = 'Requeued after worker loss'
         WHERE STATUS IN ('{active}')
           AND HEARTBEAT_AT < DATEADD('second', -{int(stale_seconds)}, CURRENT_TIMESTAMP())
           AND ATTEMPT < {MAX_ATTEMPTS}
    """)
    return rows[0][0] if rows and rows[0] else 0


def status_of(session, run_id):
    rows = _sql(session, f"""
        SELECT STATUS, PROGRESS_LABEL, PROGRESS_DONE, PROGRESS_TOTAL,
               ERROR_CODE, ERROR_MESSAGE, REPORT_PATH, WORKER_ID, ATTEMPT
          FROM {TABLE} WHERE RUN_ID = ?
    """, [run_id])
    if not rows:
        return None
    r = rows[0]
    return {"status": r[0], "label": r[1], "done": r[2], "total": r[3],
            "error_code": r[4], "error_message": r[5], "report_path": r[6],
            "worker_id": r[7], "attempt": r[8]}


def request_cancel(session, run_id):
    _sql(session, f"UPDATE {TABLE} SET CANCEL_REQUESTED = TRUE WHERE RUN_ID = ?",
         [run_id])


__all__ = [
    "Bundle", "build_capture_bundle", "claim_next", "complete", "download_bundle",
    "enqueue", "fail", "heartbeat", "input_prefix", "new_run_id",
    "output_prefix", "recover_stale", "request_cancel", "requeue_or_fail",
    "sha256_bytes", "sha256_file", "stage_prefix", "status_of", "upload_bundle",
    "QUEUED", "CLAIMED", "STARTING_APP", "CAPTURING", "GENERATING_REPORT",
    "COMPLETED", "FAILED", "BLOCKED", "ACTIVE_STATUSES", "TERMINAL_STATUSES",
    "MAX_ATTEMPTS", "HEARTBEAT_SECONDS", "STALE_HEARTBEAT_SECONDS",
]
