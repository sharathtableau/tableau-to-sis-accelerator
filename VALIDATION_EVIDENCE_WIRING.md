# Validation evidence bridge wiring

**Status: IMPLEMENTED 2026-08-07, deployed live to `pipeline_demo`.** This
document is kept as the original spec/reference; the sections below describe
the plan as given, not always the exact final code (see the deviations and
follow-ups noted here). For the as-built picture see `ARCHITECTURE.md` §11
and `deep_validation.render_proof_first_validation`.

**Extended later the same day — see `ARCHITECTURE.md` §16 (NOT deployed).**
Three things changed after this spec landed, all driven by actually OPENING
the generated image pairs instead of reading their similarity scores:
1. `headless_render` now captures ALL FOUR channels (Altair, Plotly via
   kaleido, KPI tiles and tables via PIL) in one render pass — 5 of 10
   dashboards previously produced no app-side image at all, so their visual
   validation was auto-BLOCKED. Sheet coverage 12/20 → 20/20.
2. **Cortex vision** is wired in (`deep_validation.render_cortex_vision`),
   because the structural similarity score provably cannot do this job: two
   completely different charts scored 0.797 while the one genuinely-matching
   pair scored 0.858. The deterministic score is kept beside the AI verdict
   as a labelled cross-check, never replaced.
3. A new client-facing renderer, `validation_report_dashboard.py`, emits the
   per-dashboard A/B/C/D report with a chart data contract, pairwise
   source verdicts, consolidated exceptions and a sign-off record.

The six gates in §6 below are unchanged and still enforced.

**What matches this doc exactly:** §1 (`_build_validation_pack` calls
`VEB.build_complete_validation_spec`), §2 (separate Tableau/Streamlit
screenshots via `save_png_map`, real similarity score), §3 (REST crosstab
rows via `pull_all_view_csvs`, or an uploaded-CSV fallback via
`_apply_uploaded_crosstabs` — canonical-filename matched, never guessed),
§4 (`VEB.REGISTRY.record_chart` called from inside `engine.py`, immediately
before `st.plotly_chart`/`st.dataframe`/the rank-table HTML build, with the
final post-filter/sort/top-N dataframe), §6 (all six gates unchanged —
enforced by `validation_report.py`, untouched by this work).

**What deviated, and why:**
- §1's `_build_validation_pack` signature grew `conn=`, `streamlit_shots=`,
  `uploaded_crosstabs=` instead of a single `evidence` param — the function
  builds its OWN `EvidenceBundle` internally (pulling screenshots/CSVs via
  `conn`) rather than requiring the caller to pre-build one, so
  `pipeline_app.py` only has to pass the Tableau connection through.
- §4's engine-side recording only fires explicitly for the chart kinds the
  existing Vega-Lite-encoding-based capture (`validation_adapter.
  resolve_chart_columns`) can never see at all: `r_map`/`r_treemap`
  (Plotly), `r_table` (plain `st.dataframe`), and the rank-table branch of
  `r_circle` (hand-built HTML). Every Altair-rendered kind still goes
  through the pre-existing reverse-engineered path, which
  `build_complete_validation_spec` already prefers an explicit `REGISTRY`
  payload over when both exist.
