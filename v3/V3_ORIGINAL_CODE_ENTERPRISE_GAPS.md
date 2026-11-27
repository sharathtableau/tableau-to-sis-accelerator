# Original-code enterprise gap register

## Current documentation authority

For the enterprise objective and current implementation state, read [V3_ENTERPRISE_HANDOVER.md](V3_ENTERPRISE_HANDOVER.md), [V3_STATUS.md](V3_STATUS.md), [V3_NEXT_IMPLEMENTATION.md](V3_NEXT_IMPLEMENTATION.md) and [V3_ENTERPRISE_FIX_TRACKER.md](V3_ENTERPRISE_FIX_TRACKER.md). This file retains feature details and dated checkpoint history. Older current/next/latest headings below are historical and do not override those canonical documents. Technical constraints remain applicable unless explicitly superseded by verified implementation. No product readiness claim follows from this documentation update.

## AI03.1 INCLUDE planning verified and deployed — 2026-10-01

Supersedes prior next-action/deployed-build statements below. AI03.1 (E05/E12; AI00/AI08,
E08/E25): IMPLEMENTED_AWAITING_EVIDENCE for bounded planning only. New include_lod_plan.py
independently parses physical INCLUDE keys, DAY/MONTH/YEAR of physical dates, supported
row arithmetic and SUM/AVG/MIN/MAX at two aggregation levels. Cortex proposes an array
plan; exact source-derived comparison rejects wrong structures/semantics. The unfiltered
two-stage compiler is an evaluation facility, NOT a worksheet executor. Intake shares
the existing total Cortex budget and displays a separate AI calculation planning panel.
Sheet support/acceptance is never promoted by a validated plan.

Verification: baseline/final inherited regression PASS; 157 V3 tests PASS; four same-byte
original/V3 parser comparisons completed (existing datasource_id/support differences
recorded, NOT JSON equality). Corpus remains JSON-identical: 25 files, zero structural
errors, 336 unsupported/invalid occurrences. Two live synthetic Cortex plans matched
independent ASTs; Snowflake two-stage results 7 and 14 matched independent expectations.
An initial object-shaped model response was rejected; prompt clarified to require the
array grammar, without relaxing validators. Missing inherited package entries were
reproduced and corrected in V3 snowflake.yml. Four live synthetic attempts total across
the initial failure, diagnostic and final two cases; zero production metadata calls.

Authorized deployment PASS to existing V3 target: `0d3d18783509c14a79392f3b63dab040f6e595599efb0e371861f7a24b7b0cee`.
All 38 content hashes match; prior 40 live files backed up; runtime/grants unchanged;
refreshed browser startup PASS. Prior E18.1a/AI00.1c changes retained. Tokens/cost,
production numeric/visual parity and whole-workbook acceptance remain unverified.
The two Online Retail INCLUDE sheets still refuse parameter-driven calculated dates
and nested/conditional dependencies. R00/AI00/AI08 remain IN_PROGRESS; E01-E28 remain
OPEN. Effort unmeasured; remaining estimates RE-ESTIMATION_PENDING.

Next: AI03.2 / E05-E06 implement typed transitive numeric-condition/date/parameter
dependencies, then the filtered KPI binding/execution adapter and independent numeric
references. Do not connect compile_unfiltered_sql directly to production rendering.
E18.1b remains open; LAST/LOOKUP still need E07 addressing/order contracts.
See [checkpoint](reports/ai03_1_20261001/checkpoint.md) and
[V3_INCLUDE_LOD_PLANNING.md](V3_INCLUDE_LOD_PLANNING.md).


## E18.1a verified and deployed — 2026-10-01

This checkpoint supersedes earlier local-only/deployed-build statements below.
E18.1a (E18; related E08/E25): IMPLEMENTED_AWAITING_EVIDENCE. Crosstabs now accept
a nonempty explicit ASC manual prefix with exactly one unlisted measure; malformed
sort dictionaries/options refuse before queries. Multiple unlisted measures still
require a proven remainder-order contract. The two Sales tables remain refused;
no workbook support gain or Tableau parity is claimed. Existing complete manual
orders and independent occurrence identities are preserved.

Verification: inherited regression PASS before/after; 150 V3 tests PASS; four same-byte
original/V3 parser comparisons plus old/new scoped refusal proofs PASS. Renamed and
independent numeric/render cases PASS. The 25-file corpus is JSON-identical (zero
structural errors, 336 unsupported/invalid occurrences). Original/V2 untouched.

