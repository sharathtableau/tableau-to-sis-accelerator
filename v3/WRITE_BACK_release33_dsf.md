# Write-back: release33 — DSF (workbook-wide filters) + C5 (FIXED before dimension filters) + C6 (table functions) + C7 (LOD bodies / keys) + C8 (measure names, LOD body references) — 11 Nov 2026

Device was disconnected when this was finished. Apply to `tableau_accelerator_v3_cortex` (back up first to
`before_release33/`), verify md5 on the device, run the tests, then add the journal entry below.

## Files (cloud md5 after change)
| File | New md5 | Expected device md5 BEFORE (release32 / current) |
|---|---|---|
| tableau_parser.py | 22b26051bf196c1fac68ff785f272c10 | 297b422902abac7605c92a26187f263b |
| engine.py | de7021a9bc21189a3b153b672c08bd85 | 929d14e2da1a57d0b8a3dab0e8ba984e |
| v3_sheet_plan.py | 3a038ff846d476c3cbad80782306cf71 | c91d283be687ebe8be5a78d3b546b4aa |
| visual_plan_common.py | 538cd18bca1ec7295ad244259717d9a6 | 69e1ee881d9366d6111d005ebf097e10 |
| calc_translator.py | ec5632186eb5f65ea5761843bf9382cf | cee98146e798056e7d19c945809373a4 |
| v3_local.py | bc46b71135c0acd150666e80a384bd94 | 463e04f69f33bd16f082f81f464bfc79 |
| field_identity.py | 76a0dda45a43b595d1bed8e4a933e857 | 002d366c1e0f88c8949c239ff45d6272 |
| tests/test_regression.py | b3e98e7b05004a774600b88b1d48fb8d | 3b8ef20664adcfcc7ad3e4eaf0744e84 (one block changed: test_table_calc_engine's RUNNING_/LOOKUP refusal -> translate to __TC_ORDER__) |
| tests/test_v3_dsf_workbook_filters.py (new) | e39926318f283f520db877924fdda8be | — |
| tests/test_v3_c5_fixed_order.py (new) | 45de3f2f997b3565265da836c665fbca | — |
| tests/test_v3_c9_relationship_direction.py (new) | 29fbbc8c5ef92bd07dd413c0a811d70d | — |
| tests/test_v3_generic_guard.py (new) | 3118bd25c968dd73f179e77d42ed9c14 | — |
| tests/test_v3_c8_measure_names.py (new) | 2db8231f5433e0f80d55bec06a2e552d | — |
| tests/test_v3_c7_lod_bodies.py (new) | 2e697d19fdb3da5e292b549482d87de6 | — |
| tests/test_v3_c6_table_functions.py (new) | f924fa6909cca8568f1c483dd7f2d70a | — |
| tests/test_v3_c11_lod_grain.py (new) | dcea7b8cfe21ba9ab80e1a8b1d22792f | — |
| tests/test_v3_boundary.py | 3e37383f5eb2ab5b9d862c90cd64e850 | fb1ff8465fa747786213201c9de0e5d3 (its "untranslatable" example was LOOKUP, which C6 translates; now a nested INCLUDE-over-FIXED) |
| tests/test_v3_cx3_lod.py | b5dfae96d576f124890c5a15e56e1c51 | 8d14f231ce71db2467e71283016404ff (source check window 1500 -> 4000 chars: window_sql grew in C6) |
| v3_relationship_query.py | 279fe999ece8f9501c32404a906879cd | the release32 copy (unchanged since DM4; = project claude/pending_code before 12 Nov) |
| tests/test_v3_c12_primary_and_context.py (new) | 66ee6bca1154ef89656dbadfe533b96a | — |
| tests/test_v3_x12_relationship_grain.py (new) | a6b2c513c5a70f8c9296182c485d388d | — |
| v3_review.py (new) | 80b965deacd65ee8d0f89665765925ee | — |
| v3_console.py | 7b55c2fe51933e65bc2848655b6ad30b | 654a3cba1c55169dc765451e044d6d46 (the 10 Nov copy) |
| v3_deploy.py | 442d9476b8b99e90c5a9f494abb7920c | 23701007833092bc716d0acdf30bf05b |
| tests/test_v3_x13_x15_reviewer.py (new) | cb683284bdf834b96d01ecff47b4cea9 | — |
| tests/test_v3_dm4_relationship_query.py | cde29f93765e15f2a6e95d9f17ff91ae | fa59b5359b32265f01d63ec04664e07d (the related-field filter test: refused -> applies, values checked) |

Already on the device (release33 so far): v3_exact_loop.py 4fa860e7…, tools/capture_project.py ba1a6e6e…,
tools/sheet_census.py 9f8989d7…, tests/test_v3_sheet_census.py 18b4e88f…, tests/test_v3_reference_census.py b3ff1b04….
If a device "before" md5 differs, stop: the device file changed since release32 — merge, do not overwrite.

## What changed (general rules, from the XML)
1. `tableau_parser.workbook_filters` / `add_workbook_filters`: filters outside every worksheet now reach every sheet
   of their data source:
   - "Apply to worksheets -> All using this data source" (`<shared-views><shared-view name=DS><filter>`): ordinary
     sheet filter, `scope: all_using_datasource`;
   - data source filters (`<datasource><filter>`, direct children): `context: true`, `scope: datasource`.
   A worksheet's own filter on the same field and date part wins.
2. Data source filters always apply: a dashboard control on the same field no longer lifts them
   (`engine._apply_sheet_filters`, `visual_plan_common.apply_filters`).
3. Date parts: Tableau's Month/Year (`my:`, members yyyymm) is parsed; an EXCLUDE filter on any date part keeps its
   date part (parser) and compares part numbers, not raw dates (engine `_value_predicate`, `_part_sql`);
   `visual_plan_common` routes date-part filters to the engine predicate.
4. `v3_sheet_plan.from_sheet`: a date part the plan does not model refuses with the reason instead of silently
   grouping by the raw date.

## Proof
- Fil Test (Tableau capture values, 11 Nov): Sheet 1, 2, 5 equal Tableau (e.g. Sheet 2 Jan 49.84, Sheet 5 Q4
  91,290.94) — before: all-years values.
- Second workbooks: Customer Opt-In (shared-view filters on calculations: 42 -> 39 months), BDO (data source
  filters incl. Month/Year exclude, 25/27 sheets run; 2 refusals are untranslated calcs), Clara Brazil (both kinds).
- Corpus before/after (49 workbooks, bundled data): changes ONLY in the 4 workbooks that have workbook-wide filters
  (+ Online Retail 0.02 float noise that varies run to run). FAILED 90 -> 86; RAN 419 = 419.
- Tests: new DSF file 9/9 (4 of them red on the old code); 134 tests across census/exact/filters/semantic/LOD/
  table-calc/relationship suites OK; inherited regression identical to the cloud baseline (21 ok, same fixture stop).

## C5: FIXED LODs before dimension filters (engine.py, v3_sheet_plan.py)
Tableau order: data source filters -> context filters -> FIXED -> dimension filters (sheet filters, dashboard controls,
filter actions). The plan path computed {FIXED k: AGG(x)} as a window AFTER the sheet's WHERE and a no-key FIXED over
the whole table (no context).
1. `v3_sheet_plan.lod_where`: the sheet's context predicates only (`engine.context_conditions`, extracted unchanged
   from `_apply_sheet_filters`, which now calls it).