- §4's KPI example is handled by the PRE-EXISTING `validation_adapter.
  build_kpi_chart_spec` (via `st.metric` capture), not a new
  `REGISTRY.record_chart` call — it already worked and needed no change.
- Detail/list-table sheets (`r_table`) cap recorded evidence to the top 30
  displayed rows, not the full (up to 200) displayed set — 2026-08-07
  explicit user decision: a reviewer spot-checks the top rows in their
  displayed sort order, not all 200.

**Real bugs found live, fixed same session (not in this doc's original
scope, discovered by actually running it against the real account):**
1. Tableau REST's view list is DASHBOARD-granular, not per-worksheet — a
   sheet nested inside a multi-sheet dashboard has no view_id of its own.
   Fixed with a same-dashboard-name fallback, restricted to the
   unambiguous 1-sheet-per-dashboard case only (never guessed for a
   multi-sheet dashboard).
2. `headless_render.capture_sheet_chart` kept only the LAST of several
   `st.altair_chart` calls a multi-panel sheet (`r_mbar`, faceted
   small-multiples) makes during one render, silently dropping every
   panel before it — a dashboard's Streamlit-side screenshot was missing
   5 of 6 KPI panels. Fixed by capturing every call and combining with
   `alt.hconcat`.
3. A duplicate/mistagged registry entry (keyed under an empty dashboard
   name) from `capture_sheet_kpis`' own second render pass over a sheet
   the Altair capture already tried — fixed by threading `dashboard_name`
   through that call too, plus making `engine._record_chart_evidence`
   idempotent per (dashboard, sheet) as defense-in-depth.

**Still genuinely open (not a regression, found by running this for
real):**
- Tableau's REST crosstab sometimes returns a long/pivoted shape
  (`Measure Names`/`Measure Values` columns) instead of one column per
  measure — the column aligner correctly refuses rather than guessing how
  to reshape it, but that means even a correctly-matched CSV can't
  validate yet.
- A chart's grain can ask for `Order Date` while Tableau's crosstab header
  is date-part-grouped (`Month of Order Date`, etc.) — no alias exists yet.
- Two measures (`Days to Ship Scheduled`, `Sales Forecast`) don't resolve
  against the backend table via `parity._resolve_measure_sql`.
- `treemap`/rank-table capture is unit-tested (`test_validation_evidence_
  bridge.py`) but not yet exercised by any real corpus workbook — none has
  those chart kinds.

Add `validation_evidence_bridge.py` beside the accelerator's existing
`validation_adapter.py` and `validation_report.py`.

The existing report renderer is retained. This bridge fixes evidence acquisition
and orchestration; it does not weaken the `no proof, no pass` rule.

## 1. Replace the validation-pack orchestration

Update `_build_validation_pack` so it accepts an `EvidenceBundle` and calls the
complete spec builder:

```python
import validation_evidence_bridge as VEB

def _build_validation_pack(ir, sections, book_name, stem, evidence):
    import io
    import json
    import os
    import zipfile
    import validation_report as VR

    def _table_for(dash):
        info = parity.collect_dashboard_section(ir, dash)
        return info["table"] if info else config.ORDERS

    out_dir = os.path.join("reports", f"{stem}_validation_pack")
    spec = VEB.build_complete_validation_spec(
        ir=ir,
        sections=sections,
        book_name=book_name,
        table_for_dashboard=_table_for,
        evidence=evidence,
        environment="UAT",
        visual_artifact_dir=os.path.join(out_dir, "visual-staging"),
    )
    result = VR.generate_report(spec, out_dir)

    summary = json.loads(open(result["summary"], encoding="utf-8").read())
    rows = []
    for dash in summary.get("dashboards", []):
        for chart in dash.get("charts", []):
            counts = chart.get("source_counts", {})
            rows.append({
                "Dashboard": dash["name"],
                "Chart": chart["title"],
                "Grain": ", ".join(chart.get("grain") or []) or "-",
                "Rows (T/S/B)": f"{counts.get('tableau', 0)}/{counts.get('streamlit', 0)}/{counts.get('backend', 0)}",
                "Failed values": chart.get("failed_cells", 0),
                "Status": chart.get("status"),
                "Why not validated": chart.get("skip_reason", ""),
            })

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(out_dir):
            for name in files:
                full = os.path.join(root, name)
                zf.write(full, os.path.relpath(full, out_dir))
    return {"status": result["status"], "rows": rows, "zip": buf.getvalue()}
```

## 2. Build the evidence bundle before generating the report

```python
import validation_evidence_bridge as VEB

VEB.REGISTRY.clear()
evidence = VEB.EvidenceBundle()

# Save real dashboard PNG bytes returned by the existing Tableau and
# headless Streamlit capture functions.
tableau_paths = VEB.save_png_map(
    tableau_dashboard_png_bytes,
    f"reports/{stem}_validation_input/tableau",
    "tableau",
)
streamlit_paths = VEB.save_png_map(
    streamlit_dashboard_png_bytes,
    f"reports/{stem}_validation_input/streamlit",
    "streamlit",
)

for dashboard in ir.get("dashboards", []):
    name = dashboard.get("title") or dashboard["name"]
    evidence.set_visual(
        name,
        tableau_screenshot=tableau_paths.get(name),
        streamlit_screenshot=streamlit_paths.get(name),
        threshold=0.85,
    )