User-authorized deployment PASS to the existing V3 target. Build `a1434fd9beb8261fb9f9fa3e4fb743887ad2c8d34dd773f6b3d7a5678da7a464`.
All 37 deployed content hashes match; 40 prior live files backed up; runtime/grants
unchanged; refreshed browser startup PASS. One synthetic Cortex request PASS; this
is transport/schema evidence, not production AI quality. Earlier AI00.1c privacy
changes are now deployed too. Tokens/cost and whole-workbook acceptance remain
unverified; no parent gap closes. R00/AI00/AI08 remain IN_PROGRESS. Actual effort
unmeasured; remaining estimates RE-ESTIMATION_PENDING.

Next: AI03.1 / E05 implement the source-side typed INCLUDE grain/aggregation contract
and bounded Cortex proposal validation, with renamed/independent numeric fixtures.
Keep E18.1b multi-remainder ordering evidence open; do not guess from filter-union
order or one screenshot. E07 LAST/LOOKUP follows authored partition/order extraction.
See [checkpoint](reports/e18_1_20261001/checkpoint.md) and
[V3_MEASURE_HEADER_REMAINDER.md](V3_MEASURE_HEADER_REMAINDER.md).


## Reuse/Cortex audit checkpoint — 2026-10-01

R00.2 scoped static inventory and targeted reuse assessment recorded in
[V3_REUSE_AND_CORTEX_AUDIT.md](V3_REUSE_AND_CORTEX_AUDIT.md). R00 remains IN_PROGRESS;
this does not close the outstanding full manual review. Original/V3 source inventories
contain 188/164 files; 110 same-path files are byte-identical. Existing renderer,
relationship, semantic-model, critic, vision and Analyst code offers substantial reuse.
Several legacy AI/validation modules are not in the current V3 workbench import closure.
INCLUDE/LAST/LOOKUP still need typed semantics and independent validation before Cortex
proposals can be accepted. Snowflake Tableau ingestion does not support LOD calculations;
Agents APIs require a runtime decision because this app uses warehouse runtime.
Static worker title-based screenshot attribution requires E01/E09 reproduction.

Next remains E18.1 partial Measure Names remainder-order reproduction; then AI03/E05/E07
typed calculation extensions and AI06 reuse. No runtime edits, new tests/live AI calls,
or deployment in this audit. Source hashes rechecked unchanged. Existing AI00.1c is
local; deployed AI00.1b unchanged. Prior deployment authorization persists. Parent gaps
remain open, whole-workbook acceptance unverified; effort unmeasured and estimates
RE-ESTIMATION_PENDING. Evidence and exact limitations are in the linked audit.


## Current checkpoint - AI00.1c and five-workbook intake (2026-10-01)

This section supersedes older next-action/local-build statements below.
AI00.1c (parents E11/E12/E24): IMPLEMENTED_AWAITING_EVIDENCE. Calculation prompts
now include only referenced physical fields; visual prompts use an explicit display
metadata allowlist. Prompt version is cortex-adaptation-prompt/2. Source formulas
and referenced names/IDs remain necessary inputs: this is not full anonymization.
Independent source validation, bindings, render contracts and refusals are unchanged.
The generic request API still relies on callers to provide approved evidence; this
projection applies to the scoped assist path, not every legacy Cortex entry point.

Verification: inherited V3 regression PASS before/after; 23 focused Cortex tests and
146 V3 tests PASS; 25-file corpus identical (0 structural errors, 336 unsupported/invalid
sheets). Four same-byte original/V3 parser fixtures pass; pre/post V3 adaptation
contracts identical; unrelated-marker transmission reproduced before and absent after.
Live synthetic calculation plus visual request PASS (2 attempts), including independent
AST equality. No production workbook metadata was sent to Cortex in this round.

Authorized Tableau project found: Tableau to PowerBI, 22 workbooks. Five downloads
frozen by hash and assessed: 121 sheet occurrences, 93 unsupported, 5 invalid, 23 locally
rendered without captured errors/warnings. Local rendering is NOT Tableau parity.
Four workbooks yielded six published-default reference views (CSV and PNG); three
ECommerce reference views failed. Confirmed Tableau error 400081:
DataSourcesNotConnectedException (data sources not connected). Whole-workbook
acceptance remains BLOCKED for all five. No unsupported sheet qualified for the
existing narrow Cortex substitution contract; do not broaden it by bypassing validators.
The fifth workbook is reserved from further tuning from this checkpoint; historical
unseen status is unproven, so it is not claimed as a fully independent frozen holdout.

