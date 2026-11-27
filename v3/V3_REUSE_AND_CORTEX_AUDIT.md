# Original accelerator reuse and Cortex audit — 2026-10-01

## Current documentation authority

For the enterprise objective and current implementation state, read [V3_ENTERPRISE_HANDOVER.md](V3_ENTERPRISE_HANDOVER.md), [V3_STATUS.md](V3_STATUS.md), [V3_NEXT_IMPLEMENTATION.md](V3_NEXT_IMPLEMENTATION.md) and [V3_ENTERPRISE_FIX_TRACKER.md](V3_ENTERPRISE_FIX_TRACKER.md). This file retains feature details and dated checkpoint history. Older current/next/latest headings below are historical and do not override those canonical documents. Technical constraints remain applicable unless explicitly superseded by verified implementation. No product readiness claim follows from this documentation update.

## Conclusion

Substantial reusable implementation already exists. V3's current failures combine missing scoped execution contracts, genuine missing calculation semantics, and unavailable source/reference data. Cortex can propose conversion plans and diagnose evidence; it cannot establish correctness merely by generating executable SQL.

## Coverage and evidence

R00.2 reuse audit checkpoint recorded; R00 remains IN_PROGRESS. Recursive static source/configuration inventory: original 188 files, V3 164 files; 110 same-path files byte-identical, seven changed, 47 V3-only, 71 original-only. Original inventory includes 57 generated bundle files. V2 was inspected read-only: 34 files, 24 byte-identical to V3. The seven original/V3 differences are backend.py, calc_translator.py, codegen.py, engine.py, pipeline.py, snowflake.yml and tableau_parser.py. Python inventories parsed successfully. Counts are not counts of independently reviewed capabilities.

Inventory includes Python, SQL, YAML, TOML, JS/TS, HTML/CSS and shell scripts. Excludes downloaded workbooks/data, reports, historic audit directories, dependency caches and nested product copies (V2 separately inventoried). Targeted manual traces covered intake, parser/scoped IR, source binding, relationships, calculations, renderer gates, Cortex calls, validation capture, UI and candidate export. This is a complete static scan within that scope, NOT a line-by-line manual review or security certification of every file. Remaining R00 admin, embedded-worker, report, UI interaction and test-path review is open.

Evidence: reports/reuse_audit_20261001/complete_source_inventory.json, source_comparison.json, v2_readonly_inventory.json, workbench_import_closure.json and four source diffs. All inventoried original/V3 source hashes rechecked unchanged at closeout. Current workbench closure has 35 Python modules; import inclusion does not prove invocation.

## Fix-by-fix reuse map

| Blocker / stage | Existing implementation | Missing work and Cortex role |
|---|---|---|
| Partial Measure Names order / worksheet planning (E18.1, AI05) | tableau_parser.measure_names_list, crosstab_plan.build/render, engine._manual_order | crosstab_plan deliberately refuses a partial manual prefix with remaining measures. Generic dimension ordering does not prove measure-header remainder order. Determine authored remainder semantics, retain occurrence IDs, then reuse table rendering. AI can explain ambiguity; it must not invent order. |
| INCLUDE LOD / calculation translation (E05, AI03) | calc_translator has bounded FIXED LOD expansion and aggregate infrastructure | Add a typed detail-grain and outer-aggregation plan, dependency typing and filter-phase handling. Cortex structured proposals are suitable after source semantics are independently parsed; then compile deterministically and compare numeric results. Not implemented or live-validated for INCLUDE. |
| LAST and LOOKUP / calculation translation (E07, AI03) | engine grouped/window query helpers and bounded window rewrite | Missing authored partition, addressing, order, frame/restart and domain semantics. LAST cannot simply be replaced with an unordered row number; LOOKUP requires offset and null/zero-denominator handling. Cortex may propose a constrained plan with those inputs. Legacy Cortex fallback explicitly skips these functions. |
| Relationships / binding and query planning (E04, AI02/AI04) | init_workbook.parse_relationships, semantic_layer.join_plan/generate_views, pipeline data-model functions | Reuse graph extraction/planning with composite keys, cardinality and measure-grain validation. V3 automatic binding currently requires one physical table. Legacy flattening can return largest/partial data after a failed graph: do not reuse that fallback. AI can rank evidence-backed mappings; never invent joins. |
| Grouped/composed KPIs, temporal and other chart forms / visual contracts (E16-E18, AI05) | engine KPI labels, table, map, dual-axis/date-bar families; scoped plan adapters | Preserve grouped grain and all authored encodings in adapters before dispatching existing renderers. Availability of a renderer is not proof of parity. Expand Cortex feasible-plan selection only after each contract is implemented. |
| Tableau ECommerce references / source availability (E02/E09) | Tableau REST intake and reference capture | Confirmed Tableau 400081 DataSourcesNotConnectedException on three views. Restore the source connection and establish a valid snapshot; AI cannot supply missing credentials/data or fabricate references. |

Five-workbook evidence remains 121 occurrences: 93 unsupported, five invalid, 23 locally rendered. These are occurrences, not unique sheets. Five invalid formulas identify two INCLUDE calculations, two LAST()=0 calculations and one month-over-month LOOKUP calculation. Many unsupported occurrences have multiple blockers; no fixed-sheet count is promised. Zero cases matched current narrow Cortex eligibility. Whole-workbook numeric/visual acceptance remains unverified.

