"""Real Streamlit screenshots from INSIDE Snowflake, with no Docker and no
local machine.

WHY THIS EXISTS
---------------
Streamlit in Snowflake cannot run Playwright, Chromium, a second Streamlit
server or a local port, so a pack generated from the deployed app reports the
visual leg BLOCKED for every dashboard. The planned fix was an SPCS service
built from a container image -- but that needs Docker to build and push, which
the workstation does not have.

A Snowflake NOTEBOOK on CONTAINER RUNTIME does have all of it: it runs as root
on Ubuntu, on a compute pool, and can install Playwright and Chromium at run
time. Proven 2026-08-14: chromium 129 screenshotted a real localhost page and
wrote a 3200x1800 PNG to the stage in 21 seconds.

So this module ships the generated app INTO Snowflake, runs it there, and
photographs it there. Nothing is built locally and nothing is downloaded by the
user.

WHAT IT REUSES
--------------
The capture itself is `app_screenshot.capture_app` -- the SAME function the
in-app button and the workstation CLI use. This module only moves code and
files around it. A second browser implementation would drift, and a screenshot
is evidence, so drift is not cosmetic.
"""

from __future__ import annotations

import io
import json
import os
import posixpath
import random
import re
import tempfile
import time
import uuid

STAGE = "@WBR_DB.DBX.VALIDATION_CAPTURE_STAGE"
NOTEBOOK_SCHEMA = "WBR_DB.DBX"
COMPUTE_POOL = "SYSTEM_COMPUTE_POOL_CPU"
WAREHOUSE = "POWERHOUSE"
EAI = "PLAYWRIGHT_EAI"

# ---- retry policy (2026-08-16) ---------------------------------------------
# Added after error 300002:2250622648 (incident 6979790, then 6139508) killed
# two consecutive EXECUTE NOTEBOOK calls in under a second each -- too fast to
# be Chromium, pip or the app, which is the signature of a job-scheduling
# fault on Snowflake's shared SYSTEM_COMPUTE_POOL_CPU, not a bug here. Proven
# transient the same day: an identical call, zero code changes, succeeded
# cleanly minutes later (10/10, byte-identical to the known-good baseline).
MAX_ATTEMPTS = 3
RETRY_DELAYS_S = (10, 30)   # backoff before attempt 2, before attempt 3
JITTER_S = (0, 5)           # extra random delay added on top, each retry

# Structural floor for an accepted screenshot -- NOT a content/blank check.
# MIN_WIDTH is enforced: every real dashboard renders at the app's full
# viewport width regardless of content, so a narrow image is corruption, not
# an empty dashboard. MIN_HEIGHT is deliberately NOT enforced: Sheet 22 (a
# genuinely empty placeholder sheet confirmed against both the IR --
# non_data=True, no dims/measures -- and Tableau's own screenshot of the same
# sheet, which is also blank) captures as a real 2608x66 image, a full-page
# crop of literally nothing. Rejecting it on height would fail every single
# validation run on a dashboard that can never pass, burning all 3 attempts
# for nothing. Kept as a named constant so a future per-dashboard override
# can use it without another investigation.
MIN_WIDTH = 640
MIN_HEIGHT = 360

# The generated app's own runtime. Listed explicitly rather than shipping the
# repo so an unrelated file cannot silently become a runtime dependency of the
# capture -- the same discipline snowflake.yml's artifact list follows.
RUNTIME_MODULES = [
    "engine.py", "backend.py", "config.py", "calc_translator.py",
    "findings.py", "profile_superstore.py", "profile_default.py",
    "app_screenshot.py",
]


def _run_id() -> str:
    return f"CAP-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:6]}"


def available(session) -> tuple[bool, str | None]:
    """(True, None) when a capture can be attempted here.

    Deliberately cheap: it checks the pieces whose absence is a hard stop, so
    a caller never spends minutes discovering the obvious.
    """
    if session is None:
        return False, ("no Snowflake session -- the capture runs as a "
                       "notebook job inside Snowflake and needs one to submit")
    try:
        rows = session.sql(
            f"SHOW COMPUTE POOLS LIKE '{COMPUTE_POOL}'").collect()
    except Exception as exc:                                    # noqa: BLE001
        return False, f"cannot read compute pools: {type(exc).__name__}: {exc}"
    if not rows:
        return False, (f"compute pool {COMPUTE_POOL} is not visible to this "
                       "role, so no container can run the browser")
    return True, None


