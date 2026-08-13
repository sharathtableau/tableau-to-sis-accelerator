"""deep_validation.py -- the heavyweight validation + reporting features.

Originally carried over VERBATIM from the pre-V2 pipeline_app.py; as of
2026-08 the Deep Validation UI was consolidated onto a single surface:

  * render_proof_first_validation -- per-CHART, row-level three-way
    comparison (Tableau / Streamlit / backend) via the R12 proof-first pack
    (validation_evidence_bridge + validation_report), real Tableau/Streamlit
    screenshots with a structural similarity score, and a deterministic
    per-dashboard formula/interaction summary. Explicit user decision:
    this ONE pack replaces the older three-panel UI (a Cortex per-metric
    judge, a Cortex-narrated skill-methodology write-up, and a Cortex
    vision screenshot comparison) -- it is fully deterministic, spends no
    Cortex tokens, and was judged the clearer, more useful surface.
  * render_migration_report -- the structured migration report + PDF,
    unchanged.

Both are on-demand wrappers callable from the workbench's Validation page.
They degrade honestly (state WHY unavailable) rather than rendering
silence -- the project's standing rule.
"""

import os
from collections import Counter

import streamlit as st

import config
import parity
import pipeline
import tableau_server as TS


def _tableau_ref_cell(m):
    """The 'Tableau' column for one RAW-COLUMN measure row.

    A REST-pulled value is LABELLED as such, and an approximate one (summed
    across a multi-row view) is never displayed as if it were a verified
    exact figure -- same disclosure rule the calculated-metric table and the
    Cortex judge prompt already follow. A bare number means it came from
    parity.TABLEAU_TRUTH, the hand-verified fallback used when there is no
    live REST connection (a manual file upload)."""
    v = m.get("tableau")
    if v is None:
        return "—"
    if m.get("tableau_source") != "REST":
        return v
    if m.get("tableau_approx"):
        return f"{v} (REST, ≈ sum of {m.get('tableau_rows')} rows)"
    return f"{v} (REST, exact)"


def _pull_tableau_dashboard_screenshots(conn, ir):
    """Real per-dashboard Tableau REST screenshots, keyed by dashboard TITLE
    -- the Tableau-side half of the visual evidence pair. Reuses the exact
    pull + name-matching pattern render_vision_validation already proved
    live (Tableau REST returns each view's INTERNAL name, not its
    customizable display title, so the internal dashboard name is tried
    first, the title second). Returns {} when there is no live connection --
    the caller then correctly leaves the Tableau screenshot absent, which
    validation_report.py reports as visual BLOCKED, never a false pass."""
    if not conn:
        return {}
    try:
        views = TS.pull_all_view_images(
            conn["server_url"], conn["site_content_url"], conn["workbook_id"])
    except Exception:
        return {}
    by_name = {v["view"].strip().lower(): v for v in views}
    out = {}
    for dash in ir.get("dashboards", []):
        title = dash.get("title") or dash["name"]
        tv = (by_name.get(dash["name"].strip().lower())
              or by_name.get(title.strip().lower()))
        if tv and not tv.get("error") and tv.get("png"):
            out[title] = tv["png"]
    return out


def _pull_tableau_sheet_csvs(conn):
    """Real per-WORKSHEET Tableau REST crosstab CSVs (the row-level Tableau
    evidence leg), keyed by the view's own internal name, lower-cased.
    Returns {} when there is no live connection; one view's pull failing
    never aborts the rest (same discipline as pull_all_view_images)."""
    if not conn:
        return {}
    try:
        views = TS.pull_all_view_csvs(
            conn["server_url"], conn["site_content_url"], conn["workbook_id"])
    except Exception:
        return {}
    return {v["view"].strip().lower(): v["csv"]
            for v in views if not v.get("error") and v.get("csv")}


def _assign_dashboard_csv_by_header(dname, unmatched_sheets, dash_csv, evidence):
    """A multi-sheet dashboard's REST view returns ONE sheet's crosstab, not
    a per-worksheet one -- Tableau's dashboard-level `query_view_data`
    exports whichever single sheet it treats as the view's underlying data,
    silently. FOUND LIVE 2026-08-11 on Regional Analysis: `View1` (3
    sheets) pulled a `Category,Region,Sales` crosstab -- exactly "Category
    wise Sales by Region"'s own columns -- and it was discarded outright
    because the sheet-matching code only trusted a single-sheet dashboard.
    Real, usable evidence was being thrown away.

    This assigns that crosstab to the ONE sheet whose own declared fields
    (`parity._sheet_pill_captions` -- the same closed shelf-key scan the
    dashboard-section validator already relies on, reused rather than
    re-derived) EXACTLY equal the crosstab's header columns -- every
    measure, strong dim AND weak dim, not just measures+strong-dims.
    Tableau's crosstab export for a sheet carries every pill on it, no
    more, no less, so an EXACT set match is the honest content proof; a
    mere SUBSET match is not precise enough -- on Regional Analysis's
    View2, both "Region level Sales" (needs {Region, Sales}) and "Profit
    by Category" (needs {Category, Region, Sales}) are subsets of that
    dashboard's real 3-column header, which made a subset-only version of
    this check ambiguous (2 hits) and left BOTH unmatched; requiring the
    exact set resolves cleanly to "Profit by Category" alone, since only
    its full field list equals the header. This is still a CONTENT match,
    not a guess: the header is read, not assumed. Refuses -- leaves every
    sheet unmatched, exactly as before this fix -- the moment more than
    one sheet's exact set matches, since at that point the export's owner
    is genuinely ambiguous and this project does not guess."""
    import csv as _csv
    import io as _io

    import parity

    try:
        header = next(_csv.reader(_io.StringIO(dash_csv)))
    except StopIteration:
        return
    header_cols = {c.strip() for c in header}

    hits = []
    for sheet in unmatched_sheets:
        measures, strong_dims, weak_dims = parity._sheet_pill_captions(sheet)
        needed = set(measures) | strong_dims | weak_dims
        if needed and needed == header_cols:
            hits.append(sheet)
    if len(hits) == 1:
        sheet = hits[0]
        sname = sheet.get("title") or sheet.get("name")
        evidence.set_tableau_csv(dname, sname, dash_csv)


def _apply_uploaded_crosstabs(evidence, ir, uploaded_files):
    """Fallback Tableau row evidence for a sheet REST can't reach (or a
    workbook with no live Tableau connection at all): the user uploads
    exported crosstab CSVs, matched to a (dashboard, sheet) by CANONICAL
    filename -- never a fuzzy guess (validation_evidence_bridge.canonical is
    the SAME matcher align_rows uses for column names). A REST-sourced row
    set always wins when one already exists for that sheet. Returns
    (matched_filenames, unmatched_filenames) so the caller can show exactly
    what was used and what needs renaming rather than silently dropping it."""
    import validation_evidence_bridge as VEB
    if not uploaded_files:
        return [], []
    targets = {}
    for dash in ir.get("dashboards", []):
        dname = dash.get("title") or dash["name"]
        for sheet in dash.get("sheets", []):
            sname = sheet.get("title") or sheet.get("name")
            targets.setdefault(VEB.canonical(sname), []).append((dname, sname))
    matched, unmatched = [], []
    for f in uploaded_files:
        stem = os.path.splitext(f.name)[0]
        candidates = targets.get(VEB.canonical(stem))
        if not candidates:
            unmatched.append(f.name)
            continue
        for dname, sname in candidates:
            if evidence.tableau_rows_for(dname, sname) is None:
                evidence.set_tableau_csv(dname, sname, f.getvalue())
        matched.append(f.name)
    return matched, unmatched


