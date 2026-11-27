# HR dashboard original-versus-V3 differential — 2026-09-27

## Current documentation authority

For the enterprise objective and current implementation state, read [V3_ENTERPRISE_HANDOVER.md](V3_ENTERPRISE_HANDOVER.md), [V3_STATUS.md](V3_STATUS.md), [V3_NEXT_IMPLEMENTATION.md](V3_NEXT_IMPLEMENTATION.md) and [V3_ENTERPRISE_FIX_TRACKER.md](V3_ENTERPRISE_FIX_TRACKER.md). This file retains feature details and dated checkpoint history. Older current/next/latest headings below are historical and do not override those canonical documents. Technical constraints remain applicable unless explicitly superseded by verified implementation. No product readiness claim follows from this documentation update.

## Reproduction identity

Workbook: `Workbooks/_powerbi_project_sweep/Human Resource Dashboard.twbx`  
SHA-256: `7a343dad00e653867f1d381bfea0a7e1036373d9c5f11d33867ab321330b1525`  
Bytes: 227,260. The original parser and scoped V3 parser consumed this same file.

The package contains `Human Resource Dashboard.twb` and three image assets. It
contains no `.hyper`, CSV or other data extract. A repository file search found no
HR data snapshot. The authored source is a live Snowflake connection; the V3
candidate is `SANDBOX.BI.VIZLEAP_HRDATASET_V14CSV` on the authored account host.
V3 discovered the candidate but returned `UNRESOLVED` because no active Snowflake
session was available. Access, account identity and schema compatibility are not
verified. Therefore neither implementation could execute the workbook against a
same-version source snapshot in this local differential.

## Stage-by-stage result

| Stage | Original accelerator | Scoped V3 | Evidence / limit |
|---|---|---|---|
| Intake | Original parser opens the `.twbx` and reads its `.twb`. | `v3_local.prepare` accepts the same bytes; no intake errors. | Same SHA-256 above. No data extract is bundled. |
| Datasource discovery | Authored Snowflake relation identifies the HR table candidate. | ID-based discovery returns the same `SANDBOX.BI.VIZLEAP_HRDATASET_V14CSV` candidate. | Candidate discovery only. |
| Verified binding | Not performed: no active account session. | `UNRESOLVED`; bindings contain `None`. | No account/access/schema check. Do not call this auto-binding success. |
| Query semantics | Original IR contains 13 calculations; `%diff` sheets carry `PctDiff` with `Dateof Hire` ordering. It also retains composed table label runs and `Salary (bin)`. | Assessment is `BLOCKED`: 12 unsupported, 2 unverified, 1 invalid of 17 occurrences. `%diff` sheets require temporal/table-calculation plans; tables refuse at adapter/occurrence planning; `Salary (bin)` is invalid due to untranslated dependencies. Two occurrences (`by termination year`, `performance score`) are mapped. | Original parser representation does not establish correct original query execution. No Snowflake query ran. |
| Rendering | Not executed. | Not executed against data. The termination-year occurrence is structurally mapped, but the date-bar renderer refuses null date groups at runtime with an explicit contract error. | No render screenshot, output rows or Tableau comparison. A synthetic renamed fixture confirms the generic `not_in ['%null%']` filter becomes `IS NOT NULL` and excludes a null date row; this does not establish the live workbook's selected control state or source rows. |
| Interactions | Workbook metadata contains one placed filter on Employee Dashboard and three on Hr Dashboard. | Same counts and field identities are retained in scoped metadata; V3 exposes datasource/field scoped controls. | No browser interaction was exercised because binding/data are unavailable. |
| Layout | Both parsers return two dashboard layout trees for 17 sheets. | Same dashboard/sheet counts and authored geometry are present in V3 IR. | Structural presence only; no pixel/layout comparison. |
| Export / target runtime | Not run. | Not run; no export or deployment was attempted. | Existing V3 deployment was left untouched. |

## Interpretation and next evidence gate

This confirms generic V3 coverage gaps at the planning boundary. It does not prove
that the original renders the `%diff`, crosstab or bin sheets correctly: the
original parser's IR is not a runtime or Tableau-parity result. The termination
chart's null-date error is also not yet a demonstrated generic regression. Its
workbook contains a placed date control, not a captured fixed `IS NOT NULL`
filter; the live source and effective control selection are unavailable. The
scoped renderer's explicit null-group refusal is currently the safe outcome when
such rows reach execution.

No converter change is justified by this differential alone. Do not enable the
legacy renderer or infer null filtering, table-calculation addressing, bin width,
source equivalence, or interaction state. The next implementation should follow
verified source rows and Tableau output on an authorized matching snapshot, then
use renamed and independently shaped fixtures before changing a generic contract.

## Checks run

- Before-edit `git status --short`: captured; the parent checkout already had many
  modified files and V3 was untracked. No parent file was changed.
- `C:\Users\SharathKumarKammari\python.exe -u tests/test_regression.py`: PASS,
  `ALL REGRESSION TESTS PASSED`.
- `C:\Users\SharathKumarKammari\python.exe -m unittest discover -s tests -p 'test_v3_*.py'`:
  PASS, 16 tests, `OK`.
- No source query or deployment occurred.

## Follow-up implementation checkpoint

A generic V3 text-table dashboard-filter regression was corrected after this
differential. The adapter accepts typed runtime filters only when physical
datasource/field/table identities match, and refuses mismatches before SQL.
Renamed independently shaped fixtures were added. The inherited regression and
all 134 V3 tests pass. This is not an HR workbook fix: composed labels, ordered
PctDiff, Salary (bin), live datasource validation, and Tableau/Snowflake parity
remain unresolved. No deployment occurred.
