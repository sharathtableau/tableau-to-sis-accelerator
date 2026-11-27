# INCLUDE typed calculation planning — AI03.1

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

## Contract and scope

include-lod/1 parses a single INCLUDE expression with explicit keys and one inner
SUM/AVG/MIN/MAX. Keys are unique physical same-source fields or DAY/MONTH/YEAR of a
physical date. The body reuses cortex_assist.calculation_plan: numeric physical-field
arithmetic, ABS and ZN, with bounded depth/size. Outer aggregation and physical view
dimensions are explicit. Included and view keys are combined without duplicate keys.
Nested calculations, parameters, computed dates, division, windows and other syntax
remain refused. Original calc_translator._rewrite_lod correctly refuses INCLUDE without
pane context; it was not changed or bypassed. New plans use its missing context explicitly.

The source parser produces a typed array. Cortex receives the formula, only referenced
field IDs/internal names/types, explicit view dimensions, outer aggregate and grammar.
No unrelated metadata, captions, data rows, worksheet XML or physical bindings are sent.
This is minimization, not anonymization: source formulas and referenced IDs remain.
Cortex's JSON is strictly parsed and compared to the independently derived array.
A schema-valid transport response does not mean a semantically validated plan.
Dependency hashes detect source/catalog changes before evaluation compilation.

Intake adds ai_include_plans beside existing ai_assistance, using the SAME ProposalContext
and total request limit. PLAN_VALIDATED always retains execution NOT_ENABLED. The UI
separates planning from conversion. Original sheet dictionaries, calcs, support states
and acceptance stay unchanged. The existing model SQL execution fallback is not used.

## Two-stage numeric semantics

The unfiltered evaluation compiler groups by view keys plus included keys, calculates
the inner aggregate, then reaggregates those results at view grain. For customer totals
4 and 10, AVG returns 7; averaging their three input rows would incorrectly return 14/3.
Null groups, all-null measures, empty inputs, overlapping keys, distinct view partitions,
date components across years and arithmetic are tested. Physical identifiers are quoted;
table identifiers are constrained. No model-generated SQL is executed.

compile_unfiltered_sql is an evaluation helper. Runtime source equivalence, datasource
filters, context/dimension/measure filter phases, dynamic parameters, actions and renderer
integration are NOT established by it. It must not be wired directly into engine dispatch.

Tableau documents INCLUDE as adding specified dimensions to the view grain, followed by
reaggregation; dimension filters apply before INCLUDE:
https://help.tableau.com/current/pro/desktop/en-us/calculations_calculatedfields_lod_include.htm
https://help.tableau.com/current/pro/desktop/en-us/calculations_calculatedfields_lod_filters.htm

## Production and next implementation

The frozen Online Retail formulas use YEAR/MONTH/DAY of a DATEADD result controlled by a
parameter. One body references an aggregate calculation; its numeric inputs and the other
formula use IF conditions. The current contract deliberately rejects these transitive
forms before any Cortex call. That records an exact dependency gap, not a repaired sheet.

AI03.2 must independently parse and type those dependencies, validate current parameter
values and filter phases, and bind a dedicated KPI execution adapter before any support
promotion. Use renamed and independently shaped examples, same-snapshot references and
malformed/cyclic/cross-source negative cases. LAST/LOOKUP remain separate ordered plans.
