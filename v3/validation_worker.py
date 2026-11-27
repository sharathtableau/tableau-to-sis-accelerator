"""Containerised visual-validation worker.

Polls VALIDATION_CAPTURE_JOBS, claims a job atomically, and does the ONE thing
Streamlit in Snowflake cannot: start the generated app and photograph it with a
real browser.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
It does not reimplement capture, comparison or reporting. It calls
`app_screenshot.capture_app` and the existing report modules, exactly as the
in-app button and the workstation CLI do. A second browser implementation
would drift from the first and the two would disagree about what a migration
proved -- and a screenshot is evidence, so disagreement is not cosmetic.

AUTHENTICATION
--------------
Never externalbrowser, never automated SSO. One non-interactive mechanism:
key-pair (SNOWFLAKE_PRIVATE_KEY / _PATH) or an SPCS service identity via the
injected OAuth token. Secrets arrive as environment/mounted secrets and are
never written into the bundle, the stage, the report or the logs.

Run:  python validation_worker.py --once      (drain one job, then exit)
      python validation_worker.py             (poll forever)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import traceback

import validation_jobs as VJ

POLL_SECONDS = float(os.environ.get("VALIDATION_POLL_SECONDS", "5"))
JOB_TIMEOUT_SECONDS = float(os.environ.get("VALIDATION_JOB_TIMEOUT", "900"))


def worker_id() -> str:
    return os.environ.get("VALIDATION_WORKER_ID") or f"{socket.gethostname()}:{os.getpid()}"


# ------------------------------------------------------------------ session
def open_session():
    """A Snowpark session with NO interactive step.

    Order is deliberate: an SPCS service identity is preferred because the
    platform rotates it and nothing is stored by us at all; key-pair is the
    portable fallback for a plain container/VM.
    """
    from snowflake.snowpark import Session

    token_path = "/snowflake/session/token"
    if os.path.exists(token_path):
        # SPCS service identity -- the platform injects and rotates this.
        with open(token_path, encoding="utf-8") as fh:
            token = fh.read()
        return Session.builder.configs({
            "host": os.environ["SNOWFLAKE_HOST"],
            "account": os.environ["SNOWFLAKE_ACCOUNT"],
            "token": token,
            "authenticator": "oauth",
            "database": os.environ.get("SNOWFLAKE_DATABASE", "WBR_DB"),
            "schema": os.environ.get("SNOWFLAKE_SCHEMA", "PIPELINE_DEMO"),
            "warehouse": os.environ.get("SNOWFLAKE_WAREHOUSE", "POWERHOUSE"),
            "role": os.environ.get("SNOWFLAKE_ROLE", "VALIDATION_WORKER_ROLE"),
        }).create()

    key = os.environ.get("SNOWFLAKE_PRIVATE_KEY")
    key_path = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH")
    if not key and key_path:
        with open(key_path, "rb") as fh:
            key = fh.read().decode()
    if not key:
        raise RuntimeError(
            "no non-interactive credential: expected an SPCS token at "
            "/snowflake/session/token, or SNOWFLAKE_PRIVATE_KEY(_PATH). "
            "externalbrowser is deliberately NOT supported here -- a worker "
            "must never require a human at a browser.")
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization
    passphrase = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")
    pkey = serialization.load_pem_private_key(
        key.encode() if isinstance(key, str) else key,
        password=passphrase.encode() if passphrase else None,
        backend=default_backend())
    der = pkey.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption())
    return Session.builder.configs({
        "account": os.environ["SNOWFLAKE_ACCOUNT"],
        "user": os.environ["SNOWFLAKE_USER"],
        "private_key": der,
        "database": os.environ.get("SNOWFLAKE_DATABASE", "WBR_DB"),
        "schema": os.environ.get("SNOWFLAKE_SCHEMA", "PIPELINE_DEMO"),
        "warehouse": os.environ.get("SNOWFLAKE_WAREHOUSE", "POWERHOUSE"),
        "role": os.environ.get("SNOWFLAKE_ROLE", "VALIDATION_WORKER_ROLE"),
    }).create()


def log(run_id, message):
    """Structured, greppable, and never carrying a secret -- the worker only
    ever logs ids, counts and stated reasons."""
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "worker": worker_id(), "run_id": run_id,
                      "msg": message}), flush=True)


# --------------------------------------------------------------- one job
def process_job(session, run_id: str, work_root: str) -> None:
    """Capture one run. Every early return records a STATED reason."""
    me = worker_id()
    job_dir = os.path.join(work_root, run_id)
    inp = os.path.join(job_dir, "input")
    out = os.path.join(job_dir, "output")
    os.makedirs(out, exist_ok=True)
    started = time.time()

    def beat(status=None, label=None, done=None, total=None):
        if time.time() - started > JOB_TIMEOUT_SECONDS:
            raise TimeoutError(f"job exceeded {JOB_TIMEOUT_SECONDS:.0f}s")
        return VJ.heartbeat(session, run_id, me, status, label, done, total)

    try:
        beat(VJ.CLAIMED, "Downloading validation bundle")
        VJ.download_bundle(session, run_id, inp)

        # HASH GATE. A screenshot of code that is not the validated code is
        # not evidence about this migration, so a mismatch is fatal, never a
        # warning.
        problems = VJ.Bundle().verify(inp)
        if problems:
            VJ.fail(session, run_id, "HASH_MISMATCH", "; ".join(problems[:5]))
            log(run_id, f"hash gate failed: {problems[:3]}")
            return

        with open(os.path.join(inp, "dashboards.json"), encoding="utf-8") as fh:
            dashboards = json.load(fh)
        app_path = os.path.join(inp, "app.py")

        if not beat(VJ.STARTING_APP, "Starting generated application"):
            VJ.fail(session, run_id, "CANCELLED", "cancelled before capture")
            return

        # The app must answer its own queries against the SAME data the
        # validation used, through the worker's own non-interactive session.
        import backend
        backend.set_session(session)

        import app_screenshot as APS
        ok, why = APS.available()
        if not ok:
            # In this container that means the image is wrong, not that the
            # environment is unsupported -- so it is retryable infrastructure.
            VJ.requeue_or_fail(session, run_id, "CHROMIUM_STARTUP", why)
            log(run_id, f"browser unavailable: {why}")
            return

        beat(VJ.CAPTURING, "Capturing dashboards", 0, len(dashboards))
        shots, notes = APS.capture_app(app_path)
        log(run_id, f"captured {len(shots)} of {len(dashboards)} dashboards")

        # Match captures to the STABLE ids from the manifest. Matching on the
        # displayed title alone breaks on a duplicate or renamed title, and a
        # screenshot filed under the wrong dashboard is worse than none.
        by_title = {str(d.get("title") or d.get("name")): d for d in dashboards}
        results, images = [], {}
        for title, png in shots.items():
            spec = by_title.get(title)
            if spec is None:
                results.append({"title": title, "captured": False,
                                "reason": "captured a tab that is not in the "
                                          "dashboard manifest -- not attributed"})
                continue
            name = f"streamlit-{spec['id']}.png"
            with open(os.path.join(out, name), "wb") as fh:
                fh.write(png)
            images[spec["id"]] = name
            results.append({"id": spec["id"], "title": title, "captured": True,
                            "file": name, "bytes": len(png),
                            "sha256": VJ.sha256_bytes(png)})
        for note in notes:
            if not note.get("captured"):
                results.append({"title": note.get("dashboard"),
                                "captured": False,
                                "reason": note.get("reason")})

        beat(VJ.GENERATING_REPORT, "Generating validation report",
             len(images), len(dashboards))

        manifest = {
            "run_id": run_id, "worker_id": me,
            "captured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "dashboards_total": len(dashboards),
            "dashboards_captured": len(images),
            "streamlit_images": images,
            "results": results,
            "capture_seconds": round(time.time() - started, 1),
        }
        with open(os.path.join(out, "result_manifest.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=1)

        for name in sorted(os.listdir(out)):
            session.file.put(os.path.join(out, name).replace("\\", "/"),
                             VJ.output_prefix(run_id),
                             auto_compress=False, overwrite=True)

        if not images:
            # Captured nothing: BLOCKED, never FAILED -- "no evidence" is not
            # "proven wrong", and it must never read as a pass either.
            VJ.fail(session, run_id, "NO_CAPTURE",
                    "no dashboard could be photographed; see result_manifest",
                    blocked=True)
            return

        VJ.complete(session, run_id,
                    f"{VJ.output_prefix(run_id)}/result_manifest.json",
                    f"{VJ.output_prefix(run_id)}/result_manifest.json")
        log(run_id, f"completed with {len(images)} screenshot(s)")
    except TimeoutError as exc:
        VJ.requeue_or_fail(session, run_id, "WORKER_LOST", str(exc))
        log(run_id, f"timeout: {exc}")
    except Exception as exc:                                    # noqa: BLE001
        VJ.requeue_or_fail(session, run_id, _classify(exc),
                           f"{type(exc).__name__}: {exc}")
        log(run_id, f"failed: {type(exc).__name__}: {exc}\n{traceback.format_exc()[:800]}")
    finally:
        # Leave nothing behind: a stale Streamlit process holds its port and
        # serves the NEXT run stale code.
        shutil.rmtree(job_dir, ignore_errors=True)


def _classify(exc: Exception) -> str:
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if "playwright" in text or "chromium" in text or "browser" in text:
        return "CHROMIUM_STARTUP"
    if "stage" in text or "put" in text and "file" in text:
        return "STAGE_IO"
    if "snowflake" in name or "operational" in name or "connection" in text:
        return "SNOWFLAKE_UNAVAILABLE"
    return "APP_STARTUP"          # deterministic -> not retried forever


# ------------------------------------------------------------------- loop
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Visual-validation capture worker")
    ap.add_argument("--once", action="store_true",
                    help="process at most one job, then exit")
    ap.add_argument("--work-dir", default=os.environ.get("VALIDATION_WORK_DIR")
                    or tempfile.gettempdir())
    args = ap.parse_args(argv)

    me = worker_id()
    try:
        session = open_session()
    except Exception as exc:                                    # noqa: BLE001
        print(f"worker cannot authenticate: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2
    log(None, f"worker {me} online")

    try:
        while True:
            try:
                VJ.recover_stale(session)
                run_id = VJ.claim_next(session, me)
            except Exception as exc:                            # noqa: BLE001
                log(None, f"queue unavailable: {type(exc).__name__}: {exc}")
                time.sleep(POLL_SECONDS * 2)
                continue
            if run_id:
                log(run_id, "claimed")
                process_job(session, run_id, args.work_dir)
                if args.once:
                    return 0
            elif args.once:
                log(None, "no queued job")
                return 0
            else:
                time.sleep(POLL_SECONDS)
    finally:
        try:
            session.close()
        except Exception:                                       # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