# The wrapper the generated app runs under, kept OUT of any f-string: it is
# full of braces, and escaping them all is how this broke the first time.
_WRAPPER = """
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
    "schema": os.environ.get("SNOWFLAKE_SCHEMA", "DBX"),
    "warehouse": os.environ.get("SNOWFLAKE_WAREHOUSE", "POWERHOUSE"),
}).create()
import backend
backend.set_session(_s)
_here = os.path.dirname(os.path.abspath(__file__))
exec(compile(open(os.path.join(_here, "app_generated.py"), encoding="utf-8").read(),
             "app_generated.py", "exec"))
"""


_NOTEBOOK_TEMPLATE = '''
import json, os, subprocess, sys, time, traceback, glob
RUN_ID = "__RUN_ID__"
STAGE  = "__STAGE__"
IN     = STAGE + "/" + RUN_ID + "/input"
OUT    = STAGE + "/" + RUN_ID + "/output"
WORK   = "/tmp/" + RUN_ID
R = {"run_id": RUN_ID, "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
     "steps": [], "dashboards_expected": __NDASH__}

def step(name, fn):
    t0 = time.time()
    try:
        out = fn()
        R["steps"].append({"step": name, "ok": True,
                           "seconds": round(time.time()-t0, 1), "detail": out})
        return out
    except Exception as e:
        R["steps"].append({"step": name, "ok": False,
                           "seconds": round(time.time()-t0, 1),
                           "error": (type(e).__name__ + ": " + str(e))[:900],
                           "trace": traceback.format_exc()[-1200:]})
        raise

def sh(cmd, timeout=2400, env=None):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    if r.returncode:
        raise RuntimeError("rc=" + str(r.returncode) + ": " + (r.stderr or r.stdout)[-700:])
    return (r.stdout or "")[-160:]

from snowflake.snowpark.context import get_active_session
session = get_active_session()

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
step("fetch_bundle", fetch)

WRAPPER = __WRAPPER__

def write_wrapper():
    p = os.path.join(WORK, "app_capture.py")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(WRAPPER)
    return p
app_path = step("write_wrapper", write_wrapper)

def capture():
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/tmp/pw"
    sys.path.insert(0, WORK)
    import app_screenshot as APS
    ok, why = APS.available()
    if not ok:
        raise RuntimeError("browser unavailable in container: " + str(why))
    shots, notes = APS.capture_app(app_path, startup_timeout=600)
    outdir = os.path.join(WORK, "out"); os.makedirs(outdir, exist_ok=True)
    saved = {}
    for title, png in shots.items():
        safe = "".join(c.lower() if c.isalnum() else "-" for c in str(title)).strip("-")
        name = "streamlit-" + safe + ".png"
        with open(os.path.join(outdir, name), "wb") as fh:
            fh.write(png)
        saved[title] = {"file": name, "bytes": len(png)}
    R["capture_notes"] = notes
    if not saved:
        # DIAGNOSE rather than give up. capture_app returns early when it finds
        # no tabs, so the page that actually rendered is never seen. Start the
        # app once more and photograph whatever IS there -- a Streamlit
        # exception page explains itself instantly, and guessing from an empty
        # result does not.
        import subprocess as _sp, socket as _sk, time as _t
        from playwright.sync_api import sync_playwright
        with _sk.socket() as _s0:
            _s0.bind(("127.0.0.1", 0)); dport = _s0.getsockname()[1]
        dlog = open(os.path.join(outdir, "diagnose_server.log"), "wb")
        proc = _sp.Popen([sys.executable, "-m", "streamlit", "run", app_path,
                          "--server.port", str(dport), "--server.headless", "true",
                          "--server.fileWatcherType", "none",
                          "--browser.gatherUsageStats", "false"],
                         stdout=dlog, stderr=_sp.STDOUT,
                         cwd=os.path.dirname(os.path.abspath(app_path)))
        try:
            _t.sleep(12)
            with sync_playwright() as p:
                b = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
                pg = b.new_page(viewport={"width": 1600, "height": 1200})
                pg.goto("http://localhost:%d" % dport, wait_until="networkidle",
                        timeout=60000)
                pg.wait_for_timeout(4000)
                pg.screenshot(path=os.path.join(outdir, "diagnose_page.png"),
                              full_page=True)
                R["diagnose_text"] = pg.inner_text("body")[:4000]
                R["diagnose_tabs"] = pg.locator('button[role="tab"]').count()
                b.close()
        except Exception as de:
            R["diagnose_error"] = (type(de).__name__ + ": " + str(de))[:600]
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except Exception:
                proc.kill()
            dlog.close()
    return {"captured": len(saved), "files": saved}
step("capture_app", capture)

def upload():
    outdir = os.path.join(WORK, "out"); os.makedirs(outdir, exist_ok=True)
    # ALWAYS carry the child app's own log home. When the app fails to render,
    # its traceback is the only thing that explains why, and it lives in a
    # temp dir inside a container that is about to disappear.
    for i, log in enumerate(sorted(glob.glob("/tmp/st_shot_*/server.log"))):
        try:
            with open(log, "r", errors="replace") as fh:
                text = fh.read()[-40000:]
            with open(os.path.join(outdir, "app_server_%d.log" % i), "w",
                      encoding="utf-8") as fh:
                fh.write(text)
            R.setdefault("app_log_tail", text[-3000:])
        except Exception:
            pass
    R["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    R["ok"] = all(s["ok"] for s in R["steps"])
    with open(os.path.join(outdir, "capture_manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(R, fh, indent=1, default=str)
    n = 0
    for p in sorted(glob.glob(outdir + "/*")):
        session.file.put(p, OUT, auto_compress=False, overwrite=True)
        n += 1
    return "uploaded " + str(n) + " file(s)"

try:
    step("upload_results", upload)
except Exception:
    os.makedirs(os.path.join(WORK, "out"), exist_ok=True)
    p = os.path.join(WORK, "out", "capture_manifest.json")
    R["ok"] = False
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(R, fh, indent=1, default=str)
    session.file.put(p, OUT, auto_compress=False, overwrite=True)

print(json.dumps(R, indent=1, default=str)[:3000])
'''