_PACK_README = """# Validation pack — {workbook}

Run `{run_id}` · {environment} · generated {generated_at}
**Overall status: {status}** — {passed} passed, {review} to review,
{failed} failed or blocked, across {dashboards} dashboards / {charts} charts.

## What is in here

{files_table}

## How to read a status

- **PASS** — every compared value agreed inside the tolerance derived from
  the workbook's own number format.
- **REVIEW** — compared and consistent, but something needs a human eye.
- **FAIL** — compared and proven different. A real discrepancy.
- **BLOCKED** — *not measured*. Some source was missing, so nothing can be
  claimed either way. A blocked chart is listed with its reason rather than
  omitted, because an omitted chart is indistinguishable from a passing one.

**BLOCKED is not a defect and PASS is not implied by its absence.**

A dashboard's overall status is driven by its chart, formula and interaction
checks, plus its VISUAL check whenever a screenshot was actually captured
and compared. A missing screenshot (visual BLOCKED — no browser available
where this pack was generated) does NOT by itself pull the dashboard's
status down: you can see `PASS` at the top with `Visual: BLOCKED` sitting
next to it, and that is correct, not a contradiction — it means the
measurable proof genuinely passed and the picture just wasn't available
this run. A screenshot that WAS captured and found genuinely wrong still
counts as a real FAIL, exactly as any other check.

## Reading the screenshots

The Tableau image is the real rendered view pulled over the REST API. The
Streamlit image is a REAL screenshot of the generated app running locally
(a real browser against a real `streamlit run`) — not a re-render, not an
approximation. It requires a browser and a local Streamlit process where
the pack is generated (a workstation); it cannot be produced from inside
the deployed Streamlit-in-Snowflake app itself, which has no browser. When
unavailable, the visual leg is reported BLOCKED with that reason, never
substituted.

**They are captured under different filter states.** The app side is
rendered with every filter at its default (`All`); the Tableau side carries
whatever filter state was saved in the workbook. Where a dashboard has a
saved filter, some of the difference between the two images is state, not
migration defect. Check the filter before raising an issue.

## Provenance

- Tableau source: {tableau_source}
- Backend: {backend}
- Workbook: {workbook}
"""


def _slim_validation_pack(out_dir, summary, meta=None):
    """Ship only what a reviewer can act on.

    The generated pack was 5.7 MB across 49 files, and measurably ~40% of it
    was duplication or filler (audited 2026-08-07):
      * `visual-staging/` is the renderer's own scratch directory. It sits
        inside out_dir, so the zip walk swept it in -- byte-identical copies
        (md5-verified) of the diffs already written to evidence/.
      * a `comparison.csv` was written for EVERY chart including BLOCKED
        ones, so 14 of 21 held nothing but a header. A file called
        comparison.csv that is empty reads as "compared, nothing wrong" --
        the exact stand-in-that-looks-like-evidence failure this project
        keeps correcting.
      * `comparison_rows` + the three per-source row arrays were 270 KB of
        the 698 KB summary, duplicating the CSVs verbatim.
      * both HTML reports referenced screenshots by RELATIVE PATH, so the
        report was broken the moment it left its folder.

    So: inline the images into the reports and drop the loose PNGs (no
    duplicated bytes, and the report travels as one file), delete the
    staging dir and the empty CSVs, write a slim summary that points at the
    CSVs instead of restating them, and add a README carrying the status
    legend and the filter-state caveat -- without which a reviewer cannot
    tell a saved-filter difference from a migration defect.

    Mutates only what is ON DISK: `summary` is left untouched because the
    Cortex vision step and the Stage-5 UI read it in memory afterwards.
    Returns a note dict for the caller."""
    import base64
    import copy
    import json
    import os
    import re
    import shutil

    dropped = {"staging": 0, "empty_csv": 0, "png_inlined": 0, "bytes_before": 0}
    for root, _dirs, files in os.walk(out_dir):
        for name in files:
            dropped["bytes_before"] += os.path.getsize(os.path.join(root, name))

    # 1. Inline every referenced PNG into the HTML reports, then drop the
    #    loose copies -- self-contained, and no byte is stored twice.
    def _data_uri(path):
        with open(path, "rb") as fh:
            return "data:image/png;base64," + base64.b64encode(fh.read()).decode()

    for html_name in os.listdir(out_dir):
        if not html_name.endswith(".html"):
            continue
        full = os.path.join(out_dir, html_name)
        text = open(full, encoding="utf-8").read()

        def _sub(m):
            rel = m.group(1)
            src = os.path.join(out_dir, rel.replace("/", os.sep))
            if not os.path.exists(src):
                return m.group(0)
            dropped["png_inlined"] += 1
            return 'src="%s"' % _data_uri(src)

        text = re.sub(r'src="([^"]+\.png)"', _sub, text)
        open(full, "w", encoding="utf-8").write(text)

    staging = os.path.join(out_dir, "visual-staging")
    if os.path.isdir(staging):
        dropped["staging"] = len(os.listdir(staging))
        shutil.rmtree(staging, ignore_errors=True)
    for root, _dirs, files in os.walk(out_dir):
        for name in files:
            if name.lower().endswith(".png"):
                os.remove(os.path.join(root, name))

    # 2. Drop the empty comparison CSVs, and the pointers to them. A BLOCKED
    #    chart already renders its skip_reason and never links a CSV, so
    #    nothing in the report dangles.
    slim = copy.deepcopy(summary)
    for dash in slim.get("dashboards", []):
        for chart in dash.get("charts", []):
            rel = chart.get("comparison_csv")
            if not rel:
                continue
            path = os.path.join(out_dir, rel.replace("/", os.sep))
            if not chart.get("comparison_rows") and os.path.exists(path):
                os.remove(path)
                chart.pop("comparison_csv", None)
                dropped["empty_csv"] += 1
    for root, dirs, files in os.walk(out_dir, topdown=False):
        if not files and not dirs and os.path.normpath(root) != os.path.normpath(out_dir):
            os.rmdir(root)                       # prune emptied chart folders

    # 3. Slim the summary: keep every verdict, count and reason; drop the row
    #    arrays that the CSVs already carry in a form people actually open.
    for dash in slim.get("dashboards", []):
        for chart in dash.get("charts", []):
            for key in ("comparison_rows", "tableau_rows", "streamlit_rows",
                        "backend_rows", "duplicates"):
                chart.pop(key, None)
    slim["row_detail"] = ("Row-level values live in evidence/<dashboard>/charts/"
                          "<chart>/comparison.csv, not in this file.")
    with open(os.path.join(out_dir, "validation_summary.json"), "w",
              encoding="utf-8") as fh:
        json.dump(slim, fh, indent=1)

    # 4. The entry point. 49 files with no legend is not a deliverable.
    #    The table lists what is ACTUALLY on disk -- a manifest naming a file
    #    that isn't there is worse than no manifest.
    s = summary.get("summary", {})
    meta = meta or {}
    present = sorted(f for f in os.listdir(out_dir) if not os.path.isdir(
        os.path.join(out_dir, f)))
    n_csv = sum(1 for root, _d, fs in os.walk(os.path.join(out_dir, "evidence"))
                for f in fs if f == "comparison.csv")
    table = ["| File | What it is |", "|------|------------|"]
    for name in present:
        blurb = _PACK_BLURB.get(name)
        if blurb:
            table.append(f"| `{name}` | {blurb} |")
    if n_csv:
        table.append(
            f"| `evidence/<dashboard>/charts/<chart>/comparison.csv` | "
            f"Row-by-row Tableau / Streamlit / backend values at the chart's own "
            f"grain. {n_csv} file(s) — only charts where a comparison actually "
            f"happened have one. |")
    with open(os.path.join(out_dir, "README.md"), "w", encoding="utf-8") as fh:
        fh.write(_PACK_README.format(
            files_table="\n".join(table),
            workbook=summary.get("workbook", "—"),
            run_id=summary.get("run_id", "—"),
            environment=summary.get("environment", "—"),
            generated_at=summary.get("generated_at", "—"),
            status=summary.get("status", "—"),
            passed=s.get("passed", 0), review=s.get("review", 0),
            failed=s.get("failed_or_blocked", 0),
            dashboards=s.get("dashboards", 0), charts=s.get("charts", 0),
            tableau_source=meta.get("tableau_source") or
                           "file-uploaded workbook (no Tableau Server connection)",
            backend=meta.get("backend") or "—"))

    after = 0
    for root, _dirs, files in os.walk(out_dir):
        after += sum(os.path.getsize(os.path.join(root, f)) for f in files)
    dropped["bytes_after"] = after
    return dropped