Deployment remains the earlier AI00.1b build 585428c9f5d7b02e5fb2a3b3497aa8024b5dbed5f9abb1507e514e9552b3e571.
AI00.1c is LOCAL ONLY; no new deployment was performed. R00 and AI00/AI08 remain
IN_PROGRESS; E01-E28 remain OPEN. Actual total effort unmeasured; command durations
recorded; estimates remain RE-ESTIMATION_PENDING.

Next: E18.1 reproduce and define partial Measure Names manual-order remainder semantics
(two Sales workbook sheets) against authored metadata and Tableau reference before a
generic correction; preserve the reserved fifth workbook. Also establish matching
reference/source snapshot and filter states for numeric comparison; the ECommerce
reference path requires its Tableau datasource connection restored. Continue AI00
account/role/binding provenance and token/time controls alongside the evidence work.

Evidence: [checkpoint](reports/ai00_1c_20261001/checkpoint.md),
[five-workbook results](reports/ai00_1c_20261001/five_workbook_assessment.json),
[render evidence](reports/ai00_1c_20261001/local_render.json).

## Current implementation checkpoint - AI00.1b / AI08.1 (2026-10-01)

This checkpoint supersedes older current-status and next-action statements below.
AI00.1b is IMPLEMENTED_AWAITING_EVIDENCE: local verification passes; live qualification
is still open. AI00.1 and AI08.1 are IN_PROGRESS; R00 remains IN_PROGRESS and all
E01-E28 parent gaps remain OPEN. Owner: current V3 implementer; start/update 2026-10-01.

The scoped Cortex boundary now rejects duplicate/non-finite JSON and oversized
responses, returns static service-failure codes, and adds application-stamped
versioned request traces with hashes, actual attempted calls and local elapsed time.
Prompt/response/error text is excluded from traces. Tokens and cost stay unknown.
Independent source semantics and existing substitutions/refusals remain authoritative.
Parents for this increment: E11/E12/E23/E24; AI08 also supports E08/E25.

Verification: V3's own inherited regression PASS before/after, 21 focused Cortex tests
PASS, 144 V3 tests PASS. Corpus: 25 files, zero structural errors, 336 unsupported/invalid
sheets; result unchanged from the previous checkpoint. Same-byte original/V3 parser
and pre/post V3 error comparisons cover three fixtures; an independent subtraction/null
fixture and its renamed variant produce A=0, B=-7 locally. These are development tests.
The previous checkpoint's regression log targeted the parent; current command records
explicitly target V3/tests/test_regression.py.

Live Cortex, target Snowflake execution, Tableau parity, account/role isolation,
frozen-holdout AI quality, measured AI cost and whole-workbook acceptance: NOT_RUN.
No qualified account/model/role/source-snapshot test context was established here.
Deployment: none. Focused effort unmeasured; test durations recorded. Consolidated
remaining estimates remain RE-ESTIMATION_PENDING; no new schedule savings claimed.

Changed code: cortex_assist.py; new tests/test_v3_cortex_contract.py.
Contract: [Cortex proposal v1](V3_CORTEX_PROPOSAL_CONTRACT.md).
Exact commands, hashes, input bytes, review coverage, results and outstanding gates:
[checkpoint evidence](reports/ai00_1_contract_20261001/checkpoint.md),
[final manifest](reports/ai00_1_contract_20261001/final_manifest.json).
Before bytes are preserved in that evidence directory; repeated carriage-return
line endings from the earlier documentation edit are normalized with history retained.

**Next dependency-ready action: AI00.1c** - reproduce unrelated/sensitive metadata
transmission in existing calculation/visual prompts, then implement and test an
explicit minimum-metadata projection without changing semantic validation. Follow with
trusted run/binding/account-role provenance and qualified token/time limits. Continue
remaining R00 review and retain E03 ingestion-fidelity reproduction in the queue.

## Prior records retained

Product goals and preservation rules below remain in force. Older status/next-action
claims are historical where the current checkpoint supersedes them.

September 27, 2026. Product authority: `V3_ENTERPRISE_PRODUCT_GOAL.md`.

## Review basis and limits

The original source reviewed is the parent of this V3 directory, including its
current uncommitted changes. Git status was captured before this review. The
original is the product baseline; it was read, not edited. This is a code-backed
repair inventory, not a release approval or a claim that the whole product works.

`enterprise_product_audit_20260927/original_source_inventory.json` records every
file in the defined original-source scope, its SHA-256, imports, definitions,
calls, global statements and broad exception handlers. It contains **129 files,
96 Python files, 56,183 lines and 1,632 definitions**. All 96 Python files parse
in the structural scan. These counts describe review scope, not passing tests.

