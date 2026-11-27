# Shared authored categorical filters — September 23, 2026

## Current documentation authority

For the enterprise objective and current implementation state, read [V3_ENTERPRISE_HANDOVER.md](V3_ENTERPRISE_HANDOVER.md), [V3_STATUS.md](V3_STATUS.md), [V3_NEXT_IMPLEMENTATION.md](V3_NEXT_IMPLEMENTATION.md) and [V3_ENTERPRISE_FIX_TRACKER.md](V3_ENTERPRISE_FIX_TRACKER.md). This file retains feature details and dated checkpoint history. Older current/next/latest headings below are historical and do not override those canonical documents. Technical constraints remain applicable unless explicitly superseded by verified implementation. No product readiness claim follows from this documentation update.

The bounded text-table and Cortex replacement query paths now share one typed
row-filter compiler. This completes the authored include-member subset of the
filter gap. It does not complete dashboard interactions or all renderer filters.

## Executable contract

- Ordinary worksheet categorical filters on same-source physical string, integer
  or real dimensions, using explicit discrete `none:field:nk` identities.
  Filter nodes must occur directly under the table or its view.
- A member or nested union of explicit members is an include selection. Members
  combine with OR; separate filters combine with AND. Repeated filters retain
  separate identities. Captions and workbook names never resolve fields.
- `%null%` means an explicit `IS NULL` predicate. Empty union means no selected
  rows, not all rows. Typed values must match the catalog. Quoted string values
  remain SQL literals; backslash/control literals are refused pending a shared
  dialect contract. Each filter has a 1,000-member bound.
- Predicates execute in the source query before grouping, aggregate ordering,
  whole-domain checks and the 200-group display limit. The existing truncation
  disclosure and stable output occurrence IDs remain intact.
- The original XML remains stored. Only fully validated filter nodes are removed
  from a temporary structural-check copy; unknown clauses cannot disappear to
  make a visual render. Plans are rebuilt for validation, and referenced field
  definitions are checked against the active catalog before querying. Table
  assessment also rejects a stale filter catalog.
- Cortex receives the validated filter contract with the replacement proposal.
  It cannot change the filter predicate. Existing service-error isolation and
  visible substitution/parity disclosures remain in force.

Plan versions: `categorical-members/1`, `text-table/3`, `cortex-adaptation/2`.
Runtime packaging includes `filter_plan.py`; no deployment was performed.

## Evidence

103 V3 tests pass, including eight new filter tests covering actual numeric
results across renamed/reordered workbooks and two datasource scopes, duplicate
output roles, composed string/integer selections, empty/null selections,
same-field intersections, and all three Cortex replacement renderer shapes.
The tests also check unsafe or unsupported filter refusal before querying,
healthy siblings, stale XML/plans/catalogs, literal quote handling, filtering
before aggregate sorting and truncation, and an actual Streamlit dataframe.
Cortex transport in these tests is mocked; data queries execute locally.

For the composed category/Bucket selection, the first source produces null=7
and A=8 in authored order; the second produces null=14 and A=16. A separate
205-group filtered ordering case excludes the otherwise largest group before
sorting and reports 200 of 205 groups. These are fixture numeric results,
not Tableau parity claims.

The inherited regression passes before/after. Final logs are
`filter_baseline_regression.log`, `filter_verified_regression.log` and
`filter_verified_v3.log`. Corpus assessment remains 394 sheet occurrences across
25 files: 55 UNVERIFIED, 333 UNSUPPORTED and 6 INVALID, zero structural issues.
Render smoke remains Superstore 3 mapped/17 refused and ecommerce 0 mapped/50
refused. The corpus was not run through live Cortex. All 148 recorded parent
hashes match. No new live Snowflake/Cloud execution, Tableau parity, deployment
or complete unfamiliar-workbook acceptance was performed in this increment.
Partner readiness remains NOT ESTABLISHED.

## Remaining limits and next implementation

Authored selections are fixed at intake. Legacy dashboard SQL strings are still
refused by these adapters. Interactive dashboard controls, actions, cross-sheet
targeting, context/exclusion/range/top-N filters, calculated/aggregate filters,
relationships and datasource filters need their own explicit semantics.
Unknown filter metadata, including unplanned UI annotations, is refused.
Temporal and independent-pane adapters have not adopted this contract yet.

Next: implement a typed runtime selection envelope with explicit datasource and
worksheet targets, source-validated member domains, and stable filter IDs. Wire
it into V3 controls so users can change selections without passing legacy SQL.
Prove empty/all/null selection behavior and cross-sheet isolation, then reuse
the same boundary in temporal and independent-pane query plans. Complete
Cloud-to-Snowflake workbook acceptance remains a separate integration gate.
