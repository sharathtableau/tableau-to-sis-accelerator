# Current Cortex implementation scope — 2026-09-27

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

## Current verification and deployment checkpoint (2026-10-01)

Supersedes earlier no-deployment and live-Cortex-NOT_RUN statements for this build.
User explicitly authorized deployment to the existing V3 target after checks passed.
AI00.1b remains IMPLEMENTED_AWAITING_EVIDENCE; AI00.1/AI08.1 and R00 remain IN_PROGRESS;
E01-E28 remain OPEN. This is verification/deployment of the existing implementation,
not closure of enterprise acceptance gaps. Parent scope E11/E12/E23/E24 (AI08: E08/E25).

- All 134 source/test inventory hashes match the preceding verified checkpoint.
- Fresh V3 suite: 144 tests PASS. Prior before/after inherited regression and 25-file
  corpus evidence remain applicable to unchanged source; not rerun in this checkpoint.
- Package and downloaded live files: all 37 content hashes match. Build:
  `585428c9f5d7b02e5fb2a3b3497aa8024b5dbed5f9abb1507e514e9552b3e571`.
- Deployment PASS to `WBR_DB.TABLEAU_ACCELERATOR_V3.TABLEAU_TO_SIS_V3_CANDIDATE`.
  Existing grants and runtime configuration unchanged. Forty live files backed up.
- Browser startup PASS after refresh; Upload and Tableau Cloud controls load.
- One synthetic live Cortex request PASS under WBR_OWNER in AWS_US_EAST_1:
  claude-sonnet-4-5 returned the permitted bar choice with a versioned redacted trace.
  This verifies service/schema integration, not semantic quality or role isolation.
- Tableau parity, real-source numeric correctness, whole-workbook acceptance,
  role-isolation negatives, holdout AI quality, residency/edition qualification and
  measured AI cost remain unverified. Prior corpus retains 336 unsupported/invalid sheets.
- No converter code changed in this checkpoint. Original/V2 were not edited.
  Actual total effort unmeasured; command durations recorded; estimates remain
  RE-ESTIMATION_PENDING. No parent gap closed and no delivery-time savings claimed.

Next dependency-ready implementation: AI00.1c minimum-metadata prompt projection,
with same-byte original/V3 review, reproduction, renamed/independent fixtures and
required before/after converter checks. Preserve current deployment unless a later
update is authorized; this checkpoint's explicit authorization was exercised.

Evidence and rollback: [verification report](reports/verification_deploy_20261001/checkpoint.md).

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

The bounded implementation documented below is preserved. The expanded product plan is V3_AI_IMPLEMENTATION_PLAN.md (AI00–AI08). Additional assessment, mapping, diagnosis, assistant and evaluation capabilities are planned, not implemented by this documentation rewrite. Retain source validation, explicit substitution status and independent evidence. The no-code/no-test statement in the 2026-09-27 planning rewrite is historical; the 2026-10-01 AI00.1 implementation and evidence are recorded below.

## Historical bounded implementation evidence

# Validated Cortex adaptation — September 23, 2026

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

V3 uses Cortex in the middle of the scoped Cloud/upload intake: source extraction
→ eligible structural contract → Cortex proposal → independent validation →
typed SQL compiler → isolated rendering. This implements a bounded automatic
replacement path, not arbitrary workbook support.

## Executable scope

- One physical datasource, one pane, no axes/shelves, categorical Pie/Circle/Square
  marks with one discrete physical color dimension and one aggregate numeric
  size/wedge measure. Repeated text must identify the same measure/aggregation.
  Every occurrence retains its identity and an explicit role.
- Cortex chooses bar, dot or table only from validated alternatives. No workbook,
  sheet, caption or filename dispatch. Chart type alone does not establish safety.
- Grouping and aggregation are preserved. Replacement ordering is value descending
  with category ascending ties, before a disclosed 200-group display limit.
  Layout and ordering changes are disclosed; substitution is PARTIAL, not parity.
- Relationships, pivots, totals, filters, custom ordering, table calculations,
  additional encodings/panes and ambiguous structures remain isolated refusals.
  Null category/value failures inspect the full grouped domain before display.
- Eligible row formulas support numeric physical fields, numeric literals, unary
  signs, addition/subtraction/multiplication, ABS and ZN. Cortex proposes a typed
  expression tree which must match an independently parsed source expression.
  Division, nested calculations, strings, LOD and window calculations are refused.
  This is scoped calculation conversion for replacement measures, not a general
  calculation repair system. The older cortex_calc_fallback remains review-only.

## Execution and validation

`prepare_payload` is shared by Cloud and uploads and automatically invokes the
assistance boundary when an active Snowflake session exists. Local runs without
a session retain explicit refusals. `V3_CORTEX_MODEL` overrides the tested default
`claude-sonnet-4-5`. Requests use bound AI_COMPLETE parameters, structured JSON
outputs, metadata only, and a 20-sheet per-intake budget (up to one extra
calculation call per eligible sheet). Invalid output, unavailable services,
model refusals and budget exhaustion are isolated per sheet.

Source and field snapshots are revalidated before rendering and IR assessment.
SQL comes from deterministic compiler functions; generated SQL or Python is not
accepted. The report records substitution/refusal reasons. Renderer captions
show substitutions and truncation. The V3 Snowflake preview requires explicit
DATABASE.SCHEMA.OBJECT mappings to declared sources; it does not create tables
or infer a fallback source. Existing legacy Cloud routing is not promoted.

## Evidence and limits