Scope: original top-level Python/SQL/YAML/requirements and the `tests`, `worker`,
`sql`, `accelerator_validation_reference`, and `blend_analysis` directories.
V2/V3 copies, credentials, data, workbook payloads, and generated evidence are
excluded from that inventory. Embedded worker source is present in inventoried
files but is not separately parsed as Python by the inventory scan.

Detailed code review traced intake through parser, binding/loading, relationship
planning, calculation translation, engine queries/rendering, controls/layout,
generation, validation, worker queues, Cortex, and deployment. Core modules were
read in source/AST form; large UI/reporting/worker modules received targeted path
review. **This is not yet a completed line-by-line manual review of all 56,183
lines.** Remaining review coverage is explicit below. No tool scan substitutes
for that work, security testing, real Snowflake execution, or Tableau parity.

Six isolated, in-memory reproductions were run against imports from the original
directory, with bytecode writing disabled. No live Snowflake/Tableau call was
made. Results are recorded in `local_reproductions.json`. No converter files were
changed in this audit, so the inherited/V3 regression suites were not rerun for
this documentation increment. Earlier 134-test claims are historical evidence
for the preceding filter change, not evidence for this audit's recommendations.

## Preserve the functionality that already exists

| Original capability | Preserve and strengthen |
|---|---|
| Upload, Cloud/Server browsing, pagination, PAT authentication and workbook download | `tableau_server.py`, `pipeline_app.py`, `converter_app.py`; retain the shared transport and payload normalization |
| Bundled CSV/Excel/Hyper handling, union materialization, live-source discovery, explicit mappings | `pipeline.py`, `init_workbook.py`; retain generic ingestion while enforcing fidelity and source identity |
| Physical source discovery, tree-shaped relationship view generation and separate table loading | `semantic_layer.py`, `pipeline.py`; extend semantics instead of discarding the implementation |
| Formula translation, parameters, groups, sets, reference lines and table-calc metadata | `calc_translator.py`, `tableau_parser.py`; retain proven transformations behind typed contracts |
| Broad renderer family, authored label runs, formats, layout tree, filters, parameter controls and hierarchy drill | `engine.py`; do not treat the existence of a narrower V3 adapter as justification for deleting working behavior |
| App generation, SQL/semantic models, notebooks, reports and Snowflake publishing | `codegen.py`, `cortex_semantic.py`, `pipeline.py`; retain packaging while separating candidate generation from promotion |
| Three-way validation, real-app screenshots, capture bundles/hashes, queues, retries and blocked states | `validation_report.py`, adapters/bridge, screenshot and task modules; consolidate their authority and close evidence gaps |
| V3 scoped fields/occurrences, typed bounded plans, source resolver, preview lock, transitive package hashes and constrained Cortex | Preserve these existing safeguards; widen validated generic coverage rather than bypassing them |

## Priority definitions

- **P0:** Can compromise source/data correctness, access boundaries, evidence, or
  safe release. Close before enterprise acceptance.
- **P1:** Required to make the declared migration scope complete and dependable.
- **P2:** Operational scale, maintainability and adoption work that still needs
  explicit release criteria. This is not permission to omit it from production.

Evidence labels: **R** = isolated local reproduction; **C** = confirmed code
behavior; **V** = required verification/design control, not a reproduced incident.
Locations below refer to the original parent unless prefixed `V3/`.

## P0 — data correctness, security and release integrity