2. `v3_sheet_plan._fixed_before_filters` (used by `compile_query(..., lod_where=)`, passed by `execute`): when the
   sheet has filters beyond its context, every row-level FIXED window (`engine.scan_row_windows`, extracted unchanged
   from `_hoist_windows`) is computed in base queries under the context filters only -- nested FIXED layer by layer
   (__lod0, __lod1 ...) -- and the sheet's WHERE applies outside; a no-key FIXED (`__TBL__`) reads the context rows;
   inside a context filter it reads the whole table. Sheets without other filters: SQL unchanged.
3. `engine._context_cond`: a context filter on a row-level calculation is its predicate (boolean gate / value IN),
   never the caption as a column (also fixes the existing top-N context injection).
Proof: E-Commerce CLV / CLV% = a hand-written query in Tableau's order (1,453.69 -> 8,877.30); a context filter
restricts the FIXED value; no-key FIXED reads the context rows; nested FIXED layered (Avg. Days to 2nd Purchase).
Tableau captures unchanged: Superstore 2024.3 19/20 (+Forecast partly), R1 13/13, R1-V2 13/13. Corpus before/after:
FAILED 90 -> 86 (all from DSF), no sheet newly failing; outside the DSF workbooks only E-Commerce FIXED+filter sheets
change. Tests: C5 7/7 (red before), 140 across suites OK, inherited regression identical to the cloud baseline.

