# Power BI to Streamlit in Snowflake: Phase 1

Converts a Power BI project (PBIP, with the PBIR report format and the TMDL
model format) into a Streamlit app that queries Snowflake directly. The
Tableau path is unchanged.

```bash
python pbi_convert.py path/to/Report.pbip -o out/            # or a folder / .zip
python pbi_convert.py Report.pbip -o out/ --database PROD_DB --schema BI
python pbi_convert.py Report.pbip -o out/ --bind Orders=DB.SCHEMA.VIEW
```

Output: `report_ir.json`, `app_<report>.py`, `conversion_report.md`,
`conversion_report.json`. Deploy from Python with
`pbi_convert.deploy(session, "out/app_<report>.py", db=..., schema=...)`.
It reuses `pipeline.deploy_streamlit_app`, pins `RUNTIME_NAME =
SYSTEM$WAREHOUSE_RUNTIME`, and stages `PBI_RUNTIME_MODULES`. No credentials are
written to any file.

## Architecture

```
.pbip ──► pbi_pbir.py   (pages, visuals, filters)     ┐ Power BI specific
      └─► pbi_tmdl.py   (tables, measures, rels, roles)┘
             │
             ▼
      pbi_parser.py ── dax_rules.py (documented DAX subset)
             │         binds tables from the Power Query Snowflake navigation
             ▼
      report-ir/1  (report_ir.py: shared, source-neutral, validated)
             │
      ┌──────┴──────────────┬───────────────────────┐
      ▼                     ▼                       ▼
 report_sql.py        conversion_report.py    codegen.build(ir, runtime="report_runtime")
 (one SQL per visual)  (md + json)             │
      ▼                                        ▼
 report_runtime.py ◄── report_data.py ◄── query_cache.py (per-viewer)
 (Streamlit)           (Snowpark or local DuckDB)
```

Why a new IR instead of the Tableau IR: the Tableau IR (`tableau_parser` to
`engine`) assumes one table per sheet, Tableau shelves and marks, and
queries dashboard filters against a default `ORDERS` table. Power BI measures
live in the model, are evaluated in a filter context, and work across
relationships. Forcing that into the Tableau IR would distort both. Shared
pieces reused as they are: `codegen.build` (now takes a `runtime` argument;
the Tableau output is byte-identical), `backend._normalize_columns` /
`_read_source_file` for local data, `pipeline.deploy_streamlit_app`,
`environment.yml`, and the new `query_cache.py`.

## Supported DAX (rules, no AI)

| Pattern | Level |
| --- | --- |
| `SUM`, `AVERAGE`, `MIN`, `MAX`, `COUNT`, `COUNTA`, `COUNTROWS(T)`, `DISTINCTCOUNT`, `DISTINCTCOUNTNOBLANK` over a column | Auto |
| `DIVIDE(a, b[, alt])`, measure references, `+ - *`, numbers | Auto |
| `VAR … RETURN` with no CALCULATE or iterator in the expression (substituted) | Auto |
| `/` operator (zero gives BLANK here, Infinity in Power BI) | Partial |
| `CALCULATE(expr, T[c] = literal)`, `CALCULATE(expr, T[c] IN {…})` | Partial |
| `SUMX` / `AVERAGEX` / `MINX` / `MAXX` over a plain table with a supported row expression | Partial |
| Calculated columns: `IF`, `AND`/`OR`/`NOT`, comparisons, `IN {…}`, `DATEDIFF(…, DAY/MONTH/QUARTER/YEAR)`, `YEAR`/`MONTH`/`DAY`/`QUARTER`, `FORMAT(date, "MMM"/"MMMM"/"YYYY")`, `ABS`, `ROUND`, `&` | Partial |
| Everything else: time intelligence, `FILTER`, `ALL*`, `RELATED`, `RANKX`, `SWITCH`, `USERELATIONSHIP`, iterators over table expressions, … | Held |

A held measure is not deployed. The app shows "Pending verification" in its
place and the rest of the report ships (decision of 2026-11-26). Phase 2 adds
Cortex proposals for held measures, with a numeric proof against the
published report before anything is released.

### Filter context and BLANK

* Each aggregate inside a measure is evaluated in the visual's filter context.
  A `CALCULATE` column filter replaces slicer and visual filters on that same
  column, and the visual is not split by that column. This is the
  Power BI behavior, and the tests check it: Furniture Sales stays at
  754,747.76 under a Category = Office Supplies slicer.
* Rows whose measures are all BLANK are dropped, as Power BI does.
* Table visuals get a total row computed by the model, not by summing the
  rows, so ratios total correctly.