| ID | Evidence and concrete gap | Generic repair and closure evidence |
|---|---|---|
| E01 — Structural identity throughout the lifecycle | **C.** `tableau_parser.py:46` merges field metadata by local token; `extract_calcs_params:2202` keys calculations by caption; `encodings:193` keeps the first channel across panes. Distinct sources/occurrences can lose identity. | Carry datasource, physical-object, field, calculation, pane and occurrence IDs into loading, semantics, controls, validation and export. V3 already supplies part of this. Prove same captions/different sources, repeated pills, rename invariance and exported-runtime continuity. |
| E02 — Verified source binding | **R/C.** `config.py:110` returns the default table for an unknown datasource. `pipeline.py:247` honors explicit source maps despite failed column coverage; single-name matching is weaker than source identity. Live paths bypass that resolver. `tableau_parser.py:1357,1634` use first upstream connection assumptions and do not consistently retain relation-qualified database identity. | Preserve candidate discovery; verify account/object/access/physical columns/types and source version for every binding path. Unknown sources must refuse at the binding boundary. Support quoted names and multiple connections through explicit plans. V3 exact-account/schema checks exist but are bounded. The helper fallback reproduction does not mean every renderer bypasses the existing unmapped-source guard. |
| E03 — Data ingestion fidelity | **R.** `backend.py:86` normalizes names, drops duplicate normalized columns and guesses types. Two columns `Order-ID` and `Order ID` became one; string keys `001,002` became numbers `1,2`. `read_excel:68` defaults to one sheet. | Use Tableau/extract physical schema, collision-safe names and explicit file/sheet/encoding/locale contracts. Preserve decimal scale, identifiers, nulls, timezones and rejected rows. V3 collision refusal is an existing partial mitigation; cover all ingestion paths and exported artifacts. |
| E04 — Relationship cardinality and incomplete model fallback | **R/C.** `init_workbook.py:flatten_tables` LEFT JOINs a graph without cardinality proof. A fact sum of 30 became 40 with duplicate dimension keys. With no valid graph, the helper returned only the largest table. `semantic_layer.py:join_plan/generate_views` proves tree shape, not Tableau logical relationship semantics. | Preserve physical joins/tree discovery, but model physical joins separately from logical relationships and blends; validate composite keys, join operators, null matching, cardinality, unmatched rows and measure grain. No partial largest-table success. Use aggregate-before-join where required and independent numeric tests across one/many/multi-fact cases. |
| E05 — Typed calculation semantics | **R/C.** `calc_translator.py:531` recursively replaces an unsupported nested expression with `NULL`; `[Nested] + 1` with a LOOKUP dependency became `NULL + 1`. `agg_sql:63` maps unknown aggregation to SUM. Formula rewriting is mostly string/regex based; FIXED LOD becomes a window/subquery and INCLUDE/EXCLUDE refuse. | Introduce typed expression/dependency/evaluation-level contracts around existing valid translations. Reject unsupported dependencies transitively. Preserve constants/literals, types, null/division behavior and datasource ownership. Verify LODs against filter order and grain. Use separate dialect emission and independent numeric expected results. V3's stricter dependency guards must be retained. |
| E06 — Filters and Tableau order of operations | **C.** `engine.py:961` warns and skips unresolved/aggregate/relative-date filters; the query can then show more rows. `tableau_parser.py:815` flattens portions of groupfilter structure and some branches omit context metadata. Dashboard targeting relies partly on captions. | A typed filter tree must account for datasource, extract/context, dimension, condition/top-N, aggregate and table-calc phases, null/empty/all/exclude semantics, dynamic parameters and exact targets. Reuse safe original controls. V3's new table dashboard filter path is only a bounded increment. Require before/after results for interacting filters and Top-N/LOD combinations. |
| E07 — Ordered table calculations | **C.** `tableau_parser.py:907` captures type and some order metadata; `engine.py:416` supplies `ORDER BY 1` when a token survives, and several renderers use best-effort ordering. WINDOW rewrites use `OVER ()`. Parsing PctDiff does not implement its execution. | Plan addressing, partitioning, sort/ties, restart, frame, nested table calcs, missing dates and densification explicitly. Preserve working original cases only after equivalence proof. Verify PctDiff/running totals/rank/lookup across independent shapes and state changes; refuse missing order without inventing it. |
| E08 — Truthful acceptance and coverage | **C.** `parity.py:209,490` counts EXECUTED as measures_pass; `pipeline_app.py:page_deploy` primarily gates on measures_bug and permits override. CLI prints DONE despite degraded render warnings. Better BLOCKED/PASS logic already exists in `validation_report.py`. | Make one authoritative acceptance model across CLI/UI/report/export/release. Record every required source/sheet/occurrence/control; no empty or unexecuted denominator can pass. Distinguish execution, independent numerics, Tableau parity, visual/interaction/layout and complete-workbook acceptance. An approved exception remains an exception, not PASS. |
| E09 — Independent reference identity and full result coverage | **C.** `parity.py:40` has caption-keyed fixed truth. `tableau_server.py:444` can search published sources by hints/captions rather than exact workbook lineage. `validation_evidence_bridge.py:753` narrows Tableau rows to app-displayed keys. This can test displayed values without establishing completeness. | Bind reference evidence to exact workbook/revision, datasource/view IDs, snapshot, filter/parameter state and build hash. Prove full expected key domain, omissions/duplicates, sorting/ties/totals separately from display caps. Keep the useful three-way validator; remove curated truth as product authority. |
| E10 — Session/workbook isolation and governed access | **C/V.** `engine.py:27,139,149,408`, `backend.py:18,37`, `pipeline.py:683` mutate process globals or cache only SQL. V3's `preview_context` lock/restore mitigates part of the risk but is not a whole-product multiuser proof. | Use a per-run immutable context for bindings, parameters, findings, connection and cache identity. Validate concurrent users/accounts/workbooks, policy changes and cancellation. Define migration-author, publisher and viewer privileges; verify row access and masking with the actual runtime. No data-leak incident is claimed from static analysis alone. |
| E11 — Secret custody and trusted endpoints | **C.** `tableau_crosstab_jobs.py:116` writes PAT name/secret to local JSON and a per-run Snowflake stage; its worker consumes it. No corresponding deletion was found in those two paths. REST sign-in accepts the provided server URL. | Workers should resolve a configured Snowflake secret at execution, retaining independent login without copying PATs into evidence/job artifacts. Define server allowlists, TLS, redaction, rotation, stage ACLs and retention; exercise denied-host and secret-absence cases. This review did not read credential values, rotate tokens or alter the deployed worker. |
| E12 — Constrained SQL and Cortex execution | **C/V.** `pipeline.py:configure_datasources/load_into_snowflake` executes workbook custom SQL as a derived table; SQL identifiers/literals are assembled in several paths. `cortex_calc_fallback.py:main` runs a model proposal to classify executable status; the Ask UI executes returned SQL. Executable is not semantically safe. | Preserve valid custom SQL through an approved source plan, resolved relations and read-only execution boundary with limits. Validate SQL structure, objects/functions and values; use typed bind values/identifier quoting. Keep V3 Cortex typed proposals, not arbitrary model SQL, on conversion paths. Review this as a security boundary; no SQL injection exploit is claimed. |
| E13 — Safe persistence, data changes and promotion | **C.** `pipeline.py:813` loads caption-derived tables with overwrite=True. `onboard` can create/load tables and resolve model views during Discovery; `pipeline_app.py:run_migration` deploys views/semantic views before final app approval. App publishing uses CREATE OR REPLACE and mutable stage paths. | Make discovery read-only, then produce an explicit mutation plan. Isolate objects by run/project/environment, stage versioned data/builds, validate, then promote under authorization with rollback. Preserve refresh semantics. Reject accidental collision/overwrite and retain integration/secret/grant settings during replacement. |
| E14 — Runtime revalidation and drift | **C/V.** `V3/v3_source_binding.py` verifies schemas at binding, but `V3/v3_package.py:16` validates binding syntax/completeness and `V3/v3_runtime.py` does not itself repeat account/schema validation before preview_context. A successful old bind is not permanent source proof. | Package the verified binding contract and revalidate identity/access/schema at runtime or a governed freshness boundary. Detect schema/role/source changes and invalidate plans/caches/evidence. Test the same package against wrong account, missing columns, changed types and denied access. |