_PACK_BLURB = {
    "README.md": "Start here — what each file is, and how to read a status.",
    "validation_report.html": "The full proof. Screenshots are embedded, so this "
                              "one file stands on its own.",
    "dashboard_validation_report.html": "Same run, per-dashboard, for reviewer "
                                        "sign-off.",
    "issues.csv": "Every dashboard and chart that did not pass.",
    "validation_summary.json": "Machine-readable verdicts. Row values live in the "
                               "comparison CSVs, not here.",
}


def _render_pack_files(pack, stem):
    """Surface the pack as INDIVIDUAL files, and show the two a reviewer
    actually reads (the README and the issues list) in place.

    Replaces the single .zip download (2026-08-07 user decision). A zip put a
    download-unpack-hunt step between a reviewer and the one file they wanted,
    and the pack is now small enough -- and each file self-contained enough --
    that there is nothing left for it to bundle."""
    files = pack.get("files") or []
    if not files:
        return
    by_name = {f["name"]: f for f in files}

    readme = by_name.get("README.md")
    if readme:
        with st.expander("What's in this pack, and how to read it", expanded=False):
            st.markdown(readme["bytes"].decode("utf-8", "replace"))

    st.markdown("**Validation pack — %d files, %.1f MB**"
                % (len(files), sum(f["size"] for f in files) / 1e6))
    top = [f for f in files if "/" not in f["name"]]
    for i, f in enumerate(top):
        c1, c2 = st.columns([3, 5])
        with c1:
            st.download_button(
                f"⬇ {f['name']}", f["bytes"], file_name=f"{stem}_{f['name']}",
                mime=f["mime"], key=f"_proof_dlf_{stem}_{i}",
                use_container_width=True)
        with c2:
            st.caption(f"{_PACK_BLURB.get(f['name'], '')} · {f['size'] / 1024:,.0f} KB")

    issues = by_name.get("issues.csv")
    if issues:
        import csv as _csv
        import io as _io
        try:
            rows = list(_csv.DictReader(
                _io.StringIO(issues["bytes"].decode("utf-8", "replace"))))
        except Exception:
            rows = []
        if rows:
            with st.expander(f"Issues ({len(rows)}) — everything that did not pass",
                             expanded=False):
                st.dataframe(rows, use_container_width=True, hide_index=True)

    csvs = [f for f in files if f["name"].endswith("comparison.csv")]
    if csvs:
        with st.expander(f"Per-chart comparison CSVs ({len(csvs)}) — row-by-row "
                         "Tableau / Streamlit / backend values", expanded=False):
            st.caption("Only charts where a comparison actually happened have a "
                       "file here. A blocked chart has none, and says why in the "
                       "report rather than shipping an empty CSV.")
            for i, f in enumerate(csvs):
                st.download_button(
                    f"⬇ {f['name'].split('/charts/')[-1].replace('/comparison.csv', '')}"
                    f"  ({f['size'] / 1024:,.0f} KB)",
                    f["bytes"], file_name=f["name"].replace("/", "_"),
                    mime="text/csv", key=f"_proof_dlc_{stem}_{i}")


def _render_app_screenshots(ir, stem):
    """The Streamlit-side visual evidence: a REAL screenshot of the REAL
    generated app, via app_screenshot.capture_app -- not a re-render.

    REPLACES headless_render.render_dashboard_to_png as the image source for
    this pack (2026-08-10 explicit user decision, after being asked outright
    "why are you rendering it again -- just screenshot the app"). The
    re-render was a SECOND renderer: every layout decision it made
    independently of engine.py was a fabrication reported as a migration
    defect when it differed from the app, and on Customer Analysis it
    FLATTERED the migration -- it drew all 30 customer names because it gave
    the chart more height than the app does, while the real app drops every
    other label. A capture that makes the migration look better than it is,
    is worse than no capture. See app_screenshot.py's module docstring for
    the full account and headless_render.py's for what it still does (the
    Streamlit DATA leg of the row comparison, untouched by this change).

    Returns (shots, notes): `shots` is {dashboard_title: png_bytes} — empty
    when a real screenshot is not possible here (no browser, no local
    Streamlit) — and `notes` explains why, per this project's rule against
    silently substituting an approximation. The caller feeds an empty
    `shots` straight through to evidence.set_visual(None), which reports
    visual BLOCKED with the stated reason -- never a fallback render."""
    import app_screenshot as APS
    import codegen

    ok, why = APS.available()
    if not ok:
        return {}, [{"dashboard": None, "captured": False, "reason": why}]

    try:
        src = codegen.build(ir)
    except Exception as exc:
        return {}, [{"dashboard": None, "captured": False,
                     "reason": f"could not generate the app to screenshot: "
                               f"{type(exc).__name__}: {exc}"}]

    # Written into THIS project's own directory (not a random tempdir) so the
    # generated app's `from engine import run` resolves exactly as it does
    # for every other app_<stem>.py -- then deleted, since this copy is a
    # scratch artifact for one screenshot pass, not a shipped generated app.
    here = os.path.dirname(os.path.abspath(__file__))
    app_path = os.path.join(here, f"_shot_{stem}_{os.getpid()}.py")
    with open(app_path, "w", encoding="utf-8") as fh:
        fh.write(src)
    try:
        shots, notes = APS.capture_app(app_path)
    finally:
        try:
            os.remove(app_path)
        except OSError:
            pass
    return shots, notes


