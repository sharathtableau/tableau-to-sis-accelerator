# Tableau-to-SiS V3 enterprise generalization handoff

## Current handover clarification — September 23, 2026

Read [generalization scope and next-session contract](V3_GENERALIZATION_HANDOVER.md)
before older checkpoints below. New repairs are structural within bounded contracts;
they are not proof of all-workbook support. Scoped V3 bypasses filename profiles,
but inherited caption/physical-name fallbacks still require a complete reachable-path
audit. No new converter changes, test execution or deployment in this documentation update.

Current recorded release evidence: 132 V3 tests; inherited regression PASS;
394 occurrences across 25 files: 58 UNVERIFIED / 330 UNSUPPORTED / 6 INVALID,
zero structural issues. Seven development charts render; 21 newly rendered groups
match independent Snowflake aggregates. Complete migration is still blocked by
crosstab ordering. Tableau parity, unseen-workbook acceptance and partner readiness
remain unverified. Both V3 applications are already deployed; earlier counts,
no-deployment statements and superseded next-task instructions below are historical.

Next: resolve partial manual header/remainder and row tie/null ordering through a
generic validated contract; audit reachable legacy fallbacks; complete the workbook
blocker matrix and frozen-build, download-enabled holdout evaluation. Cortex may
propose validated alternatives/calculations, never invent missing semantics.

Latest checkpoint — September 23: bounded visual repairs deployed to both existing
V3 applications. See [visual contracts and evidence](V3_VISUAL_PLANS.md).

Generic repairs cover calculated bar labels and group-key ATTR tooltips, categorical
scatter with aggregate axes/size, and calendar-part/full-date dual Bar/Line axes.
The workbench browser rendered seven charts for the reported development source,
up from four. A rebuilt standalone candidate uses the same worksheet/datasource XML
as the earlier Cloud download and updates the existing preview app identity.
Final standalone browser verification: seven charts, no rendering exceptions,
and one visible isolated table refusal (11 findings, 1 blocking).

132 V3 tests pass; inherited regression passes before and after converter changes.
Corpus: 394 sheet occurrences/25 files, 58 UNVERIFIED, 330 UNSUPPORTED, 6 INVALID;
zero structural issues. Numeric fixtures exercise renamed fields, multiple source
shapes and ordering. All 21 newly rendered development groups match independent
Snowflake aggregates. Existing smoke checks pass; all 148 parent-source hashes match.
Both deployments retain SYSTEM$WAREHOUSE_RUNTIME, TABLEAU_ACCESS_INTEGRATION and
tableau_pat (DESCRIBE evidence saved under visual_plan_evidence).

Crosstabs with complete explicit header/row ordering are implemented and numerically
tested, but the reported Product/Customer tables remain refused: nine selected
measures have only seven manually ordered headers. Row ties/nulls also refuse.
This is still a blocked complete migration, not enterprise or partner acceptance.
Tableau parity, extract snapshot parity, dashboard interaction/layout fidelity and
unfamiliar-workbook acceptance remain unverified. Existing holdouts are permission
blocked before conversion. Cortex has not been allowed to invent ordering semantics.

Next concrete task: validated partial-manual-header/remainder and row tie/null
ordering, using independent Tableau evidence or an explicitly governed alternative
presentation contract. Keep unsupported occurrences isolated; no workbook dispatch
and no wholesale enablement of legacy tables. Then complete the whole-workbook
matrix and a download-enabled frozen-build holdout evaluation.


Latest checkpoint — September 23, bounded visual plans (local verification complete;
live deployment verification in progress). See V3_VISUAL_PLANS.md.

Implemented generic occurrence contracts for calculated bar labels/group-key ATTR
tooltips, scatter aggregate axes with categorical grouping/color/text and size,
two overlaid aggregate Bar/Line axes on a validated calendar axis, and explicit-order
Measure Names/Values crosstabs. No production workbook/sheet/source-name dispatch.

Evidence: 130 V3 tests pass. Inherited regression passes before and after converter
edits (artifact-manifest omission found and fixed). Corpus: 394 occurrences/25 files,
58 UNVERIFIED, 330 UNSUPPORTED, 6 INVALID; zero structural issues. Render smoke:
Superstore 3 mapped/17 refused; ecom 0 mapped/50 refused. 148 parent hashes match.
Numeric fixtures cover renamed fields, two datasource shapes, horizontal/vertical
labels, aggregate scatter groups, month components across years versus full dates,
and crosstab aggregation/header ordering. Three new development visuals executed
against Snowflake through a diagnostic CLI adapter; browser deployment is pending.

Remaining blocker: reported crosstabs select nine measures but explicitly manually
order only seven. This remains an isolated refusal, not guessed locale/order.
Tied/null crosstab row order also refuses. Full workbook migration, Tableau parity,
extract snapshot parity and unfamiliar-workbook acceptance are NOT established.
Next concrete task: verify deployed visual changes; implement a validated partial
manual-header/remainder and tie-order contract with independent Tableau evidence,
or an explicitly disclosed and governed presentation alternative. Do not enable
the legacy table renderer to bypass these requirements.


