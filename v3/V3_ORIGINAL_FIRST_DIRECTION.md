# Original-first direction — AI-assisted V3

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

Updated 2026-09-27.

The company goal is a production AI-assisted Tableau-to-Streamlit-in-Snowflake accelerator for varied, previously unfamiliar workbooks. The original accelerator's complete end-to-end functionality remains the baseline. Correct generic original defects and V3 regressions in V3; preserve working capabilities, structural identity, semantic validation, explicit refusals and independent evidence.

AI now participates explicitly in assessment, datasource mapping, calculation conversion, semantic-model generation, chart adaptation, validation diagnosis, migration assistance and quality evaluation. AI proposes; verified bindings, typed plans, numeric checks and Tableau/UI evidence decide acceptance.

Work only in V3. Preserve the parent original, V2, concurrent changes and existing deployments. No deployment without explicit authorization. Never dispatch by workbook/sheet/filename/curated caption or known values. Do not execute model SQL/Python or enable blanket legacy fallback.

Use C:\Users\SharathKumarKammari\python.exe. Run git status --short before edits; hash-check destinations immediately before replacement. For converter changes run tests/test_regression.py before/after, V3 tests and relevant corpus/render checks. Add renamed and independently shaped fixtures. Keep numeric, local-render, live Cortex, Snowflake, Tableau, security, AI-quality/cost and whole-workbook evidence separate.

## Preserve and compare

Before any capability replacement compare original and V3 using identical bytes and source state where available. Trace intake, discovery, verified binding, queries, rendering, interactions, layout and export separately. Reuse generic original behavior only with validated semantics; correct demonstrated original defects and V3 regressions. No narrower AI-only or V3-only path may silently remove original coverage.

The HR workbook is one reproduction; see V3_HR_DIFFERENTIAL.md. Parser representation is not execution proof, and its authored Snowflake candidate remains unverified. Do not derive product rules from its names.

## Read next

1. V3_ORIGINAL_FIRST_DIRECTION.md
2. V3_ENTERPRISE_PRODUCT_GOAL.md
3. V3_SESSION_MEMORY.md
4. V3_PRODUCT_ROADMAP.md
5. V3_AI_IMPLEMENTATION_PLAN.md
6. V3_ENTERPRISE_FIX_TRACKER.md
7. V3_ORIGINAL_CODE_ENTERPRISE_GAPS.md
8. V3_SNOWFLAKE_COMPONENT_STRATEGY.md and V3_SNOWFLAKE_TEAM_BRIEF.md
9. Current V3_STATUS.md, V3_GAP_LEDGER.md, V3_GENERALIZATION_HANDOVER.md, V3_NEXT_IMPLEMENTATION.md, V3_ENTERPRISE_EXECUTION_PLAN.md, V3_ENTERPRISE_ARCHITECTURE_AUDIT.md and V3_FIX_JOURNAL.md.
10. Relevant feature contracts, differential and actual source/evidence.

## Next work

Resume R00's relevant Cortex/intake/validation path review, then AI00.1: validate the shared proposal contract and reproduce total AI-call budget behavior before selecting a code correction. Start AI08 evidence design with that work. Keep E03 ingestion fidelity and other dependency-ready original correctness repairs active in the queue. R00's broader unreviewed coverage remains outstanding.

## Preserved history

Complete pre-revision document: [V3_ORIGINAL_FIRST_DIRECTION.md](enterprise_product_audit_20260927/ai_roadmap_20260927_230202_774981/V3_ORIGINAL_FIRST_DIRECTION.md). Historical claims are evidence for their recorded builds only; current work follows this document and the tracker.