def _build_validation_pack(ir, sections, book_name, stem, conn=None,
                           streamlit_shots=None, uploaded_crosstabs=None):
    """R12 + evidence bridge -- generate the proof-first validation pack and
    return {"status", "rows", "files", ...evidence provenance...} for the UI,
    or None when nothing is comparable.

    Rewired (2026-08) onto validation_evidence_bridge.py per
    VALIDATION_EVIDENCE_WIRING.md:
      * real Tableau REST screenshots (or an uploaded crosstab's dashboard
        has none -- screenshots have no upload fallback, stated honestly)
        paired with real Streamlit-side screenshots, NEVER the same image
        for both sides, replace the old build that always sent
        tableau_screenshot=None;
      * real per-worksheet Tableau crosstab rows (REST, or an uploaded CSV
        matched by canonical filename when REST is unavailable) replace the
        old always-empty Tableau row leg;
      * engine.EVIDENCE_CAPTURE turns on for the DURATION of this call only,
        so map/treemap/plain-table/rank-table charts -- kinds
        validation_adapter's Vega-Lite-encoding guess can never see at all,
        since they render via Plotly/st.dataframe/hand-built HTML, not
        Altair -- record their OWN real, post-filter/sort/top-N/rank
        dataframe instead of being silently unvalidated. Reset to False in a
        finally so a deployed app / any other caller is never affected.

    'No proof, no pass' is enforced by validation_report.py itself, entirely
    unchanged here -- this function only supplies evidence, never a verdict:
    a chart/dashboard with no comparable proof is reported BLOCKED with its
    stated reason, not silently passed or omitted."""
    import json
    import os
    import engine
    import validation_evidence_bridge as VEB
    import validation_report as VR

    def _table_for(dash):
        info = parity.collect_dashboard_section(ir, dash)
        return info["table"] if info else config.ORDERS

    out_dir = os.path.join("reports", f"{stem}_validation_pack")
    input_dir = os.path.join("reports", f"{stem}_validation_input")

    VEB.REGISTRY.clear()
    evidence = VEB.EvidenceBundle()

    # --- Visual evidence: Tableau REST + a REAL screenshot of the REAL app,
    # kept in SEPARATE files -- never the same image compared against
    # itself, and never a re-render standing in for the real thing. --------
    tableau_shots = _pull_tableau_dashboard_screenshots(conn, ir)
    streamlit_shots = dict(streamlit_shots or {})
    screenshot_notes = []
    if not streamlit_shots:
        streamlit_shots, screenshot_notes = _render_app_screenshots(ir, stem)
    tableau_paths = VEB.save_png_map(
        tableau_shots, os.path.join(input_dir, "tableau"), "tableau")
    streamlit_paths = VEB.save_png_map(
        streamlit_shots, os.path.join(input_dir, "streamlit"), "streamlit")
    for dash in ir.get("dashboards", []):
        title = dash.get("title") or dash["name"]
        evidence.set_visual(
            title,
            tableau_screenshot=tableau_paths.get(title),
            streamlit_screenshot=streamlit_paths.get(title),
            threshold=0.85,
        )

    # --- Row evidence: Tableau REST crosstab per worksheet, or an uploaded
    # crosstab keyed by canonical filename when REST has nothing for it. ---
    view_csvs = _pull_tableau_sheet_csvs(conn)
    for dash in ir.get("dashboards", []):
        dname = dash.get("title") or dash["name"]
        dash_sheets = dash.get("sheets", [])
        dash_csv = (view_csvs.get(dash["name"].strip().lower())
                   or view_csvs.get(dname.strip().lower()))
        unmatched = []
        for sheet in dash_sheets:
            sname = sheet.get("title") or sheet.get("name")
            csv_text = (view_csvs.get((sheet.get("name") or "").strip().lower())
                        or view_csvs.get((sname or "").strip().lower()))
            if not csv_text and dash_csv:
                if len(dash_sheets) == 1:
                    # Tableau REST's own view list is DASHBOARD-granular -- a
                    # worksheet nested inside a dashboard has no view_id of
                    # its own, so its crosstab can never be pulled by its OWN
                    # name unless the sheet happens to share the dashboard's
                    # name. A dashboard with EXACTLY ONE sheet is the
                    # unambiguous case: that dashboard's view IS that
                    # sheet's data.
                    csv_text = dash_csv
                else:
                    unmatched.append(sheet)
                    continue
            if csv_text:
                evidence.set_tableau_csv(dname, sname, csv_text)
        if unmatched and dash_csv:
            _assign_dashboard_csv_by_header(dname, unmatched, dash_csv, evidence)
    matched_csvs, unmatched_csvs = _apply_uploaded_crosstabs(
        evidence, ir, uploaded_crosstabs)

    # --- Chart evidence: the explicit engine.py capture for Plotly/table/
    # rank-table kinds; the existing Vega-Lite-encoding guess still covers
    # every Altair-rendered kind as the fallback build_complete_validation_spec
    # already implements (an explicit REGISTRY payload always wins). --------
    engine.EVIDENCE_CAPTURE = True
    try:
        spec = VEB.build_complete_validation_spec(
            ir, sections, book_name, _table_for, evidence,
            environment="UAT",
            visual_artifact_dir=os.path.join(out_dir, "visual-staging"),
        )
    finally:
        engine.EVIDENCE_CAPTURE = False

    result = VR.generate_report(spec, out_dir)

    summary = json.loads(open(result["summary"], encoding="utf-8").read())
    # The CLIENT-FACING report, rendered from the SAME already-validated run
    # the engine just produced (no second judgment) -- per-dashboard chapters
    # with A/B/C/D sections, the chart data contract, a consolidated
    # exceptions register and a sign-off record.
    dash_report_path = os.path.join(out_dir, "dashboard_validation_report.html")
    try:
        import validation_report_dashboard as VRD
        with open(dash_report_path, "w", encoding="utf-8") as fh:
            fh.write(VRD.render_dashboard_report(
                summary,
                meta={"tableau_source": (
                          f"{conn['server_url']} · site {conn['site_content_url']}"
                          if conn else None),
                      "backend": f"{pipeline.LOAD_DB}.{pipeline.LOAD_SCHEMA}"},
                vision=None))
    except Exception as exc:                       # never blocks the pack
        dash_report_path = None
        notes_err = f"{type(exc).__name__}: {exc}"
        print(f"dashboard report not rendered: {notes_err}")
    # Per-dashboard DETERMINISTIC visual scores, kept so the Cortex vision
    # step can show its AI verdict beside the structural one rather than
    # replacing it (see render_cortex_vision).
    visual_scores = [{
        "dashboard": d.get("name"),
        "similarity": (d.get("visual") or {}).get("similarity"),
        "threshold": (d.get("visual") or {}).get("threshold", 0.85),
        "status": d.get("visual_status"),
    } for d in summary.get("dashboards", [])]
    rows = []
    for dash in summary.get("dashboards", []):
        for chart in dash.get("charts", []):
            counts = chart.get("source_counts", {})
            rows.append({
                "Dashboard": dash["name"],
                "Chart": chart["title"],
                "Grain": ", ".join(chart.get("grain") or []) or "—",
                "Rows (T/S/B)": (f"{counts.get('tableau', 0)}/"
                                 f"{counts.get('streamlit', 0)}/"
                                 f"{counts.get('backend', 0)}"),
                "Failed values": chart.get("failed_cells", 0),
                "Status": chart.get("status"),
                "Why not validated": chart.get("skip_reason", ""),
            })
    if not rows:
        return None

    # Ship only what a reviewer can act on -- runs LAST, after both reports
    # are rendered and after `summary` has been read for the UI, so it only
    # ever touches what is about to be zipped.
    slimmed = _slim_validation_pack(out_dir, summary, meta={
        "tableau_source": (f"{conn['server_url']} · site {conn['site_content_url']}"
                           if conn else None),
        "backend": f"{pipeline.LOAD_DB}.{pipeline.LOAD_SCHEMA}"})

    # The pack ships as INDIVIDUAL files, not a zip: after slimming there are
    # about a dozen of them, every one is directly openable (HTML, CSV, MD),
    # and a zip only adds a download-unpack-hunt step between a reviewer and
    # the one file they actually want. The report is self-contained, so it
    # travels perfectly well on its own.
    _MIME = {".html": "text/html", ".csv": "text/csv", ".json": "application/json",
             ".md": "text/markdown"}
    pack_files = []
    for root, _dirs, names in os.walk(out_dir):
        for name in sorted(names):
            full = os.path.join(root, name)
            with open(full, "rb") as fh:
                data = fh.read()
            pack_files.append({
                "name": os.path.relpath(full, out_dir).replace("\\", "/"),
                "bytes": data, "size": len(data),
                "mime": _MIME.get(os.path.splitext(name)[1].lower(),
                                  "application/octet-stream")})
    pack_files.sort(key=lambda f: ("/" in f["name"], f["name"]))
    return {
        "status": result["status"], "rows": rows, "files": pack_files,
        "tableau_screenshots": sorted(tableau_paths),
        "streamlit_screenshots": sorted(streamlit_paths),
        # The PATHS (not just the names) so the Cortex vision step can reuse
        # the very same images this pack already scored structurally --
        # re-pulling from Tableau / re-rendering the app would risk judging
        # different pixels than the ones shown in the report.
        "tableau_screenshot_paths": dict(tableau_paths),
        "streamlit_screenshot_paths": dict(streamlit_paths),
        "visual_scores": visual_scores,
        "screenshot_notes": screenshot_notes,
        "pack_slimmed": slimmed,
        "dashboard_report": dash_report_path,
        "out_dir": out_dir,
        "summary_json": summary,
        "tableau_csv_views": sorted(view_csvs),
        "uploaded_csv_matches": matched_csvs,
        "unmatched_crosstabs": unmatched_csvs,
    }