### Journal entry to append (C5)
### 11 Nov 2026 — C5: FIXED LODs computed before dimension filters (release33: engine, v3_sheet_plan)
- Rule (general): FIXED values see only context + data source filters; the sheet's other filters apply after. Nested
  FIXED layer by layer; no-key FIXED over the context rows; context filters on calculations as predicates.
- Proof: E-Commerce CLV/CLV% = Tableau-order reference; Superstore/R1 still equal Tableau's captures; corpus
  before/after changes only FIXED+filter sheets, nothing newly failing. md5s in the table above.

## C6: table functions in calculated fields (calc_translator, tableau_parser, field_identity, v3_sheet_plan, engine)
Before: LOOKUP / FIRST / LAST / RUNNING_* / TOTAL refused, ZN passed through untranslated -> ~50 of 86 corpus failures.
1. calc_translator `_rewrite_table_functions`: LOOKUP(e, n|FIRST()|LAST()) -> LAG/LEAD/FIRST_VALUE/LAST_VALUE;
   RUNNING_SUM|AVG|MIN|MAX|COUNT -> window from the first row; TOTAL(SUM|MIN|MAX|COUNT) -> re-aggregated window
   (TOTAL(AVG) refused); FIRST()/LAST() offsets; ZN -> COALESCE. Order-dependent ones use their OWN placeholder
   __TC_ORDER__ (__TC_ORDER_DESC__ for LAST): the exact plan fills it; engine.q refuses it elsewhere (never a guessed
   order). Divide-by-zero guard covers windows and LAG/LEAD/ABS/COALESCE denominators (Snowflake raises; Tableau NULL).
2. One window rule `calc_translator.is_window_sql` (parser + scoped field catalogue): WINDOW_* / TOTAL now also get the
   sheet's partitioning from the pill's Compute Using.
3. v3_sheet_plan `window_sql` orders the addressing in the SHEET'S SORT (manual order, computed sort by a measure in its
   direction, else ascending; LAST reversed); an unresolvable sort refuses an order-dependent calc.
4. Table-calculation filters (LAST()=0, INDEX() in 1..10, ...) apply LAST: computed as a flag column over the aggregated
   view and the marks hidden after every table calculation (`_table_calc_filters`, `_finish(keep=)`). INDEX/RANK <= N
   gates keep the engine's own handling.
Proof: Online Retail 'Product. (2)' INDEX() in 1..10 = exactly the 10 highest-sales countries (was all 37);
'Daily trend.' LAST()=0 = the last day only (was 305 days); MOM (Email) LAG along the addressing with NULL first row.
Tableau captures unchanged (Superstore 19/20 + Forecast partly, R1 13/13, R1-V2 13/13). Corpus: FAILED 86 -> 79, nothing
newly failing; changed sheets = only those using these functions. Tests: C6 12/12 (red before), 185 across suites,
inherited regression identical to baseline (its RUNNING_/LOOKUP expectation updated with the new contract).

### Journal entry to append (C6)
### 11 Nov 2026 — C6: table functions in calculated fields (release33)
- LOOKUP / FIRST / LAST / RUNNING_* / TOTAL / ZN translated as windows the plan fills from the sheet's addressing in
  the sheet's sort; never a guessed order (refused off the plan path). Table-calc filters applied last.
