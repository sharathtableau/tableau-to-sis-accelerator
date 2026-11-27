# V3 occurrence and declared-grain boundary — 2026-09-22

## Current documentation authority

For the enterprise objective and current implementation state, read [V3_ENTERPRISE_HANDOVER.md](V3_ENTERPRISE_HANDOVER.md), [V3_STATUS.md](V3_STATUS.md), [V3_NEXT_IMPLEMENTATION.md](V3_NEXT_IMPLEMENTATION.md) and [V3_ENTERPRISE_FIX_TRACKER.md](V3_ENTERPRISE_FIX_TRACKER.md). This file retains feature details and dated checkpoint history. Older current/next/latest headings below are historical and do not override those canonical documents. Technical constraints remain applicable unless explicitly superseded by verified implementation. No product readiness claim follows from this documentation update.

Current next task: [executable per-pane query and rendering plans](V3_NEXT_IMPLEMENTATION.md). Independent side-by-side panes come first; dual-axis overlays remain deferred. The existing occurrence/inheritance model must be reused, not rebuilt.

Superseding increment: `V3_PANE_ADAPTERS.md` documents occurrence schema v2,
declared inheritance, a strict multi-measure adapter, 45 tests and unchanged
corpus coverage. The text below records the first occurrence milestone.

## Result and scope

The first occurrence-IR increment is implemented in the scoped V3 parser. It
preserves individual shelf/encoding references and refuses sheets whose legacy
renderer cannot account for them. It is an initial subset of handoff Milestone 1,
not a completed Tableau semantic compiler or a completed generalization milestone.

**Preview coverage is substantially narrower under this strict gate.** Of 394
sheet occurrences in the 25-file assessment, 339 are UNSUPPORTED, 6 INVALID and
49 UNVERIFIED. Twenty-four files are BLOCKED and one NEEDS_REVIEW. Many refusals
mean an adapter is not yet implemented; they do not prove the original chart was
wrong. No numerical or visual parity has been inferred from these states.

The inherited `build_ir(path)` compatibility path is unchanged. The V3 app and
assessor use `build_ir(path, scoped=True)` and enforce the new gate. The difference
is deliberate and must remain visible in all progress reporting.

## Code and integration

- `worksheet_semantics.py` extracts `worksheet-occurrences/1` into each sheet's
  `worksheet_semantics` key. It includes pane IDs, pane ordering, mark classes,
  axis selectors, positional order, field IDs, datasource ownership, aggregation,
  discrete state, role, date part, evaluation level, table-calc XML, and source
  paths. Worksheet XML and filter/sort/fiscal element evidence are retained.
- `tableau_parser.build_ir` attaches this model after existing enrichment,
  including drill and measure-color metadata. Attaching it earlier produced stale
  projections when drill metadata was subsequently added; the final integration
  and a dedicated test address this.
- `ir_contract.sheet_issues` checks occurrence ownership/references, pane identity,
  grain consistency, complete renderer projection, and support state. This runs
  before code generation and through the existing sheet runtime boundary.
- Runtime bundle lists include the new module. No deployment occurred.
- No engine, backend or translator implementation was changed in this increment.

Occurrence IDs are local to a worksheet and derived from semantic tokens, channel,
pane identity and duplicate ordinal. Captions and worksheet display names do not
participate. Position is preserved separately, so reordering distinct XML
declarations does not turn them into different fields. These are not guaranteed
global Tableau worksheet identities. Indistinguishable duplicated unnamed nodes
cannot have independently stable identities when exchanged; that remains a
documented limit.

## Grain and projection behavior

Each pane has declared grouping keys, aggregate occurrences, generated display
occurrences and deferred occurrences. A discrete SUM/AVG pill remains an aggregate
and does not become a row grouping key. Axis selectors are retained independently
from their shelf occurrences.

The grain state is `DECLARED_ONLY`; filter order and relationships are explicitly
`NOT_COMPILED`. This model describes requirements and detects lost information.
It does not yet generate a query or prove LOD/window/filter correctness. Known
window/scalar-subquery SQL is deferred, including nested translated LOD cases.