- 95 V3 tests pass, including numeric execution over multiple mark/target/source
  shapes and renames, calculation results A=18/B=-3, duplicate occurrences,
  stale-plan/catalog rejection, pre-query refusals, nulls below the display cap,
  service outages, sibling isolation, an actual Streamlit chart and explicit
  Snowflake binding/restoration tests.
- Full inherited regression passed before/after converter changes. Logs:
  `cortex_baseline_regression.log`, `cortex_release_regression.log`,
  `cortex_release_v3.log`.
- Live Snowflake Cortex accepted two synthetic-metadata requests: replacement
  selection and calculation expression proposal. A separate read-only Snowflake
  VALUES query using the validated expression returned A=18.0/B=-3.0. Evidence:
  `cortex_live_verification.json`. No objects were created or modified.
- Corpus assessment remains 394 sheet occurrences/25 files: 55 UNVERIFIED,
  333 UNSUPPORTED, 6 INVALID; zero structural issues. Assessment did not invoke
  live Cortex, so it does not measure AI acceptance. Smoke: Superstore 3 mapped,
  17 refused; ecommerce 0 mapped, 50 refused. No parity inferred.
- All 148 recorded parent-source hashes match. No deployment, live Cloud
  end-to-end acceptance, deployed SiS UI verification or Tableau parity.
  The September 30 partner session is not established as ready.

## Next concrete gap

Implement shared typed categorical filter contracts for the existing validated
query plans, including source ownership, multi-select/null behavior and filter
composition. Extend substitutions only when that same contract is executable.
Then verify combined constructs through Cloud intake and explicit Snowflake
bindings; record every component and compare Tableau values/interactions.
Use unfamiliar workbooks as acceptance evidence, never as dispatch profiles.

API reference: [Snowflake AI_COMPLETE structured outputs](https://docs.snowflake.com/en/user-guide/snowflake-cortex/complete-structured-outputs).


## AI00.1 budget checkpoint (2026-10-01)

- Parent/child: E12 and E23 remain OPEN; AI00.1 is IMPLEMENTED_AWAITING_EVIDENCE. AI00 remains OPEN. R00 remains IN_PROGRESS; broader original-source review is incomplete.
- Reproduction: `assist(..., max_sheets=1)` on the renamed calculated-sheet fixture issued 2 mocked AI_COMPLETE requests (calculation plus visual) before this fix.
- Change: `cortex_assist.assist` now exposes `max_calls=20`, counts every attempted calculation/visual proposal request against the shared intake cap, refuses further requests when exhausted, and retains `max_sheets` as a keyword alias.
- Fixture: `tests/test_v3_cortex_assist.py::test_total_request_budget_counts_calculation_and_visual_calls`; renamed two-source fixture, mocked transport. Local render path remains covered by the focused suite.
- Commands/outcomes: inherited `C:\Users\SharathKumarKammari\python.exe tests/test_regression.py` ran before editing; the wrapper did not expose its exit code. The post-edit rerun from V3 (`C:\Users\SharathKumarKammari\python.exe ..\tests\test_regression.py`) exited 0 with `ALL REGRESSION TESTS PASSED`; log: `reports/ai00_1_inherited_regression_final.log`. Focused Cortex suite: 12 tests PASS. V3 test discovery exit 0. Corpus assessment: 25 files, 0 structural errors, 336 unsupported/invalid sheets; parsing does not establish fidelity. Report: `reports/ai00_1_corpus_assessment.json`. The first focused-suite invocation used the wrong import path and failed collection; corrected invocation passed.
- Evidence classes: local mocked request-budget behavior PASS; local render exercised; corpus assessment recorded. Live Cortex, Snowflake execution, Tableau parity, security/access isolation, AI quality/cost beyond local request counts, and whole-workbook acceptance NOT RUN. No live call or deployment.
- Source/build evidence: V3 cortex_assist.py SHA256 `D953CA81ABB3AA426506611A4BD90BF06A53FD43B17DA4D4EA7AE29368AF00DB`; test SHA256 `EEFD893ADC49DA2E5760EC60BC3800E0B2FDE6AC9DEE9E4242913BEBAA5A80E3`; corpus report SHA256 `2C948AF0063AE7900BC04FE4A074AF8927B6CA8FCBC2A653E92EFF313C860C70`. Regression log SHA256 `07ED14D0E9E6CF0060CFBFD103A45D85DC0E4D68EAC14F5C488FB8C81225D39D`. Pre-edit source/test hashes: `C452046FEA9500446CF7283470E1FD60F968ED8A2D4DA6243CBCFFA6C255A7D9` / `6EFCFD4BC97283B26734A7E75303B3BB9A0D45DE5F74D38AEEB83782CD7AB8A7`. Renamed calculated fixture SHA256 `093dfe9927244fdf46035e3e9f0a553d828445781b2e11fcbbe3553ed3550514`; generated test input only, not a production workbook or live source snapshot.
- Actual focused effort not measured; estimates unchanged; consolidated AI-inclusive estimate remains RE-ESTIMATION_PENDING. Deployment: none.
- AI08 evaluation design started: count attempted calls (including failures), calls by proposal type, cap adherence, refusal correctness, deterministic no-AI equivalence, latency/tokens/cost when available, and cross-source isolation. Separate mocked/local measures from live Cortex/Snowflake telemetry; add malformed/adversarial and outage cases before claiming completion.
- Exact next action: continue AI00.1 review of shared request/result schema, provenance/privacy and caller failure handling; flesh out AI08 evaluation harness; continue R00's relevant Cortex/intake/validation review. Keep E03 ingestion-fidelity reproduction in the queue.