def render_validation_proof(res, nb=None, sec_nb=None, stem="workbook",
                            key_prefix=""):
    """Render the FULL Stage-5 validation format from an already-computed parity
    result. Render-only (no recompute, no file I/O) so the Modernization report
    tab mirrors EXACTLY what Stage 5 shows inline. The format is owned by
    parity.py (`check_workbook` result shape + the two notebooks); this just
    displays it, so a parity.py format change flows to both places."""
    s = res["summary"]
    m1, m2, m3 = st.columns(3)
    m1.metric("Measures PASS", f"{s['measures_pass']}/{s['measures_checked']}")
    m2.metric("Measures with bug", s["measures_bug"],
              delta=None if s["measures_bug"] == 0 else "review",
              delta_color="inverse")
    m3.metric("Calcs translated", s["calcs_translated"])
    st.caption("Every measure computed two independent ways — through the app's "
               "own SQL path and from a direct source read — plus a cross-check "
               "against published Tableau figures where known.")
    _v = {"PASS": "✅ PASS", "EXECUTED": "☑ EXECUTED", "BUG": "❌ BUG"}
    if res.get("measures"):
        st.caption("Column measures — two independent computation paths (a "
                   "`(repull)` source value means no local extract file was "
                   "available in this environment, so the cross-check is an "
                   "independent re-pull of the same table):")
        st.dataframe(
            [{"Datasource": m["datasource"], "Measure": m["measure"],
              "App value": m["app"],
              "Source value": (f"{m['source']} (repull)"
                               if m.get("source_kind") == "table-repull"
                               else (m["source"] if m["source"] is not None else "—")),
              "Tableau": _tableau_ref_cell(m),
              "Verdict": _v[m["verdict"]]}
             for m in res["measures"]],
            use_container_width=True, hide_index=True)
    if res.get("calc_metrics"):
        st.caption("Calculated-field metrics — execution + known-value proof "
                   "(real execution + cross-check where a Tableau figure is "
                   "known):")
        st.dataframe(
            [{"Datasource": m["datasource"], "Metric": m["metric"],
              "Value": m["error"] or m["value"],
              "Tableau bound": (f"{m['tableau_bound'][0]}–{m['tableau_bound'][1]}"
                                if m.get("tableau_bound") else "—"),
              "Verdict": _v[m["verdict"]]}
             for m in res["calc_metrics"]],
            use_container_width=True, hide_index=True)
    st.caption("Row-count parity (complete load / correct routing). '—' means no "
               "independent source file was available to compare against, not a "
               "mismatch:")
    st.dataframe(
        [{"Datasource": d["datasource"], "App rows": d["app_rows"],
          "Source rows": d["source_rows"] if d["source_rows"] is not None else "—",
          "Match": ("✅" if d["match"] is True else
                   "❌" if d["match"] is False else "—")}
         for d in res["datasources"]],
        use_container_width=True, hide_index=True)
    if s["measures_bug"] == 0:
        st.success("✅ ALL MEASURES PASS — the converted app reproduces the "
                   "workbook's numbers.")
    else:
        st.error(f"❌ {s['measures_bug']} measure(s) diverge — see the table.")
    if nb:
        st.download_button("⬇ Download the validation notebook (.ipynb) — the "
                           "proof", nb, file_name=f"{stem}_validation.ipynb",
                           mime="application/x-ipynb+json",
                           key=f"{key_prefix}val_nb")
    if sec_nb:
        st.download_button("⬇ Download the SECTION validation notebook (Tableau "
                           "formula ↔ app ↔ tables, per metric, Cortex-judged)",
                           sec_nb, file_name=f"{stem}_section_validation.ipynb",
                           mime="application/x-ipynb+json",
                           key=f"{key_prefix}sec_nb")


def _report_sections(run, model):
    """Structured, human-readable migration report for ONE run: grouped counts
    of what actually carried over, the per-stage pipeline outcome, and the
    validation verdict. Returns [(section_title, [(label, value), ...]), ...]
    so the SAME content renders on screen and into the PDF (one source of
    truth -- the two can never drift)."""
    ir, res = run["ir"] or {}, run["parity"]
    vs = res["summary"]
    calcs = ir.get("calcs") or {}
    drops = ir.get("calc_drops") or {}
    params = ir.get("params") or {}
    hier = ir.get("hierarchies") or {}
    sheets = [sh for d in ir.get("dashboards", []) for sh in d.get("sheets", [])]
    kinds = Counter(sh.get("kind", "?") for sh in sheets)
    n_tables = sum(m["n_tables"] for m in model) if model else len(
        ir.get("datasources") or [])
    n_joins = sum(len(m["joins"]) for m in model) if model else 0

    bug = vs["measures_bug"]
    val_status = ("All measures reproduce the workbook's numbers"
                  if bug == 0 else f"{bug} measure(s) need review")
    rows_ok = sum(1 for d in res["datasources"] if d.get("match") is True)

    return [
        ("Items migrated - data model", [
            ("Datasources", len(ir.get("datasources") or [])),
            ("Tables", n_tables),
            ("Relationships / joins", n_joins),
            ("Calculated fields translated", len({id(v) for v in calcs.values()})),
            ("Calculated fields to review", len(drops)),
            ("Parameters", len(params)),
            ("Hierarchies", len(hier)),
        ]),
        ("Items migrated - dashboards & sheets", [
            ("Dashboards", len(ir.get("dashboards") or [])),
            ("Worksheets rebuilt", len(sheets)),
            ("Distinct chart types", len(kinds)),
            ("Chart types used", ", ".join(f"{k} x{n}"
                                           for k, n in sorted(kinds.items())) or "-"),
        ]),
        ("Pipeline stages", [
            ("1. Discovery", "Complete - datasources loaded into Snowflake"),
            ("2. Parsing", "Complete - workbook parsed into the IR"),
            ("3. Data model & semantic", "Complete - model replicated in Snowflake"),
            ("4. App creation", f"Complete - generated {run['app_name']}"),
            ("5. Validation", f"Complete - {val_status}"),
        ]),
        ("Validation result", [
            ("Measures checked", vs["measures_checked"]),
            ("Measures passed", vs["measures_pass"]),
            ("Measures needing review", bug),
            ("Datasources with matching row counts", f"{rows_ok} of "
                                                     f"{len(res['datasources'])}"),
            ("Calculations translated", vs["calcs_translated"]),
            ("Verdict", "PASS" if bug == 0 else "REVIEW"),
        ]),
        ("Output", [
            ("Generated application", run["app_name"]),
            ("Snowflake target", run["target"]),
            ("AI tokens used for the migration", "0 (fully deterministic)"),
        ]),
    ]


