# Cortex proposal contract v1

## Current documentation authority

For the enterprise objective and current implementation state, read [V3_ENTERPRISE_HANDOVER.md](V3_ENTERPRISE_HANDOVER.md), [V3_STATUS.md](V3_STATUS.md), [V3_NEXT_IMPLEMENTATION.md](V3_NEXT_IMPLEMENTATION.md) and [V3_ENTERPRISE_FIX_TRACKER.md](V3_ENTERPRISE_FIX_TRACKER.md). This file retains feature details and dated checkpoint history. Older current/next/latest headings below are historical and do not override those canonical documents. Technical constraints remain applicable unless explicitly superseded by verified implementation. No product readiness claim follows from this documentation update.

## AI03.2 typed dependencies verified and deployed — 2026-10-01

Supersedes earlier deployment/next-action statements below. AI03.2 (E05/E06/E12;
AI00/AI08) IMPLEMENTED_AWAITING_EVIDENCE for typed planning and isolated evaluation.
INCLUDE expands same-source nested calculations, numeric IF conditions and symbolic
integer-parameter DATEADD day expressions. Both Online Retail INCLUDE formulas now
pass source planning (5/8 dependencies). Filtered KPI execution remains NOT_ENABLED;
no worksheet or workbook support/acceptance was promoted.

Verification: baseline/final inherited regression PASS; 164 V3 tests PASS; four same-byte
original/V3 parser comparisons PASS including renamed internal fields/parameters and
independent formulas (outputs retain prior differences; equality not claimed). Corpus
JSON-identical: 25 files, zero structural errors, 336 unsupported/invalid occurrences.
Two final synthetic live Cortex plans matched independent ASTs; four Snowflake numeric
comparisons PASS. Earlier malformed proposals were rejected; prompt now specifies key
expansion, scalar aggregation names and a single row-expression node. Validator was
not relaxed. Nine recorded synthetic calls across debugging/final evaluation; an earlier
aborted connection wait has no usage trace. Zero production metadata/data Cortex calls;
tokens/cost and broad production AI quality remain unverified.

Authorized existing-target deployment PASS: `baa9700eb23a31afd3d4c4c0432090ae05275c29b0d77c1a9b539fe975c00bce`.
All 39 content hashes match; prior 41 live files backed up; runtime/grants unchanged.
Browser startup: PASS. Production numeric/visual/whole-workbook parity
NOT_VERIFIED. E01-E28 OPEN; R00/AI00/AI08 IN_PROGRESS. Effort unmeasured; remaining
estimates RE-ESTIMATION_PENDING. Original/V2 source files were not edited.

Next: typed month/context filters and action-filter semantics, verified data binding and
KPI execution/rendering with independent numeric references. Then LAST/LOOKUP addressing,
relationships/renderers, AI critic/vision, and five-workbook acceptance. Do not connect
the unfiltered evaluation compiler directly to worksheet rendering.
See [AI03.2 checkpoint](reports/ai03_2_20261001/checkpoint.md).

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

Current local implementation: AI00.1b, 2026-10-01. Not deployed or live-qualified.
Parents: E11/E12/E23/E24. AI00.1 and R00 remain IN_PROGRESS. Related evaluation: AI08.1.

## Authority and compatibility

`cortex_assist.request` retains its existing response dictionary (choice, reason,
and expression_json for calculations). `ProposalContext` supplies an optional,
invocation-local request budget and trace. `assist` shares one context across
calculation and visual calls and returns `ai_request_trace` and `ai_usage` in the IR.
The trace/envelope version is `cortex-proposal/1`; prompt version is
`cortex-adaptation-prompt/1`; the existing rendering contract remains
`cortex-adaptation/2`. No alternate migration engine or model SQL execution is added.

A response passing its schema is RESPONSE_VALIDATED, not accepted for migration.
`calculation_plan`, source-expression comparison, `candidate`, `validate`, scoped
bindings and rendering retain semantic authority. A substitution remains PARTIAL
and NOT_PARITY_VERIFIED. Trace fields cannot change those decisions.

## Request and response rules

- Model/task must be nonempty bounded strings; evidence is a dictionary; choices
  are 1-16 unique, nonempty bounded strings, excluding the reserved refusal option.
- The prompt preserves the untrusted-metadata instruction and bound AI_COMPLETE
  parameters. The existing prompt cap is 30,000 characters. No new API or model
  is adopted by this increment; the configured/default model still needs account qualification.