- Proof: Online Retail top-10 / last-day filters, Email MOM; Tableau captures unchanged; corpus 86 -> 79 failures,
  nothing newly failing. md5s in the table above.

## C7: FIXED bodies and keys (calc_translator, engine)
1. `_window_each_agg`: a FIXED body combining aggregates ({FIXED k: SUM(a)/SUM(b)}) -> each aggregate OVER (PARTITION
   BY k) (the body over the partition's rows); refused when an aggregate nests another or a field sits outside every
   aggregate.
2. Calculated FIXED keys resolve to their own row-level expression (`_key`); they were emitted as the internal name
   (PARTITION BY CALCULATION_2425...) -> 'column not found'. An aggregate key refuses.
3. engine.q: the no-key FIXED table placeholder never resolves to the column of EXTRACT(MONTH FROM DATE) (was 'Table
   with name DATE/MONTH does not exist').
4. v3_local `prefer_model_extracts`: a datasource whose model spans several data files binds to its decoded extract
   (every table of the model, what Tableau queries), not the first raw file. Clara Brazil (only workbook with this
   shape; both its datasources): the Calendar's Date1 now exists; its other sheets give identical numbers from the
   extract as from the CSV.
5. v3_sheet_plan `_fixed_before_filters`: FIXED windows in the WHERE's own predicates (a filter on a row-level calc
   with a FIXED) are computed in the context base query too; the engine's filter subqueries are masked, untouched.
Corpus after 4-5: FAILED 62 -> 55 (Clara Trend / Dual Trend / Date Details / Bar Line Trend x7), none newly failing
vs release32. Tests 220 across suites (incl. DM1-DM6), regression identical to baseline.
Proof: Hospitality RevPAR per property-year = SUM(revenue)/SUM(rooms available) by hand for every property-year.
Tableau captures unchanged; corpus FAILED 79 -> 62, nothing newly failing (Hospitality KPI tiles, rank sheets, GOLD CRM
Audience Size / Months since Recency now run). Tests C7 8/8; 193 across suites; regression identical to baseline.

### Journal entry to append (C7)
### 11 Nov 2026 — C7: FIXED with aggregate expressions and calculated keys (release33)
- {FIXED k: SUM(a)/SUM(b)} and calculated keys translated; EXTRACT(.. FROM col) no longer taken as the table.
- Proof: Hospitality RevPAR = hand ratio per property-year; corpus 79 -> 62 failures, none newly failing.

## C8: Measure Names escapes and LOD body references (v3_sheet_plan, calc_translator)
1. `_member_unescape`: Tableau escapes '%' as '\%' in Measure Names members; unescaped for the Measure Names filter,
   its manual order, the parser's measure list and member-token occurrences. ('\% Open Rate (copy)_...' could not be
   translated: GOLD CRM x8, Email Campaign Overview, BDO funnel.) World Indicators 'Health Indicators': the escaped
   'Health Exp \% GDP' was a 5th (duplicate) measure and out of the authored order -- now 4, in order.
2. `_inline_lod_bodies`: a calculation referenced in an LOD BODY is its formula ({FIXED k: [Users]}, Users = SUM(..)),
   inlined before parameters / table functions / fields are translated; never a calculation that has an LOD of its own
   (resolved later as its own value). Campaign Incrementality KPIs A/B/E/F, overview, campaign level now run; KPI C
   keeps its release32 numbers.
3. Divide-by-zero guard also wraps a bracketed denominator '/ (expr)' (Snowflake raises, Tableau returns NULL).
Corpus: FAILED 55 -> 39, none newly failing vs release32. Tests C8 5/5; 226 across suites; regression identical.

### Journal entry to append (C8)
### 12 Nov 2026 — C8: Measure Names '\%' escapes; LOD bodies referencing aggregate calculations (release33)
- GOLD CRM / Email / BDO measures with '%' names resolve; Campaign Incrementality's FIXED-of-calculation KPIs run.
- Corpus 55 -> 39 failures, none newly failing; Tableau captures unchanged.