## P1 — complete generic migration and reliable operation

| ID | Evidence and concrete gap | Generic repair and closure evidence |
|---|---|---|
| E15 — Intake, connectors and snapshot policy | **C/V.** ZIP extraction in `pipeline.py:625` flattens basenames; `load_twb_xml` chooses the first TWB. Hyper decoding reads full tables into memory. Original union materialization can proceed with available members. V3 checks some collisions/incomplete unions. | Validate archive members/count/expanded size, single selected workbook, source completeness, file-sheet association and source hashes. Support published datasources and approved connectors through explicit contracts. Choose live versus extract behavior visibly and retain snapshot/refresh lineage. Provide a governed decoding service where the selected SiS runtime cannot decode. |
| E16 — Generic renderer coverage and loss of authored semantics | **C.** Original has a broad renderer family; inference takes first/last fields for several shapes. V3 refuses many constructs already represented by original. Original `r_pctbar:2488` uses COUNT(*) regardless of the parsed measure; `r_dual` resolves scales independently; `r_gantt` clips durations to a minimum. | Build an original/V3 feature matrix at query, visual and runtime level. Preserve generic renderers with occurrence/grain/encoding checks. Correct measure-based percent bars, negative values, stacking, dual-axis synchronization, panels, legends, reference lines and trend models using authored metadata. Every substitution needs a visible contract and complete numeric proof. |
| E17 — Dates, bins and domain completion | **C.** Original date bars/dual charts cast nullable year values directly to int (`engine.py:2604,2766`); temporal mapping sometimes defaults to month. Numeric generated bins are not implemented by categorical-bin handling. V3 bounds temporal support and refuses null groups. | Implement typed discrete/continuous date semantics, calendar/week/fiscal/timezone rules, domain padding and explicit null treatment; do not simply discard nulls. Plan bins with authored width/origin/boundaries and prove negative/exact-boundary/null cases. HR date/bin behavior is reproduction context only. |
| E18 — Tables, sorting, labels and totals | **C.** `engine.py:3032` queries LIMIT 200 before client sorting; dimensions can be skipped; aliases can collapse repeated columns. `_rank_table` derives formatting/captions heuristically. V3 tables/crosstabs solve bounded cases but still refuse partial ordering and richer text. | Aggregate and sort the complete domain before display limits. Use stable occurrence columns, deterministic authored ordering/ties/nulls, Measure Names/Values pivots, composed text, totals/subtotals and correct total calculation semantics. Verify duplicates and a >200-group case. Preserve existing authored label-run support. |
| E19 — Interaction semantics | **C/V.** Original has filter/parameter widgets and hierarchy drill, but parser excludes Action filters and the reviewed core lacks a complete action execution model. Validation can use SQL before/after proof rather than actual widget events. | Represent filter/highlight/navigation/URL/parameter/set actions, source/target scope, selection clearing, multi-select, relevant-values behavior, dynamic visibility and synchronized state. Match authored interaction state in Tableau and the generated app. Inventory unsupported actions rather than silently dropping them. |
| E20 — Layout, assets and accessibility | **C.** Original layout deduplicates sheets by name and converts geometry to responsive rows/columns with fixed scaling/caps; device layouts, forecasts and some axes/totals are reported as omitted/changed. | Preserve zone occurrence identity, tiled/floating layout, size rules, visibility, text/image assets and navigation. Define fidelity/acceptable reflow explicitly. Validate real running-app screenshots plus keyboard/focus, contrast and accessible tables; a headless composite is not target UI parity. |
| E21 — Complete packaging and runtime compatibility | **C/V.** Original app download can contain just a wrapper that imports engine. Original runtime modules are a manual list. V3 transitive packaging and hashes improve this, but target account/warehouse/integrations remain fixed in configuration. Local, worker and SiS dependency sets differ. | One versioned install/export manifest with dependency lock/SBOM, all assets, IR/binding schema versions, target configuration, secret references and provenance. Prove clean-environment imports, startup, query/render/export behavior and upgrade/rollback in the selected Snowflake runtime. Preserve both existing V3 deployments. |
| E22 — Durable job ownership and recovery | **C/V.** `validation_jobs.py:311` completion/failure updates use run_id without attempt fencing. Heartbeats occur around long capture calls in `validation_worker.py`; stale recovery stops requeueing at MAX_ATTEMPTS without itself terminalizing exhausted active jobs. Title-keyed capture attribution can collide. | Retain queues/retries but add lease/attempt fencing, continuous heartbeat, deadline/cancellation enforcement, terminal recovery, idempotent submissions and stable dashboard IDs end to end. Prove two-worker races, death mid-upload, repeated jobs, stale completion, duplicate titles and exhausted attempts. Runtime behavior of active deployed worker variants still needs verification. |
| E23 — Resource, cost and performance bounds | **C/V.** Distinct filter domains, raw-source re-pulls and many per-sheet queries can be unbounded. Full Hyper tables are loaded in memory. Some render caps silently change content. | Set governed query/scan/time/result/memory budgets, cancellation and query tags; batch/cache safe shared work with source/role identity. Use explicit pagination or disclosed limits after semantics. Benchmark large workbooks, wide models, high-cardinality controls, cold starts and concurrent workloads; set company SLOs with measured evidence. |
| E24 — Operational observability and lifecycle | **V.** Some worker logs/run IDs exist, but no unified lifecycle proof was established across intake/query/plan/render/reference/export/promotion. | Correlate workbook/build/run/query IDs, stage timings, refusal codes, dependency versions and source freshness. Add actionable redacted errors, metrics/alerts, retention/deletion, support bundles, refresh failures and recovery runbooks. Verify evidence contains no secrets and that retention jobs preserve audit requirements. |
| E25 — Release and holdout evaluation | **C/V.** Regression/corpus fixtures are useful but development corpora and parser support counts do not demonstrate unfamiliar-workbook acceptance. | Freeze build and choose holdouts before inspecting them; if used to develop a fix, replace them. CI must run original regression, V3 semantic/refusal fixtures, numeric oracles, local UI, Snowflake execution and applicable Tableau parity. Require complete-workbook and concurrent-security acceptance; no automatic production deployment. |