- Exactly one response row with one column is required. Text responses are capped
  at 65,536 UTF-8 bytes before decoding. Object responses are size-checked after
  serialization; this is not a bound on connector/network memory allocation.
- JSON duplicate keys, non-finite constants, extra/missing properties, invalid
  choices and malformed expression JSON are refused. Reason and expression string
  limits remain 4,000 and 12,000 characters respectively.
- A model refusal may leave expression_json empty. Non-refused calculations must
  parse as strict JSON and subsequently equal the independently parsed source AST.
- Service exceptions from SQL submission or collection become CORTEX_UNAVAILABLE.
  No raw exception, SQL text or request payload is copied into that public error.
  INVALID_REQUEST, METADATA_LIMIT, CALL_LIMIT, INVALID_RESPONSE and RESPONSE_LIMIT
  are distinct static refusal codes. Existing deterministic semantic refusals remain.

## Request budget and failure behavior

A context is created per assist invocation; no process-global trace or cross-run
cache is introduced. One reservation occurs immediately before each SQL submission,
including failed submissions/collection. Local preflight rejection and budget
rejection do not increment attempted_calls. Calculations and visual proposals share
the same max_calls cap. Zero disables outbound requests. The prior max_sheets keyword
remains an alias. Cached sheet contracts require no additional request.

There are no automatic retries. A failed or malformed proposal retains the original
worksheet refusal; siblings continue within the remaining quota. Without a Snowflake
session, deterministic components and assessment are retained and no request occurs.

## Locally stamped trace and provenance

Each trace contains schema_version, prompt_version, capability_version, generated
run_id/request_id, input_ir_hash, kind, attempted, status, error_code and elapsed_ms.
After valid preflight it also carries configured model_id, allowed_options,
evidence_hash, scope_hash and request_hash. A valid result adds response_hash.

The request hash includes versions, model, actual prompt/schema and scope. Scope is
hashed from the candidate's source digest and same-source field-catalog digest.
The application stamps these values; model-supplied identity fields are rejected.
Sheet reports link newly attempted/refused request IDs. A cache reuse has no new
request IDs; the earlier request is retained in the same invocation's trace.

Traces omit prompts, source metadata values, model reasons, response bodies and raw
service exceptions. Model reasons remain untrusted display data in the existing
adaptation/report interface. This is targeted trace/error minimization, not a
complete prompt-classification or credential-redaction system.

input_ir_hash identifies the parsed IR supplied to assist; it does not replace an
immutable workbook-byte hash, verified physical binding, source snapshot, signed
provenance, account/role authorization or external evidence store. Those integrations
remain open. Hashes are correlation evidence, not authentication.

## Usage and acceptance limits

ai_usage records version, run identity/input hash, max_calls, attempted_calls,
validated_responses and model_refusals. tokens and cost remain null because this
transport uses show_details=FALSE and measures neither. elapsed_ms is local elapsed
request time, including validation, and does not enforce a timeout. The retained
max_tokens=1024 is a per-request model parameter, not a measured total-token budget.

Account/model availability, residency, grants, two-role isolation, token/time/cost
quotas, durable redacted telemetry, retention, live API compatibility, retries and
whole-workbook acceptance remain unqualified. The original legacy CLI calculation
fallback is a separate open E12 path; its generated-SQL execution is not authorized
or validated by this scoped contract.

## Evidence and continuation

Evidence: reports/ai00_1_contract_20261001/ (before backups/manifests, defect
reproductions, exact same-byte original/V3 parser runs, command/exit-code logs,
corpus result and final manifest). Development tests: tests/test_v3_cortex_contract.py.
Existing Cortex tests continue to cover stale contracts/catalogs and actual local charts.

AI08.1 starts with offline development tests for malformed/oversized responses,
service failure and quotas, trace provenance, adversarial metadata encoding,
AI-disabled equivalence, semantic rejection despite valid shape, and a separate
Square/subtraction/null fixture with renamed captions. These are not frozen holdouts,
live prompt-injection resistance, tenant/role security, measured AI cost, or Tableau parity.

Next dependency-ready action: AI00.1c, inventory the fields sent by calculation and
visual prompts and implement an explicit minimum-metadata projection with tests for
sensitive/unrelated metadata, while preserving the independent semantic validator.
Then add trusted run/binding/account-role provenance and qualified total-token/time
controls. Continue R00 and retain E03 ingestion-fidelity reproduction in the queue.