## Generic-fix guard (tests/test_v3_generic_guard.py, engine.tbl)
- The release gate now fails when a string literal in a DEPLOYED module (every .py listed in snowflake.yml) equals a
  corpus workbook / dashboard / worksheet name -- the shape of a workbook-specific special case. Comments and
  docstrings are ignored; Tableau vocabulary (Date, Year, Overview ...) is not a name; Tableau capture bundles are data.
- Scan of all release33 changes: no workbook / sheet name used in code (only in comments saying where a gap was found).
- Reviewed exceptions, listed in the test with reasons: 'SUPERSTORE' -- legacy local dev database / CLI defaults of
  the inherited numeric harness (config.py, init_workbook.py, semantic_layer.py); V3 clears config.DATASOURCES per
  workbook and refuses unbound datasources. Follow-up: move these into the harness's own fixtures.
- engine.tbl: in V3 a sheet that names no datasource refuses (was: the built-in default table). Corpus unchanged.

## C9: a sheet reads only the tables its fields come from (v3_sheet_plan, engine, v3_local)
- `tables_used`: the tables a sheet reads, from every bound field followed through calculations (any depth) and its
  filters. When that is exactly ONE related table left out of the flatten (with data), the sheet runs against that
  table alone (`_single_related` -> sheet datasource = that table) -- Tableau queries only the tables a viz uses.
- engine.field_scope: such a sheet may bind that table's fields and calculations built only on them (v3_local now
  records `uses_related` on a calculation whose missing columns all come from one related table); nested scopes
  (a measure resolved inside its sheet) inherit it. Any other use still refuses with the reason (E04 unchanged).
- Retails Dashboard (a many-to-many Customer -- Orders model): 7 sheets now run on Orders alone (Total Sales =
  SUM(Sales) of Orders = 10,121,287.60). 'Tabular data' (Customer Name beside order fields + a State filter) still
  refuses: a related-table filter in the per-table relationship query is a follow-up.
- Dropped draft: re-orienting 1 -- * relationships (it multiplied the one side's own measures; E04's fan-out test
  caught it). init_workbook.py is NOT changed.
Corpus: FAILED 39 -> 32, none newly failing; Tableau captures unchanged; 241 tests; regression identical.

## C10: date arithmetic, DATENAME / ATTR, text '+', integer parameters (calc_translator, tableau_parser)
1. Date +/- n -> DATEADD('day', +/-n, date) when the left operand is a date field, MIN/MAX of one (a no-key FIXED
   included) or a date function (types from the workbook's own datatype declarations); numbers untouched.
2. DATENAME('year'|'quarter'|'month'|'day'|'weekday', d) -> Tableau's text in portable SQL (full month / weekday names;
   weekday counted from a known Sunday, independent of WEEK_START); other parts refused. ATTR(x) -> the value when
   every row agrees, else NULL.