## P2 — product maintainability and administration

| ID | Gap | Closure |
|---|---|---|
| E26 — Duplicated entry points and mutable configuration | `convert`, classic converter, workbench, original/V2 entry variants and worker paths can drift despite shared modules. Hard-coded environments and profile dictionaries remain. | One lifecycle service/API shared by UI/CLI/workers; declarative environment/source configuration; capability registry shared by assessment, runtime and package. Retire alternatives only after regression/equivalence evidence. |
| E27 — Supported scope and operator UX | Historical docs mix demo targets, narrow milestones, deployed state and readiness claims. Source access problems and semantic refusals need different remedies. | Publish product scope, per-component disposition, binding review, actionable remediation, provenance and release notes. Maintain one current goal/ledger with dated evidence; preserve history without treating it as current authority. |
| E28 — Compliance and distribution decisions | Data classification, retention, residency, internal single-account versus multi-account distribution, license requirements and support ownership are not established by this code review. | Record company decisions and threat model before release. Prepare account/bootstrap configuration and least-privilege grants accordingly. Do not assume a Snowflake Native App packaging requirement unless the company chooses that distribution model. |

## Local reproduction evidence

| Case | Observed original behavior | Implication |
|---|---|---|
| Two independently named columns normalize identically | 2 input columns -> 1 output column | Silent ingestion loss; E03 |
| String keys `001`, `002` | Output numbers `1`, `2` | Type inference can change identifiers; E03 |
| Unknown datasource helper lookup | Returned configured default `SUPERSTORE.PUBLIC.ORDERS` | Unsafe lower-level default; E02; renderer guard reachability must be considered |
| Unsupported nested LOOKUP calculation | SQL `NULL + 1`, agg_ready false | Silent dependency substitution; E05 |
| Unknown aggregate keyword | SQL `SUM(AMOUNT)` | Inferred semantics without a contract; E05 |
| Duplicate dimension join key | Fact sum 30 -> flattened sum 40 | Tree shape alone is insufficient; E04 |
| Multiple tables without a valid graph | Largest table returned with warning | Partial source can proceed as data; E04 |