def _notebook_source(run_id: str, dashboards: list) -> str:
    """The capture notebook body with this run's id baked in.

    Baked rather than parameterised because Snowflake notebooks take no
    arguments, and a shared notebook mutated per run would race between two
    validations. Built by placeholder substitution, not an f-string: the body
    is dense with braces and escaping them all is a bug factory.
    """
    return (_NOTEBOOK_TEMPLATE
            .replace("__RUN_ID__", run_id)
            .replace("__STAGE__", STAGE)
            .replace("__NDASH__", str(len(dashboards)))
            .replace("__WRAPPER__", repr(_WRAPPER)))


def _ipynb(source: str) -> str:
    return json.dumps({
        "cells": [{"cell_type": "code", "execution_count": None, "id": "capture",
                   "metadata": {"language": "python"}, "outputs": [],
                   "source": [line + "\n" for line in source.strip().split("\n")]}],
        "metadata": {"kernelspec": {"display_name": "Streamlit Notebook",
                                    "name": "streamlit"},
                     "language_info": {"name": "python"}},
        "nbformat": 4, "nbformat_minor": 5}, indent=1)


_QUERY_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_SQLSTATE_RE = re.compile(r"\(([0-9A-Za-z]{5})\)")
_INCIDENT_RE = re.compile(r"incident[:\s]+(\d+)", re.I)
_ERRCODE_RE = re.compile(r"\berror[:\s]+(\d+)(?::\d+)?", re.I)

# Whitelist, not blacklist -- an error text this project has never seen is NOT
# retried. Retrying blind burns up to ~40s of backoff on something that will
# never succeed, and three identical-looking failures hide a real bug better
# than one clear one does. Every marker below is text actually observed
# marking a genuine Snowflake platform-side fault (300002, XX000, "incident"
# -- Snowflake's own bug-ticket marker) or a well-documented transient class
# (timeout, service unavailable, container/runtime still starting).
_TRANSIENT_MARKERS = (
    "300002", "xx000", "incident", "sql execution internal error",
    "service unavailable", "temporarily unavailable", "timed out",
    "timeout", "could not be started", "resource exhausted",
    "internal error",
)
# Checked FIRST and wins on overlap (e.g. an "internal error" phrase inside a
# sentence that is actually reporting a missing object). These are config/code
# problems that will fail identically no matter how many times they run.
_NONRETRYABLE_MARKERS = (
    "insufficient privileges", "not authorized", "does not exist",
    "sql compilation error", "invalid identifier", "syntax error",
)


