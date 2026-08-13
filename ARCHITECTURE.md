# Tableau → Streamlit-in-Snowflake Accelerator — AS-BUILT Architecture

*(This documents what exists and is verified today. The original vision is in
`tableau_to_streamlit_accelerator_architecture.md`; this is its implemented
counterpart. Updated 2026-08-07. For day-by-day session narrative see
`NEW_CHAT.md`; for the manager-facing scope/estimate table see
`MVP_ACCELERATOR_SCOPE.md`; for the full data-model scenario matrix (built /
tested / confidence per construct) see `DATA_MODEL_STATUS.md` — that document
is the detailed reference for everything summarized in §7e below.)*

## 1. One-line summary

A **fully deterministic, code-driven** pipeline (zero AI at conversion time)
that turns any Tableau `.twb`/`.twbx` into a runnable Streamlit app whose SQL
executes on DuckDB locally and Snowpark in Snowflake, with every unsupported
construct **reported, never silently dropped**. Snowflake Cortex is
integrated as an **opt-in, gated** layer on top of this deterministic core
(§10) — it never writes app code or has the final say; the conversion path
itself is unchanged and stays byte-identical without `--connection`. Two
entry surfaces share this same deterministic core: the scriptable CLI
(`convert.py`, §2) and a human-gated Streamlit workbench
(`pipeline_app.py` + `deep_validation.py`, §12–14) that also DEPLOYS the
generated app and offers an in-app Cortex Analyst chat — both call the identical `pipeline.py` /
`tableau_parser.py` / `engine.py` logic, so there is one conversion behavior,
not two.

## 2. The pipeline

```
       python convert.py Book.twbx [--serve PORT] [--connection wbr] [--deploy-semantic]
┌─────────────────────────────────────────────────────────────────────────┐
│                                                                         │
│  Book.twb/.twbx                                                         │
│      │                                                                  │
│      ▼ 1. init_workbook.py         ONBOARD                              │
│      │    extracts bundled data (csv/xls/hyper→CSV via tableauhyperapi) │
│      │    matches each datasource to its file (filename OR dbname,      │
│      │    federated-id stems, CSV-sibling preference)                   │
│      │    → datasources.json   (config.py merges over built-ins)        │
│      │                                                                  │
│      ▼ 2. audit_coverage.py + report.py       ASSESS (before building)  │
│      │    XML-declared vs IR-carried diff: mark classes, labels,        │
│      │    colors (3 mechanisms), filters, table calcs, dual axes        │
│      │    → metadata/compatibility_report.json                          │
│      │    → reports/migration_assessment.md   (client-facing)           │
│      │    → sql/generated_views.sql           (reviewable SQL)          │
│      │                                                                  │
│      ▼ 3. tableau_parser.py        PARSE → IR                           │
│      │    → <book>_ir.json                                              │
│      │                                                                  │
│      ▼ 4. codegen.py               GENERATE                             │
│      │    → app_<book>.py   (IR embedded + engine.run(IR); do not edit) │
│      │                                                                  │
│      ▼ 5. headless verify          every sheet rendered, blockers fail  │
│      │                                                                  │
│      ▼ 6. streamlit run app_<book>.py     (or --serve PORT)             │
│                                                                         │
│  --- OPT-IN, only with --connection (byte-identical run without it) --- │
│      ▼ 6b. cortex_semantic.py      SEMANTIC     → sql/cortex/*.sql+yaml │
│      │     [--deploy-semantic] also CREATEs the semantic view live      │
│      ▼ 7.  cortex_calc_fallback.py AI CALC FALLBACK (calc_drops only)   │
│      │     → reports/cortex_calc_proposals_<book>.md   (REVIEW REQUIRED)│
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘

Deploy set (frozen snapshot, never edited after deploy):
  app_<book>.py + engine.py + backend.py + config.py + calc_translator.py
  + findings.py + profile_<client>.py + environment.yml  (snowflake.yml)
```

## 3. What the parser captures (all deterministic, from the XML)

**Structure**: dashboards + zone geometry (row/column layout with widths),
standalone worksheet tabs (worksheet windows not on any dashboard),
storyboards (detected → reported unsupported), per-sheet datasource,
blends (multi-datasource sheets → flagged, primary queried).

**Chart inference** (pill grammar: `agg:Field:qk|ok|nk` — qk continuous,
ok/nk DISCRETE): 19 chart kinds —
`kpi, bar (incl. grouped side-by-side + stacked w/ midpoint labels), dtbar
(discrete-date stacked bars), line, area, mbar (multi-measure panels),
scatter, heatmap, circle, map (US states + world countries; continuous +
categorical), table, pctbar (100% strip w/ % labels), dots, gantt, bubbles
(packed circles), strips, treemap, pie` — plus multi-measure compound
shelves (`[avg:A + avg:B]` → one pane per measure).

**Semantics**: calculated fields → SQL (aggregates, ratios w/ NULLIF, COUNTD,
IF/CASE, FIXED LODs → window fns, table-scoped scalar LODs `{MAX([x])}` →
scalar subqueries via `__TBL__`, INDEX() → ROW_NUMBER window, DATETRUNC/
TODAY/NOW name mapping; nested calc + parameter refs by internal name);
cross-datasource (blend) calcs → dropped + reported. Filters: quantitative
ranges, categorical IN (incl. %null%), **EXCLUDE filters (`except` →
NOT IN / IS NOT NULL)**, date-part filters (EXTRACT), no-op detection.
Sorts (computed + manual + tie-breaks), count-of-records → COUNT(*),
% -of-total, reference lines (incl. live parameter-valued), trend lines,
field RENAMES (caption→source column map, resolved via `engine.px()`).

**The COMPLETE color model** (closed over the schema after a full sweep —
6 constructs, all implemented):
1. per-value maps (`Shipped Late → #9c755f`)
2. continuous scales (palette + domain)
3. named palette refs without maps (legend-order assignment, TABLEAU_PALETTES)
4. Measure-Names per-measure colors (datasource-level)
5. fixed per-sheet mark colors (`format attr='mark-color'`)
6. custom color lists
Plus value aliases (`true → Profitable`) from the workbook, case-insensitive.

**Runtime parameters**: translator emits `__PARAM_X__` tokens; the engine
renders sidebar what-if controls and substitutes CURRENT values before the
SQL cache. Changing New Quota 500K→600K moves OTE $142,000→$160,400 (tested).

## 4. Runtime architecture (the generated app)

```
app_<book>.py  =  IR (JSON) + engine.run(IR)
                       │
   ┌───────────────────┼─────────────────────────┐
   │ engine.py         ▼                         │
   │  configure(ir): calcs, aliases, colmap,     │
   │                 color_maps, palette_refs,   │
   │                 params                      │
   │  one tab per dashboard; zone-geometry rows; │
   │  per-tab filter widgets; sidebar params;    │
   │  19 renderers (Altair + Plotly for map/     │
   │  treemap); scrollable panes for >25-row     │
   │  category lists; workbook number formats    │
   │  on axes ($600B not $600G)                  │
   │                                             │
   │  TRANSPARENCY RULE: never guess. Unresolved │
   │  measure/filter/chart/0-row result →        │
   │  findings.py → in-app "Migration notes"     │
   │  expander + compatibility report            │
   └──────────────┬──────────────────────────────┘
                  ▼ SQL (identical text both environments)
   backend.py: Snowpark session detected? → Snowflake
               else → DuckDB over data/ files (all columns UPPER_SNAKE,
               date auto-detect, thousands-separator coercion)
   config.py:  DATASOURCES map (caption → FQN + local file), merged from
               datasources.json; profile_<client>.py = per-client overrides
               (measure library, caption aliases, label/color overrides)
```

## 5. Verification & trust layers (the "no silent failure" system)

1. **Assessment-time**: `report.py` LEADS with a **visual-risk checklist** —
   every sheet ranked HIGH (won't render / BLOCKER) or MED (mark-not-honored,
   forecast, axis, color-unresolved) so the reviewer gets the eyeball punch
   list on day one. Handled constructs are excluded so it never cries wolf.
2. **Parse-time**: `audit_coverage.py` — XML-declared vs IR-carried checks
   (correctness/appearance/cosmetic), incl. the full color category.
   `audit_calcs.py` / `audit_filters.py` / `audit_features.py` measure the
   whole surface and feed `weekly_status.py`.
3. **Generate-time**: `codegen.py` **verifies its own output** — `ast.parse()`s
   the file it is about to write and round-trip-asserts the embedded IR,
   raising rather than emitting a broken app.
4. **Query-time**: 0-row results → WARNING finding (exclude-filter bug class).
5. **Render-time**: `verify_visual.py` → `_preview/*.png` for eyeball vs
   Tableau screenshots (`screenshots/`); convert.py step 5 fails on blockers.
   Captures EVERY channel (Altair PNG, plotly PNG via kaleido, table row
   counts, KPI values); a zero-output sheet prints `[WARN]`.
6. **Numeric**: `validate_numbers.py` — 14 Tableau-verified figures
   (grand totals, per-region values, parameter math).
7. **Interaction**: `tests/test_app_interactions.py` — streamlit AppTest
   drives the real app headless; a param control must move the KPI.
8. **Layout-structure**: `test_layout_snapshots` snapshots every corpus
   dashboard's layout tree (direction + sheet order, geometry-free) into
   `tests/layout_snapshots.json` and FAILS on drift, plus enforces "no placed
   sheet lost" / "no sheet duplicated". 40 dashboards locked. Regenerate
   deliberately (`--update-layout-snapshots`) and eyeball the diff.