* BLANK handling: `a + b` and `a - b` treat one BLANK as 0. COUNT-family
  results return BLANK over no rows. `DISTINCTCOUNT` counts BLANK as a value.
  `x = 0` and `x = ""` are true for BLANK. Each of these is a test.

### Calculated date tables

A `CALENDAR`/`CALENDARAUTO` table with an active relationship from a fact date
column is not materialized. Its columns are derived from the fact date, for
example `Date[Year] = YEAR(Orders.ORDER_DATE)`. This is Partial: dates with no
facts do not appear, and time intelligence is not available. Materialize a
date dimension in Snowflake when either matters.

## Visuals

| Power BI | Rendered as | Level |
| --- | --- | --- |
| card | KPI tile | Auto |
| clustered/stacked bar and column | Altair bar | Auto |
| line, area, stacked area | Altair line/area | Auto |
| scatter | Altair points (detail, X, Y) | Auto |
| table (`tableEx`) | dataframe and model total row | Auto |
| slicer: text, number, date | multiselect, range slider, date range | Auto |
| 100% stacked bar/column | normalized stack | Partial |
| matrix (`pivotTable`) | pivot, no subtotals | Partial |
| line + column combo | layered, independent second axis | Partial |
| multi-row card, new card, KPI | KPI tiles | Partial |
| text box | markdown | Partial |
| map, pie, donut, treemap, gauge, waterfall, funnel, custom visuals, images, buttons | placeholder with the reason | Unsupported |

Not reproduced: cross-filtering by clicking a visual, drill-down,
drill-through, tooltip pages, bookmarks, custom visual interactions, and
slicers that filter other slicers. Each appears in the conversion report.

Layout: visuals are grouped into rows by their vertical position, and widths
follow the Power BI widths. Pixel positions are not reproduced.

## Security

* **Query cache.** `query_cache.py` keys cached results on the viewer in
  Snowflake. With no known viewer, results are cached per browser session
  only. Before this change the Tableau engine keyed results on the SQL text
  alone, which shares rows between viewers on the container runtime.
* **Runtime pin.** Deploys now run `ALTER STREAMLIT … SET RUNTIME_NAME =
  'SYSTEM$WAREHOUSE_RUNTIME'`. Snowflake documents that `ROOT_LOCATION`
  deploys don't support container runtimes. BCR-2342 changes the default
  for new Streamlits.
* **Row-level security is not enforced by the app.** Power BI roles become a
  proposed row access policy in the conversion report. The converter never
  applies it. Warehouse-runtime apps query with owner's rights, so a policy
  keyed on the viewer's role does not narrow rows per viewer. Production
  needs a reviewed policy and a caller's-rights setup, or an app restricted
  to viewers who may see all rows.
* Identifiers are always quoted. Filter values are typed literals with
  escaping. A test sends a value containing quotes and SQL text.

## Verified vs not verified (2026-11-27)

Verified, in `tests/test_powerbi.py` (34 tests) and `tests/test_hardening.py` (6 tests):
* The sample PBIP parses into a valid IR, with 5 pages, 28 visuals, 12
  measures, 1 relationship and 1 role.
* Using local DuckDB over `data/Sample - Superstore.csv` (the rows loaded
  into `WBR_DB.PUBLIC.SAMPLE_SUPERSTORE`), these match the README answer key:
  Total Sales 2,326,534.35, Profit 292,296.81, Profit Ratio 12.6%,
  Quantity 38,654, Orders 5,111, Customers 804, Furniture 754,747.76,
  Avg Days to Ship 3.96, and sales by region.
* The generated app renders all 5 pages with no exceptions (Streamlit
  AppTest). The KPIs read correctly, and a Region slicer changes Total Sales
  to 691,828.
* The Tableau suite gives the same result before and after: 73 passed,
  6 failed, 3 errors. The failures and errors are the same tests as before
  this change. They come from fresh-environment dependency drift and are not
  caused by this work.

Not verified:
* The sample has never been opened in Power BI Desktop. Real Desktop output
  may use field-role names, filter shapes or TMDL details that the sample
  doesn't have. The filter, implicit-aggregation and date-hierarchy readers
  follow Microsoft's schemas and docs but haven't been tested on a real file.
* Nothing has run against Snowflake from this branch. The SQL avoids
  dialect-specific features, but the first deploy is the real test.
* The implicit-aggregation codes 2 and 5 (count variants) are marked Partial
  until a real file confirms them.

## Not in Phase 1

Cortex translation of held measures, the numeric proof against the published
report (Power BI Execute Queries API), the repair agent, automatic RLS and
Native App packaging. V3 now lives in `v3/` with the same two fixes applied
(per-session cache key for unknown viewers, runtime pin on deploy). The Power
BI path does not use V3 code yet; reusing V3's Cortex and validation pieces is
Phase 2 work.