def _parse_error_detail(text: str) -> dict:
    text = text or ""
    q = _QUERY_ID_RE.search(text)
    s = _SQLSTATE_RE.search(text)
    i = _INCIDENT_RE.search(text)
    e = _ERRCODE_RE.search(text)
    return {
        "query_id": q.group(0) if q else None,
        "sqlstate": s.group(1).upper() if s else None,
        "incident_id": i.group(1) if i else None,
        "error_code": e.group(1) if e else None,
    }


def _is_transient(text: str) -> bool:
    low = (text or "").lower()
    if any(marker in low for marker in _NONRETRYABLE_MARKERS):
        return False
    return any(marker in low for marker in _TRANSIENT_MARKERS)


def _validate_capture(out_dir: str, manifest: dict | None,
                       expected_titles: list[str]) -> tuple[bool, str | None, dict]:
    """Structural completeness only -- see MIN_HEIGHT's comment for why this
    never judges content. Returns (ok, reason, shots)."""
    if manifest is None:
        return False, "manifest missing", {}
    steps = manifest.get("steps", [])
    step_ok = next((s.get("ok") for s in steps if s.get("step") == "capture_app"), False)
    detail = next((s.get("detail") for s in steps if s.get("step") == "capture_app"), None)
    if not step_ok or not isinstance(detail, dict):
        failed = next((s for s in steps if not s.get("ok", True)), None)
        if failed:
            return False, (f"capture pipeline failed at step "
                           f"{failed.get('step')!r}: {failed.get('error')}"), {}
        return False, "capture_app step did not complete or is missing from the manifest", {}

    files = detail.get("files") or {}
    if set(files) != set(expected_titles):
        missing = sorted(set(expected_titles) - set(files))
        extra = sorted(set(files) - set(expected_titles))
        return False, (f"title mismatch -- missing {missing or 'none'}, "
                       f"unexpected {extra or 'none'}"), {}
    if len(files) != len(expected_titles):
        return False, (f"expected {len(expected_titles)} dashboard(s), "
                       f"manifest lists {len(files)}"), {}

    shots = {}
    for title, info in files.items():
        path = os.path.join(out_dir, info.get("file", ""))
        if not os.path.exists(path):
            return False, f"{title!r}: file listed in manifest but not on disk", {}
        with open(path, "rb") as fh:
            data = fh.read()
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            return False, f"{title!r}: not a valid PNG signature", {}
        try:
            from PIL import Image
            with Image.open(io.BytesIO(data)) as img:
                img.verify()
            with Image.open(io.BytesIO(data)) as img:
                img.load()
                width, height = img.size
        except Exception as exc:                                # noqa: BLE001
            return False, (f"{title!r}: PIL could not open/verify/load: "
                           f"{type(exc).__name__}: {exc}"), {}
        if width <= 0 or height <= 0:
            return False, f"{title!r}: zero-sized image ({width}x{height})", {}
        if width < MIN_WIDTH:
            # Height is deliberately not checked here -- see MIN_HEIGHT.
            return False, (f"{title!r}: below minimum width "
                           f"({width}x{height}, need >= {MIN_WIDTH} wide)"), {}
        shots[title] = data
    return True, None, shots


