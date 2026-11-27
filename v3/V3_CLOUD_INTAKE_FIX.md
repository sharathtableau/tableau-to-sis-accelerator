# Tableau Cloud site browser restoration â€” September 23, 2026

## Current documentation authority

For the enterprise objective and current implementation state, read [V3_ENTERPRISE_HANDOVER.md](V3_ENTERPRISE_HANDOVER.md), [V3_STATUS.md](V3_STATUS.md), [V3_NEXT_IMPLEMENTATION.md](V3_NEXT_IMPLEMENTATION.md) and [V3_ENTERPRISE_FIX_TRACKER.md](V3_ENTERPRISE_FIX_TRACKER.md). This file retains feature details and dated checkpoint history. Older current/next/latest headings below are historical and do not override those canonical documents. Technical constraints remain applicable unless explicitly superseded by verified implementation. No product readiness claim follows from this documentation update.

The reported site URL failed in V3's direct workbook URL parser before authentication.
The original pipeline_app.render_tableau_intake already supported site browsing.
V3 now reuses tableau_server.parse_site_url, list_site_contents and fetch_workbook_by_id,
including the original PAT resolver, pagination and sign-out behavior. It does not
infer a workbook from a site URL or select semantic behavior using workbook names.

Browse site â†’ Connect to Tableau â†’ Project â†’ Workbook â†’ Fetch and assess selected workbook.
Direct workbook/view links remain available as a separate Cloud connection option.
Project/workbook choices use REST IDs, including when display names collide.
URL/mode/project/workbook changes and failed connect/download attempts discard stale
assessment/export state. Selected downloads enter the same scoped hashed-payload
intake as upload and direct links, with server/site/workbook provenance retained.

Verification: 112 V3 tests pass. Full inherited regression passed before and after.
Corpus: 25 files, 394 occurrences, 55 UNVERIFIED, 333 UNSUPPORTED, 6 INVALID;
zero structural issues. Local corpus smoke remains mapped=3/refused=17 for
Superstore and mapped=0/refused=50 for _ecom; no parity inference.
All 148 recorded parent hashes match. The deployed package includes v3_cloud_ui.py.

Remaining limits: workbook retrieval is not data onboarding, numeric/render parity
or whole-workbook migration acceptance. Snowflake source bindings are still explicit.
Next: original-versus-V3 same-version Cloud workbook blocker matrix and data-binding
acceptance; prioritize structural fixes from those results. Partner readiness remains
NOT ESTABLISHED.

Live Snowflake verification: exact reported site URL connected and listed 173 workbooks.
R1 Test Upload - Superstore (REST ID 0b53cf7f-cce2-4d1f-b74a-5339aa2916a4)
was fetched and reached Assessment: BLOCKED. This is a development intake case, not
held-out acceptance. The UI showed isolated unsupported-semantic reasons, four
bundled Hyper decode failures and four missing explicit Snowflake bindings (Sales
Commission, Sales Target, Sample - Superstore, Orders (Sample - Superstore)).
No bindings were guessed; no data tables were created or modified. No rendering or
Tableau parity acceptance is claimed. Hyper/data onboarding versus the original
is a concrete next blocker to investigate within the whole-workbook matrix.

Deployed build: e3af9c911923fcdcec5d8f6e6473721c086e6312e783f88a7682b2c7ba82b1fd.
DESCRIBE verified SYSTEM$WAREHOUSE_RUNTIME, POWERHOUSE, v3_sis_app.py,
TABLEAU_ACCESS_INTEGRATION and tableau_pat -> WBR_DB.PUBLIC.TABLEAU_PAT_SECRET.
Archive: v3_cloud_candidate.zip. Evidence: V3_DEPLOYMENT_EVIDENCE.json and
deployment_runtime_cloud.json. Earlier deployment archives are superseded.
