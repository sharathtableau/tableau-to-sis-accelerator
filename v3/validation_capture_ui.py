"""SiS side of automated visual validation: submit a job, then watch it.

The user performs exactly ONE action -- clicking "Run Validation". No download,
no local command, no upload, no second browser, no SSO prompt. This module
never imports Playwright and never tries to capture anything: the SiS sandbox
has no browser, and pretending otherwise is what produced ten BLOCKED
dashboards with no explanation.

Split of responsibility:
    SiS      builds the bundle (generated app + IR + Tableau evidence),
             hashes it, uploads it, inserts ONE queued row, returns.
    worker   claims the row, photographs the app, writes results back.
    SiS      polls the row and shows the report when it completes.
"""

from __future__ import annotations

import json
import os
import tempfile

import streamlit as st

import validation_jobs as VJ

# Ordered so the UI can render a sensible progress bar rather than guessing.
_STEP_ORDER = [VJ.QUEUED, VJ.CLAIMED, VJ.STARTING_APP, VJ.CAPTURING,
               VJ.GENERATING_REPORT, VJ.COMPLETED]

_STEP_TEXT = {
    VJ.QUEUED: "Queued for visual capture",
    VJ.CLAIMED: "Worker claimed the job",
    VJ.STARTING_APP: "Starting generated application",
    VJ.CAPTURING: "Capturing dashboards",
    VJ.GENERATING_REPORT: "Generating validation report",
    VJ.COMPLETED: "Validation complete",
}


def dashboard_manifest(ir):
    """Stable ids for every dashboard, so a capture is attributed by id and
    never by a display title that can be duplicated or renamed."""
    out = []
    for index, dash in enumerate(ir.get("dashboards", [])):
        title = dash.get("title") or dash.get("name")
        slug = "".join(c.lower() if c.isalnum() else "-" for c in str(title)).strip("-")
        out.append({"id": f"{index:02d}-{slug}" if slug else f"{index:02d}",
                    "index": index,
                    "name": dash.get("name"),
                    "title": title,
                    "sheets": [s.get("title") or s.get("name")
                               for s in dash.get("sheets", [])]})
    return out


def submit_capture_job(session, ir, book_name, stem, tableau_shots=None,
                       tableau_rows=None, spec=None, workbook_id=None):
    """Build + upload the bundle and queue one job. Returns the RUN_ID.

    Returns immediately: the capture itself takes minutes and must not hold an
    interactive SiS request open.
    """
    import codegen
    import engine

    run_id = VJ.new_run_id()
    app_source = codegen.build(ir)
    dashboards = dashboard_manifest(ir)

    bundle, manifest = VJ.build_capture_bundle(
        run_id=run_id, ir=ir, app_source=app_source, dashboards=dashboards,
        spec=spec, tableau_shots=tableau_shots, tableau_rows=tableau_rows,
        engine_version=getattr(engine, "__version__", None)
                       or VJ.sha256_bytes(_module_bytes(engine))[:16],
        data_snapshot=_data_snapshot(session),
        workbook_id=workbook_id)

    with tempfile.TemporaryDirectory(prefix="valbundle") as tmp:
        VJ.upload_bundle(session, run_id, bundle, tmp)
    VJ.enqueue(session, run_id, workbook_id=workbook_id,
               app_artifact_path=f"{VJ.input_prefix(run_id)}/app.py",
               input_manifest_path=f"{VJ.input_prefix(run_id)}/{VJ.Bundle.MANIFEST}")
    return run_id, manifest


def _module_bytes(module) -> bytes:
    try:
        with open(module.__file__, "rb") as fh:
            return fh.read()
    except Exception:                                           # noqa: BLE001
        return b""