## Existing AI implementation worth reusing

- cortex_assist.py: active bounded structured expression and visual proposals, independent expression comparison, budgets/traces and recent prompt minimization. Current grammar does not cover LOD/window calculations.
- cortex_calc_fallback.py: legacy COMPLETE SQL proposal utility; rejects view-order calculations, uses permissive SQL extraction and executes a proposed query with LIMIT 5. Execution is not semantic validation. Reuse classification/prompt ideas through the typed V3 boundary, not the arbitrary SQL execution path.
- cortex_semantic.py: existing semantic YAML and Semantic View emitters. Transitively imported by workbench dependencies; this does not establish an integrated semantic-model UI flow.
- migration_governance_v2.cortex_critic: structured AI review exists. Its persistence targets V2 objects; adapt run identity, privacy, costs and V3 destinations before reuse.
- deep_validation.render_cortex_vision: existing AI screenshot review. Useful for finding label/layout discrepancies, never an independent numeric acceptance authority.
- pipeline_app_ai_v2.cortex_analyst: existing Analyst chat over semantic definitions. Reuse after qualifying access and semantic model; Analyst answers data questions and does not automatically convert Tableau dashboards.

The legacy fallback, governance critic, deep validation/vision, Analyst UI, validation_jobs and tableau_crosstab_jobs are absent from the current V3 workbench import closure. Thus much existing functionality is present on disk but not connected to this workbench.

## Validation and lifecycle reuse

Reuse deep_validation/validation_adapter numeric and crosstab extraction, validation_jobs durable queue/bundle hashes, capture workers and reports. These need current identity/provenance and access checks before integration. One concrete static hazard: validation_worker.process_job builds by_title and attributes screenshots by title despite its stable-ID comment; duplicate titles can overwrite attribution. Record under E01/E09; reproduce before correcting, do not treat such screenshots as independent acceptance proof.

V3 Cloud browsing already uses REST workbook IDs and clears stale selections. Explicit Snowflake bindings verify physical schema and read access, with source equivalence still unverified. v3_package includes dependency hashes and exports acceptance NOT_VERIFIED; export is separate from deployment. codegen embeds JSON safely and checks round-trip equivalence. Existing layout, parameters and rendering paths require actual interaction comparisons before E19/E20 closure.

## Snowflake AI qualification

1. AI_COMPLETE structured outputs constrain response shape. Proposed use: typed calculation/model/chart plans and evidence-linked diagnosis. Schema validity does not prove Tableau equivalence. Official documentation: https://docs.snowflake.com/en/user-guide/snowflake-cortex/complete-structured-outputs
2. Semantic View Autopilot can help construct a semantic model. Tableau ingestion supports direct Snowflake connections and has limitations including unsupported LOD calculations. It is not a dashboard rendering converter and cannot solve the current INCLUDE failures automatically. Qualify access, supported workflow, snapshots and output before adoption. https://docs.snowflake.com/en/user-guide/views-semantic/autopilot and https://docs.snowflake.com/en/user-guide/views-semantic/tableau-ingestion
3. Cortex Agents APIs are not supported from Streamlit warehouse runtime according to Snowflake's current limitations. V3 uses SYSTEM$WAREHOUSE_RUNTIME. A container-runtime/service architecture must be qualified for Agents; current AI_COMPLETE use is separate and already has prior live evidence. No runtime migration was performed. https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-agents

These recommendations are inferred from documented capabilities and inspected code. This audit made no new live Cortex requests and did not prove that a model can correctly repair these production formulas.

## Implementation sequence

1. E18.1: reproduce partial Measure Names ordering on frozen original/V3 bytes; establish remainder-order behavior with Tableau, then implement the smallest generic adapter change and renamed/independent fixtures.
2. AI03 with E05/E07: expand source-side typed semantics for INCLUDE, LAST and LOOKUP; accept bounded Cortex proposals only when independently validated; add wrong-plan, ambiguous-order, null and division tests and numeric references.
3. E04/AI02/AI04: adapt existing relationship planner and verify grain/bindings; E16-E18/AI05 reuse renderers through qualified contracts.
4. AI06: integrate existing critic/vision with structured differential evidence, current privacy/provenance and deterministic verdicts. Continue shared AI00/AI08 controls/evaluation.
5. Repeat five-workbook comparison against matched Tableau state, then deploy the verified build to the already-authorized V3 target. The unavailable ECommerce source and genuine holdout provenance remain external evidence gaps.

## State at closeout

Audit artifacts and persistent records only; no runtime source edits, tests rerun, Snowflake changes or deployment in this checkpoint. Previous AI00.1c local verification is historical evidence, not rerun here. Live deployment remains AI00.1b build 585428c9f5d7b02e5fb2a3b3497aa8024b5dbed5f9abb1507e514e9552b3e571; AI00.1c remains local. Existing deployment authorization is retained. All parent gaps remain open. Effort unmeasured; remaining estimates require re-estimation after reproductions and account/API qualification.