9. **Regression**: `tests/test_regression.py` — one command, **57 gates**
   (IR invariants + locked chart kinds, all-sheets render probe, what-if math,
   numeric harness, interaction test, container + absolute layout, semantic
   layer, codegen parsability, the converter's OWN decode path, one gate per
   shipped construct, two offline Cortex-layer guards (§10), the parity
   validation check (§11), the Snowflake-uppercase-alias-folding simulation,
   the reserved-word-alias static scan, the pre-loaded-table reuse guard for
   hyper-only workbooks (§11), the pushed-Snowflake-session query-routing
   guard (§11), the no-local-file parity-fallback guard (§11), the deploy-
   button DDL/staging gate (§12), the data-model-view + scope-B gates (§12),
   and the four data-model-completeness gates added 2026-07-26 (§7e:
   `test_auto_bind_existing_snowflake_table`,
   `test_non_star_join_and_blends`, `test_r10_multitable_source_autobind`,
   `test_r9_live_multitable_join` — plus one manual, non-auto-run live-session
   gate, `test_onboard_resolves_multitable_missing_before_stopping`, not
   counted in the 57 since `snow_session` can block on interactive SSO).

**Standing process rules** (each learned the expensive way):
- When a bug class appears twice, STOP fixing instances — enumerate the entire
  XML schema surface for that category, implement the closed set, add the
  audit guard. *(the color failures)*
- **No stage is exempt from verifying itself.** This document previously
  claimed codegen was "deterministic, never the failure stage." That was FALSE:
  workbook DATA (an apostrophe inside a group member) broke generated SYNTAX
  and the app died before rendering anything. Determinism is not proof.
- **Guard every plane you claim to convert.** Three layout regressions shipped
  green because the project had strong DATA guards and ZERO layout-STRUCTURE
  guards. A test suite only protects the dimensions it actually measures.
- **Never argue a visual claim from XML.** Ask for the screenshot.
- A construct fix must be corpus-swept across ALL workbooks before shipping
  (the sweep has caught multiple false-positives pre-ship), and it must update
  its audit in the SAME change — a stale audit silently misreports coverage.

## 6. Operating model

| Activity | Mechanism | Who edits code |
|---|---|---|
| Convert a workbook | `python convert.py Book.twbx` | nobody |
| Read the gaps | `reports/migration_assessment.md` | nobody |
| Local demo | `python restart_apps.py` (kills port squatters, one listener/port) | nobody |
| Deploy | `load_snowflake.py` + `snow streamlit deploy` (DEPLOY.md) | nobody |
| New chart/feature support | development on THIS repo, gated by regression suite | maintainer |

Deployed apps are frozen snapshots; the accelerator is the maintained product.
AI is used only to DEVELOP the accelerator (per the vision doc's policy),
never in the conversion path.

## 7. Proven corpus

| Workbook | Provenance | Sheets | Result | User-facing fix rounds |
|---|---|---|---|---|
| Superstore (6 dashboards) | development | 20 | 96%, 0 failed | many (development) |
| World Indicators | Tableau Public, foreign | 9 + story | 99%, 0 failed | 4 |
| Regional Analysis | user's, Snowflake-native | 6 | 100%, 0 failed | 0 (audit caught all) |
| Globalsalesdashboard | user's, 4 datasources | 15 | 14/15 (1 blend) | colors (→ schema closed) |
| Superstore 2024.3 (official sample) | migration sample pack | 20 | 96%, 0 failed | 0 |
| World Indicators 2024.3 (official) | migration sample pack | 11 | 100%, 0 failed | 0 (28% pre-fix) |
| E-Commerce #VOTD | advanced table calcs; **deployed live to SiS** | 50 | **97%, 0 failed** (46→53→68→73→97) | many (the frontier book) |
| Fil Test | user-authored filter/param test | 5 | 100%, 0 failed | 4 (found 4 real defects) |
| Superstore_TopN_MeasureSwap | user-authored generalization test | 3 | **ASSESSED 2026-07-22: 100%, 0 blockers, 0 calc-drops** — measure-swap-driven top-N numerically exact (Sales & Profit) | 0 |
| Superstore KPI Parameter Dashboard Live | user's, LIVE Snowflake connection | 4 | rendered live vs Tableau; drove the filter/param/top-N/context fixes | 6 (found+fixed via Snowsight) |
| EMEA DTC Performance KPIs | user's, LIVE sqlproxy (ships no data) | — | OUT OF MVP SCOPE (sqlproxy live class reported honestly, not queried) | — |
| **R3_Extract_Over_Existing_Table** | purpose-built, 2026-07-26 (same worksheets/dashboard/params as the KPI Live workbook above, with a bundled extract added) | 5 | **LIVE-VERIFIED**: Stage 1 shows `existing table (auto-bound, no copy)`, 10,194 rows — proves R3's single-table auto-bind on a real Snowsight upload | — |
| **R7_Chain_Orders_Product_Category** | purpose-built, 2026-07-26 — genuine depth-2 snowflake schema (Orders→Product→Category), no corpus workbook has this shape | 3 (1 dashboard) | Verified offline + live: flatten log says `relationship snowflake` (not star); SUM(sales)/category exact vs ground truth (Furniture 754,747.76 / Office Supplies 731,893.31 / Technology 839,893.28); 3 worksheets exercise all 3 tables incl. a combined `Product Detail` table | — |
| **R10_Chain_Over_Existing_Tables** | purpose-built, 2026-07-26 — same chain, but its 3 tables were pre-loaded SEPARATELY first | 3 (1 dashboard) | **LIVE-VERIFIED**: Stage 1 shows `data model bound to existing Snowflake tables -- no decode, no copy (R10)`; surfaced + fixed a real `onboard()` sequencing bug the same session (§7e) | — |
| **R9_Live_Join_Orders_Product_Category** (`.twb`, no bundled data) | purpose-built, 2026-07-26 — genuinely LIVE (no extract at all), joins R10's 3 pre-loaded tables | 3 (1 dashboard) | Verified directly against the real account (`onboard()` end to end); real Streamlit-UI upload confirmation still pending | — |

`Fil Test.twbx` is the highest-value workbook in the corpus per defect found:
five sheets exposed the codegen syntax break, the Superstore data-routing
gravity, the view-order table-calc filter gap, and the invented control
surface. Small hostile workbooks beat large clean ones.

Local demo ports: 8501 Superstore · 8502 World Indicators ·
8503 Regional Analysis · 8504 Global Sales · 8506 E-Commerce.
Converter: `streamlit run converter_app.py --server.port 8505` (must pass the
port; a bare `streamlit run` grabs 8501 and collides).

**Corpus caveat**: several `.twbx` files (WI / Regional / GlobalSales /
E-Commerce / 2024.3) were removed from the working dir; the audits currently
see 6 workbooks, and those books' `*_ir.json` are stale artifacts pointing at
wrong tables. The regression suite skips absent workbooks rather than crashing.

## 7b. Shipped since (2026-07-10/11)

- **Groups, sets, dual-axis/combo** — true overlays with independent axes;
  conditional sets via level-partitioned windows.
- **Top-N filters** — by field AND by parameter → ranking subquery honoring
  Tableau's order of operations (top-N before dimension/measure filters).
  **CONTEXT filters** (`<filter context='true'>`) ARE injected into the ranking
  subquery (top-N *within* the context, e.g. "top 10 customers in Central"),
  via `engine._value_predicate` + parser `context_columns` (2026-07-22).
  Dashboard filters scope to their bound worksheet (+ sheets that filter on the
  field), not blanket-applied to every sheet on the datasource.
- **Hierarchies / drill-down** — `<drill-path>` → per-sheet drill-level
  selector (Streamlit has no click-to-drill); deepest axis level swaps live.
- **Device layouts** — Phone/Tablet zones excluded from the desktop scan;
  one responsive layout rendered; drop reported per dashboard.
- **Table-calc engine** — WINDOW_SUM/AVG/MIN/MAX, RANK/RANK_DENSE/
  RANK_UNIQUE, INDEX → window-over-aggregate SQL (inline in grouped SELECT);
  agg-of-FIXED and window-in-window chains execute via a layered window
  hoist in `engine.q()`. Corpus table-calc coverage 43% → 100%.
- **Honest-reporting detections** — forecast overlays, subtotals,
  viz-in-tooltip, custom/initial SQL (report "Data-model notes"),
  relative-date filters, log/reversed axes: all now surface as findings
  instead of degrading silently.
- **2024.x XML compatibility** — feature-flag `<_.fcp...column>` tags,
  content-based date typing, date-part range filters → EXTRACT.
- **verify_visual** captures every output channel (Altair PNG, plotly PNG
  via kaleido, table row counts, KPI values); zero-output sheets = [WARN].
- **Relationship flatten (semantic layer phase 1)** — multi-table extracts
  (Tableau 2020.2+ stores relationship tables separately, joins NOT
  materialized) flatten at onboard time via the workbook's relationship
  graph: star-schema LEFT JOINs with Tableau's `col (Table)` collision
  renames. Non-star graphs: largest table + loud warning (never guess).
  The relationship graph is threaded IDENTICALLY through EVERY hyper-decode
  entry point — `init_workbook.py` AND `converter_app._decode_hypers_locally`.
  (A prior gap: the converter decoded without the graph, so a 3-table extract
  dumped the fact table only and every dimension column vanished — the sheet
  crashed at query time. Any new decode path must pass `relationships=`.)
  Defence-in-depth: a top-N whose ranking column is absent from the sheet's
  table degrades to a WARNING (`topn-column-missing`), never crashing SQL.
- **Container layout — both encodings.** A dashboard's layout can be encoded as
  flow containers (`layout-flow param='horz'|'vert'`) OR absolute geometry
  (`layout-basic` with per-zone x/y/w/h). `layout_tree` honors both: flow
  containers use their `param` direction; a `layout-basic` container has its
  children grouped into rows by geometry (`_rows_from_geometry` — children whose
  vertical bands overlap form one horz row, ordered by x; bands stack by y).
  Without the geometry pass, absolute-positioned dashboards (Regional Analysis
  View2, Superstore Customer tab) collapsed every side-by-side sheet into one
  vertical column. Locked by `test_absolute_layout_rows`.
  A dashboard zone is treated as the actual worksheet ONLY when it has no
  `type-v2`/`type`: Tableau gives the filter widget, color legend, and
  highlighter bound to a sheet the SAME name (tagged `filter`/`color`/
  `highlighter`), and mistaking one of those for the chart replaced a full-width
  ProductDetails with a 10227-wide legend. Locked by
  `test_legend_zone_not_mistaken_for_sheet`.
- **Window-dim guard** — INDEX()/RANK-as-dimension sheets render with a
  WARNING (view ordering/limit not applied; per-member values unchanged)
  instead of failing on a raw-column error.
- **Rank-table renderer** — Tableau's MIN(0)-placeholder + text-mark list
  sheets ("Top 3 Channels") render as rank tables (rank / member / value /
  %delta arrow) in the sheet's computed-sort order; verified value-exact
  against Tableau screenshots.
- **SQL hoist hardening** — paren-aware clause splitting (top-N IN-subqueries
  survive); hoist aliases never collide across layers (an alias reuse made
  DATEDIFF return 0 silently — caught by screenshot diff, regression-locked).
- **Calc identity** — internal `[Calculation_...]` names bind to translations
  via the XML's own name→caption map (formula-text matching mis-bound
  same-formula "(copy)" calcs).

## 7d. Data-model constructs — MVP (2026-07-21/22)

The three named data-model scenarios plus the routing/filter correctness work.
All key on generic Tableau XML constructs (no workbook/field hardcoding) and
are resolved at the ONBOARDING/DISCOVERY layer — `table_for()`'s return string
is interpolated raw into every `FROM {T}`, so a subquery works wherever a table
name does, and no engine query-path changes were needed for any of these.

- **Custom SQL execution** (`tableau_parser.custom_sql_sources`) — a live
  `<relation type='text'>` custom-SQL datasource on Snowflake runs VERBATIM as a
  derived table `(<sql>) AS <cap>_CSQL` (execution-gated: must compile+run, else
  MISSING). Non-Snowflake dialects reported honestly, not guessed. Extract-backed
  custom SQL unaffected (the extract already materializes it).
- **Live connection support** (`tableau_parser.live_connections`) — a live
  Snowflake datasource (single named table, no join/custom-SQL) is queried
  directly at its OWN `db.schema.table`, no copy; the load report PROBES it for
  real. Other live classes (sqlproxy/sqlserver/…) reported honestly instead of
  the prior silent stand-in-table fallback. Proven live: the KPI Parameter
  Dashboard Live workbook renders against `WBR_DB.PUBLIC.SUPERSTORE_ORDERS`.
- **Union support** (`tableau_parser.union_members` + `init_workbook.
  materialize_union`) — a `<relation type='union'>` combines same-schema members
  (CSV/Excel) row-wise; onboarding reads all members, concatenates by column
  name (adds a `Table Name` source column like Tableau), writes ONE combined CSV
  so downstream is unchanged. Was: pick_local_file grabbed one member, silently
  dropping the rest. Synthetic-validated (no corpus union exists).
- **Context filters + dashboard-filter scope** (see 7b top-N note) — context
  filters inject into top-N ranking; a placed dashboard filter applies only to
  its bound worksheet + sheets that filter on the same field; an 'All' selection
  governs its column (overrides a sheet's stale saved value). All found via live
  Snowsight testing of the KPI Live workbook.

Gates: `test_custom_sql_execution`, `test_live_connection_support`,
`test_union_support`, `test_context_filter_applied_inside_topn_ranking`,
`test_dashboard_filter_scoped_to_bound_sheets`,
`test_dashboard_filter_governs_sheet_filter`,
`test_dashboard_filter_all_overrides_sheet_saved_value`,
`test_bar_colored_by_own_axis_has_no_offset`. Suite = 49 gates.

DEFERRED (tracked): FIXED LOD ignores dimension filters (order-of-ops) — a
`{FIXED}` LOD wrongly sees dimension filters; needs a context-only subquery for
the LOD window. No corpus workbook exercises it; build against a real test
workbook rather than rewriting the regression-locked LOD path blind.

## 7e. Data-model completeness — R3, R7, R9, R10 (2026-07-26)

Closes the remaining named data-model scenarios (§7d's MVP covered custom
SQL, live single-table connections, and unions — this closes joins/blends and
the "already exists in the account" question for both extracts and live
connections). Full per-scenario status matrix, built-vs-tested-vs-confidence
tracking, and the honest open items live in `DATA_MODEL_STATUS.md`; this is
the architecture-level summary.

**R3 — auto-point to an existing Snowflake table (single-table extract).**
An extract-based workbook whose DECLARED source (`dbname.schema.table` read
from the connection metadata) already exists in the account binds directly to
it — no `.hyper` decode, no `write_pandas` copy. `tableau_parser.
source_tables()` reads the declared origin (the case `live_connections()`
deliberately skips, since that's scoped to genuinely-live sources);
`pipeline.resolve_source_binding()` ranks candidates: `sources.json` explicit
override → the workbook's own declared location → a single verified name
match in the load schema. **The guard is the feature**: every inferred bind
must pass a column-cover check against the columns Tableau itself recorded
for the source (`pipeline._columns_cover`) — a name match alone never binds,
an ambiguous name (same table in >1 schema) is surfaced as a choice, and
"cannot verify" is never treated as verified. LIVE-VERIFIED: uploading
`Workbooks/R3_Extract_Over_Existing_Table.twbx` shows Stage 1 reporting
`existing table (auto-bound, no copy)` at 10,194 rows. Gate
`test_auto_bind_existing_snowflake_table`.

**R7 — non-star joins + blends.** Before this, `semantic_layer` only
auto-built a join view for a STAR (every dimension joined directly to one
fact). `semantic_layer.join_plan()` generalizes to any deterministic TREE —
a star is just its depth-1 case; a SNOWFLAKE SCHEMA (Orders → Product →
Category, where Category joins to Product, not the fact) now builds
correctly too, with each join's `ON` clause referencing its OWN parent's
alias (a latent bug found while generalizing: the old emitter hardcoded
`ON f.<key>`, correct for a star, silently wrong at depth > 1). Genuinely
ambiguous graphs — multi-fact (>1 table nothing joins TO) or cyclic/
disconnected — still refuse, with a named reason, never a guess. ONE planner
drives both the view DDL and the extract flatten (`init_workbook.
flatten_tables`), so the two paths can never disagree about what's joinable —
this project's most-repeated bug class. **Blends** (a query-time link, not a
SQL join — Tableau aggregates the secondary to shared fields, then joins the
aggregate) are extracted from the workbook's own `<datasource-relationship>
<column-mapping>` XML (collapsing Tableau's one-map-per-pill-derivation into
real fields), reported per affected sheet with reviewable pre-aggregate
remodel SQL, and — the concrete payoff — fed to `cortex_calc_fallback`'s
prompt as a hard constraint, closing a documented bug where the AI had to
INFER a blend's join key and proposed the wrong one (`Region = Segment`)
against the wrong table. Verified against Superstore's real blend (`Sample -
Superstore` + `Sales Target` on Order Date/Category/Segment) and a second,
differently-shaped real blend in `Globalsalesdashboard.twbx`. Blends are
deliberately NOT auto-materialized as a deployed join (§8). Gate
`test_non_star_join_and_blends`.

**R10 — multi-table extract auto-bind to pre-existing separate tables.**
Generalizes R3 to a JOIN GRAPH: if a star/chain extract's constituent tables
already exist SEPARATELY in the account (not just previously copied there by
this project — the workbook's own original tables), bind the view directly to
them, skipping decode AND copy. Root cause of why this didn't already work:
`semantic_layer._connection(ds)` picked up the outer `federated` connection
wrapper instead of the real upstream `snowflake` one (verified live on
Regional Analysis — every table resolved to the assumed-copy location, never
its true `SANDBOX.DS.*` origin); fixed by reusing R3's own upstream-detection.
A new `_parse_relation_table()` handles 1/2/3-segment relation shapes without
doubling the schema (the `[DB].[SCHEMA].[TABLE]` fully-qualified form Regional
Analysis actually uses). **The verification, not just the parsing, is the
feature**: `pipeline.verify_table_candidate()` existence+column-checks each
declared-source table exactly like R3's resolver; `data_model_report`'s
`deployable` requires EVERY table in the graph to verify — one bad table
refuses the WHOLE model, never a partial bind; `build_data_model_tables`
skips decode+copy entirely once everything verifies. **A second real bug**
was found and fixed testing this live: `pipeline.onboard()`'s missing-check
only ever probed for a single table named after the caption, so a
multi-table datasource always looked MISSING and Stage 1 `st.stop()`ped
before Stage 3 (which DOES know how to resolve it) ever ran — fixed by
resolving a missing multi-table caption via `build_data_model_tables` inside
`onboard()` itself, before returning. LIVE-VERIFIED end to end (decode
genuinely blocked, simulating the SiS sandbox): Stage 1 shows `data model
bound to existing Snowflake tables -- no decode, no copy (R10)`, Stage 3
deploys the correct view. Gate `test_r10_multitable_source_autobind`.

**R9 — live connection with a JOIN across multiple tables.** The documented
refusal (`has_join → "not yet supported"`) turned out to hide a WORSE bug:
`live_connections()`'s relation scan matched every `<relation>` anywhere in
the datasource, including the object-model's per-object relations (a sibling
of `<connection>`, describing each joined table for Stage 3's view) — for a
live 2-table model built with the MODERN relationship syntax (not the legacy
`type='join'` tag the old check looked for), this silently returned
`queryable: True` pointed at just the FIRST table, DROPPING the join
entirely. Fixed by scoping the scan to relations that are direct children of
the federated `<connection>` element only. **Once that refusal fires
correctly, ZERO new plumbing was needed** — R10's `onboard()`-level
verify-then-deploy machinery never special-cased extract presence, so it
resolves a genuinely live multi-table caption identically. Verified against
the real account (`pipeline.onboard()` run directly, no fake session): a
purpose-built `.twb` with NO bundled data at all, joining 3 pre-loaded
Snowflake tables live, correctly bound with zero decode/copy; the Streamlit-
UI upload confirmation is the one remaining step (see `DATA_MODEL_STATUS.md`).
Gate `test_r9_live_multitable_join`.

**Standing lesson from this arc** (worth generalizing): R9 and R10 shared
one root cause (a connection-detection bug in `semantic_layer._connection`),
and fixing R10 closed R9 almost for free once the actual bug was found by
TESTING rather than assumed from the docstring. Twice this session, the
documented reason for a refusal ("not yet supported") turned out to be
covering for something worse (a silent wrong answer) once actually exercised
against real XML — reinforcing the project's existing rule to verify a
behavior claim by running it, not by reading the comment next to it.

## 7c. Shipped since (2026-07-14/17)

- **Dashboard control surface — parsed, not invented.** The `.twb` declares
  exactly which widgets sit on the canvas (`<zone type='filter'|'paramctrl'>`).
  The engine renders ONLY those, in one control row (Tableau puts param
  controls on the canvas, not a sidebar); the old union-of-every-sheet's-filters
  survives as the fallback when a dashboard declares no zone controls. A param
  DECLARED but never placed and never referenced by any calc or top-N gets no
  control anywhere (`_param_is_live`). Date-PART pills get a real
  "Year of Order Date" dropdown via EXTRACT; an unknown part degrades to a
  WARNING + date-range fallback, never a silent vanish. `param_domains` (from
  `<members>`) makes string params dropdowns rather than free text.
  *Open question for the user*: the "Drill: Product" selectbox is OUR
  hierarchy approximation — Tableau drills by clicking the axis, there is no
  such dropdown on its canvas. Keep, default off, or drop?
- **Codegen self-verification** — the IR embeds via `repr()`; codegen
  `ast.parse()`s its own output and round-trip-asserts the embedded IR.
  Closed the class where workbook DATA breaks generated SYNTAX.
- **View-order table-calc filters** (`INDEX()<=N` / `RANK()<=N`) push as
  ranking gates: the parser reads both `<computed-sort>` and 2020+
  `<shelf-sorts>/<shelf-sort-v2>`; the engine matches ONLY its own exact
  translator output (never guesses at a window) and applies the gate AFTER
  dimension filters + aggregation, per Tableau's order of operations. No sort
  or an unresolvable sort measure → WARNING, never a guessed order.
- **Chart-kind: mark class decides line vs bars.** An Automatic mark over a
  date is a LINE. Discreteness controls the AXIS, never the mark; only a
  pinned `mark='Bar'` yields discrete-date bars (corpus invariant, gated).
- **Non-data sheets** — text boxes / show-hide toggles / blanks carry no
  plottable field (generic predicate, not a name match) → skipped quietly and
  graded `n/a`, excluded from fidelity. This alone moved E-Commerce 73→97%:
  the accelerator had been mis-grading scaffolding as failed conversions.
- **Detail tables** — 3+ distinct discrete dims on one shelf with the other
  shelf empty = a Tableau text/detail listing, not a chart; sorted by the
  workbook's sort field DESC with `ProgressColumn` in-cell bars when the mark
  is Bar/Gantt.
- **Rank tables** render as hand-built HTML (`_rank_html` via `st.markdown`) —
  `st.dataframe`/`st.table` wrapped values mid-number in narrow columns
  ("$697,68"/"2"). Plain HTML behaves identically local and in SiS.
- **Placeholder member lists** — `(MIN|AVG|MAX)(const)` dummy-axis sheets are
  Tableau member lists, rendered as dimension + text tables.

### The Snowflake deploy story (the "preview OK, deployed breaks" class)

- **PIN THE SiS STREAMLIT VERSION FIRST.** The entire class came from
  `environment.yml` not pinning streamlit → SiS fell back to a pre-1.23 default
  (hide_index / container-kwarg / column-nesting errors) while local ran 1.57.
  Snowflake's channel max is **1.52.2**; pinned → `_max_col_nest()` returns to
  99 and deployed == preview. Query available versions via
  `information_schema.packages` (no `PARSE_VERSION` in Snowflake — sort
  client-side).
- **Snowpark is strict where DuckDB is lenient.** `write_pandas` stored
  datetime columns as NUMBER = epoch NANOSECONDS → `DATE_TRUNC`/`DATEDIFF`
  failed; `load_snowflake._fix_date_columns` converts each to `TIMESTAMP_NTZ`
  after write. DuckDB parsed CSV dates on read, so LOCAL NEVER SAW IT. Error
  *messages* also differ — any error-text-triggered retry must match both
  dialects (the window-in-aggregate hoist matches DuckDB's "cannot contain
  window function" AND Snowflake's "may not appear inside an aggregate
  function"). **The deployed app is the only real dialect test.**
- **Persistence: same-session verification proves nothing.** The first load's
  tables VANISHED after the session closed — in-session `COUNT(*)` passed, the
  deployed app got "does not exist or not authorized". `load_snowflake` now
  prints `SHOW TABLES` kind per write (TABLE vs TEMPORARY, with a CTAS
  side-name + rename fallback since a temp table SHADOWS the permanent name
  in-session). **Standing rule: always re-count from a NEW session.**
- **Dev vs deploy mappings are separate files.** `datasources.json` = local dev
  (SUPERSTORE.*); `datasources.deploy.json` = Snowflake (WBR_DB.*).
  `load_snowflake --database` writes the DEPLOY file only; mutating the dev
  mapping broke local verification once. `snowflake.yml` stages the deploy
  mapping AS `datasources.json`.

### The mandatory deploy loop

`app_<name>.py` **EMBEDS the IR at codegen time** — a parser fix is INVISIBLE
in a served app until codegen re-runs. This was the root cause of repeated
user-facing iteration rounds: a session reparsed, headless checks validated the
NEW IR, and the served app kept the OLD parse. Always:
**reparse → codegen → restart → browser-verify TOP AND BOTTOM of the page.**

## 8. Known limits (all REPORTED by the assessment, by design)

- Data blends: primary datasource queried; secondary fields → findings +
  reviewable remodel SQL (§7e) — **not auto-materialized as a deployed join**,
  because a blend is a query-time link (aggregate the secondary, then join),
  not structurally a SQL join; auto-materializing it is a deliberate scope
  limit, tracked, not a gap.
- Multi-table relationship EXTRACTS: flattened automatically (star **and**,
  since 2026-07-26, any deterministic tree incl. snowflake-schema chains —
  §7e). Non-star graphs (multi-fact / cyclic) still refuse with a named
  reason, by design (never guess a join). Live-connection joins and
  extract-based models whose tables already exist separately in the account
  are now BOTH handled (§7e) — this line described a real gap before
  2026-07-26; see `DATA_MODEL_STATUS.md` for the current per-scenario status.
- Dashboard actions / cross-highlighting / stories: not converted
  (Streamlit interaction model differs — positioned, not promised).
- Table calcs RUNNING_* / LOOKUP / TOTAL / FIRST / LAST: dropped + reported
  (they need view-layout ordering the accelerator refuses to guess).
- Rich hover tooltips: basic tooltips only (154× in corpus; user-deprioritized).
- **Bins**: not built — now **197×** across the corpus (was 2×; the newer
  workbooks lean on them heavily). The top corpus gap by occurrence; also
  blocks histogram.
- **Parameter-driven measure swap** ("Metric Selector"): a parameter whose
  value SELECTS which measure a sheet plots. The param IS captured; the
  switch-wiring is the scoped build. This is the one named feature gap left in
  E-Commerce, and `Superstore_TopN_MeasureSwap.twbx` exists to test it.
- Histogram / box-whisker / bullet / true matrix-pivot: not built.
- Pixel-exact layout: structural layout only (zone tree + geometry rows).
  Known cosmetics: hollow scatter marks, refline value labels, narrow-column
  truncation.
- **Per-workbook profile / datasource routing — FIXED (2026-07-21).** Was an
  open landmine: `config.py` imported `profile_superstore` for EVERY workbook
  and its built-in SUPERSTORE captions won over `datasources.json`, so a
  foreign workbook could silently inherit Superstore's curated measure SQL /
  formats / colors. Now `config.profile_for(source_file)` resolves the client
  profile PER WORKBOOK (explicit registry + optional `profiles.json`;
  unrecognized workbook → neutral `profile_default`, never a silent Superstore
  inherit), and in-Snowflake the staged `datasources.json` wins for every
  caption. Locked by `test_per_workbook_profile_routing`.

## 9. Roadmap (tasks #22–24, the data-plane end-state)

*(This section predates the R1–R10 roadmap numbering used in
`status_config.json` / `MVP_ACCELERATOR_SCOPE.md` / `NEW_CHAT.md` — those
files are the live-updated source of truth for current roadmap status; this
section is kept for the original task framing. As of 2026-07-26: R3 (single-
table extract auto-bind), R5 (human-gated deploy button, §12), R6 (in-app
Cortex Analyst chat, §12), R7 (non-star joins + blend link extraction, §7e),
R9 (live multi-table join, §7e), and R10 (multi-table extract auto-bind, §7e)
are all DONE. Remaining: R1 (pull a workbook from Tableau Server/Cloud by
link), R2 (per-section Cortex validation), R8 (Cortex vision screenshot
validation) — see `MVP_ACCELERATOR_SCOPE.md` for estimates.)*

1. **`convert.py --deploy`** (CLI path) — extract workbook → Snowflake tables
   auto-created + loaded + app deployed. **The credentialed dry-run is DONE**
   (2026-07-13, account wb19670-c2gpartners): all 11 corpus datasources
   loaded to `WBR_DB.PUBLIC` with row counts verified exact and independently
   re-verified from a second session; the E-Commerce app is live at
   `WBR_DB.PUBLIC.TABLEAU_TO_SIS_ECOMMERCE`. The full pipeline
   `.twbx → tables → deployed app` has run END TO END on a real account via
   this CLI path. **The equivalent human-gated in-app deploy button (R5) is
   ALSO done** (§12) — `pipeline_app.py`'s "🚀 Deploy to Snowflake" button now
   ships the generated app through the Snowpark session directly, no `snow`
   CLI needed, which is the path a Streamlit-in-Snowflake sandbox actually
   has available.
2. **Semantic-layer generator** — DONE (semantic_layer.py): relationship
   graph → `CREATE VIEW` DDL, columns aliased to the app's physical naming;
   live Snowflake sources keep their own db/schema (no data movement).
   Verified executable in DuckDB (48k rows, revenue == flatten exactly).
   `report.py` emits `sql/semantic_views.sql` per workbook.
3. **Live-source migration kit** — for SQL-Server-style live connections:
   landing DDL + views + source-connection checklist. (SiS apps cannot
   DirectQuery external DBs the way Power BI can; the pattern is
   replicate → land → view → app.)

## 10. Cortex AI layer — opt-in, gated (added 2026-07-20)

**Director requirement**: use Snowflake Cortex as a first-class element
(modernize BI, give the Snowflake field team a co-sell reason). Decision,
recorded so no future session re-litigates it:

> **AI GROWS/ASSISTS THE TOOL, DETERMINISM SHIPS THE APP.**

Cortex has exactly two jobs, both run INSIDE the Snowflake account, both
gated so the AI never has the final say:

1. **Calc fallback** (`cortex_calc_fallback.py`) — `calc_translator.py`
   already handles ~97% of formulas by rule (exact, free, instant). The
   calcs it refuses (nested LODs, cross-datasource blends → land in
   `ir['calc_drops']`) go to `SNOWFLAKE.CORTEX.COMPLETE` (Claude, in-account)
   for a *proposed* SQL translation. Two gates take control back: (1) the
   proposal must compile + execute on the real table or it's marked FAILED;
   (2) a human reviews `reports/cortex_calc_proposals_<book>.md` —
   **nothing is auto-applied into the IR or the app.** Order-dependent
   table calcs (LOOKUP/LAST/RUNNING) are refused with a reason, never
   AI-guessed. The prompt is scoped to THIS workbook's own datasources only
   (an early version invited the model to pick a same-columned foreign
   table — the AI analogue of the Superstore-gravity bug, fixed).
   PROVEN: GlobalSales's nested LOD → Cortex CTE SQL → 4 region values
   exact vs local pandas ground truth. Superstore's 2 blend calcs came back
   **execute-clean but with a wrong join key** — proof that
   "verified-executable" is not "verified-correct" and why gate 2 exists.
2. **Semantic layer** (`cortex_semantic.py`) — turns the workbook's OWN
   verified calcs into a native `CREATE SEMANTIC VIEW` (Tableau measures →
   Snowflake METRICS, business captions → synonyms) + a Cortex Analyst YAML.
   Identifiers are introspected from the REAL deployed tables (`DESCRIBE`,
   or `INFORMATION_SCHEMA.COLUMNS` through the app's own session when
   running inside SiS — `introspect_columns_via_session`, since a SiS
   sandbox has no shell/CLI access) so quoted/mixed-case columns resolve
   correctly. PROVEN: `SUPERSTORE_SEMANTIC` deployed live;
   `SEMANTIC_VIEW(...)` returns Profit Ratio 0.12564 = Tableau's 12.6%
   exact. This is a governed **migration artifact** (the data model of the
   migrated estate), not an end-user chatbot — the chat/NL Q&A surface is
   explicitly descoped by the user.

**What Cortex explicitly does NOT do**: convert the workbook (stages 1–5
above are pure Python, zero AI, unchanged); write the app / infer charts /
lay out dashboards; touch the ~97% of calcs the rules already handle. Output
is never silently trusted — always execution-tested + human-reviewed.

**Why not "Cortex builds the whole app"** (considered and rejected): it
trades away visual fidelity by construction — the deterministic engine
renders a bar the same way every run, so a correct tab *stays* correct
(regression snapshots prove it). AI-writes-the-app means non-determinism
(same workbook, different app each run — the 37-gate regression suite
becomes meaningless), circular validation (numbers need external ground
truth; visual fidelity has no cheap automatic gate), and spending AI to make
the working, exact, free 97% worse to help the 3%. The adopted pattern
instead: AI fills the holes in a deterministic canvas, then the proven fix
is PROMOTED into the engine as a rule offline — the shipped pipeline stays
deterministic and every later workbook gets the fix free.

**Why Streamlit-in-Snowflake, not plain Streamlit**: SiS runs compute next
to the data (no egress), inherits RBAC/masking/audit as the app's role
(rebuilding none of it), is serverless (zero infra to patch), and makes
Cortex COMPLETE/Analyst/semantic views a *local* call inside the same
governed platform — running plain Streamlit outside and calling Cortex means
re-crossing the boundary the migration was meant to eliminate. If data were
NOT in Snowflake and there were no governance/AI requirement, plain
Streamlit would be the simpler right choice — the case rests entirely on
data-in-Snowflake + governance + Cortex, which is this project's premise.

**Wiring**: `convert.py` gained stages 6 (semantic) + 7 (ai-calcs), opt-in
behind `--connection <snow-conn>`; without it, output is byte-identical to
before. Cortex stage failures are soft (warn, never kill a conversion that
already succeeded). Account `wb19670-c2gpartners` (conn `wbr`) is fully
provisioned; working model literals are `claude-opus-4-8`, `claude-4-sonnet`,
`claude-sonnet-4-5`, `claude-haiku-4-5` (NOT `claude-opus-4-1` /
`claude-3-5-sonnet` — retired); model arg must be a string literal. Native
`CREATE SEMANTIC VIEW` is supported, staged at
`WBR_DB.PUBLIC.TABLEAU_TO_SIS_SEMANTIC`.

Regression: `test_cortex_semantic_generation` (metric dedup, window-calc
skip, param substitution, the real-identifier quoting/rewrite deploy bug,
valid YAML/DDL) + `test_cortex_calc_fallback_guards` (order-dependent
refusal rule, blend/LOD routing, SQL recovery, JSON parse) — both offline,
no live account needed (the actual `COMPLETE` call is non-deterministic and
correctly not regression-tested).

**Still open on this arc**: run the fallback across the rest of the corpus
(22 calc_drops total); deploy the E-Commerce semantic view and align the
YAML emitter to real introspected identifiers like the SQL emitter already
does; extract blend linking fields from the XML (turns the join from a
guess into a constraint — fixes the wrong-join-key class found on
Superstore); wire the calc fallback into `pipeline_app.py`'s Stage 5 (today
it's a separate script; `convert.py` has it wired, the staged demo does not
yet).

## 11. Staged demo UI + Snowflake deploy hardening (2026-07-20)

**The ask**: upload a `.twbx` → SEE each stage (Discovery / Parsing /
Semantic Model / App Creation / Validation) → end in PROOF the numbers are
right, for a 4-workbook demo.

**Built + deployed**:
- `pipeline.py` — shared discovery/decode/load logic (`onboard()`),
  extracted from `converter_app.py` so there is ONE decode code path (this
  project has been bitten before by two decode paths diverging).
  `ensure_target()` checks schema existence via a fully-qualified
  `INFORMATION_SCHEMA.SCHEMATA` read (see the deploy lessons below) and
  raises one clear actionable message (the exact GRANT to run) instead of a
  bare Snowpark traceback.
- `parity.py` — Stage 5 validation, the trust proof. Two independent
  checks: (a) raw-column measures — the app's own SQL path vs a direct
  source read, cross-checked against known Tableau grand totals where
  available; (b) `check_calc_metrics` — CALCULATED-FIELD metrics,
  execution-gated + cross-checked against a known Tableau bound (added
  because E-Commerce, 88 calcs / ~0 raw-column measures, showed "0 measures
  checked" under (a) alone — the frontier workbook would have had NOTHING
  validated). Emits a downloadable `.ipynb` (comparison tables, PASS/BUG
  verdicts, roll-up summary) — an artifact to hand someone, not a claim.
- `pipeline_app.py` — the V2 workbench UI (§14, superseding the earlier
  3-tab console of §13). `run_migration()` fires a real 5-stage callback
  (Discovery → Parsing → Data model → App build → Validation) that drives
  one connected-circle stepper as the sole progress signal — no separate
  `st.status` boxes or `overall` progress bar. Stage "Data model" actually
  EXECUTES the `CREATE SEMANTIC VIEW` in Snowflake (not just shows DDL).
  Heavyweight validation/reporting (Cortex judging, vision, the dashboard
  report, the migration PDF) lives in `deep_validation.py`, called from the
  Validation page as click-gated expanders.
- **Deployed**: `pipeline_demo` entity in `snowflake.yml` →
  `WBR_DB.PUBLIC.TABLEAU_TO_SIS_PIPELINE_DEMO` (workbook-agnostic: no
  pre-seeded `datasources.json`, deploy once, demo any book). All 4 demo
  workbooks validate 100% clean: Superstore 13/13, E-Commerce 10/10,
  Regional Analysis 8/8, World Indicators 12/12.
- Full run-the-demo steps: `DEMO.md`.

**Six real deploy bugs found live and fixed, same day** (the "preview OK,
deployed breaks" class kept recurring even after the Streamlit-version pin
in §7c — these are NEW dialect/privilege classes):

1. `CREATE DATABASE IF NOT EXISTS` privilege error — a SiS app runs with its
   OWNER ROLE's rights (commonly locked down well below ACCOUNTADMIN), and
   `IF NOT EXISTS` still runs its privilege check unconditionally. Switched
   `LOAD_DB/LOAD_SCHEMA` to an existing DB (`WBR_DB`) + a dedicated demo
   schema (`PIPELINE_DEMO`) so a re-upload's `write_pandas(overwrite=True)`
   can never touch `WBR_DB.PUBLIC` (the real corpus tables + deployed
   E-Commerce app + Cortex semantic views live there).
2. Same error on the very next real upload — round-1's "try `USE SCHEMA`
   first" strategy was itself wrong. `USE SCHEMA` is a session-context
   statement that succeeds from an interactive worksheet under the exact
   same role but **fails inside a deployed SiS app's owner's-rights
   execution sandbox**; the code's `except: pass` silently swallowed that
   and fell through to the same privilege wall. **Standing rule: inside a
   Streamlit-in-Snowflake app's Snowpark session, NEVER use `USE
   DATABASE`/`USE SCHEMA`/`USE WAREHOUSE`/`USE ROLE` — always fully-qualify.**
   A statement behaving identically in an interactive worksheet is not proof
   it behaves identically inside the app's sandbox; test session-context
   logic through the actual deployed app, not by proxy via `snow sql` under
   the identical role.
3. `ImportError: openpyxl` — the staged converter reads a workbook's own
   bundled Excel data; `environment.yml` only had the packages the
   CSV-based E-Commerce deploy needed. Added `openpyxl` + `xlrd` (confirmed
   on the Snowflake conda channel) and AST-scanned every deployed file's
   third-party imports to confirm full package-surface coverage.
   **Standing rule: `environment.yml` must declare every runtime
   third-party package the staged converter can hit for an ARBITRARY
   uploaded workbook, not just what the first demo workbook happened to use.**
4. Every dashboard tab failed `KeyError('lo')` on an untested 5th workbook —
   DuckDB folds unquoted aliases lowercase; Snowflake folds them uppercase.
   `engine.build_where`'s date-range query read `b["lo"]`/`b["hi"]` by
   lowercase name; fixed via dialect-agnostic positional access
   (`b.iloc[0,0]`). New CI technique: `test_snowflake_uppercase_alias`
   wraps `engine.q` to uppercase every result column, simulating Snowflake
   folding without a live connection — catches this whole bug class
   pre-deploy.
5. Gantt chart syntax error — `AS START` is unquoted in DuckDB (fine) but
   `START` is a Snowflake RESERVED word (rejected). Quoted it (`AS "START"`
   — preserves case in both engines); grep-audited every other unquoted
   uppercase alias in `engine.py` against Snowflake's reserved-word list —
   `START` was the only hit. Locked by `test_no_reserved_word_sql_aliases`
   (static scan; any future reserved-word alias fails CI).
6. Every date-using sheet failed (`DATE_TRUNC`/`DATEDIFF`/comparisons
   rejecting `NUMBER(38,0)`) — the same two-paths-diverge class as §7c's
   original date fix: `write_pandas` lands datetime as epoch NANOSECONDS;
   `load_snowflake._fix_date_columns` already repairs this for the CLI
   loader, but `pipeline.load_into_snowflake` (the demo's loader) did not.
   Added `pipeline._fix_date_columns_session` (the Snowpark-session twin —
   **keep the two in sync**) called after every `write_pandas`.

**Standing rule (the big one, reinforced)**: all local validation runs on
DuckDB, which is lenient where Snowpark is strict — unquoted-alias
case-folding, type strictness, function/error-text differences. A green
local run does not prove Snowflake behavior. For any new query-result
column access: alias UPPERCASE and read uppercase, or use positional
`.iloc`; never read a query result by a lowercase by-name key. The deployed
app remains the only real dialect test, but the uppercase-folding
simulation wrapper (bug 4 above) now catches the most common sub-class
pre-deploy.

**Known still-open**: (a) the map/choropleth ("Sales by Geography") renders
blank with a visible colorbar in SiS — plotly's base-map topojson does not
load in the Snowsight sandbox; offer a per-region bar/table fallback or
investigate plotly-geo-in-SiS. (b) broader reserved-word risk not yet
handled at the *column* level: a Tableau field literally named a reserved
word (`Order`/`Group`/`Start`) would break the same way since `px()`/
`to_phys` column refs are unquoted; no corpus workbook hits it yet — the
column-level twin of fix 5 above. (c) run the staged UI against a 5th/6th
workbook to further prove it isn't tuned to exactly the original 4 —
Superstore's 2024.3 variant already validated clean (13/13) as a first data
point.

### Hyper-only workbooks + the local-connected migrator (2026-07-20)

A `.hyper` extract can be decoded ONLY where Tableau's Hyper engine
(`tableauhyperapi`) exists — a laptop/server, **never inside a
Streamlit-in-Snowflake sandbox** (the engine isn't there and can't be
installed; no Cortex/LLM can substitute — it's a binary format, not a text
task). Workbooks whose data is bundled ONLY as `.hyper` (Regional Analysis,
Global Sales) therefore cannot be onboarded by *uploading the `.twbx` into the
hosted-in-Snowsight* demo app; CSV/Excel-sourced workbooks (Superstore,
E-Commerce) can, because those formats decode in-app.

**The conflation bug this exposed.** The pipeline used one flag (`in_snowflake`)
for two orthogonal things: *can we decode a `.hyper`?* (no in SiS, yes on a
laptop) and *do we have a Snowflake session to load into?* (yes in SiS, yes
locally-if-connected, no locally-unconnected). Fixed by decoupling them:
- `pipeline.onboard` now **always attempts the decode** (SiS-safe: the
  `tableauhyperapi` import is lazy, so in the sandbox every hyper simply comes
  back `blocked`) and **loads whenever any session is present**, not only the
  hosted one.
- `pipeline.load_into_snowflake`, for a datasource with no decodable file,
  **probes the target table** (`table_exists`, fully-qualified
  `INFORMATION_SCHEMA`, no `USE`) and **reuses a pre-loaded one** if present,
  else flags it `MISSING` — never leaving sheets pointed at a table that was
  never created. Locked by `test_pipeline_reuses_preloaded_table`.

**Two supported ways to migrate a hyper-only workbook:**
1. **Local-connected migrator (one upload does everything).** Run the SAME
   `pipeline_app.py` on a laptop and tick its opt-in "Push to Snowflake on
   upload" (session built from the `snow` CLI connection via
   `pipeline.snow_session`, which reads the CLI's `config.toml` — the `wbr`
   connection lives there, not in the `connections.toml` Snowpark's
   `connection_name` resolver defaults to). One upload then decodes the
   `.hyper` **here**, loads the tables into `WBR_DB.PIPELINE_DEMO`, and deploys
   the semantic view **there**. The migrator is a tool that runs outside; the
   migrated output lands inside Snowflake — the SiS story is intact.
2. **Pre-load once, hosted app reuses.** `preload_demo.py "Book.twbx"` decodes
   locally and loads the tables (reusing `pipeline.load_into_snowflake`, so the
   names match exactly); the hosted Snowsight app then reuses them, and Stage 1
   stops cleanly with this exact remediation if a hyper workbook was NOT
   pre-loaded (instead of cascading `does not exist` through Stages 3–5).

The old CLI migration path (`init_workbook` decode → `load_snowflake` →
`snow streamlit deploy`) is unchanged and still valid; the above are the
self-service equivalents wired into the staged demo.

STANDING RULE: never conflate "has a Snowflake session" with "running inside
the SiS sandbox." Decode-capability and load-capability are orthogonal axes.

### Query-execution routing + the client-credibility question (2026-07-21)

The local-connected migrator (above) pushes real tables and a real semantic
view to Snowflake — but until this fix, `backend.run_sql` only recognized
`get_active_session()` (true only when the process is *deployed inside*
Snowflake), so it had no idea a session had been opened deliberately from a
laptop. Every chart query therefore still ran on local DuckDB even after a
successful push — half-real: the data landed in Snowflake, but nothing about
rendering the dashboard used it. This is exactly the "you're just hosting an
app that writes to Snowflake on the side" gap a Snowflake client would
correctly call out, and it was a real defect, not only a demo-optics problem.

**Fix**: `backend.set_session(session)` registers an externally-opened
Snowpark session (also clearing the cached DuckDB connection so the two
sources can never silently mix); `_active_session()` prefers it over
`get_active_session()`; `run_sql` routes through whichever is present.
`pipeline_app.py` calls `backend.set_session(_sess)` immediately after
`resolve_session()`. **Proven live, not only with a fake session**: pointed a
datasource at `WBR_DB.PIPELINE_DEMO.CALLS` with `local_file: None` (nothing to
silently fall back to) and confirmed `run_sql` returned the real row count
(840) — a DuckDB fallback would have raised `FileNotFoundError`, not returned
data. Gate `test_backend_uses_pushed_session` locks the routing offline.

**The honest recommendation this produced**: don't run a client-facing demo
from a laptop at all. The local-connected mode exists only to do the one
genuinely unavoidable thing — decoding a `.hyper` (no reader exists or ever
will inside Snowflake). Once that one-time decode+load has happened, the
client demo should run entirely from the Snowsight-hosted app: upload there,
the tables get reused, and every later stage — parsing, `CREATE SEMANTIC
VIEW`, rendering, Cortex — executes as a Python process physically running on
Snowflake's own compute. Nothing touches the presenter's laptop except a
browser tab on a Snowsight URL. For a skeptical audience: open Snowsight →
Activity → Query History alongside the demo and show the semantic-view DDL /
chart queries / any `CORTEX.COMPLETE` call executing with real timestamps and
warehouse credits — Snowflake's own audit trail, not a claim.

### The validation stage's blind spot: no-local-file datasources (2026-07-21)

Uploading Regional Analysis directly into the (redeployed) Snowsight app
rendered correctly — Stage 1 reused the pre-loaded tables, Stage 4 showed the
real dashboard — but Stage 5 (Validation) reported every measure as BUG with
`Source value: None`. Not a data defect: the app's own values (e.g. Sales
2,297,200.86) were genuinely correct.

**Root cause**: `parity.check_workbook`'s per-measure check computes a second,
independent value by re-reading the *original local extract file* via pandas.
For a datasource reused from a pre-load — no local file exists in that
environment by design — that read is impossible, the second value stays
`None`, and comparing anything against `None` unconditionally fails. None of
the (then 39) regression gates caught this because the only parity test always
runs against `Workbooks/`, where the local file is present; the no-local-file
branch had never been exercised.

**Fix**: when no local file exists but the table does, fall back to an
independent **client-side re-pull + sum of the same table** (`source_kind =
"table-repull"`) instead of comparing against `None`. This is a genuinely
different code path from the app's own server-side `SUM()` — it still catches
real defects (wrong column, wrong aggregation, type/NULL handling) — but it
honestly cannot catch a bad load into that table the way a true external-file
check can, so it is labeled distinctly (`(repull)` in the UI and the
downloadable notebook) rather than silently presented as equivalent. Verdicts
are now three-way (PASS / EXECUTED / BUG, matching the tri-state pattern
`check_calc_metrics` already used for calculated fields) instead of a false
binary. Row-count "Match" now renders as unknown (`—`) rather than a false ❌
when there's no independent source to compare — the underlying data value was
already `None` (correct); the UI had been collapsing `None` into "falsy → ❌",
a display bug stacked on top of the real one.

Gate `test_parity_no_local_file_reuses_table_repull` reproduces the exact
scenario without needing a live Snowflake session: warms the local DuckDB
cache with a real file present, then blanks only the config mapping's
`local_file` (exactly what `pipeline.configure_datasources` does for a reused
datasource) — the already-loaded table stays queryable, simulating a
pre-loaded Snowflake table with no decoded source alongside it.

STANDING RULE: a "two independent computation paths" trust check is only as
strong as its weakest required input. If one path can legitimately be absent
in a valid, correctly-working deployment mode, the check must degrade to an
honestly-labeled weaker verification — never silently render a missing value
into a false failure. Test every trust-proof code path under every condition
it can actually run in, not just whichever the existing test happens to hit.

## 12. Human-gated deploy (R5) + in-app Cortex Analyst chat (R6) — 2026-07-25

**R5 — the accelerator now DEPLOYS the app, not just generates it.** After
the 5 stages, `pipeline_app.render_deploy_step` shows a human-gated "🚀
Deploy to Snowflake" button; `pipeline.deploy_streamlit_app` ships the
generated `app_<stem>.py` + its runtime modules (the same artifact set
`snowflake.yml`'s deployed-app entities ship) + a per-deploy `datasources.json`
to Streamlit-in-Snowflake THROUGH THE SNOWPARK SESSION: `session.file.put`
stages the files, then `CREATE OR REPLACE STREAMLIT`. **No `snow` CLI** — a
SiS sandbox has none, and this is the confirmed path that works whether the
migrator is running locally-connected (`backend.set_session`) or hosted in
SiS itself. Everything fully-qualified, never a session-context `USE` (§11's
standing rule). Files are copied to a space-free temp directory before
`session.file.put` (this repo's own path has spaces, which breaks Snowpark's
`file://` arg parsing). Re-deploy replaces in place (`CREATE OR REPLACE`);
best-effort Snowsight deep link returned, else the nav path. Only live when a
session is connected — a local unconnected run shows an explainer + disabled
button. Gate `test_deploy_streamlit_app` (offline stub session: identifier/
DDL well-formed, every runtime module staged, exactly one `CREATE STREAMLIT`,
no `USE`).

**R6 — in-app Cortex Analyst chat ("Ask your data").** A panel bound to the
workbook's deployed semantic view (§10); calls Cortex Analyst (the SiS
`_snowflake` bridge, with a Snowpark REST fallback), returns SQL + a natural-
language answer, runs the SQL, shows results. Single-step `st.form`, fully
guarded (never trusted blind — the SQL genuinely executes against the real
warehouse and the numbers come from that execution, same trust model as
everything else in this pipeline). Working live, user-confirmed.

**Honest Stage 3 rework** (same session): Stage 3 (Data Model & Semantic
Layer) split into two honestly-separate sub-steps instead of one that always
claimed to deploy a semantic view regardless of whether it made sense:
- **3a Data Model** — shows the workbook's Tableau join/relationship graph
  (§7e's `describe_model`/`join_plan` output) and, when the tables verify
  separately in Snowflake, deploys the real `CREATE VIEW`. A flattened
  extract is labelled honestly ("needs separate loading"), never faked as
  deployed.
- **3b Cortex layer** *(optional)* — only runs when there are metrics to
  expose, and skips-if-exists (fixed the "always shows a semantic model,
  even for a workbook with nothing to model" misleading-demo problem).

**Data-model view scope A/B**: scope A (the default) is the flatten §7b
already describes. **Scope B** — load each star's constituent tables
SEPARATELY + deploy the relationship view, so the app AND Stage-5 validation
query the REAL model, not a flatten's numbers on trust — is live-proven:
E-Commerce replicated as 3 separate tables (Events/Customers/Products) + a
relationship view in `WBR_DB.PIPELINE_DEMO` (`pipeline.build_data_model_
tables`). A casing fix was needed: `semantic_layer`'s `phys_source` mode
normalizes view refs to UPPER unquoted columns (matching `_normalize_
columns`'s convention for tables THIS pipeline writes), vs quoted-original
for a genuinely live/declared-source table it never touched (§7e's R10 work
generalized this further — see `_src_table`'s `is_declared_source` return).

Also this session: cross-schema reuse of a pre-loaded table — a table this
project already copied is found WHEREVER it lives in the account (fixed
E-Commerce's `Customers (DataDNA...)` table, present in `WBR_DB.PUBLIC` but
not `PIPELINE_DEMO`, previously reported MISSING); worksheet-shown parameter
controls (What If Forecast: New Business Growth + Churn Rate) render on
their OWN tab instead of being hoisted to the global sidebar.

New gates: `test_deploy_streamlit_app`, worksheet-params, cross-schema
reuse, data-model view, scope B, bundle-completeness, `test_no_undefined_
names_in_app` (pyflakes-based — guards the class of live `NameError` a
Stage-3 helper hit once from referencing `config` before it was imported at
that scope).

## 13. Blend-branded UI reskin + Migration report (2026-07-26)

User (a Blend employee) asked for the demo UI's LOOK to be replaced with a
Blend-branded design they authored (`ACCELERATOR_UI_HANDOFF.md` +
`10_accelerator_console.py`, a separate, more elaborate prototype console),
WITHOUT disturbing existing functionality. The console's `sis-*` CSS and
presentational `render_*` sections were extracted VERBATIM via AST from the
source file and inlined into `pipeline_app.py` — deliberately not a new
module, so `snowflake.yml`'s artifact list needed zero changes.
`_summary_from_ir(ir)` maps the REAL uploaded workbook's IR onto the
console's summary shape, so every brand panel shows actual numbers, never
illustrative placeholders. A load-bearing fix: Streamlit runs
`unsafe_allow_html=True` markdown through its own Markdown parser first,
which treats the console's deeply-indented HTML as a code block unless
dedented first — a global `st.markdown = _markdown_dedent_html` wrapper
strips leading whitespace before it reaches Streamlit's parser.

**Four rounds of live user review caught real product-truthfulness issues**,
not cosmetics — worth recording as the standing lesson: porting another
product's UI onto this accelerator must stay truthful to what THIS tool
actually does at each stage, not the source product's narrative. Round 1
dropped an inherited 8-stage narrative (this accelerator runs 5) and a
"Briefs" tab (we produce none). Round 2 removed a "Run Center" that mocked
AI-token/screenshot metrics as fake zeros and a hardcoded example parity
chart with zero connection to the uploaded workbook. Rounds 3–4 (after the
user showed style references from a DIFFERENT Blend product, explicitly "for
style, don't copy") added a live 5-step progress tracker with auto-collapsing
finished stages, then collapsed the whole shell to **three tabs** — Overview
/ Discover & Scope / Migration report — after Run Center and Element explorer
both failed to earn a distinct job across two redesigns.

**Superseded 2026-08-06 — see §14.** The three-tab shape below (Overview /
Discover & Scope / Migration report) was replaced by the V2 workbench UI
(sidebar nav, five-stage stepper). Left as-is for the historical record of
how the shell got here across four review rounds; §14 has the current shape.

**Then-current final shape** (2026-07-26, no longer current): the Migration
report is the one results surface —
verdict header, grouped item counts (data model / dashboards & sheets), a
per-stage pipeline table, the validation result, expandable detail (data
model / sheets / calculation ledger), the FULL Stage-5 validation proof
(shared via `render_validation_proof`, so Stage 5's own inline render and the
report render byte-identically from the same `parity.check_workbook()`
result — a future format change reaches both automatically), and a real **PDF
download** (`fpdf2`, chosen after querying `INFORMATION_SCHEMA.PACKAGES` —
`weasyprint` needs system libs the SiS sandbox doesn't have). One
`_report_sections()` builds the content once; both the on-screen report and
the PDF render from it, so they cannot drift.

**Key product truth baked into the UI copy** (say it, never mock it): this
accelerator's migration is 100% deterministic — genuinely 0 AI tokens to
convert a workbook. Cortex touches exactly two OPTIONAL things (§10): the
semantic view (still 0 tokens) and ask-your-data (§12, real tokens per
question actually asked). Validation today is two independent computation
paths + known-figure cross-checks — no screenshots, no vision calls (that's
R8, planned, not started — needs a live-session probe of `AI_COMPLETE`
vision availability first).

Verified every round: syntax, `pyflakes` (0 undefined names — now its own
regression gate), full suite green, local render DOM-checked (tab labels,
stage-rail card count, zero `stException`, no raw-HTML leak). Deployed live
to `WBR_DB.PUBLIC.TABLEAU_TO_SIS_PIPELINE_DEMO` repeatedly across this
session and the R3/R7/R9/R10 work in §7e (same deploy target, same
`snow streamlit deploy pipeline_demo --replace` command each time).

## 14. V2 workbench UI + deep_validation.py carve-out (R13, 2026-08-06)

User supplied an approved V2 design to implement: `pipeline_app_v2.py` (a
functional prototype), `tableau_to_sis_v2_preview.html` (the visual/
interaction reference), and a premium-UI mockup for icon/nav/progress
inspiration only. Requirements: preserve every existing capability; exactly
one workbook per run; both intake paths (upload + Tableau Server/Cloud, PAT
never entered in the UI); Streamlit-in-Snowflake always the target — never
surface DuckDB as a visible platform choice; no manual Snowflake table
selection; real live progress from actual stage boundaries, no fake delays;
no invented metrics or user profiles.

**Shell shape** — replaces §13's three-tab console entirely:
- Dark-navy, icon-led sidebar nav (`st.radio` styled as nav rows via CSS —
  Material Symbols ligatures injected as `::before` content per option,
  since `st.radio` has no native `icon=`) with 6 destinations: **Overview**
  (product pitch + platform architecture folded in as a collapsed expander —
  no longer its own nav item), **New migration** (intake + the live run),
  **Inventory**, **Preview**, **Validation**, **Deploy & Ask**.
- A five-stage connected-circle stepper (`.v2-stepper`/`.v2-step`) is the
  ONE progress signal for a run — driven by real stage boundaries
  (`run_migration()`'s `emit(stage, title, detail)` callback), not a
  separate `st.progress` bar.
- One workbook per run enforced at the widget level
  (`accept_multiple_files=False`); a run's result lives in
  `st.session_state["v2_run"]` and every results page (`require_run()`)
  shows a real empty state until one exists.

**`deep_validation.py` — verbatim carve-out, not a rewrite.** The R1–R12
heavyweight machinery (Cortex-judged per-section validation, Cortex vision
validation, the skill-methodology dashboard-by-dashboard report + the R12
proof-first pack, the migration-report PDF) was extracted from the pre-V2
`pipeline_app.py` via AST (function boundaries sliced out of the parsed
tree, not hand-retyped — this project's standing rule against two copies of
the same logic silently diverging) into its own module, wired into the new
Validation page as click-gated `st.expander` sections
(`render_cortex_section_validation`, `render_dashboard_validation`,
`render_vision_validation`, `render_migration_report`). Nothing from the
whole R1–R12 arc was lost in the UI rewrite. Added to `snowflake.yml`'s
`pipeline_demo` artifacts (21 → 22) — missing this would have crashed the
deployed app on import, caught before deploying by re-running the artifact
list against disk, not after.

**Verification discipline that caught real bugs before the demo did.** The
deployed app pins `streamlit==1.52.2` (the newest the Snowflake Anaconda
channel offers — see `environment.yml`'s own comment); local dev runs
1.57.0. Built and AppTest-verified under a venv pinned to the DEPLOYED
version specifically to catch version drift pre-merge, then ran a REAL
end-to-end migration (`Superstore.twb`) through the new code path, not just
`--server.headless` smoke checks. That surfaced, all fixed pre-merge:
`parity.build_notebook` already returns serialized `.ipynb` JSON (double
`json.dumps()` produced an unopenable file); `SL.describe_model()`'s
unverified shape was read instead of `pipeline.data_model_report()`'s
session-verified one (Deployable was ALWAYS False); the semantic view was
generated but never deployed (Cortex Analyst could never have worked);
`ir["params"]` is a dict not a list; a sheet filter's real key is `caption`
not `field`; the uploader dropzone CSS targeted a `<div>` where Streamlit
renders a `<section>`; `initial_sidebar_state="auto"` drops the sidebar out
of the DOM on narrower viewports.

**Three regressions caught live on the deployed app, fixed same session**:
(1) the Tableau site URL field's pre-filled default (`value=`) was dropped
rewriting the intake form; (2) the dashboard preview regressed from
`st.tabs()` (one tab per Tableau dashboard, matching Tableau's own
presentation) to a single `st.selectbox` dropdown; (3) Architecture as its
own nav destination — folded into Overview per explicit request.

**Two Cortex Analyst bugs, found in sequence on the live app.** First, a
bare `'str' object has no attribute 'get'` crash: Cortex Analyst's REST
bridge nests a JSON-encoded STRING at a level the parser assumed was
already-decoded, and the exact nesting depth has been observed to vary by
account/runtime. `parse_analyst`/`_maybe_json` now defensively `json.loads`
at every level and surface the raw payload on a genuinely unrecognized
shape instead of a bare traceback — unit-tested against 8 payload shapes.
Second, fixing that crash surfaced the REAL bug underneath: a live HTTP 404
("does not exist or not authorized") against the exact semantic view the
app had just labeled "reused." Root cause in `pipeline.semantic_view_exists`
(used by both `pipeline_app.py` and the carried-over §10 Stage-3b logic):
it matched by BARE OBJECT NAME ONLY, so any same-named semantic view
anywhere the session could see (a different schema, a stale prior run under
a different workbook stem) reported "exists" and the real
`WBR_DB.PIPELINE_DEMO.<stem>_SEMANTIC` view was silently never created.
Fixed to scope `SHOW SEMANTIC VIEWS` to the target schema and match the
FULL database+schema+object triple — unit-tested against the exact
false-positive scenario (same-name view in a different schema → correctly
reports not-exists) plus true-positive and legacy-bare-name cases.

**Inventory visibility restored** on Inventory → Data model, two pieces the
rewrite had dropped versus §13's pre-V2 app: a "Snowflake landing" table
(which physical table each datasource actually routed to/loaded into, row
counts, status — read from `discovery["load_report"]`, falling back to the
routed `config.DATASOURCES` table for reused/unloaded captions) and a
"Cortex semantic layer" line (view name, state, metric count — state values
updated below in §14b). The join-view column now shows the real (or, if not
yet deployed, the candidate) view name instead of a bare boolean checkbox.

KNOWN UNVERIFIED (needs the user's live account): the live Cortex Analyst
call end to end after the fix, live Snowflake deploy, and the
deep-validation Cortex features (section judging, vision, dashboard report)
— all exercised locally only as far as their unavailable-state messaging.
ROLLBACK: `pipeline_app.py.pre-v2-merge.bak` is the exact pre-merge file.

## 14b. Four more real bugs found live on the deployed V2 app (same session)

The user kept using the deployed app after §14 and kept finding real gaps.
Each was root-caused and fixed the same turn, deployed immediately, never
batched.

**Semantic view skip-if-exists gate REMOVED entirely.** §14's fix made
`semantic_view_exists()` correctly scope-match — which then correctly found
a semantic view already deployed under the current workbook's stem, from a
run BEFORE any of this session's collector fixes existed, and (as
designed) skipped regenerating it. Cortex Analyst then kept 404ing/
misanswering against that stale, pre-fix definition forever. The actual
bug was architectural, not the match logic: `CREATE OR REPLACE SEMANTIC
VIEW` is already idempotent DDL with ZERO Cortex token cost (pure schema
metadata, no AI call) — there was never a correctness reason to skip it.
The gate is gone; the DDL always executes now. `semantic_state` is
`deployed` / `updated` / `not deployed` (no more `reused`) so the UI still
distinguishes a fresh create from a replace.

**`cortex_semantic._field_candidates` was missing three bare-caption-string
shelf keys.** It only unwrapped `text_fields`; `dim` (mbar's grouping
dimension), `geo` (map location), `segment` and `panel`
(dtbar/strips/small-multiples) were silently dropped — exactly documented
in `tableau_parser.py`'s own comment ("geo/dim are strings; x/y/
color_measure/size/label are dicts"), just never applied here. This is
WHY Region was invisible to Cortex Analyst on a workbook that visibly
renders it correctly (the deterministic engine reads `dim` directly; only
this collector missed it). Fixing it also surfaced `State/Province` (a
map's `geo` field) as a second, previously unknown casualty. This is the
SAME bug class §7's R11/R12 work already found and fixed independently on
a different function (`parity.collect_dashboard_section`) — this
project's "two similar functions silently diverge" risk, realized twice.

**Reset button was missing on "New migration" itself.** `render_reset_row`
(§14) was wired only into `require_run()`, called by the four RESULT
pages. "New migration" — both mid-run and immediately after completion,
the moment someone is most likely to want to start over — has its own
render path and never called it. Factored into a standalone helper, added
to `page_migrate()`'s completed-run branch too. The mid-run case (a button
click during the live 5-stage run) is NOT fixable without moving the run
off the main script thread — stated to the user rather than faked.

**`engine.r_mbar` had two real Vega-Lite rendering bugs**, found from a
user screenshot of Superstore's CustomerOverview (6-panel mbar, 4
regions): (a) Vega's default axis `labelOverlap` heuristic hid 2 of 4 row
labels despite 33px of real vertical room per row for an 11px font —
its overlap estimate is conservative regardless of actual space; fixed
with `labelOverlap=False`. (b) the value label trailing whichever bar held
a panel's largest value ran past the plot area's own width and got
clipped by the SVG's default `overflow: hidden` — `clip=False` on the mark
alone does NOT fix this (it only removes Vega's own internal clip-path;
the SVG root still clips at its own boundary). The real fix reserves
X-axis headroom sized to the actual widest FORMATTED label in that panel
(`$739,814` vs `15.0%` need very different room — a flat 22% guess still
clipped the dollar labels). Verified by building the identical chart
construction standalone, rendering it in a real browser, and reading the
live DOM before/after (0/4 labels hidden, 0/24 value labels overflowing
across 6 panels) before applying the same fix to the real function.

All four verified against the full 76-gate regression suite (green
throughout) and deployed after each one.

## 15. Validation consolidated onto one deterministic proof-first pack (2026-08-07)

The R1–R12 arc (§14) shipped FOUR validation surfaces on the Validation
page: a Cortex-judged per-metric verdict, a skill-methodology dashboard
write-up (Cortex-narrated), Cortex vision screenshot comparison, and the
R12 proof-first pack (per-chart, row-level Tableau/Streamlit/backend
comparison) nested inside the second. Live-testing that page against the
real account, the user judged the AI-narrated panels less useful than the
deterministic pack and asked for a straight replacement: **one panel, no
Cortex tokens, real evidence.**

**What changed.** `parity.build_cortex_dashboard_validation_report` gained
`narrate=False`, which skips both Cortex calls (the per-section write-up
and the closing bug rollup) while still computing every DETERMINISTIC
field the pack needs (`formula_rows`, `interaction_rows`, the live
combined query) — verified directly: a `FakeSession` that asserts
`CORTEX.COMPLETE` never appears in any SQL text confirms zero Cortex calls
fire. `deep_validation.py` lost `render_cortex_section_validation` and
`render_vision_validation` entirely (~280 lines); `render_dashboard_
validation` was replaced by `render_proof_first_validation` — same
crosstab-upload UI, same `_build_validation_pack` call, but calling
`build_cortex_dashboard_validation_report(..., narrate=False)` and
dropping the Cortex-narrated notebook/HTML/exec-report downloads. The
underlying `parity.py` functions (`cortex_judge_section`,
`vision_validate_dashboard`, `ensure_vision_stage`, etc.) were NOT
deleted — they remain, covered only by their own regression gates,
unreachable from the UI, in case a future session wants them restored.
`pipeline_app.py`'s "Deep validation" section now has exactly two
expanders: **Proof-first validation** and **Migration report**.

**`_build_validation_pack` itself gained real Tableau evidence it never
had before.** It previously called `validation_adapter.build_validation_
spec` directly, which hardcoded `tableau_screenshot: None` unconditionally
— every report showed two blank "Missing required screenshot" boxes,
Tableau connection or not. It now calls `validation_evidence_bridge.
build_complete_validation_spec`, which:
- pulls real Tableau dashboard screenshots (`tableau_server.
  pull_all_view_images`) and real per-worksheet crosstab rows
  (`pull_all_view_csvs`) when a live connection exists, saving Tableau and
  Streamlit screenshots to SEPARATE files (`VEB.save_png_map`) — never
  the same image compared against itself;
- accepts uploaded crosstab CSVs as a fallback when REST has nothing for a
  sheet, matched by CANONICAL filename only (never fuzzy);
- turns on `engine.EVIDENCE_CAPTURE` (default `False`, zero-cost when off)
  for the duration of the build only, so `r_map`/`r_treemap`/`r_table`/the
  rank-table branch of `r_circle` — chart kinds that render via
  Plotly/`st.dataframe`/hand-built HTML, which `validation_adapter`'s
  Vega-Lite-encoding-based capture can never see at all — record their OWN
  real, post-filter/sort/top-N/rank dataframe via `validation_evidence_
  bridge.REGISTRY.record_chart`. An explicit `REGISTRY` payload always
  wins over the reverse-engineered guess when both exist. Detail/list
  tables (`r_table`) cap recorded evidence to the top 30 displayed rows,
  not the full (up to 200) displayed set (2026-08-07 user decision).

**Live-verified against the real account** (site `b360bi`, workbook `R1
Test Upload - Superstore`): real screenshots for 9/9 dashboards with
computed structural similarity scores (0.72–0.85 range, one at 0.852
scored PASS against the 0.85 threshold), real crosstab rows for the
worksheets Tableau's REST view model can reach, real independently-
recomputed backend numbers matching the app's own displayed numbers
exactly where both resolve (e.g. `Sales by Geography`: 49 Streamlit rows,
49 backend rows, exact match).

**Three real bugs found live and fixed, same session:**
1. **Duplicate registry entries under an empty dashboard name.**
   `validation_adapter.build_chart_spec` calls `render_sheet` a SECOND
   time via `headless_render.capture_sheet_kpis` whenever the first
   (Altair) capture comes back empty — true for every map/table/rank-table
   sheet, not just KPI ones. `engine._EVIDENCE_DASHBOARD` had already been
   reset back to `None` by the time that second call ran, so it recorded a
   second, mistagged entry. Fixed by threading `dashboard_name` through
   `capture_sheet_kpis` too, and making `engine._record_chart_evidence`
   idempotent per (dashboard, sheet) as defense-in-depth regardless of
   caller ordering.
2. **Tableau REST's view list is DASHBOARD-granular, not per-worksheet.**
   `list_views(workbook_id)` on the real 9-dashboard/20-sheet test
   workbook returned exactly 9 views — one per dashboard tab, confirmed by
   direct REST calls. A worksheet nested inside a multi-sheet dashboard
   has no view_id of its own; only a dashboard whose single sheet happens
   to share the dashboard's own internal name (a worksheet published as
   its own tab) matched by luck. Fixed with a same-dashboard-name
   fallback, restricted to the UNAMBIGUOUS 1-sheet-per-dashboard case —
   never applied to a multi-sheet dashboard, which would be guessing which
   sheet the crosstab belongs to.
3. **Multi-panel sheets lost every panel but the last in the Streamlit-
   side screenshot.** `headless_render.capture_sheet_chart`'s
   `st.altair_chart` monkeypatch OVERWROTE on every call instead of
   collecting them. Any sheet kind that draws multiple side-by-side panels
   via `st.columns(...)` + one `st.altair_chart(...)` call per column
   (`r_mbar`'s one small-multiple per measure, `r_strips`, faceted
   `r_timeseries`/`r_circle`) made multiple calls during ONE
   `render_sheet()` — only the LAST survived. Found from a user screenshot
   comparison: Superstore's `CustomerOverview` (a 6-measure-panel `r_mbar`
   sheet — note this is a DIFFERENT bug from §14b's `r_mbar` fix, which
   was about the LIVE APP's own rendering being correct; this bug was
   only in the separate headless PNG-export path used for validation
   screenshots) rendered its dashboard PNG with 5 of 6 KPI panels missing
   entirely and the lone survivor full-width instead of in its real
   column. Fixed by capturing every `st.altair_chart` call into a list and
   combining with `alt.hconcat(...)` when there's more than one —
   reproduces the actual side-by-side layout. Verified visually: the
   regenerated `Customer Analysis` screenshot shows all 6 KPI panels
   matching Tableau's content (vertical position still differs — the
   compositor's row-grouping is a documented approximation, unrelated to
   this bug, left alone).

Fix 3 changes `validation_adapter.resolve_chart_columns`/`streamlit_rows`'
behavior for any genuinely multi-panel/faceted sheet too: an `alt.
HConcatChart`'s top-level `.data` is `Undefined` (each panel carries its
own), so those sheets now honestly report "captured chart exposed no
dataframe to compare" instead of silently validating against only the
last panel's data under a chart-wide grain label — MORE honest than
before, not a regression (two `tests/test_regression.py` assertions were
updated to accept this new, correct refusal reason alongside the ones
already accepted).

**Still genuinely open**, found by running this for real, not
regressions:
- Tableau's REST crosstab sometimes returns a long/pivoted shape
  (`Measure Names`/`Measure Values` columns) instead of one column per
  measure — the aligner correctly refuses rather than guessing how to
  reshape it, so even a correctly-matched CSV can't validate yet.
- A chart's grain can ask for `Order Date` while Tableau's crosstab header
  is date-part-grouped (`Month of Order Date`) — no alias exists yet.
- Two measures (`Days to Ship Scheduled`, `Sales Forecast`) don't resolve
  against the backend table via `parity._resolve_measure_sql`.
- `treemap`/rank-table capture is unit-tested (`test_validation_evidence_
  bridge.py`) but not yet exercised by any real corpus workbook.
- Workbook-level status stays `BLOCKED` until every chart passes — by
  design ("no proof, no pass"), not a defect, but the realistic current
  ceiling given the gaps above.

`validation_evidence_bridge.py` added to `snowflake.yml`'s `pipeline_demo`
artifacts (22 → 23) — caught by `test_pipeline_demo_bundle_complete`
before it could crash the deployed app on import. All 75 regression gates
green throughout. Deployed live to
`WBR_DB.PUBLIC.TABLEAU_TO_SIS_PIPELINE_DEMO`.

## 16. Cortex vision + full-channel capture + the client-facing report (2026-08-07, later)

**NOT DEPLOYED.** Everything in this section is local only; the last deploy
was §15's sidebar-note removal. Deploy is the first action for the next
session (`snow streamlit deploy pipeline_demo --replace -c wbr`).

### 16a. The finding that drove all of it: the structural score is not fit for purpose

The user asked whether the generated dashboard images had actually been
LOOKED at. They had not — one pair had been checked; the rest were inferred
from similarity numbers. Opening all ten pairs showed:

- **5 of 10 dashboards had NO app-side image at all** (`commission-model`,
  `order-details`, `sheet-22`, `shipping`, `what-if-forecast`).
  `headless_render` drew Altair charts ONLY, and those dashboards are
  KPI-tile / table / map-only, so their visual validation was auto-BLOCKED
  with nothing to judge.
- Of the 5 pairs that existed, only ONE was a like-for-like comparison.
  `performance`: Tableau renders month × segment rows against category
  columns with above/below-target colouring and reference lines; the app
  renders a plain monthly bar chart. **Two completely unrelated charts —
  scored 0.797.** `overview`: Tableau has 7 KPI tiles + a choropleth + two
  faceted STACKED area charts; the app had 6 single-colour panels, no KPIs,
  no map. **Scored 0.798.**

`visual_similarity` downsamples to 320×180 greyscale, edge-filters and
diffs. The genuinely-matching pair scored 0.858; unrelated charts scored
0.797. **The entire signal range is ~0.06 wide and the 0.85 threshold sits
inside that noise.** It cannot distinguish a faithful migration from an
unrelated chart, and it had been reported as if it could.

This is what justified the user's long-standing push for Cortex in
validation, on merit rather than as a demo feature: a pixel-diff
fundamentally cannot say *"Tableau breaks this down by segment and category
against target; the app shows a single monthly total."* A vision model can.

### 16b. Full-channel capture — `headless_render._capture_all`

The four visual channels `engine.py` can draw are now captured in ONE
render pass (previously: Altair only, plus a SECOND full render just to
catch KPI tiles):

| Channel | Exporter | Was |
|---|---|---|
| Altair charts | `vl_convert` | captured |
| Plotly (maps, treemaps) | `kaleido` (already used by `verify_visual.py`) | refused |
| KPI tiles (`st.metric`) | PIL, drawn as the app's KPI row | invisible |
| Tables (`st.dataframe`) | PIL, top 25×10 with the cap STATED on the image | invisible |

`render_sheet_to_png` now stacks every channel a sheet drew (KPI tiles
first, matching Tableau's own header-row placement) rather than picking
one. `capture_sheet_chart` / `capture_sheet_kpis` became thin wrappers over
the shared pass — one capture path, per this project's standing rule
against silently-diverging duplicates.

**Result: sheet coverage 12/20 → 20/20, and all 10 dashboards produce an
image** (9 Tableau + 9 app pairs in the latest real run, 0 missing-image
placeholders).

### 16c. Cortex vision wired in — `deep_validation.render_cortex_vision`

`parity.vision_validate_dashboard` had survived §15's UI removal intact, so
this is wiring, not new machinery. Click-gated, inside the proof-first
panel, reusing the SAME image files the pack already scored (re-pulling
would risk judging different pixels than the report shows). The
deterministic score is kept BESIDE Cortex's verdict, never replaced —
`_similarity_for()` prints it as the labelled cross-check, and a
disagreement is surfaced rather than silently resolved. `_omitted_sheets`
still tells Cortex which sheets the exporter genuinely cannot draw so a
renderer limit is never reported as a migration bug.

**LIVE RESULT (9 dashboards, ~39,267 tokens, `claude-opus-5`): Cortex
returned BUG on all 9, each with a specific, checkable reason** — a
forecast/confidence band missing, a bullet-chart matrix rendered as plain
bars, per-panel axes where Tableau shares one, a `$0` reference line
replaced by a trend line, Canadian provinces missing from the choropleth,
KPI percentages differing (46/28/27 vs 49.7/24.0/26.3), two columns and a
filter header dropped. **The decisive one: Customer Analysis scored 0.858
→ structural PASS, and Cortex found a real difference.** The deterministic
metric would have shipped that as clean.

### 16d. The client-facing report — `validation_report_dashboard.py` (NEW)

Built to the structure the user supplied
(`dashboard_validation_report_sample.html`). A SECOND renderer over the
SAME already-validated `run` dict `validation_report.validate_run()`
returns — it decides nothing, and the vendored engine plus its
`test_validation_pack_adapter` lock stay untouched. Per dashboard: header
stat strip, **A** visual comparison (both images + structural score +
Cortex verdict), **B** chart index + **chart data contract** (grain,
measures, T/S/B shapes, key/order check, max diff) + expandable per-chart
records with **pairwise** verdicts (Tableau↔Streamlit, Streamlit↔Backend,
Tableau↔Backend), **C** logic/calculation, **D** filters/interactions.
Then consolidated exceptions, evidence & reproducibility, sign-off record.

**Deliberate deviation, stated in the module docstring:** the sample shows
`98.1% pixel similarity` and `Layout: max shift 4 px`. This project cannot
honestly measure either — the app-side image is a headless re-render of
chart objects, not a screenshot of the deployed SSO-gated app, so there is
no pixel registration between the two. The real structural score is shown,
explicitly labelled triage-only. **A number this project cannot measure is
never printed.**

Added to `snowflake.yml` (23 → 24 artifacts).

### 16e. A REAL app bug the validation caught — and it is still open

`Product Detail Sheet` moved from BLOCKED to **FAIL**, and the failing
`Discount` values were all integer multiples apart (app 0.1 vs backend 0.2,
0.2 vs 0.8, 0.1 vs 0.4). Rather than assume either side, both were checked
against **Tableau itself** via the REST crosstab:

| Order | Tableau | App | Backend |
|---|---|---|---|
| CA-2021-139675 | **0.2** | 0.1 ✗ | 0.2 ✓ |
| US-2021-102715 | **0.8** | 0.2 ✗ | 0.8 ✓ |
| US-2021-103310 | **0.4** | 0.1 ✗ | 0.4 ✓ |

**The validation was right and the migrated app is wrong.** Root cause:
`profile_superstore.py:20` curates `"Discount": {"sql": "AVG(DISCOUNT)"}`.
That file's own docstring calls the measure library "a FALLBACK for
format/label polish", but because `Discount` is a raw column (not a
calculated field), the curated `AVG` silently wins over the workbook's own
declared `agg: sum` on that sheet's pill. This is the curated-profile-leak
bug class §8 already warned about — realized on Superstore itself.

**DELIBERATELY NOT FIXED:** correcting it means making the engine honor the
sheet's declared aggregation over the curated profile, which CHANGES NUMBERS
THE DEPLOYED APP RENDERS, and `Discount` legitimately appears as `AVG` on
other sheets (Tableau's own "Avg. Discount" KPI). That is an app-fidelity
decision, not a validation change, and the user's standing instruction was
to keep to validation. Needs an explicit decision.

### 16f. A false failure, diagnosed but not fixed

`What if Forecast Based on` FAILs for a validation-side reason: Tableau's
crosstab for that view is at a far finer grain than the chart — **578 rows**
(Month × Quarter × Region × Segment × Measure) including **170 total rows**
labelled `All` — while the chart displays **12 marks** at (Region, Segment).
The comparison indexes by (Region, Segment) and keeps one row per key
instead of re-aggregating, so it compared one month's `$16,479` against the
app's full-period `$253,962`. Needs total-row filtering plus grain-aware
aggregation, or an honest refusal when the export grain is finer.

### 16g. Harness lesson worth keeping

The first live Cortex vision run reported "all seven KPI tiles display n/a"
as a migration BUG on Executive Overview. Cortex was describing the image
accurately; **the image was the harness's fault.** Creating a Snowpark
`Session` makes it the ACTIVE session, and `backend.run_sql` auto-detects
that and routes EVERY query to Snowflake — where the local IR's table names
(`SUPERSTORE.PUBLIC.ORDERS`) do not exist, so every measure returned `n/a`.
Fixed by rendering all app images on DuckDB FIRST and connecting to
Snowflake only afterwards, for the Cortex calls. Generalizable: an AI
verdict is only as good as the artifact handed to it — verify the artifact
before trusting the judgment.

### 16h. Open, and explicitly unresolved

- **The user's last question is unanswered**: with this many REVIEW/FAIL/
  BLOCKED rows, how can anything read PASS? Section A can show a green
  `visual gate: PASS` (structural 0.858) directly above a Cortex **BUG**
  panel — a real, visible contradiction in the report that needs resolving,
  most likely by refusing to show a green PASS for a metric documented as
  unable to discriminate.
- The Discount app bug (§16e) — needs a decision.
- Pivoted-CSV alignment (`Measure Names`/`Measure Values`), date-part column
  aliasing (`Month of Order Date` → `Order Date`), and two measures that
  don't resolve against the backend (`Days to Ship Scheduled`,
  `Sales Forecast`) — all still open from §15.
- Table PNGs render RAW values (`4807.371999999999`) where Tableau shows
  `$4,807`, because `r_table` formats via `column_config`, not in the frame.
- Nothing in §16 is deployed.

All 75 regression gates green throughout. `test_headless_render_to_png` was
updated — a Plotly sheet now asserts a real PNG export instead of the old
"not yet supported" refusal, with a separate case proving a figure-less
Plotly call still yields a stated reason and never a fabricated image.

---

## 17. The Streamlit screenshot capture was inventing a layout (2026-08-07, later still)

**DEPLOYED 2026-08-07** (`snow streamlit deploy pipeline_demo --replace -c wbr`
-> `WBR_DB.PUBLIC.TABLEAU_TO_SIS_PIPELINE_DEMO`). This deploy also shipped
everything in §16, which had been pending since that session -- so §16's
"NOT DEPLOYED" banner is now stale too. Object existence and the staged
`headless_render.py` content were both verified; the running app was NOT
opened (SSO-gated browser).

User: *"Streamlit app is correctly rendering but you are capturing it wrong."*
Correct. Three separate defects in `headless_render.py`, all of which made
every Tableau-vs-app image pair mismatch on things the migration never got
wrong. Each was found by OPENING the image, not by reading a score — the
same lesson as §16a, now applying to the capture rather than the metric.

### 17a. The zone tree was thrown away

`render_dashboard_to_png` did `rows = [[s] for s in dash["sheets"]]` for any
dashboard with a `layout` — one sheet per row, in SHEET-LIST order. The app
renders the same dashboard through `engine._render_layout`, which walks the
workbook's zone-container tree and hands each `horz` zone's `w` weights to
`st.columns`.

Superstore's Customer Analysis was therefore captured as
scatter → rank → KPI-row stacked vertically, where both Tableau and the app
show the KPI row on top with scatter and rank side by side.

Fixed with `_composite_zone`, the image-side twin of `_render_layout`: it
walks the same tree, splits `horz` zones by the same `w` weights and stacks
`vert` zones in child order. A dashboard with no `layout` gets an equivalent
tree built from per-sheet `geom` rectangles (`_geom_layout_tree`, via the
existing `engine._rows_from_geom`), so both shapes go through ONE compositor
instead of two independently-guessed ones. A sheet the tree never references
is still rendered, appended and noted — never silently dropped.

The dashboard's own title is drawn above the composite (`_title_band`),
because Tableau's REST view image carries it and its absence was the first
visible difference in every pair.

### 17b. Panel arrangement inside a sheet was assumed, not read

Multi-panel sheets (`r_mbar`, `r_strips`, `r_circle`) make several
`st.altair_chart` calls in one render. The capture combined them with
`alt.hconcat` unconditionally. That is right for panels drawn into an
`st.columns(...)` row and wrong for panels drawn one after another down the
page — Executive Overview's two 3-panel small-multiple sheets came out as a
single 6-across strip where Tableau (and the app) show two columns of three.

`_capture_all` now tags each captured chart with the id of the `st.columns()`
call it was drawn into (`altair_cols`), including the `with col:` form that
routes a module-level `st.altair_chart` into a column, and `_arrange_altair`
hconcats only same-group runs, vconcat-ing everything else.

### 17c. Charts were rendered small and then MAGNIFIED

`engine.py` draws with `use_container_width=True`, so in the app a chart
fills its zone. Nothing carried that to the export, and Vega-Lite's default
for a discrete axis is a 20px step — Product Drilldown's heatmap (12 month
columns, a `$16,946`-style label in every cell) exported ~240px wide with
every label overlapping its neighbours. The compositor then upscaled that to
the zone width, magnifying the collision into an unreadable smear that read
as an app bug. **The app renders it fine; the capture was making it up.**

`render_sheet_to_png` now takes a `width` and pushes it into every channel
that can honour it: `_fit_spec_width` sets an explicit pixel width on the
Vega-Lite spec's leaf views (leaving an intentional `{"step": n}` alone),
kaleido is given the width directly, and a KPI row spreads across its zone
instead of being drawn at 230px/tile and blown up. The compositor DOWNSCALES
a supersampled render and PADS a narrower one — it never magnifies.

### Gate

`test_dashboard_composite_follows_zone_tree` proves zone ORDER, side-by-side
GROUPING, even WIDTH SHARE and the pad-don't-magnify rule POSITIONALLY on the
real Superstore fixture (each sheet stubbed to a distinctly-coloured block,
then pixels inspected), plus `_arrange_altair`'s three arrangement cases.
Deliberately not a similarity score — a score is what hid this class of bug
in the first place.

All 77 regression gates green.

### Still open after this

- The app's filter defaults are "All"; Tableau's saved state on several
  dashboards is not (Shipping is Year 2024 / Q4, which is most of why its
  49.7/26.3/24.0 split differs from Tableau's 46/28/27). The two images are
  therefore of different filter states. This is an app/migration gap, not a
  capture one, and is not fixed here.
- Tableau's right-hand filter/legend cards are not drawn app-side (every
  input widget is mocked away during a headless render).
- Everything listed under §16's "Still open" remains open.

---

## 18. Real screenshots of the real app, not a re-render (2026-08-10)

**DEPLOYED 2026-08-10.** New module `app_screenshot.py`; wired into
`deep_validation.py`'s `_build_validation_pack` and `render_proof_first_
validation`. Also fixed live, same session: `_table_to_png` IndexError on a
duplicate-column table, tofu-box/Δ glyphs in rank tables, and a Streamlit
subprocess pipe-write deadlock.

### The question that ended §17's whole approach

User, mid-way through a THIRD round of headless-render patching (zone tree,
panel arrangement, chart width): *"why are you rendering it again -- just
screenshot the app."* Correct, and it undercuts every fix in §17 at once: a
re-render is a SECOND RENDERER, and any layout decision it makes
independently of `engine.py` is a fabrication. Proven concretely by
screenshotting the real running app and diffing against the latest
re-render: on Customer Analysis the re-render drew all 30 customer names
because it gave the chart more height than the app does, while the real app
correctly drops every other label past its real render height. **The
re-render was flattering the migration**, not approximating it.

### Why a real screenshot is possible at all

`headless_render.py`'s original rejection of screenshotting was about the
DEPLOYED app -- correctly, since Streamlit-in-Snowflake sits behind SSO with
no browser runtime and no outbound access. But the artifact under
validation is the GENERATED app, which runs on localhost from a workstation
exactly like `restart_apps.py` already does. No SSO involved. `playwright`
was already an installed dependency; `scikit-image` (SSIM, used elsewhere in
the vision pipeline) was the only gap, now installed.

### `app_screenshot.py`

`capture_app(app_path)`: launches `streamlit run <app_path>` as a
subprocess, drives a real Chromium tab via Playwright at 1800px/2x device
scale (matches the report's layout width and Tableau's own exported view
width), clicks through every `st.tabs(...)` dashboard tab (the same
mechanism `engine.run` uses), and screenshots each `tabpanel` element.
Returns `(shots, notes)` -- `shots` is `{dashboard_title: png_bytes}`,
`notes` states per-dashboard capture failures. `available()` checks
playwright + a launchable Chromium and returns `(False, reason)` otherwise --
the caller reports visual evidence BLOCKED with that reason, never a
fallback render. `_render_app_screenshots(ir, stem)` in `deep_validation.py`
generates the app source via `codegen.build(ir)`, writes it into the
project's own directory (so `from engine import run` resolves), calls
`capture_app`, and always deletes the temp file.

**Explicit 2026-08-10 user decision on scope:** real screenshot ONLY;
`headless_render.render_dashboard_to_png` is no longer called for the
IMAGE leg of the validation pack (it still supplies the STREAMLIT DATA leg
via `capture_sheet_chart` -- a data capture, untouched). When no browser/
local Streamlit is available (inside the deployed SiS app itself), visual
evidence is BLOCKED with that reason -- this is a real, accepted cost:
Stage 5 running from inside the deployed app can never produce Streamlit-
side visual evidence, only from a workstation, which is how packs are
actually generated today (the Tableau REST connection lives there too).

### Three real bugs found chasing this down, all fixed live

1. **Subprocess pipe deadlock (the big one).** `capture_app`'s first version
   redirected the child's stdout to `subprocess.PIPE` and never read it.
   Streamlit's own console warnings fill the 64 KB Windows pipe buffer after
   about two dashboards, and the child then BLOCKS ON WRITE mid-render --
   every tab after the first two timed out and looked exactly like a slow
   or broken app for two full investigation rounds before the mechanism (an
   undrained pipe, not app or Playwright logic) was found by diffing
   against a working standalone script that used `DEVNULL`. Fixed: redirect
   to a file. 10-dashboard capture time dropped from 646s to 137s in the
   same move, since nothing was blocked. Gated by
   `test_app_screenshot_no_pipe_deadlock`, which reproduces the hang on a
   bare `PIPE` and proves the file-redirect survives the same chatty child.
2. **`_table_to_png` IndexError on duplicate column names.** Found on a
   DIFFERENT corpus workbook (Global Sales Dashboard), the whole reason for
   this section's bugs to surface: `frame[list_of_names]` on a table with
   duplicate column names returns every matching column per name, so the
   sliced body came back wider than the header. Fixed to a POSITIONAL slice
   (`frame.iloc[:, :max_cols]`) instead of a label selection.
3. **12 of 27 sheets on E-Commerce (Software) Sales Dashboard invisible to
   the OLD capture.** `engine._rank_html` draws a rank table via hand-built
   HTML through `st.markdown`, which `_capture_all`'s monkeypatching never
   saw -- more than half that dashboard read "drew nothing capturable" even
   though the app renders it correctly. Fixed by patching `_rank_html`
   itself during capture (restored in the `finally`, since it patches the
   live app's `engine` module). This bug is now MOOT for the image leg
   (a real screenshot sees whatever the app draws, through any channel) but
   stays fixed because `headless_render` still supplies the data leg.
   Same pass fixed unrenderable glyphs (▲/▼/Δ/nbsp) drawing as tofu boxes
   in table PNGs -- `_ascii_glyphs` swaps known glyphs and drops the rest.

### Proof

`_build_validation_pack` run end-to-end against the real Superstore fixture
(`workbook_ir.json`, fake Snowflake session, `conn=None`): 9/9 dashboard tabs
captured as real screenshots, zero fallback notes, real per-chart comparison
CSVs produced. `pipeline_demo` bundle gate updated (`app_screenshot.py`
added to `snowflake.yml` -- its `playwright` import is lazy, inside
functions, so shipping the file costs nothing at runtime in the SSO-gated
sandbox where it correctly reports itself unavailable).

All 80 regression gates green.

---

## 19. Chart data-capture gaps + Tableau CSV assignment fixed, live-verified on a second corpus workbook (2026-08-11/12)

**DEPLOYED.** User asked to generate the R12 pack against Regional Analysis
(a corpus workbook §17/§18's fixes had never been run against) and then,
after that came back clean offline, to connect it to the real live Tableau
account and rerun. Both requests surfaced real bugs that Superstore's own
corpus had never exercised.

### 19a. Pie / stacked-bar / line charts had no comparable data

Five of six Regional Analysis charts reported `captured chart exposed no
dataframe to compare`. Root cause, found by directly inspecting the
captured chart objects rather than guessing from source: `engine.py` draws
a stacked bar or a line-with-labels sheet as `alt.layer(mark, text)`.
Altair's `LayerChart` only hoists a shared `.data` to its own top level
when EVERY layer carries the exact same dataframe object — and the
text-label layer always adds derived columns (a stacking cumulative
offset, a label midpoint), which breaks that equality every time. The real
rows were one attribute away, on `chart.layer[0].data`, and
`validation_adapter.streamlit_rows` only ever checked `chart.data`.

Fixed with `_candidate_dataframes` (`validation_adapter.py`): walks
`layer`/`hconcat`/`vconcat`/`concat` depth-first and returns the first
candidate whose columns are a FULL superset of the columns the mapping
needs, falling back to the best partial match rather than silently
preferring a worse one.

The pie chart failed differently: `channel 'theta' renders column 'VAL'
under no resolvable Tableau caption`. `engine.r_pie`'s `theta` channel had
no Vega-Lite `title=`, while `color` (built from the same sheet spec) did.
One-line fix — `title=m["caption"]` on both `theta` and the tooltip.

### 19b. A multi-sheet dashboard's Tableau crosstab was being thrown away

Tableau's dashboard-level `query_view_data` export returns ONE sheet's
crosstab, not a per-worksheet one — a known, documented limitation. The
existing matcher's single-sheet-dashboard fallback (`len(dash_sheets) ==
1`) correctly refused to guess for a multi-sheet dashboard, but that meant
real, usable evidence was discarded outright: View1's pulled CSV was
literally `Category wise Sales by Region`'s own data, and View2's was
`Profit by Category`'s.

Fixed with `_assign_dashboard_csv_by_header` (`deep_validation.py`): the
crosstab's own header proves which sheet it belongs to. A sheet's full
declared field set (`parity._sheet_pill_captions` — measures + strong
dims + weak dims, the same closed shelf-key scan the dashboard-section
validator already relies on) must EXACTLY equal the header's columns. A
SUBSET match was tried first and found genuinely ambiguous live — View2's
real 3-column header is a superset of both `Region level Sales`'s needed
fields `{Region, Sales}` and `Profit by Category`'s `{Category, Region,
Sales}` — so subset matching produced two hits and refused both. The exact
match resolves uniquely to `Profit by Category` alone. Still refuses (never
guesses) the moment more than one sheet's exact field set matches.

### 19c. Tableau's own numbers were silently never being compared

Even once matched, the pulled Tableau values never actually landed in a
comparison: Tableau's crosstab export formats a number with a thousands
comma (`"163,797.1638"`), which Python's `Decimal()` rejects outright.
`validation_report._d()` silently returned `None` for every Tableau cell
in a comma-formatted export — never reported wrong, which is a worse
failure mode than reporting wrong: the chart could read PASS purely on
Streamlit-vs-backend agreement while the Tableau leg was never checked at
all. Fixed by stripping the comma before parsing, matching the same
handling this project already applies to Tableau-sourced numbers elsewhere
(`parity.py`, `validation_adapter.py`).

### Proof

Live-verified twice against the real account
(`prod-useast-b.online.tableau.com`, site `b360bi`, workbook `Regional
Analysis Dashboard`): the second run shows real Tableau screenshots and
crosstabs pulled for both dashboards, genuine structural similarity scores
(View1 0.844 REVIEW, View2 0.880 PASS — not BLOCKED for missing evidence),
every chart producing real Tableau + Streamlit + backend rows, and every
compared cell on the two fully-evidenced charts reconciling within
tolerance (`tableau_streamlit_diff` now a real near-zero number, not
blank).

Two new gates prove the exact live mechanism, not a reimplementation:
`test_layered_chart_streamlit_rows_and_pie_theta_caption` (against the real
Regional Analysis fixture, plus a synthetic partial-vs-full-match case) and
`test_multisheet_dashboard_csv_matched_by_header_and_thousands_comma`
(reproduces the real View2 subset-match ambiguity and proves the exact-set
fix resolves it). Suite 80 → 82 gates, all green. **CORRECTED 2026-08-13**:
this section originally read "82 → 84," an arithmetic slip (the same class
of miscount R1's "59→60"/"60/60" note already flagged and fixed once
before) — §18 ended at 80, this section added exactly 2 named gates
(`test_layered_chart_streamlit_rows_and_pie_theta_caption`,
`test_multisheet_dashboard_csv_matched_by_header_and_thousands_comma`), so
80 + 2 = 82. Verified by a full live run of `tests/test_regression.py`
2026-08-13: 82 gates defined total, 81 auto-run (all pass), 1 deliberately
manual (`test_onboard_resolves_multitable_missing_before_stopping`, needs a
live session).

### One real finding surfaced, deliberately not silently patched

Both fully-evidenced charts still read chart-level `FAIL` despite every
cell passing — from `order_match`, an exact row-SEQUENCE check in the
vendored comparison engine (`validation_report.py`). That check is
meaningful for a ranked list, where display order carries visual meaning;
it is not meaningful for a grouped bar chart, where it doesn't. This
touches the vendored engine's own structural-failure semantics, a bigger
and more contestable decision than what was asked — flagged to the user
rather than loosened unilaterally.

A third corpus workbook (Global Sales Dashboard) was opened during this
session's screenshot testing and its View2 dashboard crashed the
re-render-based capture — surfaced, then superseded before being
diagnosed: the conversation moved to the real-screenshot rewrite (§18)
before this crash was root-caused. Not fixed; not investigated further
this session. Worth a follow-up.
