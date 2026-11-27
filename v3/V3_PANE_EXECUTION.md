# First executable independent-pane subset — 2026-09-22

## Current documentation authority

For the enterprise objective and current implementation state, read [V3_ENTERPRISE_HANDOVER.md](V3_ENTERPRISE_HANDOVER.md), [V3_STATUS.md](V3_STATUS.md), [V3_NEXT_IMPLEMENTATION.md](V3_NEXT_IMPLEMENTATION.md) and [V3_ENTERPRISE_FIX_TRACKER.md](V3_ENTERPRISE_FIX_TRACKER.md). This file retains feature details and dated checkpoint history. Older current/next/latest headings below are historical and do not override those canonical documents. Technical constraints remain applicable unless explicitly superseded by verified implementation. No product readiness claim follows from this documentation update.

## Implemented boundary

Occurrence schema is now `worksheet-occurrences/3`; reparse older saved IR.
`worksheet_semantics.py` compiles an `independent-panes/1` plan for multi-pane
sheets. Plans retain occurrence roles, grouping/aggregate IDs, datasource identity,
axis order, source paths, inheritance and explicit occurrence dispositions.
Validation recomputes plans from evidence and rejects missing/stale plans.
Malformed XML evidence becomes an IR validation issue.

The new engine route queries and renders from these plans, without using legacy
measure/dimension slots, curated measure formulas or caption-based dispatch.
Field IDs resolve physical columns directly from the configured catalog; runtime
checks reject changed field definitions and mismatched datasource bindings.
SQL selects the planned dimension and aggregate with GROUP BY and no arbitrary
row cap. Runtime filters are refused pending a typed filter plan.

The initial supported subset is deliberately small:

- Exactly one declared physical table and an explicit runtime datasource binding.
- Distinct slash-separated column pills, one matching pane per axis, one discrete
  row dimension. Pane order follows authored shelf order rather than XML order.
- Physical numeric SUM/AVG/MIN/MAX/COUNT/COUNT DISTINCT fields on continuous axes.
- Bar/Automatic panes; inherited or overridden color only when it is the same
  physical grouping field. Other color grain/stacking is not compiled.
- No authored filters/sorts/fiscal/style/axis settings, explicit view aggregation
  setting, dates, calculations, dynamic grain, joins, relationships or overlays.

An unsupported independent pane emits a BLOCKER finding and a visible warning;
healthy independent panes continue. Query/render exceptions have the same local
containment. Shared topology/datasource constraints block the whole dependent
group. Partial sheets retain PARTIAL status, never a verified workbook verdict.
Legacy single-pane adapters and the compatibility parser entry point remain.

## Verification

The six new tests in `tests/test_v3_pane_execution.py` cover:

- Actual DuckDB/chart SUM/AVG/MAX values for two- and three-axis layouts across
  two datasource identities, with duplicate rows and negative values.
- Renamed captions, reversed pane order, inherited/overridden dimension colors,
  and deliberately corrupted legacy slots that the new path must ignore.
- An unsupported mark or injected SQL error alongside a healthy numeric sibling.
- Stale plans, malformed evidence, runtime filters, joins, unsupported shelf
  algebra, indexed axes, calculations, changed color grain and view aggregation.

Evidence logs: `panes_baseline_regression.log`, `panes_baseline_v3.log`,
`panes_v3_tests.log`, `panes_final_regression.log`, and final verification in
`panes_verified_regression.log`. Both baseline and final full regressions passed
with exit code 0. The prior baseline had 45 V3 tests; all 51 final V3 tests pass.

`panes_corpus_assessment.json` has zero structural errors: 49 UNVERIFIED,
339 UNSUPPORTED, six INVALID across 394 sheet occurrences / 25 files.
24 files remain BLOCKED and one NEEDS_REVIEW. Scoped smoke remains Superstore
three mapped / 17 refused and E-Commerce zero mapped / 50 refused
(`panes_corpus_render.log`). This is unchanged coverage, not Tableau parity.
The 148 original root snapshot hashes matched at verification. No deployment,
Cortex call or live Tableau visual sign-off occurred.

## Limits and continuation

This is a physical-field independent-bar compiler, not a completed Tableau query
planner. Its source-shape recognition is conservative and not proof of arbitrary
workbook safety. Source actions/interactions, complete mark/style semantics,
aliases/format fidelity and broader shelf algebra still need explicit contracts.
Synthetic fixture proofs do not establish live Tableau visual parity.

Continue by adding typed filter and calculation planning to this executable
boundary, with explicit refusals until each construct is proved. Then add date
adapters separating continuous dates from discrete parts, and table adapters
correcting limit-before-sort and duplicate labels. Additional color grouping,
stacking, dual axes, relationships and table calculations need their own plans.
Do not loosen the gate to recover corpus counts. Cortex proposals remain deferred
until deterministic plan validation and independent numeric checks can govern them.
