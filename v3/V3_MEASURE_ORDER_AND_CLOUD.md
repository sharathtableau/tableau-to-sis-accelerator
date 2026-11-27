# Shared Cloud intake and explicit aggregate ordering — September 23, 2026

## Current documentation authority

For the enterprise objective and current implementation state, read [V3_ENTERPRISE_HANDOVER.md](V3_ENTERPRISE_HANDOVER.md), [V3_STATUS.md](V3_STATUS.md), [V3_NEXT_IMPLEMENTATION.md](V3_NEXT_IMPLEMENTATION.md) and [V3_ENTERPRISE_FIX_TRACKER.md](V3_ENTERPRISE_FIX_TRACKER.md). This file retains feature details and dated checkpoint history. Older current/next/latest headings below are historical and do not override those canonical documents. Technical constraints remain applicable unless explicitly superseded by verified implementation. No product readiness claim follows from this documentation update.

Read [the enterprise execution plan](V3_ENTERPRISE_EXECUTION_PLAN.md) for the
architecture review, implementation sequence and separate acceptance gates.
Neither this milestone nor the corpus establishes enterprise readiness.

## Implemented

`v3_local.prepare_payload` validates downloaded/uploaded bytes through the
existing container validator, assigns a content-hash filename, and uses the
scoped parser. `prepare_cloud` reads via the existing PAT-based Tableau client
and passes bytes to that same boundary. `v3_app.py` now offers Upload and Tableau
Cloud sources. Both lead to assessment, explicit data binding and local preview;
failed Cloud retries clear prior results, and a changed URL cannot show a stale
assessment. No Cloud credentials are included in result provenance.

This fixes a V3 routing gap, not the deployed app. The existing `pipeline_app.py`
Cloud picker still uses the legacy parser. Its onboarding path can create/load
Snowflake objects, so it was not invoked or silently substituted for scoped
intake. Production promotion remains a separate, explicitly authorized task.

`table_plan.py` now emits `text-table/2`. In addition to manual ordering it
supports one row dimension and a visible physical numeric aggregate key with
explicit ASC/DESC from `computed-sort` or `shelf-sort-v2`. Field tokens resolve
only within the existing occurrence set and same datasource; duplicate identical
aggregate occurrences remain distinct display columns. No caption is a key.
All grouped rows are checked for tied aggregate keys and null keys/headers before
display limiting, preventing a tie hidden below row 200 from appearing valid.
Nested/mixed sorts, hidden sort keys/calculations, foreign-source references and
unknown sort options refuse. Missing directions are not guessed. Old table plans
must be reparsed; validation rejects stale plan versions/content.

## Verification

- Baseline: 76 V3 tests and full inherited regression pass, exit 0:
  `enterprise_baseline_v3.log`, `enterprise_baseline_regression.log`.
- Final: 84 V3 tests and full inherited regression pass, exit 0:
  `enterprise_verified_v3.log`, `enterprise_final_regression.log`.
- Five measure-sort tests cover actual DuckDB SUM/AVG ordering, both XML forms,
  ASC/DESC, source/label/declaration changes, negatives, >200 groups, ties/nulls
  outside the display slice, healthy sibling rendering, static no-SQL refusals
  and stale-plan rejection. No Tableau parity is inferred.
- Three Cloud-intake tests exercise actual scoped parsing of XML and packaged
  bytes, identical upload/Cloud semantic results under different labels,
  malformed-payload refusal and the real Streamlit assessment/preview workflow.
  The preview emits a real chart from bundled fixture data; failed retry clears
  the prior result. Transport is MOCKED. Live Cloud authentication/download,
  Tableau parity and Snowflake execution are NOT_VERIFIED.
- Corpus: 25 files, 394 occurrences; unchanged 55 UNVERIFIED / 333 UNSUPPORTED /
  6 INVALID, zero structural issues (`enterprise_corpus_assessment.json`).
  No new corpus sheet is accepted; no migration gain is claimed from these counts.
- Scoped smoke remains Superstore 3 mapped / 17 refused; E-Commerce 0 mapped /
  50 refused (`enterprise_smoke.log`).
- All 148 parent-source snapshot hashes match. Replacements compare current V3
  hashes before writing. `enterprise_verification_summary.json` records evidence
  categories separately. No deployment or complete-workbook acceptance.

## Next

Implement a shared typed physical categorical filter plan and consume it in
table queries: include/exclude, typed/null/empty semantics, pre-aggregation WHERE
phase and explicit datasource/target ownership. Keep context/top-N/aggregate/
table-calculation/action filters refused until separately planned. Test the
filter/order/limit combination across source structures and healthy siblings.
Then expand complete construct combinations and validate whole Cloud workbooks;
do not treat isolated fixtures as the product acceptance unit.
