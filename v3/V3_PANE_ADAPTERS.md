# V3 pane inheritance and multi-measure adapter — 2026-09-22

## Current documentation authority

For the enterprise objective and current implementation state, read [V3_ENTERPRISE_HANDOVER.md](V3_ENTERPRISE_HANDOVER.md), [V3_STATUS.md](V3_STATUS.md), [V3_NEXT_IMPLEMENTATION.md](V3_NEXT_IMPLEMENTATION.md) and [V3_ENTERPRISE_FIX_TRACKER.md](V3_ENTERPRISE_FIX_TRACKER.md). This file retains feature details and dated checkpoint history. Older current/next/latest headings below are historical and do not override those canonical documents. Technical constraints remain applicable unless explicitly superseded by verified implementation. No product readiness claim follows from this documentation update.

Current next task: [executable per-pane query and rendering plans](V3_NEXT_IMPLEMENTATION.md). Independent side-by-side panes come first; dual-axis overlays remain deferred. The existing occurrence/inheritance model must be reused, not rebuilt.

## Implemented

`worksheet_semantics.py` now emits occurrence schema `worksheet-occurrences/2`.
Reparse older saved IR; version 1 is deliberately rejected by this validator.
The raw pane/occurrence inventories remain intact. A unique axis-free marks card
alongside axis-specific cards is recorded as a shared-card candidate. Effective
panes retain inherited encoding IDs, child overrides, shadowed source IDs, and
excluded measure-axis IDs. Color/size/text/shape have exclusive-channel override
policies; other inherited channels produce an explicit planning reason. Missing
child marks inherit the shared mark; explicitly authored marks win.

Declared grain is derived for effective panes, including inherited grouping
fields and the selected aggregate axis. The validator recomputes inheritance,
grain and projection rather than trusting serialized plans. Ambiguous shared
cards and missing/ambiguous selectors remain visible. This is source modeling,
not executable layered query planning: every multi-pane sheet still refuses.
Index-based axes, axis synchronization, stacking and complete marks-card semantics
are not implemented. Do not describe the All-card feature as complete.

The new multi-measure bar adapter accepts one Bar/Automatic pane, one row
dimension and two or more explicit continuous aggregate column pills. Measures
must match their authored positions, field IDs and aggregations. It rejects
vertical output, discrete aggregate headers, color/stacking, extra dimensions,
unconsumed encodings, generated fields, dates, dynamic grain and table calcs.
The legacy renderer's horizontal small multiples are used only for this subset.

`engine.py` includes negative/zero aggregate values in multi-measure bar domains.
Scoped multi-measure rendering also bypasses the legacy caption-pattern shortcut
that otherwise turns fields named like `AVG(0)` into a member list. Field identity
and validated occurrences, rather than that caption string, control this path.
Other legacy heuristics still exist and must be audited as adapters expand.

## Evidence and limits

- 45 V3 tests pass (`adapters_v3_tests.log`), six more than the prior checkpoint.
- Numeric tests execute DuckDB and inspect chart data for two-axis SUM/AVG and
  three-axis physical/calculated measures, with physical/calculated grouping
  fields, separate datasource IDs, renamed captions and reordered measures.
- Tests cover mixed-sign/all-negative data, formula-like display captions,
  inheritance, child override, pane reordering, per-pane aggregate grain,
  corrupted inheritance and runtime refusal of unsupported shapes.
- Full inherited regression passed before edits (`adapters_baseline_regression.log`)
  and after initial changes (`adapters_final_regression.log`). Final safeguard
  verification passed with exit code 0 (`adapters_verified_regression.log`).
- `adapters_corpus_assessment.json`: 25 files, 394 sheet occurrences, zero
  structural errors; 49 UNVERIFIED, 339 UNSUPPORTED, six INVALID. File states:
  24 BLOCKED, one NEEDS_REVIEW. Corpus coverage did not increase.
- `adapters_corpus_render.log`: Superstore three mapped / 17 refused;
  E-Commerce zero mapped / 50 refused. These are mechanics checks, not parity.
- The 148 original root snapshot hashes were unchanged at verification.

No Tableau visual sign-off, Snowflake deployment, Cortex call, or broad workbook
support is implied. Existing corpus multi-measure charts still need additional
plans; the new subset is proved by synthetic structural fixtures. Filter order,
relationships, fiscal/date semantics and typed calculations remain unfinished.

## Next implementation

Current next task: [executable per-pane query and rendering plans](V3_NEXT_IMPLEMENTATION.md). Independent side-by-side panes come first; dual-axis overlays remain deferred. The existing occurrence/inheritance model must be reused, not rebuilt.

Use that specification for the ordered implementation, pane-level failure containment, numeric acceptance criteria, subsequent temporal/table work, verification commands and Cortex constraints. This milestone records declared inheritance only; executable layered plans are not implemented.