```

Do not use the same screenshot for both sides. Missing either side must remain
`BLOCKED`.

## 3. Supply Tableau sheet data

When the workbook came from Tableau Server/Cloud, map each Tableau worksheet to
its view ID and load the rendered crosstab/CSV:

```python
loader = VEB.rest_tableau_loader(
    tableau_client,
    {
        ("Customer Analysis", "Customer Ranking"): "tableau-view-id-123",
        ("Sales Forecast", "Sales Forecast"): "tableau-view-id-456",
    },
)
VEB.hydrate_tableau_rows(evidence, ir, loader)
```

For a file-uploaded workbook, accept exported crosstab files instead:

```python
evidence.set_tableau_csv(
    "Customer Analysis",
    "Customer Ranking",
    uploaded_customer_ranking_csv_bytes,
)
```

A `.twb` definition alone cannot produce authoritative Tableau aggregate rows.
If no REST export or uploaded crosstab exists, keep the Tableau leg absent.

## 4. Emit explicit chart evidence from the rendering engine

Add this immediately after a chart's final dataframe has been filtered, sorted,
ranked and prepared for rendering, before calling `st.altair_chart`,
`st.plotly_chart`, `st.dataframe`, or `st.metric`:

```python
import validation_evidence_bridge as VEB

VEB.REGISTRY.record_chart(
    dashboard=dashboard_title,
    sheet=sheet["title"],
    chart_type=sheet["kind"],
    grain=["Customer Name"],
    measures=[
        {"name": "Sales", "field": "Sales", "kind": "currency", "display_decimals": 0},
        {"name": "Profit Ratio", "field": "Profit Ratio", "kind": "percent",
         "display_decimals": 1, "value_scale": "fraction"},
    ],
    rows=final_dataframe.rename(columns={"DIM": "Customer Name", "VAL": "Sales"}),
    query=sql_used_by_the_app,
    filters=active_filters,
    sort=["Sales DESC"],
)
```

The payload must contain exactly the rows shown to the user. Record after Top-N,
filters, table calculations and sorting, not the raw query result.

### KPI example

```python
VEB.REGISTRY.record_chart(
    dashboard=dashboard_title,
    sheet=sheet["title"],
    chart_type="kpi",
    grain=["KPI tile"],
    measures=[{"name": "Estimated Sales", "field": "Estimated Sales",
               "kind": "currency", "display_decimals": 0}],
    rows=[{"KPI tile": sheet["name"], "Estimated Sales": raw_numeric_value}],
    query=sql_used_by_the_app,
)
```

Use stable dashboard/sheet/internal-measure IDs to construct the payload. Visible
KPI labels may be duplicated and must not be used as the only identity.

### Scatterplot example

```python
VEB.REGISTRY.record_chart(
    dashboard="Customer Analysis",
    sheet="Sales and Profit by Customer",
    chart_type="scatter",
    grain=["Customer ID"],
    measures=["Sales", "Profit"],
    rows=plot_dataframe[["Customer ID", "Sales", "Profit"]],
)
```

A scatterplot requires a stable mark key even when the visible axes contain only
measures. Use a real detail/tooltip key from the chart specification; do not
invent one.

## 5. Call the updated pack builder

```python
dv_pack = _build_validation_pack(ir, sections, up.name, stem, evidence)
```

## 6. Required gates

Keep these rules unchanged:

- Missing Tableau screenshot: visual `BLOCKED`.
- Missing Tableau chart rows: chart `BLOCKED`, even if Streamlit equals backend.
- Ambiguous field mapping: `BLOCKED`, never guessed.
- Missing backend recomputation: `BLOCKED`.
- Any value outside precision-derived tolerance: `FAIL`.
- AI narrative cannot change deterministic status.

## 7. Recommended regression tests

Add tests for:

1. Both screenshots are copied separately and produce a visual score.
2. Tableau CSV with a UTF-8 BOM is parsed correctly.
3. `SUM(Sales)` aligns to expected `Sales` without fuzzy guessing.
4. Ambiguous columns block the chart.
5. KPI payloads use stable IDs despite duplicated visible labels.
6. Scatter payloads include a stable mark key.
7. A full Tableau/Streamlit/backend match passes.
8. Missing Tableau rows remains blocked.
9. Streamlit/backend mismatch fails even when Tableau is missing.
10. Every compared row is retained in `comparison.csv`.
