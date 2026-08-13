# Delivery checklist — Tableau dashboard → Streamlit

Follow this for EVERY tab. Do not tell the user a tab "matches" until step 5 passes.

1. **Ground truth first.** Obtain a Tableau screenshot of the tab AND the `.twbx`
   (bundles full data + exact worksheet definitions). Never build a chart blind.
2. **Full/real data.** Extract bundled data from the `.twbx`; verify a grand total
   against Tableau (Superstore Sales = $2,326,534) before trusting anything.
3. **Parse the exact worksheet def** — rows/cols shelves incl. COMPOUND fields,
   mark, every encoding (color/size/text/detail), detail grain, datepart.
4. **Build the chart.**
5. **RENDER TO PNG AND LOOK.** `python verify_visual.py "<Tab>"` → open the PNGs in
   `_preview/` and compare to the screenshot. Headless "no exception" is NOT proof.
6. **One tab at a time, verified**, then move on.
7. **Restart Streamlit** after regenerating (Ctrl+C, then `streamlit run app.py`).

## Engine conventions that avoid known failures
- No Altair `.facet()` in Streamlit (renders blank). Use combined row labels +
  `st.columns` for column-facets.
- Never `SELECT col AS col ... GROUP BY 1` (DuckDB alias error). Use aliases that
  differ from column names.
- Dates: auto-detect ISO vs day-first; read CSV as `utf-8-sig` (BOM).

## Definition of done for a NEW FEATURE/CONSTRUCT (prevents tracker drift)

Shipping code is not "done." The weekly status report is mostly hand-maintained
(only the regression gate count self-updates), so a feature that lands without
its tracker updates makes the report **silently understate progress**. Every new
construct must, IN THE SAME CHANGE:

1. **Code** — parser/engine rule (never a workbook-specific hack).
2. **Regression gate** — a `test_<feature>` in `tests/test_regression.py`, wired
   into `main()`, proven to have teeth (fails on the pre-fix code).
3. **Audit status** — flip the construct's label in `audit_features.py` /
   `audit_filters.py` from `gap`/`partial` to its real status.
4. **status_config.json** — update the relevant `roadmap` / `mvp` / `phases` /
   `components` entry (status + note).
5. **Register it in `test_tracker_consistency`** — add a row to that gate's
   `SHIPPED` list mapping the feature to its proving gate.

`test_tracker_consistency` **mechanically enforces 3–5**: if a feature has a
passing proving-gate but its audit still says `gap` or its roadmap still says
`planned`, the suite goes RED and names exactly what's stale. So "remember to
update 6 files" becomes "the tests tell you which file is behind." Run
`python weekly_status.py` after, and the report will reflect the change.

## Regenerate
```bash
python tableau_parser.py Superstore.twb -o workbook_ir.json
python codegen.py workbook_ir.json -o app.py
python verify_visual.py "Product Drilldown"   # eyeball _preview/*.png
streamlit run app.py
```