3. '+' between an IF/CASE block whose results are string literals and anything else is concatenation ('||').
4. STR() of an integer-typed parameter -> its integer text ('2025', not '2025.0').
5. Measure filters in the exact plan are the grouped query's own HAVING (`_having_predicate`): exact at the marks,
   before table calculations, no re-derived mark key (a FIXED dimension broke the engine's IN-subquery form). The
   engine's subquery form stays for disaggregated views, blends and the other renderers.
6. A FIXED nested in parentheses (MAX((DATEADD(..))) OVER (PARTITION BY ('C_' || ..))) is row-level, not an aggregate:
   the window detection is bracket-matched (`_replace_windows`; was a one-level regex).
Corpus after 5-6: FAILED 26 -> 22 (Online Retail Customers. / Q.customer, Hospitality Property|Brand /
Property|Country), none newly failing; Tableau captures unchanged; 246 tests; regression identical.
Proof: GOLD KR 'Last Complete Week' (x2 workbooks) and E-commerce Widget KPIs / narration now run; the narration reads
"You're viewing May 2025 across all channels and all categories". Corpus FAILED 32 -> 26, none newly failing;
Tableau captures unchanged; 245 tests; regression identical.

7. Date literals in filter members (`#2022-03-01 00:00:00#`) -> CAST('...' AS TIMESTAMP) / DATE in in / not_in
   (`_member_literal`); a datepart member '#2022#' is the number. These context filters were silently dropped
   before. CORRECTION: Online Retail's sheets have a March-2022 context filter, so their numbers changed to Tableau's
   (Customers. 4,343 -> 973 customers; Weekly trend 305 -> 27 days). The two C6 Online Retail tests now derive their
   counts from the sheet instead of the old unfiltered 37 / >100.
8. Bins (`[X (bin)]`, calc class 'bin') -> FLOOR((x - peg) / size) * size + peg (field_identity; Tableau's bin
   lower edge). Clara Histogram runs.
9. A table calculation (window) used in a measure filter: the HAVING term is hoisted into the SELECT as
   `__HAVINGi__` and filtered on the outside query (`_split_top`) -- DuckDB/Snowflake refuse windows in HAVING.
   Hospitality Property / Country, City / YoY Metric Change (2) run.
Corpus after 7-9: FAILED 22 -> 17, none newly failing vs release32; 248 tests; regression identical; Tableau
captures unchanged (Superstore 19/20, R1 13/13, R1-V2 13/13).

## C11: FIXED over FIXED, INCLUDE / EXCLUDE at the sheet's grain (calc_translator, engine, v3_sheet_plan)
1. A FIXED whose body holds another FIXED (AVG(SUM(x) OVER (..)) OVER (..)) is row-level, not an aggregate: the
   agg_ready check strips inner windows first. (Hospitality =SuccessFlag was classed aggregate, so as a dimension
   or filter it became a raw column name.)
2. A FIXED keyed on a FIXED calc (window in the PARTITION BY) and a table calculation ordered by a FIXED (window in
   the ORDER BY): `scan_row_windows` takes the window inside an OVER clause first; `_fixed_before_filters` layers
   whenever windows nest (`_window_in_over`) -- SQL refuses a window inside a window. Sheets with no nesting keep
   their SQL unchanged.
3. INCLUDE / EXCLUDE (were refused): translated to `__INCLUDE__(AGG(x), __KEYS__(dims))` / `__EXCLUDE__(..)`; the plan
   writes the SQL at the sheet's grain, after the dimension filters (`lod_view_sql`). INCLUDE under the view's
   aggregation: SUM/SUM, MIN/MIN, MAX/MAX, SUM/COUNT = the inner aggregate; AVG/SUM, AVG/COUNT = inner / the number of
   include groups in the mark that have a value (HASH: a NULL member is a group). EXCLUDE: the inner aggregate
   re-aggregated over the view's dimensions minus the excluded ones (existing __VIEW_EXCEPT). Anything else (the LOD
   inside a larger expression, other aggregate pairs, AVG body) refuses with a reason. Proven on independent
   two-step arithmetic (every pair x 3 key sets) and on Online Retail: Q.avg = total quantity / days in the sheet
   (348,457 / 27 = 12,905.81), sales. = 590,587.85 / 27.
Corpus after C11: FAILED 17 -> 8, none newly failing vs release32; regression identical; Tableau captures
unchanged (Superstore 19/20, R1 13/13, R1-V2 13/13). Full suite (tests/test_v3_*): 908 tests; the only errors are
the 9 that need report fixtures absent in the cloud copy (composed_cards, rank_cards, original_presentation) --
run on the device.
Remaining 8: Retails Customer / Total Customer / Tabular data (calcs and a filter on the related Customer table),
Customer Opt-In Pie x2 (unlinked blend), Globalsales Details (blend without a link -- known), GOLD CRM Sankey (nested
table calcs -- refused on purpose), Regional (download error, not code).