These are local helper-level results. They establish the behaviors above, not
Snowflake execution, Tableau equivalence, or whole-workbook defects for every path.

## Current implementation order

Follow V3_PRODUCT_ROADMAP.md and V3_AI_IMPLEMENTATION_PLAN.md. E01–E28 remain open and keep the closure criteria above. AI00–AI08 are mapped workstreams in V3_ENTERPRISE_FIX_TRACKER.md, not replacements for these defects. Resume R00's relevant Cortex/intake/validation path review, then AI00.1: validate the shared proposal contract and reproduce total AI-call budget behavior before selecting a code correction. Start AI08 evidence design with that work. Keep E03 ingestion fidelity and other dependency-ready original correctness repairs active in the queue. R00's broader unreviewed coverage remains outstanding.

## Review coverage still to complete

Full-byte/AST inventory covers all 129 source/config files. Detailed runtime
review covers the core parser, engine, backend, config, translator, pipeline,
data model, intake/generation and semantic-model code. Targeted review covers
the large UI, validation/report, capture workers and deployment scripts.

Still required for a complete manual audit: all remaining branches in legacy UI
variants and generated standalone app implementations; all report/template and
notebook-embedded code; all validation alignment/interaction heuristics; every
administrative SQL/script; the entire regression and interaction-test body; and
active-runtime reachability for alternative worker paths. The inventory provides
file/definition coordinates for that continuation. Historical duplicate code is
not silently excluded merely because the current main entry does not import it.

## Snowflake platform constraints verified for this review

The security design must match the selected runtime. Snowflake documents default
owner-rights execution; restricted caller rights are supported for container
runtime subject to documented requirements, and those connections require
session-scoped caching. A different runtime/security mode is a product decision,
not something this audit changes. Sources checked September 27, 2026:

- [Owner rights](https://docs.snowflake.com/en/developer-guide/streamlit/object-management/owners-rights)
- [Restricted caller rights](https://docs.snowflake.com/en/developer-guide/streamlit/features/restricted-callers-rights)
- [Runtime environments](https://docs.snowflake.com/en/developer-guide/streamlit/app-development/runtime-environments)
- [Secrets and configuration](https://docs.snowflake.com/en/developer-guide/streamlit/app-development/secrets-and-configuration)

No live account grants, policies, objects or deployments were changed or verified
in this audit. Existing deployments remain outside this local review's claims.

## Preserved history

Complete pre-revision document: [V3_ORIGINAL_CODE_ENTERPRISE_GAPS.md](enterprise_product_audit_20260927/ai_roadmap_20260927_230202_774981/V3_ORIGINAL_CODE_ENTERPRISE_GAPS.md). Historical claims are evidence for their recorded builds only; current work follows this document and the tracker.
