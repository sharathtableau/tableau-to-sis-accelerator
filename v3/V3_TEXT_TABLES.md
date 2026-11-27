# Bounded text-table query contract — September 23, 2026

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

This is a local, bounded converter increment. Arbitrary-workbook support,
Tableau parity, Snowflake execution, complete migration acceptance and partner
readiness remain unverified. No deployment was performed.

## Implemented contract

`table_plan.py` compiles `text-table/1` plans from the existing occurrence model.
It accepts one explicit Text pane with slash-separated physical dimension rows,
an empty column shelf and explicit aggregate Text occurrences. One physical
table relation and one datasource identity are required. SUM, AVG, MIN, MAX,
COUNT and COUNT DISTINCT of physical numeric fields are planned; calculations,
generated Measure Values pivots and other encodings are not enabled.

Every executed column carries its occurrence ID, field ID, datasource identity,
role and physical definition. Repeated fields, repeated identical aggregates,
SUM/AVG of one field, and a field used both as dimension and aggregate retain
separate columns. SQL and Arrow/dataframe columns use stable occurrence aliases;
captions are display labels, disambiguated by role and position. The caption
bridge's ambiguity refusal is bypassed only for this structurally selected
adapter; source binding errors and calculation failures remain blocking.
The legacy `r_table` renderer is not used. Existing generated Measure Names KPI
dispatch remains intact.

Ordering is deliberately bounded to explicit ASC manual-sort dictionaries for
every row field, applied lexicographically in shelf order. Typed string/numeric
members and explicit `%null%` buckets are supported. Unknown/default, locale,
computed/measure-based and conflicting orders refuse before SQL. No ordering is
guessed from captions or source row order. String SQL literals escape apostrophes;
backslashes/control characters refuse until a dialect contract exists.

The grouped query computes total group count and an ordering-domain guard over
ALL groups, then orders before a fixed 200-row display limit. Unknown members,
including unplanned null headers, refuse at runtime even if they would fall
outside the displayed slice. Successful truncated output states both displayed
and total group counts and records a warning. Display limiting is not a Tableau
Top-N filter and does not change the query's aggregation domain.

Plans are recomputed during structural validation and before execution. Runtime
field catalog definitions and datasource bindings are checked before SQL.
Unsupported pivots, totals, relationships/joins, filters (including runtime
filters), table calculations, unplanned style/label clauses and source aliases
refuse. Runtime query/member failures are contained by the sheet lifecycle;
healthy siblings remain usable. Bundle manifests include the new runtime module;
no remote object was created or changed.

## Evidence and limits

- Baseline: all 66 V3 tests passed; full inherited regression passed before edits
  with exit 0 and `ALL REGRESSION TESTS PASSED` (terminal session 3036).
  `table_baseline_verification.json` records this observed baseline; it is a
  summary, not a reconstructed full log.
- Final: 76 V3 tests, including ten new table tests, pass. See
  `table_final_v3_tests.log`. Fixtures execute DuckDB queries across one- and
  multiple-dimension shapes, separate/reordered datasources, duplicate captions,
  repeated fields/aggregates, nulls, empty data, negative values, explicit member
  order, quoted strings and more than 200 groups. Expected aggregates are
  independent literal expectations. Refusal tests assert no SQL for static
  unsupported constructs and verify sibling usability.
- `table_numeric_evidence.json` retains four local executions, their plans, SQL,
  output rows and display labels. Reproduce with
  `python tests/capture_v3_table_evidence.py table_numeric_evidence.json`.
- A real Streamlit AppTest serializes the repeated-caption 4-row, 9-column
  table without exceptions and checks actual output values. This is local
  component rendering evidence, not browser screenshot/visual parity.
- Full inherited regression passed after converter edits
  (`table_verified_regression.log`); the final full regression after packaging changes also passed (exit 0),
  recorded in `table_final_regression.log`. An earlier run exposed the missing module in
  `snowflake.yml`; the manifest and generated-app module list were fixed.
- An initial V3 run exposed an overlap with the established Measure Names KPI
  dispatch. That was fixed without weakening its test. The initial failure is
  retained in `table_initial_v3_tests.log`.
- Corpus: 25 files / 394 sheet occurrences; 55 UNVERIFIED, 333 UNSUPPORTED,
  6 INVALID; zero structural issues. States are unchanged, with 23 BLOCKED and
  2 NEEDS_REVIEW files. `table_corpus_assessment.json` stores the assessment.
  There are no newly accepted corpus sheets to claim as rendered migrations.
- Scoped smoke remains Superstore 3 mapped / 17 refused and E-Commerce
  0 mapped / 50 refused (`table_verified_smoke.log`). The previously accepted
  date-bar occurrence still has no datasource and remains NOT_VERIFIED.
- Parent-source hashes: all 148 recorded files unchanged; see
  `table_parent_hash_check.json`. Every staged replacement compared its current
  V3 file hash to the captured baseline/last installed hash first.

## Remaining boundary and next concrete task

This contract intentionally does not accept most ordinary text tables: default
member ordering, Measure Names/Values pivots, customized text layout, number
formatting, totals, dates, typed filters/calculations and relationships need
their own contracts. Synthetic fixtures are development evidence, not held-out
workbooks. No corpus improvement or complete-workbook success follows from them.

Next implement a bounded explicit measure-based row-order contract on this
occurrence boundary. Start with one row dimension and an explicit aggregate
sort key, validate computed-sort/shelf-sort-v2 field ownership and evaluation
grain, and refuse ambiguous ties until a tie-break contract is justified.
Retain ordering-before-limit and whole-domain validation. Prove it across
multiple workbook shapes with tied/null/negative aggregates and different
datasource declaration orders. Select an actual candidate workbook/version
and document all remaining blockers; never substitute corpus parsing for
Tableau comparison or actual Snowflake runtime acceptance.