## C12: primary datasource, context FIXED, related-table calculations and filters (tableau_parser, engine, v3_sheet_plan, v3_relationship_query)
1. A sheet with nothing on rows / columns (pie, text card) takes the FIRST datasource its view lists as primary
   (Tableau's rule); it took the first datasource-dependency. Customer Opt-In Pie / Pie (2) ran on the wrong
   datasource ("no blend link").
2. A no-key FIXED inside a CONTEXT filter reads the whole table also where that condition is repeated in the sheet's
   WHERE (it was scoped to the context rows: "complete months" = before the max of the complete months, which dropped
   the last complete month). Customer Opt-In: Table A (2) 0 -> 3 rows, Table B / C values 0 -> real, Line 4 / 5 gain
   the last month. Pie = the selected month's total exactly (1,883,776).
3. Relationships (DM4 scope widened): a calculation over the related table alone runs on that table's rows; one that
   MIXES both tables runs on their join (Tableau's rule for cross-table calculations), grouped at the view; the
   parent's own measures stay on the parent's rows. A filter on a related table's field keeps that table's rows and
   the parent rows they relate to (semi-join). A related field reads its own table's column. Still refused: a filter
   or dimension that mixes both tables, measure filters, cross-fact filters / calculations.
   Retails Total Customer: YOY Growth Customer 0.301675 = pandas on the joined rows (12,979 vs 9,971); Count of
   Customer Name 37,907 = the Customer table (the join would count the 54 repeated keys twice). R1 Superstore: a
   filter on Order ID (Returns) gives each order's own sales.
Corpus after C12: FAILED 8 -> 4, none newly failing vs release32; 915 tests (9 fixture-only errors as before);
regression identical; Tableau captures unchanged. Remaining 4: Retails Tabular data (a filter whose FIXED mixes both
tables), Globalsales Details (blend without a link), GOLD CRM Sankey (nested table calculations), Regional (download).

## C13: mixed-table filters, nested table calculations, Month / Year on a shelf (v3_sheet_plan)
1. A filter whose condition spans both related tables (Retails 'Customer 1' = {FIXED Order_ID (Orders):
   COUNT(Customer Name (Customer))} > 1, kept False) is evaluated per joined row; it keeps the relationship keys of
   the joined rows that pass, and each table keeps its rows for those keys (measures still from their own table).
   Refused on a sheet with context filters. Retails Tabular data: 8,985 orders, Sales 2,373,378.64 = pandas.
2. A table calculation computed over another table calculation (GOLD CRM Sankey) is refused with a reason
   (PlanError) instead of a database binder error reaching the sheet.
3. Discrete Month / Year ('my:' on a shelf) plans as the truncated month (as 'mdy' already did for the day): Clara
   KPI Monthly Details / Monthly Distribution (+ M variants) now plan, 8 months each.
4. Online Retail INDEX shows 1 per row: checked -- the shelf instance's Compute Using is Table (across)
   (ordering-type='Columns') with no column dimension, so each row is its own partition; the filter instance uses
   Table (down). Follows the workbook; confirm against a Tableau capture when one exists. No code change.
Corpus after C13: FAILED 3 (Globalsales Details blend without a link, Sankey refused on purpose, Regional
download), none newly failing vs release32; 919 tests (9 fixture-only errors); regression identical; captures
unchanged.

## X12: relationship-grain aggregation (field_identity, v3_sheet_plan)
Rule (master plan X12, root cause confirmed earlier against Tableau's capture): in a RELATIONSHIP datasource (object
model with <relationship> elements -- field_scopes[..]['relationship_model']), an AVG / SUM / COUNT / MEDIAN whose
inputs are only FIXED values on ONE key K (nested FIXED on the same key included) is aggregated once per (mark, K):
ROW_NUMBER() OVER (PARTITION BY the marks, K) numbered over the FILTERED rows, the aggregate reads the first row of
each key. Every such aggregate inside a measure is rewritten (WINDOW_MAX(AVG(..)) sees the same values). Unchanged:
single-table / physical-join datasources (row replication, Tableau's FIXED behaviour there), an expression that also
reads a row-level field (ECommerce CLV, the Current Period KPIs), MIN / MAX, two different keys, no-key FIXED.
Corpus: exactly one sheet changes -- E-Commerce (Software) VOTD 'Avg. Days to 2nd Purchase' (21 points), now the
per-customer average on every point (pandas, same filters); the old values equal the per-event average. Nothing else
in the 44 workbooks moves; Superstore / R1 captures unchanged (both are relationship models: no sheet qualifies).
NOT YET PROVEN AGAINST TABLEAU: no ECommerce capture or data in the cloud copy. On the device: compare the VOTD /
live ECommerce 'Avg. Days to 2nd Purchase' with the stored Tableau capture (target 21/21) before deploy.
The 'Max' colour flag is Max on every point before and after: its Compute Using is Table (down) with only a column
date, so each date is its own partition -- follows the workbook.

## X13-X15: reviewer decisions at deploy (v3_review, v3_console, v3_deploy, engine)
Roadmap items X13-X15 (master plan, raised 5 Oct). Deploy page gains "3 · Sheet decisions" (a table of every sheet
with its validation status):
- X14 hold back a sheet: ticked + a reason -> the deployed app does not draw that sheet; a one-line notice with
  the reason stands in its place (engine.render_sheet, review_hold). The rest of the workbook deploys. Every sheet
  held back is refused (hold the workbook instead); a hold needs a reason.
- X13 known-difference note: a short note (<= 300 chars) shown to app users under the sheet ("Known difference:
  ..."; review_note). A held-back sheet takes no note.
- Both go into the deployed IR (ir['review'] + per sheet) and into the deploy approval record (held_back,
  known_differences). Decided by a person only.
- X15 protect hand edits: when "replace the existing app" is ticked, the console reads the deployed app's own
  build manifest (candidate_build.json in its stage folder; DESCRIBE STREAMLIT / LIST / read, read-only) and lists
  files edited, removed or added since that build. Any change -- or a check that cannot run (not a V3 app, no
  access) -- needs a second confirmation; the result is recorded in the approval (replaced_app_edits).
  New SQL, all read-only, run only on the Deploy page when replace is ticked:
    DESCRIBE STREAMLIT WBR_DB.TABLEAU_ACCELERATOR_V3.<APP>
    LIST @<that app's root_location>/
  (and reading files from that folder through Snowpark session.file.get_stream).
Tests: tests/test_v3_x13_x15_reviewer.py (15, written first). Corpus / regression / captures unchanged.
Not verified live: the Snowpark Row shape of DESCRIBE STREAMLIT / LIST and get_stream on the V3 stage (owner run).

## Follow-ups
- Move the legacy SUPERSTORE dev-database defaults (config.py DATASOURCES / DB, CLI --db) into the inherited
  harness fixtures; then drop them from the guard's ALLOWED list.
- Online Retail INDEX on a shelf: confirm "1 per row" (Table across) against a Tableau capture (C13 item 4).
- Globalsales Details: a blend with no link field -- Tableau shows the secondary's grand total per mark; not
  reproduced yet.
- X12: prove 'Avg. Days to 2nd Purchase' against the ECommerce Tableau capture on the device (21/21).
- X15 live check: replace a hand-edited deployed app once and confirm the listed files.
- Master plan: mark G04 / G05 done (Global Sales parses; 'Sales above Target?' is evaluated by the blend path, Superstore Performance verified), X12-X15 built, X16 already labelled 'built'.
- DONE: `visual_plan_common.apply_filters` refused range / top N / relative-date filters instead of turning them into FALSE (an empty chart); none in the corpus today. Test VisualAdapterFilters.

## Journal entry to append (V3_FIX_JOURNAL.md)
### 11 Nov 2026 — DSF: workbook-wide filters (release33: tableau_parser, engine, v3_sheet_plan, visual_plan_common)
- Found by the sheet census: Fil Test all 5 sheets differ — its Year(Order Date)=2022 filter is "All using this
  data source" (<shared-views>), which the parser never read. Data source filters (<datasource><filter>) were also
  ignored (BDO, Clara).
- Fix (general): both kinds added to every sheet of their data source; data source filters as context and never
  lifted by a dashboard control; Month/Year date part; date-part exclude filters; unmodelled plan date parts refuse.
- Proof: Fil Test = Tableau; Customer Opt-In / BDO / Clara second workbooks; corpus before/after changes only in
  workbooks with these filters. Tests first (9 new, 4 red before). md5s above.
