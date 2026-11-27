# Scoped date-bar contract — September 22, 2026

## Current documentation authority

For the enterprise objective and current implementation state, read [V3_ENTERPRISE_HANDOVER.md](V3_ENTERPRISE_HANDOVER.md), [V3_STATUS.md](V3_STATUS.md), [V3_NEXT_IMPLEMENTATION.md](V3_NEXT_IMPLEMENTATION.md) and [V3_ENTERPRISE_FIX_TRACKER.md](V3_ENTERPRISE_FIX_TRACKER.md). This file retains feature details and dated checkpoint history. Older current/next/latest headings below are historical and do not override those canonical documents. Technical constraints remain applicable unless explicitly superseded by verified implementation. No product readiness claim follows from this documentation update.

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

Latest September 23 checkpoint: read V3_STATUS.md and V3_VISUAL_PLANS.md first.
132 tests pass; visual repairs are deployed. Complete migration remains blocked
by partial crosstab ordering. Older counts and next-step text below are historical.

This increment implements single-pane scalar date bars in isolated V3. It does
not establish arbitrary-workbook support, Tableau parity, or partner readiness.

## Defect and implementation

The legacy `r_dtbar` query path extracted years but otherwise used truncated
dates, and selected temporal axes for non-year bars. That cannot represent a
discrete month-of-year correctly: January across different years belongs to one
date-part group, whereas truncated calendar months must retain the year.
Scoped V3 previously refused all date bars through its occurrence adapter.

`worksheet_semantics.temporal_projection` now accepts explicit Bar marks with
one physical date/date-time column axis and one aggregate row axis. It accounts
for every occurrence and validates fiscal metadata, ownership and calendar
grain. It reuses the existing date-axis plan: DATE_PART for discrete year,
quarter, month and day components; DATE_TRUNC for full year/quarter/month/day
values. Full date values can have ordinal or temporal axes according to the
authored pill. Validation recomputes the projection before rendering.

Scoped `r_dtbar` uses `_timeseries_one(..., bar=True)` to consume this plan and
emit an unstacked Bar mark. The legacy unscoped path remains compatible. This
change has no workbook-name, worksheet-name or field-caption allowlist.

Colored/stacked bars, labels, extra encodings, multiple axes/panels, week/fiscal
and exact-date semantics still require separate contracts. Null date groups
refuse visibly rather than silently disappearing; another sheet can render.
This increment still uses the existing scoped measure/query/filter execution
path. It does not implement the pending typed calculation/filter/relationship
compiler or prove those paths semantically complete.

## Verification

- Baseline: 63 V3 tests and full inherited regression passed.
- Final: 66 V3 tests passed (`datebar_v3_tests.log`). The new tests execute
  local queries and inspect numeric aggregates, grouping keys, Bar mark type,
  axis types and absence of stacking across 12 calendar-axis combinations,
  renamed/reordered worksheet/source definitions and two separate datasources.
- Refusal tests cover color, text, detail, weeks, fiscal calendars, custom
  ordering and a tampered plan; unsupported sheets do not query. Runtime tests
  cover null dates and continued rendering of a healthy sibling.
- Full pre/post regression logs: `datebar_baseline_regression.log` and
  `datebar_verified_regression.log`: both passed, exit 0.
- Corpus: 25 files / 394 sheet occurrences; 55 UNVERIFIED, 333 UNSUPPORTED,
  six INVALID, zero structural validation issues. File states remain 23 BLOCKED
  and two NEEDS_REVIEW. `datebar_corpus_assessment.json` stores the evidence.
- The only new accepted occurrence is `_filtest.twb / Dashboard 1 / Sheet 5`.
  `datebar_gains_render.json` records NOT_VERIFIED: the workbook has no bound
  `Sample - Superstore (2)` datasource. No data was guessed or substituted.
- Scoped smoke remains Superstore 3 mapped / 17 refused and E-Commerce 0 mapped
  / 50 refused (`datebar_corpus_render.log`). These are not parity results.
- All 148 recorded parent-source hashes remained unchanged. No deployment,
  Cortex execution, Tableau comparison or Snowflake runtime verification.

## Next implementation

Implement a bounded table/text occurrence-to-query plan. Start with one source,
explicit physical dimension rows and explicit aggregate text measures; preserve
duplicate field roles using occurrence IDs. Sort before any display limit and
make truncation explicit. Refuse unsupported pivots, totals, table calculations,
relationships and ambiguous orderings before SQL. Use independent numeric
fixtures with duplicate captions, nulls, repeated fields and multiple source
orders. Do not enable the legacy table renderer wholesale. Typed filter phases,
calculation evaluation levels and relationship fan-out remain major follow-ups.
