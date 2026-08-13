# Tableau -> Streamlit-in-Snowflake (SiS) Accelerator

Converts a Tableau **workbook** (every dashboard) into one runnable Streamlit
app whose data lives in Snowflake. Mirrors the team's Tableau->Power BI
accelerator: Python parses the workbook + infers the visuals; a calc-translation
layer turns Tableau formulas into Snowflake SQL (instead of DAX).

Generic, workbook-agnostic. Not hand-built per dashboard.

## Pipeline

```
 any .twb / .twbx
      |
      v  tableau_parser.py    Stage 1  PARSE + INFER  -> workbook_ir.json
      |    per sheet: reads cols/rows shelves + encodings, classifies every
      |    field (dimension / measure / date), and INFERS the chart kind the way
      |    Tableau's "Automatic" mark does: kpi | bar | line | area | scatter |
      |    heatmap | map | table. Also extracts calcs, parameters, filters, tabs.
      v  calc_translator.py   Stage 2  TRANSLATE      (Tableau formula -> Snowflake SQL)
      |    aggregates, ratios, COUNTD, FIXED LOD, IF/CASE, DATEDIFF, params.
      v  codegen.py           Stage 3  BUNDLE         -> app.py  (IR + engine)
      v  engine.py            RUNTIME interpreter      renders any spec, one st.tab per dashboard
      |
      +--> run locally (DuckDB over CSV/Excel)
      +--> deploy to Snowsight (Snowpark), unchanged
```

Regenerate for any workbook:

```bash
python tableau_parser.py YourWorkbook.twb -o workbook_ir.json
python codegen.py workbook_ir.json -o app.py
streamlit run app.py
```

## Files

| File | Role |
|---|---|
| `tableau_parser.py` | Stage 1. Workbook -> IR with chart inference, calcs, filters, tabs. |
| `calc_translator.py` | Stage 2. `to_phys`, `agg_sql`, measure library, generic formula->SQL. |
| `codegen.py` | Stage 3. Bundles IR + engine into `app.py`. |
| `engine.py` | Runtime interpreter: renders kpi/bar/line/area/scatter/heatmap/map/table; one tab per dashboard; per-tab filters. |
| `app.py` | GENERATED. Embeds the IR, calls `engine.run(IR)`. |
| `backend.py` | Data layer. DuckDB locally / Snowpark in SiS. Loads ALL columns (any field queryable). |
| `config.py` | DB/schema/table + optional column overrides. |
| `data/` | Local export for testing. |

Deploy these together: `app.py`, `engine.py`, `backend.py`, `config.py`, `calc_translator.py`.

## What it does generically

- **Reads any column** (backend normalizes every CSV/Snowflake column to UPPER_SNAKE; no hand-mapping).
- **Infers chart kind** from the shelves, so `mark="Automatic"` sheets work.
- **Translates calculated fields** to SQL and substitutes them (so a measure that
  is a Tableau calc, e.g. `Profit Ratio`, `Days to Ship`, `Sales Forecast`, renders).
- **One tab per dashboard**, with each dashboard's filters as Tableau-style
  single-select (All) / date-range controls.

## Coverage on Superstore.twb (6 dashboards)

Renders: **ALL 6 — Overview, Customers, Product, Shipping, Order Details,
Commission Model** (17/17 sheets; `report.py` fidelity 99%, 0 failed).

- **Multi-datasource** works: each sheet queries the table mapped for its
  Tableau datasource in `config.DATASOURCES` (Orders + Sales Commission +
  Sales Target).
- **`INDEX()`** translates to a `ROW_NUMBER()` window (best-effort ordering,
  recorded as a finding). Reference lines render, including parameter-valued
  ones (resolved to the parameter default).
- **Nothing fails silently**: anything not convertible becomes a finding in
  `findings.py`, shown in-app under "Migration notes" and in
  `reports/migration_assessment.md`.

Verify any regeneration with `python validate_numbers.py` (numeric parity
vs Tableau-verified figures) and `python verify_visual.py` (PNG eyeball).

## Known limits (all reported, none silent)

- **Table calcs** `RANK`, `WINDOW_*`, `LOOKUP`, running totals -> still
  untranslated (reported per sheet). `INDEX()` is supported.
- **Parameters** are frozen at workbook defaults (no runtime controls yet).
- **Custom rich tooltips, exact pixel layout** -> not modeled.
- Pixel-exact parity is not the goal; structural + numeric parity is.

## Calc-translation reference

| Tableau | Snowflake SQL |
|---|---|
| `SUM/AVG/MIN/MAX(x)` | same aggregate |
| `COUNTD([x])` | `COUNT(DISTINCT x)` |
| `SUM([Profit])/SUM([Sales])` | `SUM(PROFIT)/NULLIF(SUM(SALES),0)` |
| `MONTH([Order Date])` | `DATE_TRUNC('MONTH', ORDER_DATE)` |
| `{FIXED [k]: SUM(x)}` | `SUM(x) OVER (PARTITION BY k)` |
| `IF/ELSEIF/END`, `CASE` | `CASE WHEN ... END` |
| `[Parameters].[X]` | substituted with the parameter's default value |
| `INDEX()`, `RANK()` | not yet (table calcs) |