def _pdf_safe(text):
    """fpdf2's built-in fonts are latin-1 only -- a stray unicode character in a
    workbook/sheet name would raise mid-render. Downgrade unrepresentable
    characters instead of failing the whole report."""
    return str(text).encode("latin-1", "replace").decode("latin-1")


def _build_migration_report_pdf(run, sections):
    """Render the SAME sections shown on screen into a downloadable PDF.
    Import is function-local so a missing fpdf2 degrades to an honest caption
    rather than breaking app startup."""
    from fpdf import FPDF

    bug = run["parity"]["summary"]["measures_bug"]
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 10, "Tableau to Streamlit-in-Snowflake", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 10, "Migration Report", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    pdf.set_text_color(90, 90, 90)
    pdf.cell(0, 7, _pdf_safe(f"Workbook: {run['workbook']}"),
             new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7, _pdf_safe(f"Target: {run['target']}   |   Run at {run['ts']}"),
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)

    # Verdict banner
    pdf.ln(3)
    pdf.set_fill_color(*((232, 248, 239) if bug == 0 else (253, 240, 230)))
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 11, _pdf_safe(
        "  VERDICT: PASS - the converted app reproduces the workbook's numbers"
        if bug == 0 else
        f"  VERDICT: REVIEW - {bug} measure(s) diverge"),
        new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.ln(4)

    # Fixed two-column rows. The VALUE column is word-wrapped by measuring text
    # with get_string_width() -- available in every fpdf2 version (local dev has
    # 2.8.2, the Snowflake channel ships 2.8.7), unlike the newer multi_cell
    # dry-run helpers. Without wrapping, long values (the chart-type list, the
    # per-stage status text) run straight off the right edge of the page.
    LABEL_W, VALUE_W, LH = 82.0, 108.0, 7.0

    def _wrap(text, width):
        out, cur = [], ""
        for word in str(text).split():
            trial = (cur + " " + word).strip()
            if pdf.get_string_width(trial) <= width - 4 or not cur:
                cur = trial
            else:
                out.append(cur)
                cur = word
        if cur:
            out.append(cur)
        return out or [""]

    for title, rows in sections:
        if pdf.get_y() > 250:                      # keep a heading with its rows
            pdf.add_page()
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_fill_color(23, 32, 42)
        pdf.set_text_color(255, 255, 255)
        pdf.cell(0, 9, _pdf_safe("  " + title.upper()),
                 new_x="LMARGIN", new_y="NEXT", fill=True)
        pdf.set_text_color(0, 0, 0)
        for i, (label, value) in enumerate(rows):
            pdf.set_font("Helvetica", "B", 10)
            vlines = _wrap(_pdf_safe(str(value)), VALUE_W)
            h = LH * len(vlines)
            if pdf.get_y() + h > 280:
                pdf.add_page()
            fill = (248, 250, 252) if i % 2 == 0 else (255, 255, 255)
            x0, y0 = pdf.get_x(), pdf.get_y()
            pdf.set_fill_color(*fill)
            pdf.set_font("Helvetica", "", 10)
            pdf.cell(LABEL_W, h, _pdf_safe("  " + str(label)), border="B",
                     fill=True)
            pdf.set_font("Helvetica", "B", 10)
            for j, line in enumerate(vlines):
                pdf.set_xy(x0 + LABEL_W, y0 + j * LH)
                pdf.set_fill_color(*fill)
                pdf.cell(VALUE_W, LH, line, fill=True,
                         border=("B" if j == len(vlines) - 1 else 0))
            pdf.set_xy(x0, y0 + h)
        pdf.ln(5)

    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(120, 120, 120)
    pdf.multi_cell(0, 5, _pdf_safe(
        "Generated by the Tableau to Streamlit-in-Snowflake accelerator. The "
        "migration is fully deterministic (no AI writes the app or its SQL); "
        "every measure above was computed two independent ways and "
        "cross-checked against known Tableau figures where available."))
    out = pdf.output()
    return bytes(out)


# --------------------------------------------------------------------------- #
# On-demand sections. Previously inline inside run_pipeline's Stage 5; now
# callable from the workbench Validation page against an already-computed run.
# --------------------------------------------------------------------------- #
def build_section_notebook(ir, res, book_name, stem, live_truth=None):
    """Per-SECTION Tableau validation notebook: each migrated metric against
    its Tableau TWB formula + known figure, Cortex-judged at notebook-run
    time. Returns the notebook text, or None with the reason surfaced."""
    try:
        sec_nb = parity.build_section_validation_notebook(
            ir, res, book_name, tableau_truth=live_truth or None)
        os.makedirs("reports", exist_ok=True)
        with open(os.path.join("reports", f"{stem}_section_validation.ipynb"),
                  "w", encoding="utf-8") as f:
            f.write(sec_nb)
        return sec_nb
    except Exception as e:
        st.caption(f"Section validation notebook unavailable: {e}")
        return None