def _capture_once(session, ir, stem: str, run_id: str, dashboards: list,
                  log) -> tuple[dict, list, dict]:
    """One capture attempt under its own child run_id: own notebook name, own
    stage input/output prefix, own local staging directory, own manifest --
    isolated from every other attempt so a stale or partial result from one
    can never leak into another. Returns (shots, notes, attempt) where
    `attempt` always carries {run_id, ok, retryable, error, query_id,
    sqlstate, incident_id, error_code}; the caller fills in `attempt` (the
    1-based count) and `elapsed_s` (it owns the clock)."""
    import codegen

    def _log(message):
        if log:
            log(f"[{run_id}] {message}")

    def _fail(reason: str, retryable: bool):
        parsed = _parse_error_detail(reason)
        rec = {"run_id": run_id, "ok": False, "retryable": retryable,
               "error": reason, **parsed}
        return {}, [{"dashboard": None, "captured": False, "reason": reason}], rec

    try:
        here = os.path.dirname(os.path.abspath(__file__))
        tmp = tempfile.mkdtemp(prefix="nbcap")
        input_prefix = f"{STAGE}/{run_id}/input"

        # ---- bundle: the generated app + the runtime it imports ---------
        app_src = codegen.build(ir)
        with open(os.path.join(tmp, "app_generated.py"), "w", encoding="utf-8") as fh:
            fh.write(app_src)
        with open(os.path.join(tmp, "workbook_ir.json"), "w", encoding="utf-8") as fh:
            json.dump(ir, fh)
        staged = ["app_generated.py", "workbook_ir.json"]
        for name in RUNTIME_MODULES:
            src = os.path.join(here, name)
            if os.path.exists(src):
                with open(src, "rb") as rfh, open(os.path.join(tmp, name), "wb") as wfh:
                    wfh.write(rfh.read())
                staged.append(name)
        # TABLE ROUTING. Ship the routing the CALLER is actually using -- the
        # live config.DATASOURCES, as configured for this workbook by
        # pipeline.configure_datasources -- rather than a file from disk.
        # Two reasons, one of which cost a full run to learn. (1) Data
        # fidelity: the captured app must query the same tables the
        # validation queried, or the screenshot shows different numbers than
        # the comparison. (2) The repo's own datasources.json is the LOCAL
        # DEV map and points at a SUPERSTORE database that does not exist in
        # Snowflake -- shipping it produced ten real screenshots of ten
        # "Database 'SUPERSTORE' does not exist or not authorized" errors.
        routing = {}
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

        _log(f"uploading {len(staged)} file(s) to {input_prefix}")
        for name in staged:
            session.file.put(os.path.join(tmp, name).replace("\\", "/"), input_prefix,
                             auto_compress=False, overwrite=True)

        # ---- the per-attempt notebook -------------------------------------
        nb_name = f"CAPTURE_{run_id.replace('-', '_')}"
        nb_path = os.path.join(tmp, f"{nb_name.lower()}.ipynb")
        with open(nb_path, "w", encoding="utf-8") as fh:
            fh.write(_ipynb(_notebook_source(run_id, dashboards)))
        nb_prefix = f"{STAGE}/{run_id}/nb"
        session.file.put(nb_path.replace("\\", "/"), nb_prefix,
                         auto_compress=False, overwrite=True)

        fqn = f"{NOTEBOOK_SCHEMA}.{nb_name}"
        _log("submitting the capture job")
        # nb_prefix already carries the leading '@' -- CREATE NOTEBOOK wants
        # the stage path exactly as written, so it is interpolated as-is.
        session.sql(f"""
            CREATE OR REPLACE NOTEBOOK {fqn}
              FROM '{nb_prefix}'
              MAIN_FILE = '{os.path.basename(nb_path)}'
              QUERY_WAREHOUSE = {WAREHOUSE}
              RUNTIME_NAME = 'SYSTEM$BASIC_RUNTIME'
              COMPUTE_POOL = '{COMPUTE_POOL}'
        """).collect()
        session.sql(f"ALTER NOTEBOOK {fqn} ADD LIVE VERSION FROM LAST").collect()
        try:
            session.sql(f"ALTER NOTEBOOK {fqn} SET EXTERNAL_ACCESS_INTEGRATIONS = ({EAI})").collect()
        except Exception as exc:                                # noqa: BLE001
            # Without egress the container cannot install Chromium at all --
            # a config problem, never worked around by retrying.
            return _fail(f"could not attach {EAI} (needed for pip and the "
                        f"Chromium download): {type(exc).__name__}: {exc}", False)

        try:
            session.sql(f"EXECUTE NOTEBOOK {fqn}()").collect()
        except Exception as exc:                                # noqa: BLE001
            text = f"{type(exc).__name__}: {exc}"
            return _fail(f"capture job failed: {text}", _is_transient(text))
        finally:
            try:
                session.sql(f"DROP NOTEBOOK IF EXISTS {fqn}").collect()
            except Exception:                                   # noqa: BLE001
                pass

        # ---- bring the images home, into THIS attempt's own directory ----
        out_dir = os.path.join("reports", f"{stem}_validation_input",
                               "streamlit", run_id)
        os.makedirs(out_dir, exist_ok=True)
        try:
            session.file.get(f"{STAGE}/{run_id}/output/", out_dir)
        except Exception as exc:                                # noqa: BLE001
            # A download failure immediately after a successful EXECUTE
            # NOTEBOOK is itself unusual, not a structural problem -- worth
            # one more attempt.
            return _fail(f"capture produced no downloadable output: "
                        f"{type(exc).__name__}: {exc}", True)

        manifest_path = os.path.join(out_dir, "capture_manifest.json")
        if not os.path.exists(manifest_path):
            return _fail("capture job wrote no manifest", True)
        try:
            with open(manifest_path, encoding="utf-8") as fh:
                manifest = json.load(fh)
        except Exception as exc:                                # noqa: BLE001
            return _fail(f"manifest malformed: {type(exc).__name__}: {exc}", True)

        expected_titles = [d["title"] for d in dashboards]
        ok, reason, shots = _validate_capture(out_dir, manifest, expected_titles)
        if not ok:
            return _fail(f"in-Snowflake capture incomplete: {reason}", True)

        rec = {"run_id": run_id, "ok": True, "retryable": False, "error": None,
               "query_id": None, "sqlstate": None, "incident_id": None,
               "error_code": None}
        return shots, [], rec
    except Exception as exc:                                    # noqa: BLE001
        # Anything not explicitly handled above (a bundle-build failure, a
        # CREATE NOTEBOOK compilation error, a missing stage) -- classify by
        # the same whitelist rather than assume either way.
        text = f"{type(exc).__name__}: {exc}"
        return _fail(f"capture attempt raised: {text}", _is_transient(text))