Latest checkpoint — September 23: automatic source binding implemented and deployed.
See [source binding and end-to-end evidence](V3_SOURCE_BINDING.md) and the
`enterprise` section of `status_config.json`.

V3 reads datasource IDs, named Snowflake connections and physical relations;
verifies account, object read access and exact physical columns/types; binds one
unambiguous table automatically. Explicit manual bindings validate too. Available
previews open automatically. No workbook/table names were added to production.

Verified live: reported Cloud workbook fetched, auto-bound and rendered four charts.
All 17 displayed values and ordering match independent Snowflake aggregates. The
exact downloaded standalone candidate was deployed and browser-verified with the
same four charts and isolated refusals. Both apps retain TABLEAU_ACCESS_INTEGRATION,
tableau_pat and SYSTEM$WAREHOUSE_RUNTIME. 121 V3 tests pass; inherited regression
passes before/after; corpus and render smoke unchanged; 148 parent hashes match.

Limits: source resolution is bounded to exact single-table Snowflake metadata;
live upstream does not prove extract-snapshot/Tableau parity. Other sheets remain
explicitly refused. Whole-workbook acceptance and enterprise readiness are NOT
established. Two unreferenced Cloud hold-out candidates both returned Tableau
403019 download forbidden before conversion. Evidence records external refusal,
not a passed or failed converter evaluation.

Next concrete tasks: download-enabled unfamiliar Cloud evaluation; generic
Measure Names/Values crosstabs, scatter encodings, dual-axis plans and calculated
labels, prioritized from whole-workbook blockers. Source candidate disambiguation
for incomplete metadata and full removal of legacy reachable name assumptions
remain open. Do not add workbook-specific production patches.


Latest checkpoint — September 23, enterprise architecture correction (local only).
See [architecture audit](V3_ENTERPRISE_ARCHITECTURE_AUDIT.md) and the authoritative
`enterprise` section of `status_config.json`. Earlier inherited "done" counts are
legacy history, not enterprise genericity. No V4 was created.

Fixed: scoped engine configuration bypasses filename-selected curated profiles;
scoped parameter/findings UI no longer uses authored/ filenames as a mode switch.
Regression coverage changes filenames, datasource aliases and field/sheet captions
across bar/KPI shapes and executes numeric results (21 and 101). The pre-fix test
failed on filename routing; the corrected implementation passes.

Verification: 114 V3 tests pass; full inherited regression passes before/after;
corpus remains 25 files,394 occurrences,55 UNVERIFIED,333 UNSUPPORTED,6 INVALID,
zero structural issues. Render smoke retains 3 mapped/17 refused Superstore and
0 mapped/50 refused ecom occurrences. All 148 parent hashes remain unchanged.
No new deployment, live Snowflake execution, Tableau parity or held-out acceptance
was performed for this increment.

Remaining: scoped profile isolation is not complete removal of all hardcoding.
Legacy physical aliases/map defaults need review; auto source binding is absent
from V3 despite original generic resolver code; broad visual/calculation support
and end-to-end evidence remain incomplete. Next concrete task: safe exact authored
Snowflake source binding (account, object and schema validation), followed by a
whole-workbook blocker matrix and frozen-code unfamiliar Cloud holdout.


Latest checkpoint (September 23): Tableau Cloud site browsing restored and deployed.
See [Cloud intake fix](V3_CLOUD_INTAKE_FIX.md). V3 previously sent a site URL to the
direct workbook parser before authentication. It now reuses the original site list
and REST-ID download flow with the same PAT credentials and scoped V3 intake.
Live Snowflake connection to the reported b360bi URL lists 173 workbooks; selected
R1 Test Upload - Superstore downloads and reaches assessment. Workbook acceptance
is BLOCKED by unsupported semantics, four Hyper decode failures and four missing
Snowflake data bindings. Connection/download success is not migration acceptance.
112 V3 tests pass, inherited regression passes before/after, corpus/smoke unchanged,
and 148 parent hashes match. Runtime, EAI and tableau_pat bindings verified.
Latest archive: v3_cloud_candidate.zip; previous archives are superseded.
Next: compare the original and V3 data-onboarding path for this same workbook,
record whole-workbook blockers, then implement generic fixes from that evidence.
Partner readiness and Tableau parity remain NOT ESTABLISHED.

