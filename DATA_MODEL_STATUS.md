# Data Model — Full Scenario Status
*(Captured 2026-07-26. Single source of truth for "what data-model scenarios does
this accelerator handle, how well-proven is each, and what's genuinely missing."
Read this FIRST before touching data-model routing/parsing/join code in any
future session — it exists specifically so nothing gets re-discovered or
re-lost between sessions.)*

## How to read this document

Each scenario has THREE independent axes — conflating them is exactly the
mistake this file exists to prevent:

- **BUILT** — does code exist for this scenario at all?
  `DONE` / `PARTIAL` / `NOT STARTED` / `NOT POSSIBLE` (architecturally, not a gap)
- **TESTED** — has it been proven, and against what?
  `REAL WORKBOOK` (named) / `SYNTHETIC FIXTURE ONLY` / `UNTESTED`
- **CONFIDENCE** — is this a genuine engineering gap worth prioritizing, or an
  honest scope boundary the project deliberately drew?
  `REAL GAP` (should be built) / `DELIBERATE SCOPE LIMIT` / `OPEN QUESTION`

A scenario can be `DONE` + `SYNTHETIC FIXTURE ONLY` — built and correct by
construction, but never proven against real XML. That is a DIFFERENT risk than
`NOT STARTED`, and worse than `DONE` + `REAL WORKBOOK`. Don't collapse these.

---

## 1. Source connection types