def _capture_with_retry(session, ir, stem: str, dashboards: list, log,
                        sleep_fn=time.sleep, jitter_fn=None) -> tuple[dict, list]:
    """Up to MAX_ATTEMPTS attempts, each its own child run_id / notebook /
    stage prefix / manifest -- never resubmitted under the same identifiers,
    never combined. Retries only what `_capture_once` marks retryable (a
    transient platform fault, or evidence that came back incomplete even
    though EXECUTE NOTEBOOK itself did not raise); a deterministic failure
    (bad privileges, a missing object, a SQL compilation error) stops after
    one attempt, because retrying something that cannot succeed only delays
    reporting it.

    `sleep_fn`/`jitter_fn` exist purely so tests can inject a no-op instead of
    actually waiting -- `capture()`, the public entry point, never exposes
    them."""
    jitter_fn = jitter_fn or (lambda: random.uniform(*JITTER_S))
    parent_run_id = _run_id()
    attempts = []
    for n in range(1, MAX_ATTEMPTS + 1):
        child_run_id = f"{parent_run_id}-A{n}"
        t0 = time.time()
        shots, _notes, rec = _capture_once(session, ir, stem, child_run_id,
                                           dashboards, log)
        rec["attempt"] = n
        rec["elapsed_s"] = round(time.time() - t0, 1)
        attempts.append(rec)

        if rec["ok"]:
            out_notes = []
            if n > 1:
                summary = (f"{len(shots)}/{len(dashboards)} screenshot(s) "
                          f"captured on attempt {n} of {MAX_ATTEMPTS}")
                out_notes = [{"dashboard": None, "captured": True,
                             "reason": summary, "summary": summary,
                             "attempts": attempts}]
            return shots, out_notes

        if not rec["retryable"] or n == MAX_ATTEMPTS:
            break
        delay = RETRY_DELAYS_S[n - 1] + max(0.0, jitter_fn())
        if log:
            log(f"attempt {n} failed ({(rec.get('error') or '')[:120]}) -- "
                f"retrying in {delay:.0f}s")
        sleep_fn(delay)

    summary = (f"Streamlit screenshot unavailable because workbook capture "
              f"failed after {len(attempts)} attempt(s). See capture "
              f"diagnostics above.")
    final_note = {"dashboard": None, "captured": False, "reason": summary,
                 "summary": summary, "attempts": attempts}
    return {}, [final_note]


def capture(session, ir, stem, dashboards=None, log=None):
    """Run the generated app inside Snowflake and photograph every dashboard,
    retrying transient Snowflake-platform failures (see the retry-policy
    comment near MAX_ATTEMPTS) up to 3 total attempts.

    Returns (shots, notes) in exactly the shape
    `deep_validation._render_app_screenshots` returns, so the caller cannot
    tell which path produced the images -- only that they are real
    screenshots. Signature is unchanged from before the retry logic existed;
    the retry loop and its knobs live in `_capture_with_retry`, not here.
    """
    dashboards = dashboards or [
        {"title": d.get("title") or d.get("name"), "name": d.get("name")}
        for d in ir.get("dashboards", [])]
    return _capture_with_retry(session, ir, stem, dashboards, log)


__all__ = ["available", "capture"]
