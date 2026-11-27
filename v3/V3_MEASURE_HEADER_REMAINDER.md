# Measure-header remainder contract — E18.1a

## Current documentation authority

For the enterprise objective and current implementation state, read [V3_ENTERPRISE_HANDOVER.md](V3_ENTERPRISE_HANDOVER.md), [V3_STATUS.md](V3_STATUS.md), [V3_NEXT_IMPLEMENTATION.md](V3_NEXT_IMPLEMENTATION.md) and [V3_ENTERPRISE_FIX_TRACKER.md](V3_ENTERPRISE_FIX_TRACKER.md). This file retains feature details and dated checkpoint history. Older current/next/latest headings below are historical and do not override those canonical documents. Technical constraints remain applicable unless explicitly superseded by verified implementation. No product readiness claim follows from this documentation update.

Scope: one Text/Automatic crosstab pane, one row dimension, expanded Measure Values,
explicit visible-measure row sort, nonempty ASC Measure Names manual dictionary.
Existing crosstab contracts still apply. The manual prefix is honored by exact tokens;
one remaining selected occurrence is appended because there is no relative remainder
order to choose. This never deduplicates by caption or sorts by caption. Multiple
remainders remain refused, even when the filter union happens to look ordered.

Manual dictionaries require exactly one dictionary, string JSON buckets, no duplicate
or unknown tokens and no unrecognized options/children. Cached projections are
validated before rendering; header order is rebuilt from current source metadata.

Original trace: tableau_parser.measure_names_from_filter/list used filter membership
and engine rendering; generic engine._manual_order concerns dimension rows, not a
proven Measure Names remainder rule. V3 worksheet_semantics expands exact occurrences,
crosstab_plan.build validates header order, and crosstab_plan.render reuses the
existing aggregate renderer/query boundary. Only crosstab_plan.py runtime code changed.

The Sales workbook has seven explicit manual entries and two additional members.
Its frozen Tableau image visually shows Shipping Cost then Shipping Cost/Unit.
That image does not prove a general locale/default/remainder ordering rule, nor does
it establish same-snapshot numerical parity. Its two tables remain unsupported.

Tableau documentation describes manual sorting and ICU-dependent sorting, without
establishing this serialized partial dictionary's general remainder contract:
https://help.tableau.com/current/pro/desktop/en-us/sortgroup_sorting_computed_howto.htm

Verification: tests/test_v3_header_remainder.py checks two sources, renamed forms,
a three-measure independent shape with another physical field, reversed membership
serialization, exact aggregate outputs, malformed metadata, multi-remainder refusal
and stale projection rejection. Existing test_v3_more_visuals partial refusal now
uses two remaining members. Same-byte original/V3 parser comparisons cover all four
synthetic variants. Full corpus remains identical. This is bounded support, not
whole-workbook acceptance. E18.1a IMPLEMENTED_AWAITING_EVIDENCE; E18.1b remains OPEN.