def render_proof_first_validation(ir, res, session, book_name, stem, conn=None):
    """The validation surface for this page: proof-first, per-CHART,
    row-level comparison (Tableau / Streamlit / backend), real Tableau vs.
    Streamlit screenshots with a structural similarity score, and a
    deterministic per-dashboard formula/interaction summary.

    Replaces the older three-panel Deep Validation UI (Cortex per-metric
    judge, skill-methodology Cortex-narrated write-up, Cortex vision
    comparison) -- 2026-08 explicit user decision: this one pack is the
    clearer, more useful validation surface, and it is FULLY DETERMINISTIC
    (build_cortex_dashboard_validation_report is called with narrate=False,
    so no Cortex tokens are spent and no CORTEX.COMPLETE reachability is
    required for anything shown here) -- matching this project's "AI grows/
    assists the tool, determinism ships the app" rule (ARCHITECTURE.md §10):
    this is the SHIPPED validation surface, not an AI-narrated one.

    Still needs a live Snowflake session: _compute_section_data's own
    combined per-dashboard query runs through session.sql(...) (Snowpark)
    directly, not the local/Snowflake-agnostic backend.run_sql the rest of
    the app uses, so there is no DuckDB fallback for that one query.

    `conn` (the Tableau REST connection this workbook was fetched through,
    or None for a file upload) feeds the pack's REAL Tableau screenshots
    and crosstab rows -- see _build_validation_pack."""
    if not ir.get("dashboards"):
        return
    st.markdown("**Proof-first validation** — per-CHART, row-level three-way "
                "comparison (Tableau / Streamlit / backend) at each chart's "
                "own displayed grain, real Tableau vs. Streamlit screenshots "
                "with a structural similarity score, and a deterministic "
                "Tableau-formula-vs-app-SQL + interaction check per "
                "dashboard. Fully deterministic — no AI narrative, no Cortex "
                "tokens spent. A chart whose data could not be extracted is "
                "reported BLOCKED with its stated reason — never silently "
                "omitted.")
    if session is None:
        st.info("Unavailable for this run: no live Snowflake session. This "
                "runs a real combined query per dashboard, which needs a "
                "live Snowflake session (Snowpark) — not available against "
                "the local DuckDB backend. Available in the deployed "
                "Snowsight app, or connect a local `snow` CLI session in "
                "the sidebar.")
        return

    if conn:
        st.caption("Live Tableau connection detected — real Tableau "
                   "screenshots and per-worksheet crosstab rows are pulled "
                   "over REST automatically. Upload crosstabs below only "
                   "for a worksheet REST can't reach.")
    else:
        st.caption("This workbook was uploaded as a file, so there is no "
                   "live Tableau connection to pull screenshots/rows from — "
                   "every chart's Tableau leg is honestly BLOCKED unless you "
                   "supply an exported crosstab CSV below.")
    uploaded_crosstabs = st.file_uploader(
        "Optional: Tableau crosstab CSV exports (one per worksheet — name "
        "each file after its worksheet, e.g. 'Customer Ranking.csv'; "
        "Data → Download Crosstab in Tableau). Matched by exact worksheet "
        "name, never guessed.",
        type=["csv"], accept_multiple_files=True, key=f"_proof_csvs_{stem}")

    key = f"_proof_{stem}"
    if st.button("Run proof-first validation", key=f"_proof_btn_{stem}",
                 icon=":material/play_arrow:"):
        with st.status("Running one live query per dashboard, pulling "
                       "Tableau evidence, and building the proof pack …",
                       expanded=False) as pstg:
            try:
                sections, _rollup = parity.build_cortex_dashboard_validation_report(
                    ir, session, book_name, res=res, narrate=False)
                # Real app-side screenshots -- ONE real browser session
                # against the ONE running app captures every dashboard, so
                # this is also far cheaper than the old per-dashboard
                # re-render loop (one Streamlit boot instead of N).
                #
                # `render_notes` no longer carries per-SHEET detail (a real
                # screenshot has no "which sheet did the exporter fail to
                # draw" concept -- if a sheet is missing from the real app,
                # that IS the app's real behaviour, correctly visible to
                # Cortex vision rather than suppressed as a renderer limit).
                # A dashboard whose TAB itself could not be captured still
                # gets a note, so it is skipped from the visual comparison
                # rather than compared against nothing.
                all_shots, screenshot_notes = _render_app_screenshots(ir, stem)
                resolved_titles = {s["title"] for s in sections
                                   if not s.get("skipped")}
                shots = {t: p for t, p in all_shots.items() if t in resolved_titles}
                render_notes = {
                    n["dashboard"]: [{"sheet": None, "rendered": False,
                                      "reason": n["reason"]}]
                    for n in screenshot_notes
                    if n.get("dashboard") in resolved_titles and not n["captured"]}
                try:
                    pack = _build_validation_pack(
                        ir, sections, book_name, stem, conn=conn,
                        streamlit_shots=shots,
                        uploaded_crosstabs=uploaded_crosstabs)
                    pack_error = None if pack is not None else (
                        "no comparable chart evidence in this workbook")
                except Exception as e:
                    pack, pack_error = None, f"{type(e).__name__}: {e}"
                pstg.update(state="complete",
                            label=f"✅ Validated {len(sections)} dashboard "
                                  "section(s) — deterministic, 0 tokens")
            except Exception as e:
                sections, pack, pack_error = [], None, None
                render_notes = {}
                pstg.update(state="error",
                            label=f"❌ Proof-first validation failed — "
                                  f"{type(e).__name__}: {e}")
        st.session_state[key] = {"sections": sections, "pack": pack,
                                 "pack_error": pack_error,
                                 "render_notes": render_notes}

    result = st.session_state.get(key)
    if not result:
        return
    sections = result.get("sections") or []
    if sections:
        rows = []
        for sec in sections:
            if sec.get("skipped"):
                rows.append({"Dashboard": sec["title"],
                            "Formula/interaction check":
                            f"⚠️ skipped — {sec['skipped']}",
                            "Interaction proof": "—"})
                continue
            bugs = sum(1 for r in sec.get("formula_rows", []) if not r["match"])
            irows = sec.get("interaction_rows") or []
            ifails = sum(1 for r in irows if r["status"] == "FAIL")
            rows.append({
                "Dashboard": sec["title"],
                "Formula/interaction check": ("✅ no bugs found" if bugs == 0
                                              else f"❌ {bugs} formula mismatch(es)"),
                "Interaction proof": (f"{len(irows) - ifails}/{len(irows)} pass"
                                      if irows else "—")})
        st.dataframe(rows, use_container_width=True, hide_index=True)

    pack = result.get("pack")
    if pack:
        st.markdown(f"**Workbook decision: `{pack['status']}`.**")
        st.dataframe(pack["rows"], use_container_width=True, hide_index=True)
        _render_pack_files(pack, stem)
        with st.expander("Evidence provenance — what was actually used"):
            st.caption(
                f"Tableau screenshots pulled: {len(pack.get('tableau_screenshots', []))} · "
                f"Streamlit screenshots (real app, not a re-render): "
                f"{len(pack.get('streamlit_screenshots', []))} · "
                f"Tableau worksheet views with REST crosstab rows: "
                f"{len(pack.get('tableau_csv_views', []))} · "
                f"uploaded crosstabs matched: {len(pack.get('uploaded_csv_matches', []))}")
            shot_problems = [n for n in (pack.get("screenshot_notes") or [])
                             if not n["captured"]]
            if shot_problems:
                st.warning(
                    "Real app screenshot unavailable" +
                    (f" for: {', '.join(n['dashboard'] for n in shot_problems if n['dashboard'])}"
                     if any(n["dashboard"] for n in shot_problems) else "")
                    + " — visual evidence for the affected dashboard(s) is "
                    "BLOCKED, not substituted. Reason: "
                    + (shot_problems[0]["reason"] or "unknown")[:400])
            if pack.get("unmatched_crosstabs"):
                st.warning(
                    "Uploaded crosstab file(s) matched no worksheet name, so "
                    "they were NOT used (rename to the exact worksheet name "
                    "and re-run): " + ", ".join(pack["unmatched_crosstabs"]))
            blocked_rows = [r for r in pack["rows"] if r["Status"] == "BLOCKED"]
            if blocked_rows:
                st.markdown("**BLOCKED charts and why:**")
                st.dataframe(
                    [{"Dashboard": r["Dashboard"], "Chart": r["Chart"],
                      "Why not validated": r["Why not validated"] or
                      "no comparable Tableau/Streamlit/backend evidence"}
                     for r in blocked_rows],
                    use_container_width=True, hide_index=True)
    elif result.get("pack_error"):
        st.caption(f"Validation pack unavailable: {result['pack_error']}")

    render_cortex_vision(ir, session, result, stem)

    if st.button("Clear proof-first validation results",
                 key=f"_proof_clear_{stem}", icon=":material/refresh:"):
        st.session_state.pop(key, None)
        st.session_state.pop(f"_vision_{stem}", None)
        st.rerun()