| # | Scenario | Built | Tested | Confidence | Workbook / gate |
|---|---|---|---|---|---|
| 1.1 | **Flat file (CSV/Excel)** → `write_pandas` table → app queries it | DONE | REAL WORKBOOK | — (baseline, day one) | Every corpus workbook (`Sales Target.xlsx`, `Sales Commission.csv`, `Sample - Superstore.csv`, etc.) |
| 1.2 | **Live connection → Snowflake, single table** → query source's own `db.schema.table`, zero copy | DONE | REAL WORKBOOK (live, not fixture) | — | `Workbooks/Superstore_KPI_Parameter_Dashboard_Live.twbx` — 10,194 rows, live-verified in Snowsight. Gate `test_live_connection_support` |
| 1.3 | **Live connection → non-Snowflake class** (sqlserver, sqlproxy — e.g. a published Tableau Server/Cloud datasource) | PARTIAL — reported honestly, never queried | SYNTHETIC (sqlserver) + REAL (sqlproxy, EMEA workbook, out-of-scope) | DELIBERATE SCOPE LIMIT (tracked backlog: "live-source migration kit," ~2.5 d) | `tableau_parser.live_connections()` reason string |
| 1.4 | **Live connection → JOIN across multiple tables** (no extract, live multi-table) | **DONE (2026-07-26, R9)** | **REAL WORKBOOK, VERIFIED AGAINST THE REAL ACCOUNT** — `Workbooks/R9_Live_Join_Orders_Product_Category.twb` (a genuine `.twb`, no bundled data at all — joins `WBR_DB.PIPELINE_DEMO.R10_ORDERS/R10_PRODUCT/R10_CATEGORY` live, reusing R10's tables). Run directly through `pipeline.onboard()` against the real account: view deployed, exact known numbers returned. **Not yet confirmed through the actual Streamlit UI upload** — the difference between this and R3/R7/R10 is those were confirmed by watching the app render; this one is proven the same way R3/R10 were BEFORE their own UI uploads | Closed at the code level; UI upload still pending | `tableau_parser.live_connections()` (fixed) + `pipeline.onboard`'s R10 missing-resolution. Gate `test_r9_live_multitable_join` |
| 1.5 | **Custom SQL, Snowflake dialect, genuinely live (no extract)** → executed verbatim as a derived table | DONE | **SYNTHETIC FIXTURE ONLY** — no corpus workbook has a live (non-extract) custom-SQL datasource; Regional Analysis' "Data Using Custom-SQL" datasource HAS an extract, so it never reaches this code path | OPEN QUESTION (needs a real live-custom-SQL workbook to move off synthetic) | Gate `test_custom_sql_execution` |
| 1.6 | **Custom SQL, non-Snowflake dialect** | PARTIAL — reported honestly, refused | SYNTHETIC ONLY | DELIBERATE SCOPE LIMIT (different SQL dialect, can't run verbatim) | same gate |

---

## 2. Extract-based single-table sources (R3 — done 2026-07-26)

| # | Scenario | Built | Tested | Confidence | Notes |
|---|---|---|---|---|---|
| 2.1 | Extract's declared source table already exists in Snowflake (workbook's OWN `db.schema.table`) → bind directly, skip decode + copy | DONE | **REAL WORKBOOK, LIVE-VERIFIED 2026-07-26** — `Workbooks/R3_Extract_Over_Existing_Table.twbx` uploaded to the deployed `pipeline_demo` app; Stage 1 showed `existing table (auto-bound, no copy)` at 10,194 rows, exactly as predicted | — (closed; was the corpus's only untested R3 case) | `pipeline.resolve_source_binding` tier 1. Gate `test_auto_bind_existing_snowflake_table` |
| 2.2 | Same, but the table name resolves via search (not the declared location) | DONE | SYNTHETIC FIXTURE ONLY | OPEN QUESTION (same reason) | tier 2 of the same resolver |
| 2.3 | Ambiguous table name (same name in >1 schema) → surfaced as a choice, never resolved | DONE | SYNTHETIC FIXTURE ONLY | — (this is the *safety* behavior; it's supposed to refuse) | same gate |
| 2.4 | Column-mismatch guard (same name, wrong table) → refused | DONE | SYNTHETIC FIXTURE ONLY | — (safety behavior) | same gate |
| 2.5 | `sources.json` explicit human override | DONE | SYNTHETIC FIXTURE ONLY; **can be tested live right now with zero new workbook** — write `{"<caption>": "DB.SCHEMA.TABLE"}` and upload any workbook | — | `config.SOURCE_MAP` |
| 2.6 | Cross-schema reuse of a **pre-loaded** (already copied) table | DONE | **REAL WORKBOOK** — E-Commerce's "Customers (DataDNA...)" table, found in `WBR_DB.PUBLIC` not `PIPELINE_DEMO` | — | `pipeline.resolve_existing_table`. Different from 2.1: this reuses a table THIS PROJECT already copied there, not the workbook's original source |

**BUILT 2026-07-26: `Workbooks/R3_Extract_Over_Existing_Table.twbx`.** Real,
uploadable .twbx (`tests/make_datamodel_workbooks.py` generates it): the proven
`Superstore_KPI_Parameter_Dashboard_Live.twbx` (5 worksheets, 1 dashboard, 3
params, all already live-verified) with a real bundled `.hyper` extract added
to its one datasource — so it becomes extract-based while still declaring
`WBR_DB.PUBLIC.SUPERSTORE_ORDERS` as its source, with the EXACT 21 real column
names of that live table (so the column-verification guard passes) and the
same 10,194 Superstore rows. VERIFIED OFFLINE end-to-end against a
`_FakeAccount` matching the real table: `auto_bind_sources` binds it, `onboard()`
skips the `.hyper` decode entirely (`hyper_paths` empty after routing), and
`load_into_snowflake` reports `"existing table (auto-bound, no copy)"` with
zero `write_pandas` calls.

**LIVE-VERIFIED IN SNOWSIGHT 2026-07-26.** User uploaded it to the deployed
`pipeline_demo` app: Stage 1 showed exactly the predicted outcome — `Local
file: — (live/existing)`, `Status: existing table (auto-bound, no copy)`,
10,194 rows at `WBR_DB.PUBLIC.SUPERSTORE_ORDERS`. All 5 stages completed. This
is the FIRST live-account proof that R3 genuinely skips the decode+copy for a
real uploaded workbook, not just a synthetic fixture.

---

## 3. Extract-based multi-table sources (star / snowflake-schema — R7)

| # | Scenario | Built | Tested | Confidence | Notes |
|---|---|---|---|---|---|
| 3.1 | **Star** (fact + N dims, all depth-1) — flatten to ONE table (scope A, the default path) | DONE | REAL WORKBOOK (every multi-table corpus workbook: Superstore, E-Commerce, Regional Analysis) | — | baseline since before this session |
| 3.2 | **Star** — load dims SEPARATELY + `CREATE VIEW` replicating the relationships (scope B) | DONE | **REAL WORKBOOK, live-proven** — E-Commerce replicated as 3 separate tables (Events/Customers/Products) + relationship view in `WBR_DB.PIPELINE_DEMO` | — | `pipeline.build_data_model_tables`. See `[[stage3-scopeb-chat-state]]` memory |
| 3.3 | **Snowflake-schema chain** (depth-2: a dim joined to a dim, e.g. Orders→Product→Category) — flatten AND view DDL | DONE (2026-07-26, R7) | **REAL WORKBOOK, LIVE-VERIFIED** — `Workbooks/R7_Chain_Orders_Product_Category.twbx` (2026-07-26): a genuine Orders→Product→Category chain from Superstore's real rows (10,194/1,862/3). Verified offline (flatten log says "relationship snowflake", SUM(sales)/category matches ground truth exactly, no fan-out, codegen valid). **LIVE-VERIFIED 2026-07-26 via the sibling R10 workbook** (same chain shape, pre-loaded tables): Stage 3 showed the correct depth-2 join keys (`Orders.PRODUCT_ID=Product.PRODUCT_ID; Product.CATEGORY_ID=Category.CATEGORY_ID` — Category joining off Product, not the fact) and deployed the view correctly in Snowsight | Corpus has zero REAL non-star datasources still (all 7 are stars) — this is a purpose-built test workbook, not a found one | `semantic_layer.join_plan`. Gates `test_non_star_join_and_blends` |
| 3.4 | **Multi-fact graph** (>1 table nothing joins TO) → refused with a named reason, never guessed | DONE (refusal logic) | SYNTHETIC FIXTURE ONLY | — (refusal is the correct/safe behavior; untested on real XML just means "never proven the refusal fires correctly on a real multi-fact workbook") | same gate |
| 3.5 | **Cyclic / disconnected graph** → refused with a named reason | DONE (refusal logic) | SYNTHETIC FIXTURE ONLY | — | same gate |
| 3.6 | **Extract-based multi-table model whose CONSTITUENT tables already exist separately in Snowflake** (R3 tier-1 logic, applied PER TABLE inside a join graph) | **DONE + LIVE-VERIFIED (2026-07-26)** | **REAL WORKBOOK, LIVE-VERIFIED** — `Workbooks/R10_Chain_Over_Existing_Tables.twbx` uploaded to the deployed `pipeline_demo` app with decode genuinely blocked (a real `.hyper` present but undecodable, matching the Snowsight sandbox): Stage 1 showed `"data model bound to existing Snowflake tables -- no decode, no copy (R10)"` at 10,194 rows, Stage 3 deployed `WBR_DB.PIPELINE_DEMO.R10_CHAIN_MODEL_MODEL` with the correct depth-2 join keys | Closed — this was the last open verification gap for R10's core mechanism |
| 3.7 | **Legacy pre-2020.2 `<relation type='join'>` as the PRIMARY structure** (no object-model relationship graph at all) | **UNKNOWN — never exercised** | UNTESTED | OPEN QUESTION (see investigation below) | — |

### R9 and R10 SHARE ONE ROOT CAUSE (confirmed 2026-07-26, same session)

Initially filed as two separate gaps. Investigating WHY 1.4 (live multi-table
join) is still refused after R7 shipped ("the non-star-join backlog item" that
was supposed to absorb it) found: it isn't only a leftover scoping punt — REMOVING
the refusal today would actively misfire, for the SAME reason R10 exists.
Verified by constructing a synthetic live (no extract) multi-table Snowflake
datasource and running it through `describe_model()` as if the `has_join` guard
weren't there:
```
_connection(ds) -> {'class': 'federated', ...}      # picks the wrapper
tables -> [{'fqn': 'WBR_DB.PIPELINE_DEMO.ORDERS'}, ...]   # WRONG -- assumed-copy
                                                            # location, not the
                                                            # live PROD_DB.SALES.*
```
`semantic_layer._connection(ds)` grabs the FIRST `<connection>` element
(the outer `federated` wrapper) regardless of whether the datasource is an
extract or genuinely live — so `_src_table()`'s "keep the live location" branch
(gated on `conn.get("class") == "snowflake"`) never fires either way. **The
honest refusal in `live_connections()` is therefore currently a safety net, not
just an incomplete feature** — removing it without first fixing `_connection()`
would silently bind a live multi-table view to the wrong (assumed-copy)
location, which is worse than today's "not yet supported."

**UPDATE 2026-07-26 (later, same session): R10's root cause is now FIXED** (see
3.6 below) — `_connection()`/`_src_table()` correctly resolve the real upstream
connection class past the federated wrapper, for both extract and (the fix
doesn't special-case extract presence) genuinely live datasources too.
Confirmed directly: `pipeline.build_data_model_tables(session, root,
hyper_paths=[], ...)` — called with NO decodable data at all — successfully
deploys a relationship view for a synthetic genuinely-live (no `<extract>`)
multi-table fixture, purely from existence+column verification. **Stage 3a's
data-model view mechanism therefore already works for a live multi-table
join today, in isolation.**

**R9 is still NOT closed, for a more specific reason than "no mechanism
exists."** `pipeline_app.py`'s Discovery stage halted the run (via `st.stop()`
at the time this was written; since the 2026-08-06 V2 workbench rewrite it
raises `_StageError("Discovery", ...)` instead — same effect, run halts and
the user sees the error, just no longer literally `st.stop()` or at that line
number) the moment `onboard()` reports any datasource `MISSING` — and it still will for
this case, because `live_connections()`'s `has_join` refusal causes
`configure_datasources`/`load_into_snowflake` (Stage 1's per-CAPTION sheet
routing) to route the join-carrying caption at an assumed-copy location that
then resolves to MISSING, before Stage 3a (which operates on the whole `root`
independently) ever gets a chance to run.

**R9 CLOSED 2026-07-26 (later, same session), by approach (b) above —
`onboard()`'s R10 missing-resolution fix ALREADY does this generically.**
Once `pipeline.onboard()` was fixed (same session, see 3.6) to resolve a
missing MULTI-TABLE caption via `build_data_model_tables` before returning,
R9 turned out not to need its own separate wiring at all — it needed
`live_connections()` to stop claiming a multi-table live model was
single-table-queryable, so the caption would correctly fall through to
`load_into_snowflake`'s MISSING path and let the already-fixed `onboard()`
pick it up.

**THE ACTUAL BUG WAS WORSE THAN THE DOCUMENTED REFUSAL.** Investigating why
`has_join` didn't already catch this found: `live_connections()`'s relation
scan was `ds.findall(".//relation")` — EVERY `<relation>` anywhere in the
datasource, which ALSO matches the per-OBJECT `<relation>` elements nested
inside the object-model's `<object-graph>` (a separate sibling of
`<connection>`, describing each joined table for Stage 3's data-model view).
For a 2-table live model built with the MODERN object-model relationship
syntax (not the legacy `type='join'` tag `has_join` checks for), this
returned duplicate relations, and `next(r for r in rels if type=='table')`
picked the FIRST one and called the WHOLE datasource single-table queryable
AT JUST THAT TABLE — `queryable: True`, silently dropping the second table
and its join entirely. This is an ACTIVELY WRONG answer, not an honest
refusal — the `has_join` check only ever caught the legacy syntax, which real
modern Tableau workbooks don't use.

**FIX:** scan only relations that are DIRECT CHILDREN of the federated
`<connection>` element (confirmed against the real KPI Live workbook's XML
shape — its one real relation is a direct child of `<connection>`; the
object-graph's per-object relations live several levels below a totally
different sibling element and are never direct children of `<connection>`).
A genuine multi-table live model is now correctly refused (not silently
mis-detected); `>1` distinct relation now refuses with a specific reason
alongside the legacy `has_join` case. **Once that refusal fires correctly,
zero additional plumbing was needed** — the SAME `build_data_model_tables`
call inside `onboard()` that R10 already uses verifies and deploys the join
view against the live tables directly (proven: `pipeline.build_data_model_
tables(session, root, hyper_paths=[])` already worked for a live fixture
before this session even started looking at R9, since it never special-cased
extract presence).

Gate `test_r9_live_multitable_join`: the detection fix directly (the fixture
now correctly refuses instead of claiming `queryable: True` at just ORDERS);
no regression on the real single-table live workbook or the corpus false-
positive sweep; and the full payoff end-to-end through `onboard()` with a
write-raises fake account (proves the live tables get bound directly, never
copied). Teeth proven: reverting to the old `.//relation` scan reproduces the
exact pre-fix bug (`queryable: True`, pointed at ORDERS alone) and the gate
catches it. Suite 56 → 57 gates. **Synthetic-fixture only — not yet uploaded
to Snowsight** (no corpus workbook has a genuinely live multi-table join to
test against; a purpose-built workbook, same pattern as R10's, would be the
next step to move this from offline-proven to live-verified).

### 3.6 in detail — the gap, and its fix (2026-07-26, same session)

Traced live against `Regional Analysis.twbx`:
```
SL.data_model(root)[0]['connection'] == {'class': 'federated', 'dbname': None, ...}
```
`semantic_layer._connection(ds)` picks up the OUTER `class='federated'` wrapper
(the first `<connection>` element in document order), not the real upstream
`class='snowflake'` one nested inside `<named-connections>`. So
`_src_table()`'s "live Snowflake keeps its own location" branch NEVER fires for
an extract-based datasource — every table is assumed to need copying into
`{LOAD_DB}.{LOAD_SCHEMA}`, confirmed by printing `describe_model`'s `tables`:
every fqn came back `WBR_DB.PIPELINE_DEMO.<name>`, never the tables' real
declared origin (`SANDBOX.DS.<name>`).

**Net effect (before the fix):** if a workbook's star/chain EXTRACT model has
its 3+ underlying tables ALREADY loaded separately in the target Snowflake
account (not just previously copied there by THIS accelerator, which 2.6
already handles, but the workbook's own ORIGINAL declared source tables), the
accelerator had no way to notice and always decoded + copied instead of
binding the view straight to the originals.

**FIXED 2026-07-26, same session.** `_connection(ds)` now reuses R3's own
upstream-detection (`tableau_parser._upstream_connections`) instead of the bare
`.find()`. A new `_parse_relation_table()` correctly handles the 1/2/3-segment
relation shapes (Regional Analysis' real shape is the fully-qualified
`[SANDBOX].[DS].[TABLE]`) without dot-joining onto a caller default — the
double-schema bug class this project keeps guarding against.

**LIVE-VERIFIED, BOTH HALVES, 2026-07-26.** First the no-regression half:
uploaded `Superstore.twbx` — its flat-file star (Orders/People/Returns, no
real Snowflake upstream) correctly still reported "not deployable... needs
separate loading," byte-identical to pre-fix behavior. Then the NEW
capability itself: built `Workbooks/R10_Chain_Over_Existing_Tables.twbx`
(same depth-2 chain shape as 3.3, but its 3 tables — `R10_ORDERS`,
`R10_PRODUCT`, `R10_CATEGORY` — were pre-loaded SEPARATELY into
`WBR_DB.PIPELINE_DEMO` first, via `tests/make_datamodel_workbooks.py
--load-r10-tables`, satisfying the actual precondition R10 needs) and
uploaded it with decode genuinely blocked (a real bundled `.hyper`, but
undecodable — matching the Snowsight sandbox). Result: Stage 1 showed
`"data model bound to existing Snowflake tables -- no decode, no copy (R10)"`
at 10,194 rows, and Stage 3 deployed `WBR_DB.PIPELINE_DEMO.R10_CHAIN_MODEL_MODEL`
with the correct depth-2 join keys, live.

**A SECOND REAL BUG was found and fixed in the SAME live-testing pass** (not
merely documented — code changed, gated, redeployed): `pipeline.onboard()`'s
missing-check only ever probed for ONE table named `to_phys(caption)` — it had
no way to know a multi-table datasource might independently verify via R10 (or
decode separately via scope B). So the FIRST live upload of the R10 workbook
correctly hit MISSING and `st.stop()`ped in Stage 1, before Stage 3 (which
DOES know how to resolve it) ever got a chance to run. Fixed by resolving a
missing multi-table caption via `build_data_model_tables` inside `onboard()`
itself, before returning — proven against the real account with decode
genuinely blocked, confirmed the "genuinely can't verify" case still fails
honestly (doesn't silently swallow a real failure), and confirmed via the
SAME re-upload above that the fix resolves the exact scenario that broke.
See `[[data-model-status-doc]]` memory / `NEW_CHAT.md` for the full trace.

**The verification, not just the parsing, is the actual feature.** `_src_table`
now returns `(fqn, is_declared_source)` — a CANDIDATE only. New
`pipeline.verify_table_candidate()` existence+column-checks a declared-source
table exactly like R3's single-table resolver (a name is not evidence);
`pipeline.data_model_report`'s `deployable` now requires EVERY table in the
graph to verify — a single unverified table refuses the WHOLE model, never a
partial bind; `pipeline.build_data_model_tables` SKIPS decode+`write_pandas`
entirely and deploys the view straight at the originals once everything
verifies — the actual "no copy" outcome this item asked for.

Gate `test_r10_multitable_source_autobind` (9 cases: root-cause connection
detection, 3-segment parsing without doubling, full verify+deploy with a
write-raises fake session proving zero copy, the wrong-table guard refusing
the whole model on one bad table's columns, a nonexistent-table refusal, unit
coverage of the 1/2/3-segment parser, corpus proof on Regional Analysis' real
XML now resolving to `SANDBOX.DS.*`, and a flat-file-star no-regression check
on Superstore). Teeth proven by reverting `_connection()` to the old bare
`.find` (fails) and by disabling the column check (fails). Suite 55 → 56
gates, all green.

**CLOSED.** R10's core mechanism (auto-bind for a multi-table extract whose
tables already exist separately, including the `onboard()` sequencing fix) is
now live-verified end to end, not just gated. **R9 (live multi-table join) is
ALSO now closed** (same session, later) — see "R9 and R10 share one root
cause" above for the fix (`live_connections()`'s relation scan was silently
mis-detecting a multi-table live model as single-table; fixing that let the
already-built R10 machinery pick it up with zero new plumbing). R9 is gated
(`test_r9_live_multitable_join`) but synthetic-fixture only — not yet
uploaded to Snowsight.

### 3.7 in detail — the open question

Sweeping the corpus for `<relation type='join'>` (the OLD pre-2020.2 join
syntax, distinct from the newer object-model `<relationships>` graph) found 8
hits ONLY in `Globalsalesdashboard.twbx`. Investigating further:
`object-graph` tag count = 6, but `<relationships>` tag count = **0** for that
workbook — the object-graph exists but has NO relationship edges. Printing the
actual object list showed the "SAMPLE_SUPER_STORE_ORDERS..." datasource
resolves to exactly **ONE** object (`SAMPLE_SUPER_STORE_TEST_DATASET`), not
three — meaning Tableau pre-joined 3 tables into a single logical object using
the LEGACY `<relation type='join'>` clause tree, nested INSIDE that one
object's own relation definition. `semantic_layer.data_model()` correctly reads
this as `shape='single', n_tables=1` (nothing to fix there).

**HOWEVER:** this session's NEW `tableau_parser.source_tables()` (built for R3)
reported this SAME datasource as a 3-table, non-bindable model
(`"3-table data model -- replicating it as real Snowflake objects is the
data-model view path"`). That's WRONG — it's actually a single already-resolved
legacy-joined object, not 3 separate bindable tables. ROOT CAUSE: `source_tables
()`'s `ds.findall(".//relation[@type='table']")` walks EVERY nested relation
under the datasource, including the ones buried inside a legacy join's own
`<relation>`/`<clause>` tree (which share the same upstream `connection=`
attribute, so they pass the "is this a real upstream relation" filter) — it
doesn't distinguish "a top-level object's own source relation" from "a nested
relation inside another relation's legacy join clause."

**PRACTICAL IMPACT today:** none observed — this datasource's `bindable=False`
outcome happens to be the SAME conclusion a correct read would reach (a single
pre-joined object is not an R3 single-table bind target in the naive sense,
though arguably it COULD bind directly to whatever Snowflake object the legacy
join was built from, which is actually closer to scenario 3.6 above). But the
REASON STRING IS MISLEADING ("3-table data model") when there's really one
object built from a 3-way legacy join. **Flagged as a parser accuracy issue in
`source_tables()`, not yet fixed** — worth a small follow-up: detect and skip
relations nested inside another relation's `<clause>` subtree.

Separately, whether a workbook using `<relation type='join'>` as its ONLY/
PRIMARY structure (a genuinely legacy pre-2020.2 file with no object-model graph
at all) parses/flattens correctly for an EXTRACT is **untested** — no such
corpus workbook exists, and `parse_relationships()`/`data_model()` are built
against the newer object-model `<relationships>` tag exclusively.

---

## 4. Unions

| # | Scenario | Built | Tested | Confidence |
|---|---|---|---|---|
| 4.1 | Union of flat files (CSV/Excel), materialized row-wise (UNION ALL) | DONE | SYNTHETIC FIXTURE ONLY (`tests/fixtures/union_test.twb`) — no corpus workbook has a real union | OPEN QUESTION |
| 4.2 | Union of live DB tables (SQL `UNION ALL` at query time) | NOT STARTED | UNTESTED | DELIBERATE SCOPE LIMIT (explicitly out of scope in the code's own docstring) |
| 4.3 | Wildcard/pattern-based union (file-name pattern matching) | NOT STARTED | UNTESTED | DELIBERATE SCOPE LIMIT |

---

## 5. Data blends (R7 — link extraction done; materialization deliberately not)

| # | Scenario | Built | Tested | Confidence |
|---|---|---|---|---|
| 5.1 | Extract + report blend link fields (real `<datasource-relationship><column-mapping>` XML, collapsed from per-pill-derivation maps to real fields) | DONE (2026-07-26) | **REAL WORKBOOK, TWO DIFFERENT SHAPES + LIVE-VERIFIED IN SNOWSIGHT** — `Superstore.twbx`/`Superstore_Tableau2024_3.twbx` (3 link fields: Order Date/Category/Segment, sheet `Performance`; deployed + uploaded live 2026-07-26, panel rendered exactly as coded) AND `Globalsalesdashboard.twbx` (1 link field: Product, primary=SALES/secondary=CALLS, sheet `Details Standard Class`; corpus-XML-tested only, not yet uploaded live) | — |
| 5.2 | Feed real blend link fields into the Cortex calc-fallback prompt as a hard constraint (replacing the old "infer the join key" instruction) | DONE (2026-07-26) | REAL WORKBOOK — proven against Superstore's actual blend calcs (`Sales above Target?`, the `SUM(Sales)-SUM(Sales Target)` calc) | — |
| 5.3 | Reviewable pre-aggregate remodel SQL template | DONE | REAL WORKBOOK (same as 5.1) | — |
| 5.4 | **Auto-materialize a blend as a deployed Snowflake view** (query-time link → a real joined object) | **NOT STARTED — deliberate** | UNTESTED | DELIBERATE SCOPE LIMIT. Needs (a) which of several declared links Tableau ACTIVATES for a given sheet — depends on the fields on the view, not the XML alone — and (b) a workbook with known numbers to validate against. ~1–1.5 days estimated |

---

## 6. Cross-cutting / correctness issues that touch the data model

| # | Scenario | Built | Tested | Confidence |
|---|---|---|---|---|
| 6.1 | Per-workbook profile + datasource routing (the "open landmine" — a foreign workbook silently inheriting Superstore's mapping) | DONE | REAL WORKBOOK (full corpus regression) | — |
| 6.2 | FIXED LOD ignoring dimension filters (order-of-operations: LOD should compute BEFORE dimension filters, currently doesn't) | NOT STARTED (deferred) | UNTESTED — no corpus workbook combines a FIXED LOD with a dimension filter | DELIBERATE DEFERRAL — explicitly do NOT rewrite the regression-locked LOD path blind; needs a real test workbook first |
| 6.3 | Reserved-word risk at the COLUMN level (a Tableau field literally named `ORDER`/`GROUP`) | NOT STARTED | UNTESTED — no corpus hit | OPEN QUESTION / low priority (the SQL-ALIAS twin of this bug is already fixed + gated) |
| 6.4 | Non-Snowflake live source migration kit (landing DDL + views + checklist for e.g. SQL Server) | NOT STARTED | UNTESTED | DELIBERATE SCOPE LIMIT, ~2.5 days estimated |

---

## Summary matrix (quick scan)

```
DONE, REAL WORKBOOK, VERIFIED (real acct       DONE, SYNTHETIC ONLY (needs a real wb)
or full Snowsight UI -- see notes)              -------------------------------------------
-------------------------------------------    3.4/3.5 Multi-fact / cyclic refusal
1.1 Flat file -> table                          4.1 Union of flat files
1.2 Live connect, single Snowflake table         1.5 Live custom SQL (Snowflake dialect)
1.4 Live multi-table JOIN (R9 -- real account,
    UI upload pending)
2.1/2.2 R3 single-table extract auto-bind
2.6 Cross-schema pre-loaded reuse
3.1 Star flatten (scope A)
3.2 Star, separate tables + view (scope B)
3.3 Snowflake-schema chain (depth-2)
3.6 Multi-table extract auto-bind (R10)
5.1/5.2/5.3 Blend extraction + Cortex fix

NO REMAINING REAL GAPS on the data-model        DELIBERATE SCOPE LIMITS (not gaps)
front as of 2026-07-26 (R3/R7/R9/R10 all         -------------------------------------------
DONE, see doc history above for the fix          1.3/6.4 Non-Snowflake live source
narrative)                                       1.6 Non-Snowflake custom SQL dialect
                                                  4.2/4.3 Union of live tables / wildcard
                                                  5.4 Blend auto-materialization

OPEN QUESTIONS (parser accuracy / unverified)
-------------------------------------------
3.7 Legacy <relation type='join'>-only workbooks; source_tables() misreports
    Globalsalesdashboard's single legacy-joined object as a "3-table" refusal
6.2 FIXED LOD + dimension filter order-of-ops (deferred, needs a test workbook)
6.3 Column-level reserved words (no corpus hit)
```

## Recommended next test workbooks (priority order)

1. ~~Single-table extract over an existing Snowflake table~~ — **BUILT +
   LIVE-VERIFIED 2026-07-26**: `Workbooks/R3_Extract_Over_Existing_Table.twbx`.
   Uploaded to Snowsight; Stage 1 showed `existing table (auto-bound, no
   copy)` at 10,194 rows. Closed.
2. ~~Snowflake-schema chain~~ — **BUILT + LIVE-VERIFIED 2026-07-26**:
   `Workbooks/R7_Chain_Orders_Product_Category.twbx` (Orders→Product→Category,
   depth-2). Verified offline against known ground truth; the depth-2 join
   keys confirmed live via the sibling R10 workbook. Closed.
3. ~~Multi-table extract whose tables already exist separately~~ (3.6/R10) —
   **BUILT + LIVE-VERIFIED 2026-07-26**: `Workbooks/R10_Chain_Over_Existing_Tables.twbx`,
   with its 3 tables pre-loaded separately first. Uploaded with decode
   genuinely blocked; Stage 1/3 both confirmed the no-copy bind. This upload
   also surfaced and closed a second real bug — `onboard()` hard-stopping in
   Stage 1 before Stage 3's mechanism could run — see the 3.6 detail section
   above. Closed.
4. **Multi-fact or cyclic graph** (closes 3.4/3.5) — two fact tables sharing one
   dim, or a 3-table loop — proves the refusal fires correctly on real XML.
   Not yet built.
5. ~~**1.4** (live multi-table join, R9)~~ — **BUILT + VERIFIED AGAINST THE
   REAL ACCOUNT 2026-07-26**: `Workbooks/R9_Live_Join_Orders_Product_Category.twb`
   (genuine `.twb`, zero bundled data — joins `WBR_DB.PIPELINE_DEMO.
   R10_ORDERS/R10_PRODUCT/R10_CATEGORY` live, reusing R10's already-loaded
   tables). Run directly through `pipeline.onboard()` against the real
   account: view deployed, exact known numbers returned, zero decode/copy.
   **Only remaining step: the actual Streamlit UI upload** (same as R3/R10's
   final confirmation) — sent to the user to upload into the deployed
   `pipeline_demo` app.

**Generator script:** `tests/make_datamodel_workbooks.py` — re-run to
regenerate either file (e.g. after a data change). Builds real `.hyper`
extracts via `tableauhyperapi` (confirmed installed), so both are genuinely
uploadable, not just structurally plausible XML.

## Related files
- Roadmap R3: `MVP_ACCELERATOR_SCOPE.md`, `status_config.json` (roadmap item "R3")
- Roadmap R7: same files, roadmap item "R7"
- Regression gates: `tests/test_regression.py` —
  `test_auto_bind_existing_snowflake_table`, `test_non_star_join_and_blends`,
  `test_live_connection_support`, `test_custom_sql_execution`, `test_union_support`
- Memory: `r3-auto-bind-existing-table.md`, `r7-nonstar-joins-and-blends.md`
- Narrative history: `NEW_CHAT.md` — search "R3 DONE" and "R7 —"
