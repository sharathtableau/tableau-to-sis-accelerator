# V3 Snowflake candidate — September 23, 2026

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

Latest checkpoint (September 23): V3 deployment authorized and completed.
App: [WBR_DB.TABLEAU_ACCELERATOR_V3.TABLEAU_TO_SIS_V3_CANDIDATE](https://app.snowflake.com/WB19670/c2gpartners/#/streamlit-apps/WBR_DB.TABLEAU_ACCELERATOR_V3.TABLEAU_TO_SIS_V3_CANDIDATE).
Runtime is verified as SYSTEM$WAREHOUSE_RUNTIME on POWERHOUSE; DESCRIBE confirms
TABLEAU_ACCESS_INTEGRATION and tableau_pat → WBR_DB.PUBLIC.TABLEAU_PAT_SECRET.
The server initially selected container runtime; an explicit ALTER corrected
only this newly created V3 app. Live startup also rejected python=3.11 as a
package; it was removed because the runtime selects Python. Future manifests
pin the runtime and deployment
must verify it after upload. Existing accelerator apps were not modified.
109 V3 tests pass after the manifest fix. Browser startup: PASS: warehouse-runtime V3 heading, candidate caption and workbook-source controls visible in Snowsight.
Deployment is not whole-workbook acceptance: Cloud pulls, data binding/query,
visual/interaction checks and Tableau parity remain NOT_VERIFIED in this run.
Deployed build: 41a92df9169382b0a46a6b94382a8bf0249dc01123d686153944b6ebf65bc869. The new archive is
v3_snowflake_deployed_candidate.zip; the previous archive is superseded.
See V3_DEPLOYMENT_EVIDENCE.json and deployment_runtime_final.json.
Next: verify actual Cloud intake and representative complete workbook scopes
against the original using identical versions/data bindings; then prioritize
generic fixes from the blocker matrix. Partner readiness is NOT ESTABLISHED.
Earlier no-deployment/no-authorization checkpoints are historical.

## Concrete candidate for review

- Archive: `v3_snowflake_candidate.zip` (33 explicitly inventoried files).
- Target: `WBR_DB.TABLEAU_ACCELERATOR_V3.TABLEAU_TO_SIS_V3_CANDIDATE`.
- Dedicated stage: `WBR_DB.TABLEAU_ACCELERATOR_V3.V3_CANDIDATE_STAGE`.
- Warehouse: `POWERHOUSE` (proposed from existing project configuration).
- Entry point: `v3_sis_app.py`; rejects missing Snowpark sessions instead of
  falling back to local DuckDB. It re-executes the workbench on each interaction.
- External integration: `TABLEAU_ACCESS_INTEGRATION`; secret alias `tableau_pat`
  points to the existing `WBR_DB.PUBLIC.TABLEAU_PAT_SECRET`. No secret values are
  included in the archive. No shared credential/network objects are recreated.
- Build SHA-256: `5b75d6753e0cc97e13e19684d80b4443f8a4941322504457d5d7c66b6391f30b`.
- Archive SHA-256: `d94d9960a882e3bb2963f288617663bf7463034a48f5576948427f381ba48a11`.

The original multi-app `snowflake.yml` is not the V3 deployment manifest. Extract
the candidate archive into a separate clean directory and use its single-app
manifest. Do not deploy from the shared parent or use the inherited app names.

## Workflow now packaged

Cloud URL or upload → shared scoped intake → validated Cortex assistance where
eligible → assessment/refusal report → explicit Snowflake source bindings →
preview → standalone candidate export. No app or data objects are deployed by
these controls. This is a deployment candidate for integration testing, not a
complete accepted workbook migration or enterprise-ready release.

Standalone export embeds the validated IR and complete explicit source bindings,
retains unsupported-sheet findings, neutralizes filename profile selection and
uses the same isolated runtime binding context as preview. Missing, foreign,
local-file or SQL-like bindings refuse export. Each standalone candidate has a
separate content-derived app identifier, assessment and build inventory.
Packages exclude workbook files, data extracts, local connection configuration
and inherited datasource manifests. Code dependencies are discovered from local
imports, including lazy imports; clean-directory execution tests check the path.

The V3 workbench remains a development UI. Existing V1/V2 navigation, automatic
onboarding, worker-based Tableau evidence capture and acceptance dashboards have
not been promoted wholesale. Explicit bindings require existing accessible
Snowflake tables/views. Hyper decoding is unavailable in the proposed warehouse
runtime; extracts require a separately validated data-loading path. Neither this
package nor export silently invokes legacy onboarding to bypass those gaps.

## Verification and outstanding environment gate

109 V3 tests pass. Six packaging/integration tests cover exact artifact inventory,
deterministic packages, manifest isolation, IR/binding roundtrip, missing/unsafe
bindings, clean-directory startup/export, explicit Snowflake session enforcement,
state restoration and repeated workbench Cloud/binding/export interactions.
Cloud transport and Snowflake sessions are mocked in those UI tests.

The installed Snowflake CLI successfully parses the packaged manifest; every
inventoried file hash matches. Full inherited regression passes before/after.
Corpus and render smoke are unchanged: 394 occurrences/25 files, 55 UNVERIFIED,
333 UNSUPPORTED, 6 INVALID, zero structural issues; mapped/refused 3/17 and 0/50.
All 148 parent-source hashes remain unchanged.

Read-only Snowflake preflight was attempted twice. Neither established a session
or returned environment metadata; both were stopped. The second remained at
CONNECTING despite configured login/network/socket timeouts. This does not prove
an authentication failure or a missing package/object. Package availability,
target absence, role privileges, warehouse access and EAI/secret metadata remain
NOT_VERIFIED for this candidate. No secret values were retrieved. No SQL mutation,
deployment, live Cloud pull or target-runtime app validation was performed.

Evidence: `candidate_package_verification.json`,
`candidate_snowflake_preflight.json`, `candidate_final_v3.log`,
`candidate_baseline_regression.log`, `candidate_final_regression.log`,
`candidate_corpus_assessment.json`, `candidate_final_smoke.log`.

## Authorized-deployment procedure (not executed)

1. Obtain explicit authorization for the exact isolated app above. Restore a
   working Snowflake connection and verify the intended role/account. Confirm
   required packages from `environment.yml`, warehouse usage, candidate schema
   privileges and access to the existing integration/secret. Inspect existing
   app names and do not replace any pre-existing app without separate approval.
2. Verify the archive SHA-256, extract to a clean directory and recheck the
   `candidate_build.json` file hashes. The manifest must contain only this V3
   target. Keep credentials outside the package directory.
3. If absent, create only the isolated candidate schema/stage using fully
   qualified names. Deploy the single candidate from its extracted directory
   using the configured connection and explicit database/schema. Do not use
   `--replace`, shared-app identifiers, or the inherited multi-app manifest.
4. Run `DESCRIBE STREAMLIT WBR_DB.TABLEAU_ACCELERATOR_V3.TABLEAU_TO_SIS_V3_CANDIDATE`.
   Verify `TABLEAU_ACCESS_INTEGRATION` and the `tableau_pat` mapping to the existing
   secret remain present. Record the app URL and exact candidate build.
5. Verify actual startup and reruns, Cloud authentication/download, Cortex,
   explicit source queries, rendering, interactions and standalone export. A
   successful CREATE STREAMLIT is deployment evidence only, not runtime success.
6. Then compare original and V3 using identical Cloud workbook versions and
   source bindings. Build the complete-workbook blocker matrix and select the
   next generic semantic fixes from those failures. Compare Tableau values and
   interactions before accepting any migration.

Official manifest reference: https://docs.snowflake.com/en/developer-guide/snowflake-cli/streamlit-apps/manage-apps/initialize-app

## Next task

Resolve the connection/preflight gate and obtain deployment authorization for
this reviewed candidate; deploy in isolation, verify bindings and actual runtime,
then perform the original-versus-V3 Cloud comparison. Do not resume isolated
feature expansion before establishing this application path. Partner readiness
remains NOT ESTABLISHED.