def render_cortex_vision(ir, session, result, stem):
    """CORTEX VISION over the dashboard image pairs -- the one place in this
    project where an AI judgment genuinely beats the deterministic check,
    and the reason it was (re-)wired in 2026-08-07 at the user's request.

    WHY, concretely: the pack's own `visual_similarity` downsamples both
    images to 320x180 greyscale and diffs their edges. Opening every real
    Superstore image pair showed what that score is worth -- Tableau's
    'Sales Performance vs Target' (month x segment rows, category columns,
    bars coloured above/below target) against the app's plain monthly bar
    chart, two COMPLETELY different charts, scored 0.797, while the one
    genuinely-matching pair scored 0.858. The whole signal range is ~0.06
    wide and the 0.85 threshold sits inside that noise: the metric cannot
    tell a faithful migration from an unrelated chart. A vision model can
    say "Tableau breaks this down by segment and category against target;
    the app shows a single monthly total" -- which is the actual question.

    The deterministic score is KEPT and shown beside Cortex's verdict as a
    labelled cross-check, never replaced or hidden: same rule R2 used --
    Cortex is handed two ALREADY-REAL images and judges them; it never
    renders, computes or invents either side. A disagreement between the
    two is surfaced, not silently resolved.

    Cortex CANNOT capture the images -- that is entirely on us. As of
    2026-08-10 the app image is a REAL screenshot of the real running app
    (app_screenshot.py -- a real browser against a local `streamlit run`),
    the same one shown in the report above, not a re-render of the app's
    chart objects; the improvement flows through here automatically since
    both this function and the pack read the same `streamlit_screenshot_
    paths`. It is still not the DEPLOYED SSO-gated app's own pixels (that
    remains structurally impossible from outside it) -- but it is now the
    real generated app, not an approximation of one. When no browser was
    available where the pack was generated, `s_paths` is simply empty for
    the affected dashboards and no pair is judged for them, rather than
    Cortex being handed a fabricated or stale image."""
    pack = (result or {}).get("pack") or {}
    t_paths = pack.get("tableau_screenshot_paths") or {}
    s_paths = pack.get("streamlit_screenshot_paths") or {}
    pairs = sorted(set(t_paths) & set(s_paths))

    st.markdown("**Cortex visual validation** — an AI vision verdict per "
                "dashboard, on the SAME image pair scored above. The "
                "structural similarity score is kept beside it as a "
                "labelled cross-check, never overwritten.")
    if not pack:
        st.caption("Run the proof-first validation above first — this judges "
                   "the image pair that run produces.")
        return
    if session is None:
        st.info("Unavailable: no live Snowflake session — Cortex vision runs "
                "through `AI_COMPLETE`, which does not exist against the "
                "local DuckDB backend. Available in the deployed Snowsight "
                "app.")
        return
    if not pairs:
        missing = []
        if not t_paths:
            missing.append("no Tableau screenshots (needs a live Tableau "
                           "Server/Cloud connection — a file-uploaded "
                           "workbook has no view to render)")
        if not s_paths:
            missing.append("no app-side screenshots")
        st.info("Unavailable: " + "; ".join(missing or
                ["no dashboard has BOTH a Tableau and an app image"]) + ".")
        return

    vkey = f"_vision_{stem}"
    if st.button(f"Run Cortex vision on {len(pairs)} dashboard pair(s)",
                 key=f"_vision_btn_{stem}", icon=":material/visibility:"):
        notes_by_dash = (result or {}).get("render_notes") or {}
        stage_fqn = f"{pipeline.LOAD_DB}.{pipeline.LOAD_SCHEMA}.R8_VISION_STAGE"
        rows, tokens, raw_by_dash = [], 0, {}
        with st.status("Staging each image pair and asking Cortex to "
                       "describe then compare them …", expanded=False) as vst:
            for title in pairs:
                try:
                    with open(t_paths[title], "rb") as fh:
                        t_png = fh.read()
                    with open(s_paths[title], "rb") as fh:
                        s_png = fh.read()
                except Exception as e:
                    rows.append({"Dashboard": title, "Cortex verdict": "⚠️ SKIPPED",
                                "Similarity": "—",
                                "Cortex says": f"could not read image: {e}"})
                    continue
                try:
                    res = parity.vision_validate_dashboard(
                        session, stage_fqn, title, t_png, s_png,
                        app_render_notes=notes_by_dash.get(title))
                except Exception as e:
                    rows.append({"Dashboard": title, "Cortex verdict": "⚠️ SKIPPED",
                                "Similarity": "—",
                                "Cortex says": f"{type(e).__name__}: {e}"})
                    continue
                tokens += res.get("tokens") or 0
                raw_by_dash[title] = res
                icon = {"PASS": "✅ PASS", "BUG": "❌ BUG",
                        "UNKNOWN": "⚠️ UNKNOWN"}.get(res["verdict"], res["verdict"])
                note = res.get("explanation") or "; ".join(res.get("errors") or []) or ""
                if res.get("omitted_sheets"):
                    note += (" · not compared (the static exporter cannot draw "
                             "these): " + ", ".join(res["omitted_sheets"]))
                rows.append({"Dashboard": title, "Cortex verdict": icon,
                            "Similarity": _similarity_for(pack, title),
                            "Cortex says": note})
            vst.update(state="complete",
                       label=f"✅ Cortex judged {len(rows)} dashboard pair(s) · "
                             f"~{tokens} tokens")
        st.session_state[vkey] = {"rows": rows, "tokens": tokens,
                                  "raw": raw_by_dash}
        if tokens:
            st.session_state["_ai_tokens_used"] = (
                st.session_state.get("_ai_tokens_used", 0) + tokens)
        # Re-render the client-facing report WITH the Cortex verdicts now
        # folded into each dashboard's section A. The deterministic verdicts
        # are untouched -- the report re-renders from the same stored
        # summary, only gaining the vision commentary.
        if pack.get("dashboard_report") and pack.get("summary_json"):
            try:
                import validation_report_dashboard as VRD
                with open(pack["dashboard_report"], "w", encoding="utf-8") as fh:
                    fh.write(VRD.render_dashboard_report(
                        pack["summary_json"],
                        meta={"vision_note": f"run — ~{tokens} tokens",
                              "backend": f"{pipeline.LOAD_DB}."
                                         f"{pipeline.LOAD_SCHEMA}"},
                        vision=raw_by_dash))
                st.caption("The dashboard validation report above has been "
                           "regenerated with these Cortex verdicts included.")
            except Exception as exc:
                st.caption(f"Could not fold vision into the report: {exc}")

    vis = st.session_state.get(vkey)
    if not vis:
        return
    st.dataframe(vis["rows"], use_container_width=True, hide_index=True)
    disagree = [r for r in vis["rows"]
                if r["Cortex verdict"].endswith("BUG") and "PASS" in str(r["Similarity"])]
    if disagree:
        st.warning(
            f"⚠️ {len(disagree)} dashboard(s) where Cortex found a real visual "
            "difference that the structural similarity score passed. Cortex's "
            "reasoning is shown above; neither verdict silently overrides the "
            "other — review these first.")
    if vis["tokens"]:
        st.caption(f"~{vis['tokens']} tokens on the last run — added to the "
                   "session's Cortex token total (sidebar).")


def _similarity_for(pack, title):
    """The deterministic similarity score already computed for this
    dashboard, shown beside Cortex's verdict as the labelled cross-check."""
    for row in pack.get("visual_scores") or []:
        if row.get("dashboard") == title:
            sim, thr = row.get("similarity"), row.get("threshold")
            if sim is None:
                return "not scored"
            return f"{sim:.3f} ({'PASS' if sim >= thr else 'REVIEW'} @ {thr})"
    return "—"


def render_migration_report(report_run, model):
    """Structured migration report for ONE run, on screen and as a PDF.
    `report_run` must carry: ir / parity / workbook / target / ts / stem."""
    sections = _report_sections(report_run, model)
    for title, rows in sections:
        st.markdown(f"**{title}**")
        st.dataframe([{"Item": label, "Value": value} for label, value in rows],
                     use_container_width=True, hide_index=True)
    try:
        pdf = _build_migration_report_pdf(report_run, sections)
    except Exception as e:
        st.caption(f"PDF export unavailable ({type(e).__name__}: {e}). The "
                   "report above is complete; only the download is affected.")
        return
    st.download_button("⬇ Download the migration report (PDF)", pdf,
                       file_name=f"{report_run['stem']}_migration_report.pdf",
                       mime="application/pdf", icon=":material/download:")