def _data_snapshot(session):
    """Identify the data state both legs must share.

    A Tableau image and a Streamlit image drawn from different data states are
    not a comparison, so the snapshot identity is recorded and travels with the
    bundle. Best-effort: if the account does not expose a query id here, the
    field says so rather than inventing one.
    """
    try:
        row = session.sql(
            "SELECT CURRENT_DATABASE(), CURRENT_SCHEMA(), CURRENT_ROLE(), "
            "CURRENT_WAREHOUSE(), CURRENT_TIMESTAMP()").collect()[0]
        return {"database": row[0], "schema": row[1], "role": row[2],
                "warehouse": row[3], "captured_at": str(row[4])}
    except Exception as exc:                                    # noqa: BLE001
        return {"unavailable": f"{type(exc).__name__}: {exc}"}


def render_capture_panel(session, ir, book_name, stem, tableau_shots=None,
                         tableau_rows=None, spec=None, workbook_id=None):
    """One button, then live progress. Safe to call on every rerun."""
    state_key = f"_capture_run_{stem}"

    if st.button("Run Validation", key=f"_capture_btn_{stem}",
                 icon=":material/play_arrow:", type="primary"):
        with st.spinner("Preparing evidence …"):
            try:
                run_id, _manifest = submit_capture_job(
                    session, ir, book_name, stem, tableau_shots=tableau_shots,
                    tableau_rows=tableau_rows, spec=spec, workbook_id=workbook_id)
                st.session_state[state_key] = run_id
            except Exception as exc:                            # noqa: BLE001
                st.error(f"Could not queue the capture job — "
                         f"{type(exc).__name__}: {exc}")
                return None

    run_id = st.session_state.get(state_key)
    if not run_id:
        return None

    info = VJ.status_of(session, run_id)
    if info is None:
        st.warning(f"Run {run_id} is no longer in the queue.")
        return None

    _render_progress(session, run_id, info)
    return info


def _render_progress(session, run_id, info):
    status = info["status"]
    st.caption(f"Run `{run_id}` · worker `{info.get('worker_id') or '—'}` · "
               f"attempt {info.get('attempt') or 0}")

    if status in VJ.ACTIVE_STATUSES or status == VJ.QUEUED:
        step = _STEP_ORDER.index(status) if status in _STEP_ORDER else 0
        label = info.get("label") or _STEP_TEXT.get(status, status)
        if status == VJ.CAPTURING and info.get("total"):
            label = (f"Capturing dashboard {int(info.get('done') or 0) + 1} "
                     f"of {int(info['total'])}")
        st.progress(min(0.99, (step + 1) / (len(_STEP_ORDER) + 1)), text=label)
        # The page polls itself; the user never refreshes or re-clicks.
        st.caption("This page refreshes itself while the capture runs.")
        try:
            st.autorefresh(interval=5000, key=f"_poll_{run_id}")
        except AttributeError:                                  # older Streamlit
            pass
        if st.button("Cancel", key=f"_cancel_{run_id}"):
            VJ.request_cancel(session, run_id)
            st.info("Cancellation requested; the worker stops at its next "
                    "heartbeat.")
        return

    if status == VJ.COMPLETED:
        st.success("Validation complete — real browser captures attached.")
        return
    if status == VJ.BLOCKED:
        st.warning(
            "Visual evidence is **BLOCKED** — it could not be produced, which "
            "is NOT a pass and is never substituted with a server-side "
            f"render. Reason: {info.get('error_message') or info.get('error_code')}")
        return
    st.error(f"Capture failed ({info.get('error_code')}): "
             f"{info.get('error_message')}")


def fetch_capture_results(session, run_id, dest_dir):
    """Bring the worker's screenshots back so the report can embed them."""
    os.makedirs(dest_dir, exist_ok=True)
    session.file.get(f"{VJ.output_prefix(run_id)}/", dest_dir)
    manifest_path = os.path.join(dest_dir, "result_manifest.json")
    if not os.path.exists(manifest_path):
        return None, {}
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    images = {}
    for dash_id, name in (manifest.get("streamlit_images") or {}).items():
        path = os.path.join(dest_dir, name)
        if os.path.exists(path):
            images[dash_id] = path
    return manifest, images


__all__ = ["dashboard_manifest", "fetch_capture_results",
           "render_capture_panel", "submit_capture_job"]
