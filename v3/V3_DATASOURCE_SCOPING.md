# V3 datasource identity increment — 2026-09-21

## Current documentation authority

For the enterprise objective and current implementation state, read [V3_ENTERPRISE_HANDOVER.md](V3_ENTERPRISE_HANDOVER.md), [V3_STATUS.md](V3_STATUS.md), [V3_NEXT_IMPLEMENTATION.md](V3_NEXT_IMPLEMENTATION.md) and [V3_ENTERPRISE_FIX_TRACKER.md](V3_ENTERPRISE_FIX_TRACKER.md). This file retains feature details and dated checkpoint history. Older current/next/latest headings below are historical and do not override those canonical documents. Technical constraints remain applicable unless explicitly superseded by verified implementation. No product readiness claim follows from this documentation update.

Current next task: [executable per-pane query and rendering plans](V3_NEXT_IMPLEMENTATION.md). Independent side-by-side panes come first; dual-axis overlays remain deferred. The existing occurrence/inheritance model must be reused, not rebuilt.

Implemented only in this isolated folder. No Snowflake deployment. The original
148-file root source snapshot was rechecked with SHA-256: no drift.

## Problem and change

The inherited parser merged column metadata by internal name across the entire
workbook. Calculation dictionaries merged by display caption. A sheet could
therefore use metadata or SQL belonging to another datasource, depending on XML
order. Dashboard filters also deduplicated by caption and could share values.

`field_identity.py` builds a catalog keyed by datasource ID and internal field
name. Top-level declarations are authoritative; worksheet dependency copies fill
missing declarations. Each datasource compiles its own nested calculations.
Captions remain display labels, and renaming them does not change field IDs.

The parser attaches field IDs to measure/encoding/filter references and retains
sheet-local bindings for legacy string slots. The renderer enters a scoped
context for each sheet and restores its maps in `finally`. Explicit foreign
measure references require a relationship plan. Ambiguous unscoped lookups raise
instead of taking the old workbook-wide definition.

Placed filters retain field/datasource IDs, load their own source values, and do
not propagate across datasource boundaries merely because captions match. A
filter binding/query failure reports its own error and preserves other controls.

Repeated inline fields retain separate identities. A shared caption binding is
allowed only when their compiled SQL, type, role, and format agree within one
datasource. Different definitions with the same consumed caption are refused
where the remaining legacy caption adapter cannot represent them safely.

Missing catalog references are rejected by IR validation before code generation.
Untranslated dependencies block the affected sheet in V3. Qualified field
references require a relationship plan regardless of the datasource's spelling;
the inherited translator only recognized a `federated.` prefix. Parameter
references and quoted text are excluded from this new refusal check.

## Entry points and compatibility boundary

`v3_local.prepare` and `assess_v3.py` call `build_ir(..., scoped=True)`.
Consequently the V3 upload app and batch assessment use the new contract.
Legacy callers of `build_ir(path)` retain the compatibility model during this
migration. The other inherited app entry points are not V3 release targets.

This separation matters: enabling scoped parsing for the inherited full suite
made its blanket "all sheets render without blockers" assertion fail on
Superstore's Performance sheet. That sheet uses an untranslated cross-datasource
color calculation. The old path permits an approximation; V3 refuses the sheet.
The original assertion was not relaxed. `scope_after_regression.log` retains this
failure. A legacy regression pass therefore establishes compatibility only; it
does not verify the scoped path or fidelity of that sheet.

## Evidence

- `scope_baseline_regression.log`: full inherited suite passed before engine edits.
- `scope_legacy_regression.log`: inherited compatibility suite passed after the
  entry-point separation. The final run also passed, exit code 0, recorded in
  `scope_final_regression.log`.
- `scope_v3_tests.log`: 28 tests passed, including 12 new scoped-identity tests.
  Bar and KPI fixtures contain two datasources with identical internal calculation
  names and captions but different formulas. Real CSV queries produce 21 and 241;
  alternating source scopes preserves the result. XML order and caption/sheet
  renames vary independently. Separate tests cover nested dependencies, duplicate
  filter captions, conflicting/equivalent inline declarations, invalid references,
  exception restoration, foreign references, and healthy siblings after refusal.
- `scope_corpus_assessment.json`: 25 corpus files; zero structural IR errors,
  19 NEEDS_REVIEW, 6 BLOCKED, 12 untranslated-calculation entries. Six sheet
  occurrences are explicitly invalid, across related workbook versions. These
  files are not 25 independent held-out workbook families.
- Existing local-app tests still exercise upload, assessment, CSV binding, and
  preview through Streamlit AppTest, now with scoped parsing enabled.
- `scope_corpus_render.log`: scoped rendering smoke check of Superstore (20
  sheets) and E-Commerce (50 sheets). One expected refusal (Performance); the
  other 69 sheet occurrences had no unexpected warning/blocker in the headless
  probe. This is not numeric comparison or visual sign-off. The local app's
  health endpoint returned `ok` after the changes.

## Remaining limits and next work

This is a scoped compatibility adapter, not a completed typed semantic compiler.
Some renderer slots still use caption strings. Physical table routing still uses
datasource captions; duplicate datasource captions are explicitly blocked.
Process-global runtime state remains, serialized by the local preview and scoped
adapter, rather than redesigned for independent concurrent sessions.

The typed calculation AST, aggregation grain, filter phases, LOD semantics,
table-calculation partition/address/order, and cardinality-aware relationship
plans remain unfinished. Fields with different names can also normalize to the
same backend identifier; the earlier backend collision guard refuses these.
Known mark classes with novel shelf/pane combinations can still be misinterpreted.

Next structural milestone: preserve and validate each pane/shelf occurrence and
its grain before choosing a renderer. Then compile a typed calculation/query plan
with explicit unsupported outcomes. Extend tests with independent shapes and
renames, plus a genuinely held-out workbook. Live Tableau/app/Snowflake evidence
is still required for parity. No universal support claim follows from this work.
