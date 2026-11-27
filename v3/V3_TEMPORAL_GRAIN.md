# Continuous temporal chart gap — 2026-09-22

## Current documentation authority

For the enterprise objective and current implementation state, read [V3_ENTERPRISE_HANDOVER.md](V3_ENTERPRISE_HANDOVER.md), [V3_STATUS.md](V3_STATUS.md), [V3_NEXT_IMPLEMENTATION.md](V3_NEXT_IMPLEMENTATION.md) and [V3_ENTERPRISE_FIX_TRACKER.md](V3_ENTERPRISE_FIX_TRACKER.md). This file retains feature details and dated checkpoint history. Older current/next/latest headings below are historical and do not override those canonical documents. Technical constraints remain applicable unless explicitly superseded by verified implementation. No product readiness claim follows from this documentation update.

## Fix implemented

The time-series SQL renderer omitted Tableau's continuous-year (`tyr`) and
continuous-quarter (`tqr`) tokens from its date-grain map. Both silently fell
through to MONTH. `engine._timeseries_one` now uses YEAR and QUARTER respectively.
Existing continuous-month (`tmn`) and continuous-day (`tdy`) behavior is retained.
No workbook filenames, sheet names or field captions control this correction.

`worksheet_semantics.temporal_projection` adds a validated occurrence adapter for
single-pane continuous-date line and area charts. It requires one physical
date/datetime field on Columns and one continuous aggregate on Rows, checks
datasource ownership and exact date-grain agreement with the renderer, and
accounts for every occurrence. Colored lines may use one physical row-level
dimension; line labels must explicitly match the aggregate. Scalar areas are
supported without authored labels or color stacking.

An explicit, otherwise unadorned `<aggregation value="true"/>` is accepted as
grouped behavior. False or unfamiliar aggregation settings are refused. Fiscal
attributes and fiscal XML elements are retained as temporal context and refused
until a calendar plan exists. Existing occurrence schema remains v3; reparse old
IR to populate temporal context and regenerate its projection.

The scoped date adapter refuses discrete dates/date parts, weeks, calculated date
axes, multiple date pills, panels, derived/forecast layers, unsupported axis
settings, cross-datasource occurrences, table calculations, area color stacking,
extra encodings, and stale/missing temporal contracts. The legacy parser entry
point retains its compatibility behavior; the stricter adapter is scoped V3.

## Verification

`tests/test_v3_temporal.py` executes real DuckDB queries and inspects chart data:

- Line/SUM with category colors and scalar area/AVG on two separate datasources.
- Year, quarter and month grouping across 2024/2025 boundaries; repeated rows and
  distinct Januarys prove that years are neither merged nor silently monthly.
- Day grouping with multiple observations on the same day.
- Renamed captions and reversed declarations, explicit aggregate-view settings.
- Refusal of discrete/week/fiscal/calculated-date/extra-detail/disaggregated
  variants, with a healthy sibling still rendering.
- A modified date grain invalidates the stored projection before rendering.

Evidence: `temporal_baseline_regression.log`, `temporal_baseline_v3.log`,
`temporal_v3_tests.log`, `temporal_verified_regression.log`,
`temporal_corpus_assessment.json`, and `temporal_corpus_render.log`.
All 59 V3 tests pass. The full inherited regression passed before and after,
including the final `temporal_verified_regression.log` run (exit code 0).

Scoped corpus coverage increased by three sheet occurrences: UNVERIFIED rose
from 49 to 52, UNSUPPORTED fell from 339 to 336, and INVALID remained six across
394 occurrences / 25 files. Structural errors remain zero. This is sheet-level
coverage, not three successfully migrated workbooks or held-out evidence.

The newly accepted sheets are Measure Trend in `Superstore_TopN_MeasureSwap.twbx`,
Profit Trend in `Workbooks/Globalsalesdashboard.twbx`, and Measure Trend in
`Workbooks/Superstore_MeasureSelector_Native.twbx`. Each produced one local chart
without blockers using its bundled extract, a neutral hash-derived upload name,
and the isolated V3 preview context. See `temporal_gains_render.json` and
`temporal_gains_render.log`; rerun with `python verify_temporal_gains.py .`.
These checks do not establish Tableau numerical/visual parity. The standard
Superstore/E-Commerce smoke counts remain three/zero mapped sheets respectively.
All 148 original root snapshot hashes matched at the verification check.

## Limits and next gaps

This closes the year/quarter fallback defect and implements a bounded temporal
adapter. It does not establish complete temporal-chart support or arbitrary
workbook conversion. The adapter still bridges the existing scoped renderer;
it does not replace the inherited filter, relationship or calculation compiler.
SQL/data tests are not Tableau visual, interaction or Snowflake parity checks.
No deployment or Cortex call is part of this fix.

Continue with actual converter fixes: discrete date-part versus date-value axes,
temporal panels and area stacking require separate contracts. Typed filters,
calculation planning, tables and relationships remain open. Track each bounded
fix and its evidence in `V3_GAP_LEDGER.md`; do not mark an entire feature family
complete because one supported subset works.