Latest checkpoint (September 23): V3 deployment authorized and completed.
App: [WBR_DB.TABLEAU_ACCELERATOR_V3.TABLEAU_TO_SIS_V3_CANDIDATE](https://app.snowflake.com/WB19670/c2gpartners/#/streamlit-apps/WBR_DB.TABLEAU_ACCELERATOR_V3.TABLEAU_TO_SIS_V3_CANDIDATE).
Runtime is verified as SYSTEM$WAREHOUSE_RUNTIME on POWERHOUSE; DESCRIBE confirms
TABLEAU_ACCESS_INTEGRATION and tableau_pat → WBR_DB.PUBLIC.TABLEAU_PAT_SECRET.
The server initially selected container runtime; an explicit ALTER corrected
only this newly created V3 app. Live startup also rejected python=3.11 as a
package; it was removed because the runtime selects Python. Future manifests
pin the runtime and deployment
must verify it after upload. Existing accelerator apps were not modified.
109 V3 tests pass after the manifest fix. Browser startup: PASS: warehouse-runtime V3 heading, candidate caption and workbook-source controls visible in Snowsight.
Deployment is not whole-workbook acceptance: Cloud pulls, data binding/query,
visual/interaction checks and Tableau parity remain NOT_VERIFIED in this run.
Deployed build: 41a92df9169382b0a46a6b94382a8bf0249dc01123d686153944b6ebf65bc869. The new archive is
v3_snowflake_deployed_candidate.zip; the previous archive is superseded.
See V3_DEPLOYMENT_EVIDENCE.json and deployment_runtime_final.json.
Next: verify actual Cloud intake and representative complete workbook scopes
against the original using identical versions/data bindings; then prioritize
generic fixes from the blocker matrix. Partner readiness is NOT ESTABLISHED.
Earlier no-deployment/no-authorization checkpoints are historical.

Latest product integration checkpoint (September 23): [Snowflake candidate](V3_SNOWFLAKE_CANDIDATE.md).
The isolated V3 workbench now has a Snowflake entry point, complete candidate
package and standalone workbook export using scoped IR and explicit bindings.
109 V3 tests pass; full inherited regression passes before/after; CLI manifest
and archive hashes validate. Original apps remain untouched. No deployment.
Read-only Snowflake connection attempts did not complete; environment package,
privilege and shared-object checks remain NOT_VERIFIED. Local tests are not
target-runtime acceptance. Corpus/smoke unchanged; 148 parent hashes match.
Next: resolve connection/preflight and obtain explicit authorization for the
reviewed isolated candidate, then deploy, verify EAI/secret bindings and runtime,
and build the original-versus-V3 Cloud blocker matrix. Earlier plans to compare
deployed V3 immediately or continue isolated features omit this prerequisite.
Partner readiness remains NOT ESTABLISHED.

Latest checkpoint (September 23): [shared authored categorical filters](V3_CATEGORICAL_FILTERS.md).
Text tables and validated Cortex substitutions now share typed same-source
physical member filters: OR within a selection, AND across filters, explicit
null/empty behavior, applied before aggregation/order/limit. Unknown semantics
refuse before SQL; worksheet/caption/filename profiles are not used.
103 V3 tests pass, including numeric multi-source/filter/order proofs and an
actual Streamlit render. Inherited regression passes before/after. Corpus stays
55 UNVERIFIED / 333 UNSUPPORTED / 6 INVALID (394 sheets, 25 files, zero structural
issues); render smoke stays 3/17 and 0/50 mapped/refused. All 148 parent hashes
match. No new live Cloud/Snowflake or Tableau parity evidence; no deployment.
This completes authored include selections only. Interactive controls, other
filter kinds, temporal/pane adoption and whole-workbook acceptance remain open.
Next: typed runtime selections and V3 controls with explicit source/sheet targets,
validated member domains and cross-sheet isolation. Partner readiness remains
NOT ESTABLISHED. Earlier checkpoints below are historical.

Latest checkpoint (September 23): [validated Cortex substitutions](V3_CORTEX_ADAPTATION.md).
V3 Cloud/upload intake now asks Snowflake Cortex AI_COMPLETE for eligible visual
replacements and bounded row-calculation expression plans. Independent source
validation controls every accepted plan; model SQL/code is never executed.
Original unsupported status is retained; validated substitutions assess PARTIAL,
with visible layout/order changes and parity unverified. Existing Snowflake
tables/views require explicit datasource bindings in the V3 preview.
95 V3 tests pass; inherited regression passes before/after. Two live Cortex
requests passed; the resulting calculation returned A=18, B=-3 in a read-only
synthetic Snowflake query. This is not a deployed SiS/real-workbook parity test.
Corpus remains 55 UNVERIFIED / 333 UNSUPPORTED / 6 INVALID across 394 sheets,
25 files, zero structural issues; no AI corpus acceptance is claimed.
All 148 parent hashes remain unchanged. No deployment; partner readiness remains
NOT ESTABLISHED. Next: shared typed categorical filters across validated plans,
then combined-construct Cloud/target-runtime acceptance. Earlier sections are
historical and cannot override this checkpoint's evidence boundaries.

Latest checkpoint (September 23): [shared Cloud intake and aggregate sorting](V3_MEASURE_ORDER_AND_CLOUD.md).
The [enterprise execution plan](V3_ENTERPRISE_EXECUTION_PLAN.md) now governs the
implementation sequence and evidence gates. V3 Cloud pulls and uploads share
validated content-hash/scoped intake. The existing legacy Cloud pipeline has
NOT been promoted to V3. Text-table plans now support bounded explicit aggregate
ordering with whole-domain tie/null refusal and sort-before-limit semantics.
84 V3 tests pass; full inherited regression passes before/after. Cloud transport
is mocked in tests; actual local chart rendering is tested. Corpus unchanged:
55 UNVERIFIED / 333 UNSUPPORTED / 6 INVALID, zero structural issues. All 148
parent hashes match. No live Cloud, Tableau parity, Snowflake execution,
complete migration or partner readiness claim; no deployment.
Next: shared typed physical categorical filters, then combined-construct and
whole-Cloud-workbook acceptance. Earlier checkpoints below are historical.

Current checkpoint (September 23): [bounded text tables](V3_TEXT_TABLES.md).
Physical dimension rows and aggregate Text occurrences now use a dedicated
occurrence-to-query plan with stable output IDs, explicit manual row ordering,
sorting before a visible 200-row display cap, and whole-domain ordering checks.
76 V3 tests pass, including ten table tests and real Streamlit dataframe execution.
The inherited regression passed before and after converter edits; final evidence
is in `table_final_regression.log` and `table_final_v3_tests.log`.
Corpus coverage is unchanged: 55 UNVERIFIED / 333 UNSUPPORTED / 6 INVALID across
394 occurrences and 25 files, zero structural issues. No newly accepted corpus
sheet or complete migration is claimed. All 148 parent-source hashes are unchanged.
No deployment, Tableau parity or Snowflake execution; partner readiness remains
NOT ESTABLISHED. Next: explicit measure-based row sorting with validated grain,
field ownership and tie handling; see the milestone for scope and remaining limits.
Earlier checkpoints below are historical.

Current implementation: [scoped date bars](V3_DATE_BARS.md), reusing validated
calendar grain and ordinal/temporal axis plans. 66 V3 tests pass. Corpus now has
55 UNVERIFIED / 333 UNSUPPORTED / 6 INVALID occurrences, with zero structural
issues. Its one newly accepted sheet cannot render without its missing source
binding; no migration/parity gain is claimed. See `V3_STATUS.md` for final
regression evidence. Next implement the bounded table/text query plan in the
milestone; typed filters/calculations/relationships remain open. No deployment.
Earlier checkpoints below are historical.

Latest converter increment: [discrete calendar axes](V3_DISCRETE_DATES.md). Scoped plans distinguish extracted date parts from truncated full dates and select ordinal versus temporal axes. All 63 V3 tests pass. Corpus counts are now 54 UNVERIFIED / 334 UNSUPPORTED / 6 INVALID, with zero structural errors. Two additional accepted sheets lack their source data and remain NOT_VERIFIED for actual rendering. This is not a workbook-migration or parity claim. Read the milestone and `V3_GAP_LEDGER.md`; earlier statements that all discrete dates refuse are historical. Weeks/fiscal/null-header/custom-sort contracts and other listed gaps remain open. No deployment.

Latest converter fix: [continuous temporal grain](V3_TEMPORAL_GRAIN.md). Continuous-year/quarter tokens no longer fall back to monthly SQL. A bounded single-pane continuous-date line/area adapter is implemented with numeric tests across year boundaries and separate datasources. Read [V3_GAP_LEDGER.md](V3_GAP_LEDGER.md) for fixed subsets and open gaps; update this handover after each actual converter fix. Do not substitute another planning/diagnostics increment for conversion work. Old checkpoints below are historical; no arbitrary-workbook or partner-readiness claim follows from these changes.

Latest checkpoint: read [V3_PANE_EXECUTION.md](V3_PANE_EXECUTION.md). A bounded physical-field independent-pane compiler is implemented, with occurrence schema v3 and pane-level refusal/query-error isolation. The suite contains 51 V3 tests; corpus coverage is unchanged. Full filter/calculation/relationship planning and layered rendering remain incomplete. Continue with typed filter and calculation plans on this boundary, then temporal/table adapters. Do not rebuild the completed occurrence model or treat the full original plan as achieved.

The preceding checkpoint/original task specification below is retained as historical context; its "not implemented" and 45-test statements do not describe the new bounded subset. Current evidence and limits are in the linked milestone document.

## Prior checkpoint / original specification

Status date: 2026-09-22

Current next task: [executable per-pane query and rendering plans](V3_NEXT_IMPLEMENTATION.md). Independent side-by-side panes come first; dual-axis overlays remain deferred. The existing occurrence/inheritance model must be reused, not rebuilt.

Current checkpoint: occurrence schema v2, declared shared-card inheritance, effective-pane grain and strict bar/KPI/horizontal multi-measure adapters. 45 V3 tests pass; full regression passes before/after. Corpus coverage is unchanged: 49 UNVERIFIED, 339 UNSUPPORTED, six INVALID across 394 sheet occurrences / 25 files; zero structural errors. Layered execution remains blocked. See `V3_PANE_ADAPTERS.md` for evidence and limitations.

## 1. Purpose

This document is the durable handoff for continuing the Tableau-to-Streamlit-in-
Snowflake accelerator in a new coding session. It records the actual repository
state, work completed in this conversation, evidence, known limitations, target
architecture, and the next implementation milestone.

The goal is an enterprise generalized accelerator that can ingest an unfamiliar
Tableau workbook without relying on its filename, workbook name, worksheet name,
or previously curated field captions. Supported constructs must compile and
validate correctly. Unimplemented constructs must be isolated and reported rather
than guessed, silently dropped, or allowed to corrupt an entire dashboard.

No honest implementation can promise an exact interactive Streamlit equivalent
for every Tableau extension, proprietary connector, server-only object, custom
JavaScript integration, or unsupported Tableau feature. For this project,
"works on any workbook" means:

1. Every workbook can be inspected without a process-level failure.
2. Every dashboard and sheet receives an explicit support result.
3. Supported constructs have deterministic field, calculation, relationship,
   grain, filter, and query semantics.
4. An unsupported construct fails only its own component where possible.
5. No approximation is presented as exact.
6. A deployment is never declared successful without numerical, structural,
   interaction, and visual evidence appropriate to the workbook.

## 2. Workspace and repository state

Main project:

`C:\Users\SharathKumarKammari\Downloads\Tableau to SiS_Cowork - Cortex`

All work from this conversation is in the isolated V3 child folder:

`C:\Users\SharathKumarKammari\Downloads\Tableau to SiS_Cowork - Cortex\tableau_accelerator_v3`

Continue work only in `tableau_accelerator_v3` unless the user explicitly asks
to merge changes into the inherited root implementation. The V3 directory is
currently untracked by the main repository. The main repository already contains
a large unrelated dirty working tree belonging to the user and other assistants.
Never reset, clean, discard, or overwrite those changes.

The V3 directory was initially copied from the source project. Its
`source_snapshot.json` contains SHA-256 hashes for 148 root source files. The last
verification found zero drift in the source files represented by that snapshot.
The original V3 location under OneDrive was moved; it no longer exists.

Local V3 app:

`http://127.0.0.1:8513`

Entry point: `v3_app.py`

Launcher: `run_v3.ps1`

The app health endpoint returned `ok` after the folder move. Do not assume the
server is still running in a future session; check the health endpoint first.

## 3. Non-negotiable operating rules

1. Run `git status --short` before editing. The main tree is shared and dirty.
   Because the sandbox may report dubious ownership, use a command-local safe
   directory override rather than changing global Git configuration:

   `git -c safe.directory='C:/Users/SharathKumarKammari/Downloads/Tableau to SiS_Cowork - Cortex' status --short`

2. Run the complete inherited regression suite before and after changes to the
   converter engine, parser, calculation translator, backend, pipeline, or code
   generator:

   `C:\Users\SharathKumarKammari\python.exe -u tests/test_regression.py`

3. Run all V3 tests after V3 changes:

   `C:\Users\SharathKumarKammari\python.exe -m unittest discover -s tests -p 'test_v3_*.py'`

4. Do not deploy to Snowflake unless explicitly asked. If deployment is later
   authorized, verify with `DESCRIBE STREAMLIT` that
   `TABLEAU_ACCESS_INTEGRATION` and the `tableau_pat` secret binding survived.

5. Never call a change generic because one workbook or the existing corpus
   passes. A reusable rule must target a Tableau construct and must be exercised
   across at least two unrelated workbook shapes. Renaming and XML ordering
   should be varied where relevant.

6. Preserve retained failures and honest support states. Do not weaken assertions
   merely to make a suite green.

7. Treat corpus regression as compatibility evidence, not held-out evidence.

## 4. Root cause found

The inherited pipeline compresses Tableau metadata into renderer-friendly,
caption-keyed dictionaries too early:

`tableau_parser.py` -> flat parsed dictionaries -> `engine.py` -> SQL strings

This loses datasource identity, individual shelf/pane occurrences, aggregation
grain, filter phase, table-calculation addressing, and relationship semantics.
The renderer then reconstructs intent using captions and heuristics. Repairs have
therefore accumulated around discovered workbook symptoms instead of a durable
semantic model.

Concrete failure patterns include:

- `column_meta` historically merged internal names across datasources with the
  last declaration winning.
- Calculations and runtime mappings were keyed by display caption.
- Sheet parsing frequently discarded datasource/internal identity when creating
  measure and encoding dictionaries.
- Dashboard filters were deduplicated and matched by caption.
- The runtime used process-wide `CALCS`, `COLMAP`, `ALIASES`, color maps, and
  palette maps.
- Calculation translation is primarily regex/text rewriting rather than a typed
  parser and compiler.
- Relationships, joins, blends, unions, and custom SQL are not represented by a
  sufficiently explicit query plan.
- Chart selection uses incomplete summaries and can misinterpret a familiar mark
  class with an unfamiliar shelf or pane combination.
- Known workbook profiles add curated SQL, formatting, colors, and labels based
  on the input filename.

The filename behavior is visible in `config.py` at `_PROFILE_REGISTRY`. Known
Superstore and World Indicators filenames receive `profile_superstore`; unfamiliar
filenames receive the neutral `profile_default`. The V3 upload path intentionally
saves uploads under a neutral hash-derived filename so an upload does not receive
corpus-specific profile behavior.

Profiles may remain as optional presentation themes. They must not control field
binding, calculation correctness, aggregation, filters, joins, or query semantics.
Renaming a workbook must never change its data result.

## 5. Implemented in V3 during this conversation

### 5.1 Structural IR boundary and failure containment

`ir_contract.py` now provides:

- Structural IR validation with precise issue paths.
- A shared chart-handler registry used for validation and runtime dispatch.
- Explicit support states.
- Pre-code-generation rejection of malformed IR.
- Validation of datasource-scoped field references when present.

Parser and engine behavior now includes:

- Unknown Tableau mark classes become explicit unsupported sheets.
- Worksheet extraction exceptions preserve a named invalid placeholder.
- The generic table inference fallback is labelled as partial.
- Sheet preparation, SQL, and chart rendering are contained within the sheet.
- Layout nodes and dashboard rendering have containment boundaries.
- One broken sheet does not automatically blank valid sibling sheets.
- Filter binding/query failures are isolated from other dashboard controls.

### 5.2 Safer calculation and backend behavior

- Missing parameter values refuse translation rather than becoming zero.
- Failed nested calculations propagate refusal rather than substituting `NULL`.
- Local normalized-column collisions raise instead of silently discarding a
  source column.
- Qualified field references without a relationship plan are refused regardless
  of datasource-name spelling. Parameter references and quoted strings are not
  mistaken for cross-datasource fields.

### 5.3 Datasource-scoped field identity

`field_identity.py` was added. It provides:

- Stable IDs derived from datasource ID plus Tableau internal field name.
- Datasource-scoped metadata roots, with top-level declarations authoritative
  and worksheet dependency declarations filling missing metadata.
- Per-datasource calculation compilation, including nested dependencies.
- A field catalog retaining internal name, caption, type, role, format,
  physical name, calculation and datasource ownership.
- Per-datasource calculation, physical-column, alias, color, palette, hierarchy,
  and measure-color maps.
- Sheet binding that attaches field IDs to renderer-consumed references.
- Detection of conflicting duplicate captions within a sheet.
- Preservation of separate identities for equivalent inline declarations; a
  caption adapter is shared only when compiled meaning, type, role and format
  agree.
- Explicit refusal when distinct datasource IDs use the same display caption and
  caption-based table routing cannot distinguish them.
- A safe compatibility view that exposes only globally unambiguous definitions.

`tableau_parser.build_ir(path, scoped=True)` enables the scoped V3 contract.
Legacy `build_ir(path)` behavior remains available only to preserve inherited
compatibility while migration continues.

`v3_local.prepare` and `assess_v3.py` use `scoped=True`.

### 5.4 Runtime scoped adapter

`engine.field_scope(sheet)` temporarily installs only the selected datasource's
field and calculation maps. It validates explicit field bindings and restores
all prior global maps in `finally`, including nested and exceptional execution.

Measures with field IDs are resolved against the catalog. Foreign datasource
measure references are rejected unless a future relationship plan supports them.
Unscoped lookup of an ambiguous caption raises instead of choosing a definition.

Dashboard filter parts retain datasource and field IDs. Filter values are loaded
from the filter's source datasource, and same-caption filters from different
datasources do not leak into each other.

This is a transitional adapter. The runtime still has caption-based slots and
process-global maps. The final architecture should consume immutable typed IR
and request-scoped execution state directly.

### 5.5 Local V3 application

`v3_app.py` supports TWB/TWBX upload, structural assessment, assessment download,
local datasource file binding, extract handling, and an explicitly labelled
development preview. It does not deploy to Snowflake.

`v3_local.py`:

- Prevents reuse of stale corpus datasource bindings.
- Refuses duplicate bundled data basenames.
- Uses isolated preview datasource configuration.
- Restores engine and configuration state after preview.
- Serializes access to the inherited global engine.
- Refuses local preview when an active Snowflake session would create an unsafe
  mixed execution context.

`assess_v3.py` performs read-only batch assessment and never infers fidelity from
successful parsing.

## 6. Historical verification evidence

The counts in this section describe the datasource-scoping checkpoint. Current evidence is in `V3_STATUS.md` and `V3_PANE_ADAPTERS.md`; do not use the older 28-test or 69-rendered-sheet results as current coverage.

Files containing evidence:

- `baseline_regression.log`: inherited full regression before the first V3
  engine changes.
- `final_regression.log`: inherited full regression after the first boundary
  increment.
- `scope_baseline_regression.log`: inherited full regression before datasource
  scoping changes.
- `scope_final_regression.log`: final inherited compatibility suite; exit code 0.
- `scope_v3_tests.log`: 28 V3 tests passed.
- `scope_corpus_assessment.json`: 25 corpus files assessed; zero structural IR
  errors, 19 NEEDS_REVIEW, 6 BLOCKED, 12 untranslated-calculation entries.
- `scope_corpus_render.log`: scoped headless smoke run of 70 Superstore and
  E-Commerce sheet occurrences. One expected refusal and 69 without unexpected
  blockers. This is not numeric or visual parity evidence.
- `scope_after_regression.log`: intentionally retained failure showing the
  semantic conflict between legacy approximation and V3 refusal. The inherited
  suite expects Superstore Performance to render, while scoped V3 blocks its
  untranslated cross-datasource color calculation. Assertions were not weakened.
- `after_regression.log`: an earlier transient Kaleido subprocess cleanup
  failure. The isolated export recheck and subsequent complete regression passed.

The scoped identity tests use both bar and KPI shapes, two datasources with the
same internal calculation names and captions but different formulas, nested
calculations, reversed XML order, caption and sheet renames, duplicate filter
captions, equivalent and conflicting inline declarations, invalid references,
foreign references, exception restoration, and sibling failure isolation.
Real local CSV queries returned different expected results, 21 and 241, proving
the selected datasource—not XML order—controlled calculation semantics.

None of this proves arbitrary workbook support, Tableau parity, pixel fidelity,
or production readiness.

## 7. Known external blockers and unfinished validation

- Sixteen nested Tableau sheets across five dashboards have no Tableau REST view.
- Tableau VDS access is blocked by site-admin setting error
  `403800 VIZQL_DATA_API_ACCESS`; a Tableau administrator must enable it.
- BDO Pre-Optimization fixes are locally regression-tested but not visually
  signed off against live Tableau.
- The existing AI-assisted V2 is built and deployed but has not completed a real
  unfamiliar-workbook planning, repair, rerender and validation cycle.
- No V3 Snowflake deployment or live Tableau/Snowflake parity run has occurred.

## 8. Target enterprise architecture

The target is a hybrid governed compiler:

1. Lossless deterministic Tableau extraction.
2. Versioned canonical IR with semantic, visual, layout and interaction sections.
3. Typed calculation and query compiler.
4. Confidence classification based on explicit evidence.
5. Restricted dashboard DSL with a finite renderer catalog.
6. Cortex planner for ambiguous mappings and bounded repair proposals.
7. Deterministic schema, field, type, query and security validation.
8. Isolated rendering.
9. Numerical, structural, interaction and visual acceptance gates.
10. Deployment only for accepted results.

### Deterministic authority

An LLM must not be authoritative for:

- Datasource or field identity.
- Data types.
- Aggregation grain.
- Filter scope and order.
- Joins, relationships, unions, blends or fan-out safety.
- Calculation meaning.
- Security, credentials or deployment authorization.
- The final validation verdict.

### Appropriate Cortex responsibilities

Cortex can receive a small, evidence-rich IR fragment and propose:

- Classification of an unfamiliar mark/shelf/pane combination.
- Selection among approved renderer components.
- A schema-constrained IR or dashboard-DSL override.
- Layout or formatting mappings.
- An explanation of numeric, interaction or visual discrepancies.
- Candidate feature-class tests.
- A ranked fallback with stated uncertainty.

Cortex must return schema-constrained JSON containing target node, field IDs,
proposed component, evidence, confidence, expected differences and fallback.
It must not directly modify the shared generic engine, emit unrestricted
production SQL/Python, or override failed deterministic validation.

An accepted workbook-level repair may remain a versioned IR overlay. Promotion
into the generic engine requires review plus proof across at least two unrelated
workbook shapes.

## 9. Prioritized implementation roadmap

### Milestone 1: occurrence-based worksheet IR and grain contract

Partially implemented: occurrence schema and declared grain/inheritance exist. The checklist below is the original milestone scope. Continue with the executable plans specified in `V3_NEXT_IMPLEMENTATION.md`; do not rebuild completed inventory work.

Preserve every field occurrence rather than collapsing by caption. Add a new,
versioned occurrence representation containing:

- Stable occurrence ID.
- Worksheet, pane and mark IDs.
- Shelf or encoding role.
- Stable field ID and datasource ID.
- Aggregation.
- Discrete/continuous state.
- Dimension/measure role.
- Date grain and fiscal metadata.
- Axis/layer identity.
- Sort and top-N metadata.
- Table-calculation partitioning, addressing and ordering evidence.
- Source XML path or evidence descriptor.

Derive an explicit sheet query-grain contract from occurrences. Initially keep
the current renderer adapter, but validate that a selected renderer's required
roles and grain can be satisfied. Unknown or contradictory combinations must
receive an explicit support finding.

Acceptance criteria:

1. XML ordering and caption renaming do not change semantic occurrence IDs.
2. Two occurrences of one field with different aggregations remain distinct.
3. Multiple panes/layers remain distinct.
4. Renderer selection cannot discard an occurrence silently.
5. Unknown combinations block only the affected sheet.
6. Existing scoped tests and inherited compatibility regression pass.
7. New fixtures cover at least two chart shapes and two unrelated datasource
   layouts.

Do not start with Cortex in this milestone. Cortex needs this evidence boundary.

### Milestone 2: typed Tableau calculation AST

Replace regex-first calculation translation with a parser and typed AST covering
field references, literals, operators, functions, conditional expressions,
parameters, aggregates, LOD expressions and table calculations. Annotate type,
calculation level, dependencies, required grain and supported/refused state.
Generate Snowflake and DuckDB SQL from separate emitters over the same AST.

Never substitute zero, `NULL`, a profile calculation or a physical field for an
untranslated expression.

### Milestone 3: relationship-aware query planner

Create explicit plans for physical joins, logical relationships, unions, blends,
custom SQL and secondary datasources. Record cardinality and referential-integrity
evidence. Detect possible many-to-many measure fan-out before execution.

Represent Tableau filter phases explicitly: extract, datasource, context,
dimension, measure, table calculation and dashboard/action filters.

### Milestone 4: restricted dashboard DSL and renderer contracts

Define a finite schema for KPI, table, bar, line, area, scatter, heatmap, map,
dual-axis, layered and other approved components. Each renderer declares accepted
marks, shelves, occurrences, data roles, interactions and approximations. The
runtime validates DSL and field IDs; it does not execute generated arbitrary code.

### Milestone 5: immutable validation and acceptance harness

Produce independent results for structural validity, SQL compilation, numerical
correctness, chart semantics, interactions and visuals. Persist one immutable
result per run. HTML/PDF/UI views must consume the same verdict.

Hard blockers include missing field bindings, untranslated required calculations,
unsafe relationships, SQL failure, material numerical mismatch, unsupported
security behavior and unavailable mandatory evidence.

### Milestone 6: Cortex planner in advisory mode

Reuse only the useful V2 Cortex connectivity and structured-response components.
Feed Cortex canonical IR fragments, XML evidence, supported DSL schemas, renderer
capabilities and validation findings. Validate the response and show it as an
advisory candidate. Invalid or unavailable responses cannot fail the deterministic
pipeline or alter the application.

### Milestone 7: controlled Cortex repair loop

For a low-confidence or failed component:

1. Select the smallest relevant evidence package.
2. Request a bounded IR/DSL proposal.
3. Validate all IDs, types and capabilities.
4. Compile with deterministic code.
5. Render in isolation.
6. Run numerical, interaction and visual checks.
7. Accept only if mandatory gates pass and existing baselines do not regress.
8. Persist the candidate, evidence, model/version/cost metadata and verdict.

### Milestone 8: held-out portfolio and productization

Separate development fixtures, regression workbooks and truly held-out workbook
families. Once a workbook informs a fix, it is no longer held out. Track intake
success, supported coverage, numerical accuracy, interaction accuracy, visual
score, graceful-refusal rate, Cortex repair uplift, regression rate, duration and
cost.

## 10. First concrete task for the next session

Current next task: [executable per-pane query and rendering plans](V3_NEXT_IMPLEMENTATION.md). Independent side-by-side panes come first; dual-axis overlays remain deferred. The existing occurrence/inheritance model must be reused, not rebuilt.

Follow `V3_NEXT_IMPLEMENTATION.md` for the bounded sequence, acceptance criteria, verification commands and Cortex boundary. Compile queries and renderer roles from the same validated occurrence-based plan. Add isolation for independent panes and numeric proofs across different shapes. Temporal/table adapters follow this increment.

## 11. Required reading order for a new session

1. This document, `V3_NEXT_IMPLEMENTATION.md`, and `V3_PANE_ADAPTERS.md`.
2. `V3_STATUS.md`.
3. `V3_DATASOURCE_SCOPING.md`.
4. `V3_START_HERE.md`.
5. Root `CURRENT_STATUS.md`.
6. Root `README.md` and `ARCHITECTURE.md`.
7. Newest section of root `NEW_CHAT.md`.
8. Root `status_config.json`.
9. `ENTERPRISE_ACCELERATOR_HARDENING_PLAN.md`.
10. `PRACTICAL_DEMO_ARCHITECTURE.md`.
11. `AI_ASSISTED_ACCELERATOR_ARCHITECTURE_AND_DEMO_PLAN.md`.
12. In order: `tableau_parser.py`, `field_identity.py`, `worksheet_semantics.py`, `ir_contract.py`,
    `engine.py`, `calc_translator.py`, `backend.py`, `v3_local.py`, `v3_app.py`.
13. `tests/test_v3_boundary.py`, `tests/test_v3_field_identity.py`,
    `tests/test_v3_local.py`, `tests/test_v3_occurrences.py`, `tests/test_v3_adapters.py`, and relevant parts of `tests/test_regression.py`.

## 12. Honest current product statement

V3 has a structural validation boundary, sheet-level isolation, conservative
calculation refusal, local upload/preview, and datasource-scoped field identity.
It is safer than the inherited converter and removes a major XML-order/caption-
collision defect. It is not yet a general Tableau semantic compiler.

The next durable leverage is executable per-pane planning from preserved occurrences and grain, followed by a
typed calculation compiler and relationship-aware query planner. Cortex becomes
valuable after those facts and validation contracts exist; before then it would
automate guessing.
