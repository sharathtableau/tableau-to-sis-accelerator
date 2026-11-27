# Discrete calendar axes — 2026-09-22

## Current documentation authority

For the enterprise objective and current implementation state, read [V3_ENTERPRISE_HANDOVER.md](V3_ENTERPRISE_HANDOVER.md), [V3_STATUS.md](V3_STATUS.md), [V3_NEXT_IMPLEMENTATION.md](V3_NEXT_IMPLEMENTATION.md) and [V3_ENTERPRISE_FIX_TRACKER.md](V3_ENTERPRISE_FIX_TRACKER.md). This file retains feature details and dated checkpoint history. Older current/next/latest headings below are historical and do not override those canonical documents. Technical constraints remain applicable unless explicitly superseded by verified implementation. No product readiness claim follows from this documentation update.

## Gap fixed

The temporal adapter previously accepted only continuous date values. The legacy
renderer also used DATE_TRUNC and temporal axes for every date token, which would
give incorrect grouping for month-of-year, quarter-of-year or day-of-month.

The scoped occurrence projection now contains a validated `date_axis` plan:
source occurrence ID, `date-part` versus `date-value`, SQL calendar unit, and
discrete/continuous axis state. It is recomputed during contract validation;
stale/tampered plans refuse before queries. Reparse existing IR to regenerate
the changed projection (occurrence schema remains v3).

| Authored date construct | SQL grouping | Scoped chart axis |
| --- | --- | --- |
| Discrete yr/qr/mn/dy | DATE_PART(YEAR/QUARTER/MONTH/DAY) | Ordinal component values, ascending |
| Discrete tyr/tqr/tmn/tdy | DATE_TRUNC(YEAR/QUARTER/MONTH/DAY) | Ordinal full-date values, ascending |
| Continuous tyr/tqr/tmn/tdy | DATE_TRUNC at the declared grain | Temporal axis |

For example, month-of-year combines January observations from different years;
a discrete truncated month preserves January 2024 and January 2025 separately.
Numeric date parts are no longer coerced to timestamps. Discrete full-date
headers use ISO date labels, preserving identity and chronological ordering.
This is not a claim of exact Tableau label/format parity.

The execution changes apply only when a scoped date plan exists. Legacy callers
retain their compatibility route. No filename/caption conditions were introduced.

## Tests and refusal boundaries

Four new tests in `tests/test_v3_discrete_dates.py` execute actual DuckDB queries
and inspect both chart data and compiled ordinal axis/tooltip specifications.
They cover colored Line/SUM and scalar Area/AVG shapes, two datasource layouts,
renamed fields, duplicate observations, year boundaries, and discrete day,
month, quarter and year components/full-date values.

Authored discrete-date sort rules remain unsupported pending a sort contract.
Weeks, fiscal calendars, ambiguous exact-date tokens, calculated date axes,
panels/overlays, area stacking and unconsumed encodings retain their refusals.
Null discrete-date groups refuse at runtime with a visible finding rather than
silently disappearing; a healthy sibling still renders. Continuous numeric date
parts without an approved contract remain unsupported.

The old assertion that every discrete date must refuse was replaced with positive
numeric coverage and a still-unsupported exact-date-token assertion. Refusal
tests were not simply deleted to make acceptance counts rise.

## Evidence

- Baseline: `discrete_baseline_regression.log`, `discrete_baseline_v3.log`.
- V3 tests: `discrete_v3_tests.log` (63 tests pass).
- Full inherited regression passed before and after changes. Final log:
  `discrete_verified_regression.log`, exit code 0. The 148 original root snapshot
  hashes still match.
- Corpus: `discrete_corpus_assessment.json`, zero structural errors across 25
  files / 394 sheet occurrences. UNVERIFIED increased 52 → 54, UNSUPPORTED
  decreased 336 → 334, INVALID remains six. This counts occurrences, not successful
  workbook migrations or held-out workbook families.
  File assessment states are now 23 BLOCKED and two NEEDS_REVIEW; NEEDS_REVIEW
  still does not establish data-backed migration success.
- The two additional acceptances are Sheet 2 and Sheet 4 of `_filtest.twb`.
  `discrete_gains_render.json` records NOT_VERIFIED for both: the source TWB does
  not bundle its referenced data and no explicit datasource binding was supplied.
  No corpus dataset was substituted merely because its caption looked familiar.
- Standard scoped smoke remains Superstore three mapped / 17 refused and
  E-Commerce zero mapped / 50 refused (`discrete_corpus_render.log`).

Rerun gain evidence with:

```powershell
& 'C:\Users\SharathKumarKammari\python.exe' verify_temporal_gains.py . --before temporal_corpus_assessment.json --after discrete_corpus_assessment.json
```

No Tableau numeric/visual parity, Snowflake runtime sign-off, deployment or
Cortex call is claimed. Update the gap ledger after each further converter fix.
Next applicable gaps include temporal bar contracts and typed filter/sort
planning; complete calendar, layered, relationship and calculation support is
still unfinished.