The initial renderer adapters accept:

- Simple single-pane bars whose dimension axis, aggregate axis, optional row-level
  color and same-measure label occurrences all match the existing sheet spec.
- Scalar KPI sheets with explicit aggregate Text occurrences.
- Scalar KPI Measure Values containers with explicit positive Measure Names
  member lists. Each authored member is expanded as its own occurrence, preserving
  its internal field ID and aggregation. The generator does not infer members
  from every datasource declaration. More than seven measures are refused because
  the current KPI renderer caps its output at seven.

Multiple panes remain distinct but require an explicit layer plan before rendering.
This includes Tableau's shared All marks card: its inheritance behavior is not
yet modeled. Conditional/exclusion-based Measure Names selections, generated
geographic fields, dynamic drill grain, table calculations, unknown encodings,
missing references, unconsumed occurrences and unsupported renderer kinds produce
an explicit per-sheet refusal. Existing invalid calculation findings are retained.

## Verification

- `occurrence_baseline_regression.log`: inherited full suite passed before edits.
- `occurrence_baseline_v3.log`: 28 existing V3 tests passed before edits.
- `occurrence_v3_tests.log`: 39 V3 tests passed, including 11 occurrence tests.
- `occurrence_final_regression.log`: complete inherited suite passed on the initial
  integration. `occurrence_verified_regression.log` records the final rerun after
  Measure Values expansion; it passed with exit code 0.
- `occurrence_corpus_assessment.json`: zero structural IR errors across 25 files;
  24 BLOCKED, one NEEDS_REVIEW. Counts include related workbook versions.
- `occurrence_corpus_render.log`: Superstore has 3 mapped sheets and 17 explicit
  refusals; E-Commerce has 50 explicit refusals. Refused sheets produce blockers
  and no chart/metric output. This verifies refusal mechanics, not broad renderer
  success. The prior milestone's 69-rendered/1-refused count no longer describes
  the scoped V3 preview.
- The 148 original files recorded in `source_snapshot.json` still match their
  SHA-256 hashes. Changes remain inside the isolated V3 child project.

Focused tests vary bar/KPI shapes, datasource field ownership, caption/sheet names,
declaration order, repeated SUM/AVG occurrences, discrete aggregates, pane order,
axis selectors, table-calculation evidence, Measure Values on rows/columns,
LOD requirements, unknown marks, stale/tampered contracts, and late drill metadata.
They also exercise a healthy sibling chart alongside an unconsumed-detail refusal.
Existing tests continue to cover numeric datasource isolation and actual app upload.

Run from the V3 folder:

```powershell
& 'C:\Users\SharathKumarKammari\python.exe' -m unittest discover -s tests -p 'test_v3_*.py'
& 'C:\Users\SharathKumarKammari\python.exe' tests/test_v3_occurrences.py --smoke-corpus
& 'C:\Users\SharathKumarKammari\python.exe' assess_v3.py --output occurrence_corpus_assessment.json
& 'C:\Users\SharathKumarKammari\python.exe' -u tests/test_regression.py
```

## Immediate continuation

1. Extend source evidence extraction for view aggregation, fiscal/date attributes,
   Measure Values variants, path/order encodings, and full table-calc metadata.
2. Model shared All-card inheritance and pane-specific axes/encodings. Preserve
   each layer and test different marks and aggregations across multiple shapes.
3. Add explicit adapters for table, multi-measure bar, line/area and dual/layered
   output. Every occurrence must map to an executed renderer role or a visible
   unsupported reason; do not relax the gate to recover a green coverage count.
4. Replace caption slots with occurrence IDs and compile query grain, rather than
   only comparing the model to the legacy spec. Complete filter/relationship and
   typed-calculation planning in subsequent milestones.
5. Maintain a separate scoped corpus acceptance run. The inherited suite exercises
   compatibility behavior and cannot establish scoped correctness.

Cortex is still deferred until this evidence and deterministic validation boundary
is sufficiently complete to test its proposed mappings. No Cortex call, Snowflake
deployment, or live Tableau parity check was performed in this increment.
