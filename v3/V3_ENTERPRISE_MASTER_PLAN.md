# V3 Enterprise Master Plan and Tracker

**Owner:** Sharath. **Created:** 3 October 2026. **Status:** living document — update the item rows and the change log after every work session.

This is the single working tracker for turning the Tableau → Streamlit in Snowflake accelerator into an enterprise product. It consolidates the findings in `V3_ENTERPRISE_HANDOVER.md`, `V3_ORIGINAL_CODE_ENTERPRISE_GAPS.md`, `V3_ENTERPRISE_FIX_TRACKER.md`, `V3_PRODUCT_ROADMAP.md`, `V3_AI_IMPLEMENTATION_PLAN.md`, `V3_SNOWFLAKE_COMPONENT_STRATEGY.md`, `V3_REUSE_AND_CORTEX_AUDIT.md` and the 3 October checkpoint evidence. Those documents stay as detailed references and dated evidence. The IDs (E01–E28, AI00–AI08, SF01–SF08) are kept so nothing is renumbered.

**Rules for using this tracker**

- Work one item at a time, in priority order unless a dependency forces otherwise.
- An item moves to DONE only with evidence: the test, check or hosted proof named in its "Done when" column, recorded in the change log with a path.
- Evidence proves only its own scope. A passing test, deployment or rendered chart is not a workbook acceptance.
- No workbook-name special cases, no copying expected values into calculations, no execution of model-generated SQL or Python.

**Status values:** `DONE` (verified for its stated scope) · `PARTIAL` (implemented for a bounded case, wider scope open) · `IN PROGRESS` · `OPEN` · `DECISION` (needs a company/owner decision) · `BLOCKED` (external dependency).

**Size (my estimate, to be re-measured):** S = up to 2 focused days · M = 3–7 days · L = 2–4 weeks · XL = more than a month.

---

## 0. Next session starts here

*Overwrite this block at the end of every session. Keep it under 15 lines.*

- **Last updated:** 2026-10-04 late evening (Cowork session, demo prep).
- **Live build:** release 22 `8346dea7…` (63 files, verified 0 mismatches, config/grants unchanged; 355/355 V3 tests on Windows). Next deploy: new folder `release23`, previous = `reports/ui01_console_20261003/release22/deployment/candidate/candidate_build.json`. Rollback to 21: `release21/deployment/candidate`.
- **Proven live (ECommerce):** 24 of 25 comparable sheets match Tableau (2 text-only Not comparable); semantic view verified; AI cost per run $0.23 + ~$0.20 per Ask question.
- **Open difference:** X03 Avg. Days to 2nd Purchase — root cause confirmed (per-customer vs per-event grain); fix = X12 after the demo.
- **Demo (Mon 5 Oct):** deck `Tableau_to_SiS_Accelerator_Partner_Demo_v18_FINAL.pptx` + word-for-word `Talk_track_30min_v19_FINAL.txt` and `Cue_card_v19.txt` (demo output folder; builder `Claude outputs/build_deck_v18.py`). Do not click Diagnose with Cortex live. Snapshot: `reports/demo_snapshot_20261005/`. No engine changes before the demo.
- **After the demo, in order:** X12 engine fix → AI06.1 + AI06.2 → RL1 (Cortex proposals for refused sheets) → AI06.3 → RL2 (calculation repair) → RL3 (visual alternatives/fallback); AI08 seeded set alongside; vision token estimate; G00 / Priority 1A in parallel; X08 (auto previous manifest) when convenient. See section 6 "Cortex repair loop — delivery plan".
- **Deploy authority:** authorized for this V3 target after checks; deploy runs on Windows (`release\release_v3.cmd`).

---

## 1. Definition of enterprise-ready

The product is enterprise-ready when all of the following are true and evidenced:

1. **Correct or honest.** For any workbook within the published supported scope, every component is either converted with independently verified numbers, or listed as an exception with a reason and a remedy. Nothing is silently dropped, guessed or defaulted.
2. **Proven against Tableau.** Whole workbooks are accepted on numbers, visuals and interactions against matched Tableau references, on a cohort that includes genuinely unseen holdouts.
3. **Safe in a customer account.** Installs into a fresh Snowflake account by configuration, with role separation, per-user isolation, secret custody, read-only discovery and authorized promotion with rollback.
4. **Operable.** Durable jobs, monitoring, cost limits, support bundles, runbooks, upgrades.
5. **AI with measured value.** Cortex features show measured accuracy, unsafe-proposal rejection, latency and cost, and never decide their own acceptance.
6. **Commercially evidenced.** Pilots measure accepted workbooks, operator effort, Snowflake/AI cost and support load before any ROI claim.

**Headline metrics to track from now on** (start empty; fill as evidence arrives):

| Metric | Current | Target for pilot | Source |
|---|---|---|---|
| Whole workbooks accepted (numbers + visuals + interactions) | 0 | 5 cohort + 3 unseen | Acceptance reports |
| Worksheet occurrences converted with verified numbers, 5-workbook cohort | Not measured on current build (last measured 23 rendered / 93 unsupported / 5 invalid of 121, older build) | Set after F06 | Cohort run |
| Silent-substitution paths remaining (guess/default/skip) | 7 reproduced in original helpers; V3 coverage of each not fully traced | 0 on supported paths | E01–E07 tests |
| Operator minutes per accepted workbook | Not measured | Measure in pilot | Pilot log |
| Cortex proposals accepted after verification / unsafe proposals rejected | Synthetic only | Measured on holdouts | AI08 |
| Cost per migration (warehouse + AI) | Not measured | Measured | SF07 |

---

## 2. Current state snapshot (3 October 2026)

**Deployed:** build `5fb9bb1a599814f452034421a14a704f627ac9caac309f0a078d9b5c43b1039f` at `WBR_DB.TABLEAU_ACCELERATOR_V3.TABLEAU_TO_SIS_V3_CANDIDATE` (warehouse POWERHOUSE, `SYSTEM$WAREHOUSE_RUNTIME`, Streamlit 1.52). 52 runtime hashes match. Rollback of the preceding build `e68c5e8b…` is stored under `reports/original_presentation_20261002/binding_final/deployment/rollback_live`.

**Re-verified in this session (3 Oct):** rebuilding the package from the V3 source gives exactly the deployed build hash. The V3 suite (273 tests) on Linux with Streamlit 1.52.2: 268 passed; 1 passed after installing `vl-convert-python`; 3 fail because they look for `vega_lite_chart` while Streamlit 1.52 names it `arrow_vega_lite_chart`; 1 fails only because of the per-user package install used here.

**Carried forward, not re-run on this build:** 72 SALES executions, 50 ECommerce output occurrences, 42 Main filter states, 120 Product comparisons, 13 member/Details comparisons, 25 quantity/customer-history checks, 72 rank value/change checks. Corpus: 25 files, 0 structural errors, 132 unsupported/invalid occurrences (not 25 accepted workbooks).

**Not verified:** exact Tableau visual/interaction parity; any whole-workbook acceptance; Upload → Open dashboard → Back on the hosted app; Show/Hide target-pane visibility; native PowerPoint rendering of the demo deck.

**Five-workbook cohort (frozen 1 Oct):** Sales, Online Retail, ECommerce relationships, Email Campaign, Retail Sales/Customer — selection and hashes in `reports/ai00_1c_20261001/five_workbook_selection.json`. The fifth is reserved from tuning but its unseen status is unproven. Six published reference views captured for four workbooks; ECommerce references fail with Tableau error 400081 (DataSourcesNotConnectedException).

**Live Cortex context:** `AI_COMPLETE` with `claude-sonnet-4-5`, role WBR_OWNER, region AWS_US_EAST_1 (1 Oct synthetic check). Hosted Streamlit rejected layered-chart click selections, so chart interactions use explicit multi-select controls (an adaptation, not Tableau parity).

**Stale records to be aware of:** the bodies of `V3_STATUS.md`, `V3_NEXT_IMPLEMENTATION.md`, `V3_ENTERPRISE_FIX_TRACKER.md` and `V3_SESSION_MEMORY.md` still describe build `9f1cacbd…` (2 Oct) and older demo targets (Superstore/Global Sales); the 3 Oct state exists only as identical blocks prepended to many files. This tracker supersedes them for current state.

**Historical estimate:** 188–318 engineering days (226–382 with a 20% allowance), recorded 27 Sep, later marked RE-ESTIMATION_PENDING because it overlapped AI and platform work. Not a commitment.

**Repository hygiene:** V2 lives at `..\tableau_sis_accelerator_v2` and must be preserved. The V3 folder is untracked in the parent git repository (`?? tableau_accelerator_v3/`). The parent repository shows 76 uncommitted or untracked entries from several sessions.

---

## 3. The original accelerator

### 3.1 Capabilities to preserve and reuse

| Capability | Where | Reuse status in V3 |
|---|---|---|
| Upload, Tableau Cloud/Server browsing, PAT auth, workbook download, TWB/TWBX byte detection | `tableau_server.py`, `pipeline_app.py`, `converter_app.py` | Reused (V3 Cloud intake) |
| CSV/Excel/Hyper ingestion, unions, live-source discovery | `pipeline.py`, `init_workbook.py`, `backend.py` | Partly; collision refusal added in V3 |
| Relationship discovery and tree-shaped join planning | `init_workbook.parse_relationships`, `semantic_layer.py` | Reused for the ECommerce relationship model with fact-grain checks |
| Formula translation, parameters, groups, sets, reference lines, FIXED LOD | `calc_translator.py`, `tableau_parser.py` | Reused behind V3 typed guards |
| Broad renderer family, authored label runs, layout tree, filters, parameter controls, drill | `engine.py` | Partly reused; KPI cards and rank tables restored 3 Oct; many V3 contracts still refuse what the original draws |
| App generation, semantic YAML/Semantic View emitters, Snowflake publishing | `codegen.py`, `cortex_semantic.py`, `pipeline.py` | Emitters present, not in the V3 operator flow |
| Three-way validation, screenshots, capture bundles, durable queues | `validation_report.py`, `validation_jobs.py`, workers | Not integrated in V3 |
| Cortex critic, vision review, Analyst chat, SQL fallback | `migration_governance_v2.py`, `deep_validation.py`, `pipeline_app_ai_v2.py`, `cortex_calc_fallback.py` | On disk, not integrated; SQL fallback must not be reused as is (executes model SQL) |

### 3.2 Why workbooks convert incorrectly (reproduced defects)

The original renders broadly but often guesses and continues. Reproduced in isolated helper tests (`enterprise_product_audit_20260927/local_reproductions.json`):

| # | Behaviour | Gap |
|---|---|---|
| 1 | Two columns that normalise to the same name become one | E03 |
| 2 | String IDs `001`, `002` become numbers `1`, `2` | E03 |
| 3 | Unknown datasource returns a default table | E02 |
| 4 | Unsupported nested calculation becomes `NULL + 1` | E05 |
| 5 | Unknown aggregation defaults to `SUM` | E05 |
| 6 | Join to duplicate dimension keys inflates a total (30 → 40) | E04 |
| 7 | No valid relationship graph → largest table used | E04 |

Confirmed in code but not reproduced as incidents: unresolved filters skipped (E06), `ORDER BY 1` / `OVER ()` ordering guesses (E07), PASS counted when a query merely executed (E08), caption-keyed reference truth (E09), process-global state (E10), PAT written to job files (E11), model SQL executed in the fallback/Ask paths (E12), discovery that creates/overwrites tables (E13), LIMIT 200 before sorting (E18), `r_pctbar` using COUNT(*) regardless of measure (E16).

---

## 4. What V3 has implemented

| Area | Implemented | Status |
|---|---|---|
| Scoped identity | Field/datasource/occurrence scopes in the IR; refusal on ambiguous captions | PARTIAL (E01) |
| Source binding | Explicit binding with account/schema/column/read-access checks; automatic authored relationship binding with key and fact-grain checks | PARTIAL (E02, E04) — no runtime revalidation (E14) |
| Calculations | Typed dependency guards; bounded FIXED LOD; typed INCLUDE planning and isolated evaluation (AI03.1/AI03.2) | PARTIAL — INCLUDE execution NOT_ENABLED; LAST/LOOKUP open |
| Filters and actions | Categorical, date, discrete-date filters; dashboard filter/parameter actions; reset | PARTIAL (E06, E19) |
| Visuals | Text tables, crosstabs (one unlisted measure), date bars, panes, dual axis, scatter, pie, KPI cards and rank tables (original presentation restored) | PARTIAL (E16–E18) |
| Operator flow | Cloud/upload intake → assessment → binding → Open dashboard → Back with state retained | DONE for Cloud route (scoped); upload route unverified on hosted app |
| Packaging | Transitive module packaging, hash manifest, `acceptance: NOT_VERIFIED` provenance | PARTIAL (E21) — target, warehouse, integration hard-coded |
| Cortex | `AI_COMPLETE` with response schema, call budgets, minimised metadata, redacted traces; live structural assessment (one call per intake) | PARTIAL (AI00, AI01) |
| Evidence | Dated checkpoint folders with release gates, numeric oracles, hosted UI proofs, rollback | Working process; heavy manual overhead |

---

## 5. My challenges and recommended direction

These are changes to how the programme is run, not only what is built. Each has a tracker item in section 6.

**C1. Stop documenting, start tracking.** There are about 60 Markdown files, and most repeat the same stacked "current checkpoint" blocks at the top. Every session spends a large share of its effort re-reading and re-writing them. Recommendation: this file becomes the one tracker; per-session evidence goes in dated `reports/` folders; feature design notes stay as reference. Stop prepending checkpoint blocks to every document. (F03)

**C2. Put V3 under version control and CI before more features.** V3 has no git history, and the "273 tests pass" claim depends on whichever Streamlit is installed locally. A CI job pinned to the deployed runtime versions is the cheapest reliability gain available. (F01, F02)

**C3. Declare a supported scope and measure against it.** "Any workbook" is not achievable or sellable. Define Tier 1 (live Snowflake sources, single or related tables, the common chart families, categorical/date filters, parameters, filter actions) and publish a capability matrix driven by the same registry the assessment uses. Success is the percentage of Tier 1 workbooks accepted, not the number of tests. (F05, E27)

**C4. Measure workbook outcomes, not component counts.** Build a repeatable cohort run: N workbooks in, a table out of components converted / verified / refused / failed, plus whole-workbook verdicts. Run it on every release. This replaces the many workbook-specific evidence scripts. (F06, E25)

**C5. The Tableau reference is the long pole — productise it.** Acceptance needs Tableau's own numbers for the same data and filter state. Today that depends on one-off captures and a disconnected ECommerce source. Make reference capture (REST view data/crosstab export per filter state, bound to workbook revision and snapshot) a product capability with its own worker. (E09, SF04)

**C6. Make Semantic Views the governed middle layer.** Compile each Tableau datasource into a validated Snowflake Semantic View (dimensions, facts, metrics, relationships), and generate dashboard queries from it where its semantics fit, falling back to compiled SQL for table calculations and LODs it cannot express. Benefits: aligns with Snowflake's strategy, gives customers a reusable governed model, and lets Cortex Analyst/Agents answer questions over the same model as a bonus deliverable. Risk: Semantic Views do not express every Tableau semantic — keep the hybrid explicit. Qualify before committing (SF02). (AI04)

**C7. Position AI as the "repair loop" for the long tail, and measure it.** The deterministic engine should handle the common cases. Where it refuses, Cortex proposes a typed plan (calculation, mapping or visual adaptation); deterministic code validates and compiles it; an independent numeric check accepts it; the operator approves. That loop is the differentiator Snowflake will care about. Today the demo shows only assessment, which a technical audience may see as thin. Build the loop on the calculation path first, with AI08 measurement from the start. (AI03, AI06, AI08)

**C8. Prioritise renderer recovery by real frequency.** Do not port every original renderer. Use the assessment output across the corpus (and later pilot workbooks) to count which refusals block the most occurrences, and recover those first through checked contracts. (E16, F06)

**C9. Decide the runtime early.** Restricted caller rights and Cortex Agents need the container runtime; the current app uses the warehouse runtime. This choice changes isolation, caching and the assistant design, so it should be made before E10 work, not after. (SF01, DEC-03)

**C10. Keep the interpreter architecture, but version it.** The generated app embeds an IR and a shared engine. That is good for central upgrades, but every package must pin the engine version, IR schema version and binding contract so customer apps can be upgraded and rolled back predictably. (E21)

---

## 6. Prioritised backlog

### Priority 0 — Monday demo (5 October 2026)

| ID | Item | Status | Size | Done when |
|---|---|---|---|---|
| D01 | Fix the three tests that assume `vega_lite_chart` so they accept the Streamlit 1.52 element name; rerun the full suite | DONE | S | Done 3 Oct: suites pass on 1.52.2; `reports/ui01_console_20261003/` |
| D02 | Hosted rehearsal of SALES: Cloud select → Fetch and assess → Cortex assessment and binding → Open dashboard → USA 880 → January 601 → Reset 1,000 → Back | PARTIAL — 4 Oct: hosted run + REST Categ_KPI 5/5; per-sheet capture 9/9 (267 values) re-proven locally on current code; full rehearsal with capture upload pending | S | Screenshots/values recorded against the build hash |
| D03 | Hosted rehearsal of ECommerce: 21-day KPIs, 7 days, 15 October, Product default, Details 142, Product Customers action, Reset, Back | PARTIAL — 4 Oct: scope changed to the published Dashboard only (Product/Details unpublished); KPIs equal Tableau; 24/25 comparable sheets match Tableau values (2 text-only Not comparable, REPORT-01); image Consistent | S | All expected values match on the live app |
| D04 | Fix any reproducible demo blocker found in D02/D03 (check the original first), redeploy to the same target, verify hashes | OPEN | S–M | Deployment verification JSON with zero mismatches |
| D05 | Align deck, talkthrough, rehearsal runbook and quick check with live behaviour; add "Questions for the Snowflake team" and "where AI goes next" (C6, C7) | PARTIAL — 4 Oct: deck v18 + talk track v18 (24/25, confirmed X03 cause, backup A6, Cortex diagnosis as a Q&A story); final rehearsal on release 22 pending | S | Updated files; claims match evidence |
| D06 | Snapshot V3 (zip or git commit) before demo | DONE — 4 Oct: 377 source/doc/test files, `reports/demo_snapshot_20261005/` (zip + sha256 manifest) | S | Snapshot path recorded |
| UI01 | **Original console on the V3 engine.** 3 Oct: Monday slice deployed (83d8160b…); Deploy, Tableau comparison and architecture added in candidate cb0b7817…; still missing Ask (Analyst), Create app, Metrics, deep-validation reports. Rebuild the original workbench experience (dark nav, Overview, New migration with the 5-stage stepper and live progress, Inventory tabs, Preview, Validation) as a V3 entry point wired only to V3 functions: read-only discovery + verified binding (Discovery), V3 scoped IR + assessment + Cortex review (Parsing), verified relationship model + generated (not deployed) Semantic View DDL (Data model), V3 standalone package (App build), V3 evidence with honest verdicts (Validation). Reuse the original's styles/stepper/layout code by extraction; do not import `pipeline_app.py` (it pulls in mutating legacy paths). Keep the current V3 workbench reachable as a fallback page. Monday slice: Overview, New migration, Inventory, Preview, Validation. Later: Deploy & Ask (approved deploy + Cortex Analyst over a validated Semantic View), Create app, Metrics, deep validation (Cortex section/vision validation, notebooks, PDF report) | PARTIAL — Monday slice built and tested locally (7 console tests; 279/280 V3 tests, 1 env-only); awaiting Windows regression, deploy and hosted rehearsal | M (Monday slice) / L (full) | SALES and ECommerce run through all five stages on the hosted app with the D02/D03 values; no stage mutates Snowflake before approval; tests pass |

### Priority 1A — Parity with the original accelerator (gap closure; starts the day after the demo)

**Why:** the brief was "take the original, close its gaps, make it enterprise-grade". V3 is stricter (verified data model, Semantic View, validation against Tableau, governed Cortex) but converts fewer workbooks than the original. Example, Superstore 2024.3: the original drew 20/20 sheets (by approximation); V3 converts 3/20 and refuses 17 with reasons. Removing V3's gate does not help: the original renderer needs its own IR and data path (tested 4 Oct, 0/17 drew). **Exit rule:** every workbook that worked in the original works in V3, and every sheet is labelled Verified / Approximated / Refused-with-reason and validated against Tableau.

| ID | Item | Status | Size | Done when |
|---|---|---|---|---|
| G00 | **Gap register**: run the whole corpus through the original and V3; record per sheet original result, V3 result, refusal reason, data-path difference | OPEN | S | Register committed (CSV + summary); every V3 gap has an owner item below |
| G01 | **Original-engine fallback for refused sheets**: build the original IR for the workbook, render only V3-refused sheets with the original renderer on V3's verified binding, label "Approximated (original engine), not verified", include in sheet checks, Tableau value comparison and image comparison (delivered via RL3) | OPEN | M | Superstore 2024.3, Regional Analysis, Superstore KPI Parameter show ≥ the original's sheet count; no change to verified sheets of SALES/ECommerce |
| G02 | **File/extract workbooks**: (a) auto-detect existing tables (original naming WBR_DB.PIPELINE_DEMO.<datasource>, unique match in WBR_DB) + column/type/access check — built 4 Oct, live proof pending; (b) explicit, approved "load bundled data to Snowflake" (original onboard path) for files with no table | PARTIAL | S–M | Superstore family binds without manual input when tables exist; load path needs one approval and is audited |
| G03 | **Verified renderers by refusal frequency** (feeds E05, E07, E16, E18): stacked area/bar with labels, maps, table calculations and LOD/dynamic grain, multi-measure axes, crosstabs (Measure Names/Values), % of total bars, Gantt, forecast | OPEN | XL | Each class moves from Approximated to Verified only with Tableau value matches on ≥2 workbooks |
| G04 | **Relationship parsing gaps**: "Relationship extract requires a simple equality between two declared fields" (Global Sales); multi-source joins + member aliases (CustomerScatter) | OPEN | M | Global Sales parses; joins bound through the verified model |
| G05 | **Untranslated calculations**: e.g. "Sales above Target?" (Superstore Performance) | OPEN | S–M | Calculation translated or repaired via AI03 with numeric proof |
| G06 | Published-dashboard scoping (only dashboards Tableau Cloud publishes are migrated; others listed) | DONE | S | 4 Oct: ECommerce migrates Dashboard only; run log lists Product/Details |

#### Enterprise backlog raised during the 4 Oct rehearsal (priority order)

| ID | Item | Status | Size | Done when |
|---|---|---|---|---|
| X01 | Ask the data (Cortex Analyst) live proof in console and deployed app; better starter questions (revenue/quantity by category, channel, country) | PARTIAL | S | Live answer + chart + SQL on ECommerce; suggestions use dashboard measures |
| X02 | Live glossary (DESCRIBE SEMANTIC VIEW) proof; Data model tile on the Cortex activity panel | PARTIAL | S | Glossary renders from Snowflake; tile counts the enrichment call |
| X03 | Avg. Days to 2nd Purchase per-day difference (14/21 match; overall KPI matches). **Root cause confirmed 4 Oct:** the measure uses only `{FIXED [customer_id]}` values in a relationship model, so Tableau averages once per customer per chart point; V3 averages once per event row. Snowsight check (per-customer average) matches Tableau on 21/21 days; per-event average equals the app. Fix = X12 | ROOT CAUSE FOUND | S–M | X12 done and ECommerce shows 21/21 |
| X04 | Image snapshot styling (card backgrounds, text-only sheets such as donut labels) | OPEN | S | Snapshot shows dark cards and text sheets; Cortex still Consistent |
| X05 | Automatic Tableau per-sheet capture (Playwright + Tableau Connected App) instead of uploading the JSON | BLOCKED (Tableau admin) | M | Capture runs unattended and stores to a Snowflake table |
| X06 | Inventory: Validation result column next to engine status | OPEN | S | Inventory shows Match/Difference after validation |
| X07 | Refused-sheet Cortex adaptation proof (Regional Analysis) (now part of RL1) | OPEN | S | Activity shows adaptation calls; accepted/rejected proposals visible |
| X08 | Release tooling: preflight hang timeout; previous-manifest auto-detect from the last verified folder | OPEN | S | No manual previous-manifest argument; hung step times out with a message |
| X09 | Diagnose with Cortex for per-sheet capture differences (today only REST view differences) | DEPLOYED 4 Oct (release 22, 8346dea7…, verified); hosted click-through pending; do not demo live (AI06 finding). Shared `_diagnosis_block`; evidence adds sheet axes and data model; test in test_v3_ai | S | Button on capture differences; grounded hypotheses |
| X12 | **Relationship-grain aggregation (engine).** In a relationship model, an aggregate whose inputs are only FIXED LOD values keyed on a field of another logical table is evaluated at that key's grain: dedupe to one row per (chart point, key) before aggregating. Single-table sources and expressions with row-level fields (e.g. the Current Period KPI) unchanged. Generic, no workbook/sheet dispatch | OPEN — after demo | M (7–11 h) | ECommerce Avg. Days 21/21; other 24 ECommerce sheets unchanged; 25-file corpus and Superstore single-table results unchanged; new unit tests |
| X13 | **Known-difference note in the deployed app.** A sheet that validation marked Differs (and a reviewer signed off) shows a short note to app users naming the difference and its status | OPEN — after demo (raised 5 Oct) | S | Deployed ECommerce app shows the note on Avg. Days to 2nd Purchase until X12 ships; test |
| X14 | **Hold back a single sheet.** Reviewer can exclude one sheet from the deployed app (with reason recorded in the report) instead of holding the whole workbook | OPEN — after demo (raised 5 Oct) | S–M | Excluded sheet absent from app, listed in report; other sheets unchanged; test |
| X15 | **Protect manual edits on re-run.** Before a re-run replaces a deployed app, detect files that differ from the last deployed build and warn (or require explicit confirmation) | OPEN — after demo (raised 5 Oct) | S | Re-run over a hand-edited app shows the warning and the changed files |
| X16 | **Touchpoint label accuracy.** Console Cortex touchpoint table marks "Refused-sheet adaptation proposals" as live, but live proof is X07 (open). Label it "built, live proof pending" until X07 is done | OPEN — after demo (found 5 Oct) | XS | Label matches evidence; test |
| X10 | **Semantic View + Ask the data for single-table dashboards** (e.g. SALES): view over the bound table (every column a dimension, TOTAL_/DISTINCT_ metrics, safe Tableau calcs, Cortex glossary), proven equal (distinct rows + every total), Ask tab in console and deployed app; dashboard keeps reading the table | BUILT 4 Oct — live proof pending | S | SALES run shows the view verified, Ask answers on SALES in console and deployed app |
| X11 | **AI tokens and cost per run** (user request: migration report must show tokens used and cost): AI_COMPLETE called with show_details => TRUE in v3_ai.call and cortex_assist; Snowflake-reported prompt/completion tokens per call; cost from configurable list rates (v3_usage.py); vision calls estimated and labelled (image tokens not reported by the PROMPT/TO_FILE shape); Ask the data billed per message; totals on the Cortex AI page and in the migration report | BUILT 4 Oct - live proof pending (release 20) | S | ECommerce run shows Snowflake-reported tokens and cost per call; report section 'Cortex activity, tokens and cost' |

### Priority 1 — Foundation (start immediately after the demo)

| ID | Item | Status | Size | Done when |
|---|---|---|---|---|
| F01 | Put V3 under git; first commit of the deployed build; branch per work item | OPEN | S | V3 tracked; tag for build `5fb9bb1a…` |
| F02 | CI pipeline pinned to the deployed runtime (Python, Streamlit 1.52, Snowpark); runs regression + V3 tests on every commit | OPEN | M | Green CI run on the tagged build |
| F03 | Consolidate documentation: this tracker + dated evidence; stop stacking checkpoint blocks; archive superseded docs | OPEN | S | `V3_START_HERE.md` points here; archive folder created |
| F04 | Generic, reusable release scripts (preflight, deploy, verify) parameterised by checkpoint folder, replacing per-checkpoint one-shot scripts | PARTIAL — `release/` scripts + `release_v3.cmd` written 3 Oct; first real use pending | S | Same script deploys and verifies two consecutive builds |
| F05 | Capability registry: one machine-readable list of supported/partial/refused constructs, used by assessment, runtime and the published scope page | OPEN | M | Assessment output and scope page generated from the registry |
| R00 | Finish the original-code manual review (UI variants, reports, workers, admin SQL, tests) for each path a fix touches | IN PROGRESS | ongoing | Coverage recorded per path in the inventory |
| F06 | Cohort runner: run N workbooks end to end and output per-component and per-workbook verdicts; baseline the 5-workbook cohort on the current build | OPEN | M | Baseline table committed; rerun reproducible |

### Priority 2 — P0 correctness and security (blocks any acceptance or customer use)

| ID | Item | Status | Size | Depends on | Done when |
|---|---|---|---|---|---|
| E01 | Structural identity carried from parse to export | PARTIAL | M | F01 | Same caption/different source, repeated pills and rename tests pass on every path |
| E02 | Verified binding; no default-table fallback anywhere | PARTIAL | M | E01 | Unknown/ambiguous/wrong-account sources refuse in every path, including exported apps |
| E03 | Lossless ingestion (names, types, IDs, nulls, decimals, sheets) | OPEN | M | E01 | Collision and `001` cases preserved through load and export |
| E04 | Relationships: cardinality, composite keys, aggregate-before-join, no partial-model success | PARTIAL | L | E01–E03 | One-to-many, many-to-many and multi-fact numeric tests pass; no inflation |
| E05 | Typed calculations; no NULL/SUM substitution; LOD verification | PARTIAL | L | E01, E03 | Transitive refusal tests; INCLUDE/FIXED numeric oracles; filtered INCLUDE KPI executes (AI03.3) |
| E06 | Filter order of operations (context, dimension, measure, table-calc, Top-N) | PARTIAL | L | E04, E05 | Interacting filter and Top-N/LOD before/after results match Tableau |
| E07 | Ordered table calculations (LAST, LOOKUP, rank, running, % difference) | OPEN | L | E05, E06 | Null, tie and missing-period cases match Tableau |
| E08 | One truthful acceptance model across UI/CLI/report/package | OPEN | M | F05 | No path can show PASS for executed-only or partial results |
| E09 | Reference identity and full-domain comparison; productised Tableau capture (C5) | OPEN | L | E01, E02 | References bound to revision/snapshot/state; omissions and duplicates detected |
| E10 | Session isolation, roles (author/publisher/viewer), cache identity | OPEN | L | DEC-03, SF01 | Two-user/two-workbook concurrency and denied-access tests pass on the chosen runtime |
| E11 | Secret custody (no PAT copies), endpoint allowlist, redaction | OPEN | M | — | Denied host and missing-secret tests; no secrets in any artifact |
| E12 | Constrained SQL; no model SQL execution anywhere; retire fallback/Ask execution paths | OPEN | M | E02, E05 | Static check proves no model output reaches execution; custom SQL via approved read-only plans |
| E13 | Read-only discovery; explicit mutation plan; versioned promotion with rollback | OPEN | M | E02, E11 | Discovery creates nothing; promotion requires approval; rollback tested |
| E14 | Runtime revalidation of packaged bindings; drift detection | OPEN | M | E02, E21 | Wrong account, missing column, changed type and denied access detected at runtime |

### Priority 3 — P1 coverage and operation (needed for a pilot)

| ID | Item | Status | Size | Done when |
|---|---|---|---|---|
| E15 | Safe intake: archive validation, published datasources, live vs extract policy, snapshots | OPEN | M | Malformed/oversized archives refused; snapshot lineage recorded |
| E16 | Renderer recovery by frequency (C8): percent bars, stacking, dual-axis sync, legends, reference lines | PARTIAL | XL | Top refusal classes in the cohort converted with numeric proof |
| E17 | Dates, bins, domain completion, nulls | PARTIAL | L | Boundary, negative, null and fiscal cases verified |
| E18 | Tables: sort before limit, Measure Names/Values, totals, multiple remainder measures (E18.1b) | PARTIAL | L | >200-group, ties and totals verified |
| E19 | Interactions: filter/highlight/parameter/set actions, Show/Hide visibility | PARTIAL | L | Actual UI events match Tableau state for the cohort |
| E20 | Layout, assets, accessibility; defined reflow rules | PARTIAL | L | Running-app screenshots within agreed tolerance; keyboard/contrast checks |
| E21 | Complete versioned packages (C10): configurable target, lockfile, IR/engine/binding versions, upgrade/rollback | PARTIAL | M | Fresh-account install from config; upgrade and rollback proven |
| E22 | Durable validation jobs: leases, fencing, recovery, idempotency | OPEN | M | Two-worker race and crash tests pass |
| E23 | Performance and cost budgets, cancellation, query tags | OPEN | M | Large-workbook benchmark; budgets enforced without silent truncation |
| E24 | Observability: correlated run/query/AI IDs, event tables, support bundles | OPEN | M | One run traceable end to end; redaction verified |
| E25 | Release gate with frozen holdouts (uses F02, F06) | OPEN | M | Release blocked automatically on any failing gate |

### Cortex repair loop — delivery plan (added 5 Oct 2026; delivers C7)

Goal: when a sheet works in Tableau but V3 refuses or mis-converts it, Cortex proposes an alternative, code builds and checks it against Tableau's values, and a person approves. Cortex never decides a verdict and model-written SQL is never executed. These rows sequence existing items; they do not replace them.

| ID | Phase | Builds on | Status | Size | Done when |
|---|---|---|---|---|---|
| RL1 | **Proposals in the assessment.** For every refused sheet, Cortex suggests a typed alternative (e.g. "rewrite this calculation as X", "show as bar plus table"), using the refusal reason, sheet spec, original Tableau formula (AI06.1) and table schema. Shown in the console (Inventory/Validation) and the migration report with its tokens and cost. Advisory only; nothing is applied | X07, AI00 schemas, X11 cost tracking | OPEN — after demo | S–M (~1 week), low risk | Every refused sheet in the 25-file corpus has a proposal or a stated reason why none; cost per proposal in the report; no IR/app change without approval (test) |
| RL2 | **Calculation repair.** After a person approves an RL1 calculation proposal, it is compiled by V3's typed calculation code (never executed as model SQL), its values are compared with Tableau's captured values, and the sheet becomes Verified only on a match | AI03, G05, AI06.3 (same propose → code-test → compare mechanism) | OPEN — after RL1 | M (~2 weeks), medium risk | ≥2 corpus calculations repaired to Tableau-matching values; unsafe/incorrect proposals rejected by code (measured in AI08) |
| RL3 | **Visual alternatives and fallback drawing.** A refused chart type is redrawn as a supported one (AI05) or with the original engine (G01), labelled "Approximated", and still compared with Tableau's values | AI05, G01, G03 | OPEN — after RL2 | M–L (~2–3 weeks), medium risk | Approximated sheets shown with label and value check; open problem solved: value comparison when the chart type changes (compare underlying aggregates, not marks) |

Order after the demo: X12 → AI06.1 + AI06.2 → RL1 → AI06.3 → RL2 → RL3 (AI08 measurement alongside from RL1). Limit: sheets that depend on the view's row order (LOOKUP, PREVIOUS_VALUE, running totals) stay "Refused with a reason".

### Priority 4 — AI and Cortex (built alongside P2/P3 as prerequisites land)

| ID | Item | Status | Size | Done when |
|---|---|---|---|---|
| AI00 | Shared Cortex controls: versioned schemas, budgets, provenance, privacy, outage behaviour | PARTIAL | M | Contract tests including malformed, adversarial and outage cases |
| AI01 | Assessment with findings that cite real component IDs and remedies | PARTIAL | M | Every finding links to a component; AI cannot change a support verdict |
| AI03.3 | Filtered INCLUDE KPI execution (the recorded next enterprise task before the demo detour): typed month/context filters, action state, verified runtime binding, two-stage query, KPI rendering, independent numerics. Plan in `V3_NEXT_IMPLEMENTATION.md` body | OPEN | L | Positive/negative fixtures and independent numeric results pass; Online Retail KPIs execute without name dispatch |
| AI03 | Calculation repair loop (C7): propose typed plan → validate → compile → numeric oracle → operator approve (delivered via RL2) | PARTIAL | L | Measured on holdout formulas: accepted, rejected-unsafe, refused |
| AI06 | Validation diagnosis from structured differences | PARTIAL — console 'Diagnose with Cortex' on REST and per-sheet differences. **4 Oct finding:** on X03 Cortex answered 'high confidence' with a wrong lead (rounding); its first suggested check (compare customers behind one day) was the right one. Cause: it saw only translated SQL and values. Plan AI06.1–AI06.3 | M | Ranked, evidence-cited hypotheses on seeded differences; X03 cause proposed and proven by code |
| AI06.1 | **Better diagnosis evidence.** Carry the original Tableau formula into the IR next to translated SQL; per differing point, code computes facts: rows vs distinct LOD-key count, gap size vs Tableau display precision, filter stage, relationship vs single-table model. Facts rule causes in or out (e.g. rounding impossible when the gap exceeds half a display unit) | OPEN — after demo | S (~½ day) | X03 diagnosis names the per-customer grain without hints |
| AI06.2 | **Honest confidence.** Code caps confidence at 'medium' unless a check has confirmed the hypothesis; label 'unconfirmed' otherwise | OPEN — after demo | XS (~1 h) | No 'high' without a confirming check (test) |
| AI06.3 | **Prove the hypothesis (repair-loop diagnosis).** Cortex picks hypotheses from a fixed typed menu (LOD/relationship grain, filter before/after FIXED, null handling, date grain, aggregation); code runs each as a read-only query and compares with Tableau's captured values; UI shows e.g. 'Per-customer grain reproduces Tableau on 21/21'. Model never executes SQL it wrote; verdict unchanged until a fix ships. Feeds X12/AI03 | OPEN — after demo | M (2–3 days) | X03 reproduced 21/21 by a code-run hypothesis; seeded set passes |
| AI02 | Datasource and field mapping proposals | OPEN | M | Wrong/ambiguous candidates refused; no binding from confidence alone |
| AI04 | Semantic View generation and Autopilot comparison (C6) | PARTIAL — 4 Oct: generated from the verified relationship plan, created in the migration run, verified equal (48,000 rows/all totals), dashboard reads through it, Cortex descriptions/synonyms, readable names; Autopilot comparison open | L | Emitted views match independent queries; unsupported items listed |
| AI05 | Visual adaptation proposals; reuse critic/vision for presentation review only (delivered via RL3) | PARTIAL — 3 Oct: original Cortex vision comparison reused on the Validation page (advisory verdict, never changes a status) | L | Substitutions disclosed and numerically verified |
| AI07 | Operator assistant (Agents + Search, read-only tools) | PARTIAL — 3 Oct: 'Ask Cortex about this migration' answers from the run's evidence only (no tools, no retrieval); Agents/Search version still open | L | Needs SF01/SF06; cited answers, denied retrieval proven |
| AI08 | AI quality and cost evaluation (start with AI03/AI06) | IN PROGRESS — tokens/cost per call live (X11). Next: seeded-difference set (X03 plus seeded wrong filter, wrong grain, null handling, date grain); track top-hypothesis-confirmed rate, false 'high confidence', cost per diagnosis | M | Dashboard of accuracy, unsafe acceptance, latency and cost per model/prompt version |

### Priority 5 — Platform qualification and company decisions

| ID | Item | Status | Size | Done when |
|---|---|---|---|---|
| SF01 | Runtime and access model (warehouse vs container, caller rights) | OPEN | M | Two-role isolation test on the chosen runtime |
| SF02 | Autopilot and Semantic View fit | OPEN | M | Same-input comparison; Snowflake answer on supported API |
| SF03 | Cortex proposal controls | PARTIAL | S | Rejection suite; identical results with AI disabled |
| SF04 | SPCS workers for Hyper decoding and Tableau/browser capture | OPEN | L | Dependencies, secrets, retry/fencing proven |
| SF05 | Run and evidence records in Snowflake tables/stages | OPEN | M | Immutable references; retention policy |
| SF06 | Agent/Search qualification | OPEN | M | Read-only tools; cross-role denial |
| SF07 | Operations and cost measurement | OPEN | M | Measured cost per migration |
| SF08 | Distribution (internal vs Native App) | DECISION | — | Decision recorded |
| E26 | Single lifecycle service and declarative configuration | OPEN | L | UI/CLI/workers share one path; alternates retired with equivalence evidence |
| E27 | Published scope and operator UX (C3) | OPEN | M | Scope page generated from F05 |
| E28 | Governance: retention, residency, support ownership | DECISION | — | Decisions recorded |

---

## 7. Milestones and exit gates

| Milestone | Exit gate |
|---|---|
| M0 — Demo (5 Oct) | D01–D06 done |
| M1A — Parity with the original (target: 2 weeks after the demo) | G00 register published; G01 fallback live; G02 done; every corpus workbook shows ≥ the original's sheet count, each sheet labelled Verified / Approximated / Refused and validated against Tableau |
| M1 — Foundation | F01–F06 done; cohort baseline published |
| M2 — First accepted workbook | One whole workbook accepted on numbers, visuals and interactions against a matched Tableau reference; E01–E05 done for the paths it uses |
| M3 — Cohort accepted | All five cohort workbooks accepted, then 3 genuinely unseen holdouts; E06–E09 done; AI03 loop measured |
| M4 — Controlled pilot | Customer workbooks within published scope; E10–E14, E21, SF01 done; effort and cost measured |
| M5 — Enterprise release | All P0/P1 gates; fresh-account install/upgrade/rollback; concurrency, recovery, cost controls; runbooks and support ownership |

---

## 8. Decisions needed

| ID | Decision | Needed before |
|---|---|---|
| DEC-01 | Tier 1 supported scope (connectors, constructs) | F05 / M2 |
| DEC-02 | Numeric tolerances and acceptable visual substitutions/reflow | M2 |
| DEC-03 | Runtime and access model (warehouse vs container; owner vs caller rights) | E10 |
| DEC-04 | Distribution: internal service, customer-account install, or Native App | E21 / M4 |
| DEC-05 | Snapshot/refresh policy for acceptance data | E09 |
| DEC-06 | Data classification, retention, residency (incl. Cortex cross-region inference) | M4 |
| DEC-07 | Pilot acceptance owner, support ownership, pricing review | M4 |
| DEC-08 | Holdout selection rules and custodian | M3 |

---

## 9. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| No working Tableau reference for ECommerce | Blocks acceptance of a demo workbook | Restore the data source connection or pick a cohort workbook with a working reference (E09) |
| Documentation overhead consumes engineering time | Slow progress | F03 |
| Untracked V3 source | Lost or conflicting work | F01 |
| Test environment differs from deployed runtime | False confidence | F02 |
| Over-claiming AI in demos | Credibility with Snowflake engineers | Keep the three-way distinction: deterministic / actual Cortex assessment / validated AI repair (not yet) |
| Runtime decision made late | Rework of isolation and assistant design | DEC-03 early |
| Workbook-specific tuning creeps in | Product does not generalise | Frozen holdouts; no name dispatch; C4 cohort runner |

---

## 10. Change log

| Date | Change | Evidence |
|---|---|---|
| 2026-10-05 | Pre-demo: talk track Q&A made Cortex-specific (AI_COMPLETE text/multimodal, native Semantic View, Cortex Analyst REST; adaptation built but unproven; repair loop planned); post-cutover change answer and accelerator-gap answer added. Backlog X13-X16 added | `Downloads/Tableau_SiS_Demo_5Oct/Talk_track_30min_v19_FINAL.txt`; `reports/demo_snapshot_20261005/before_customer_edit/` |
| 2026-10-05 | Roadmap: "Cortex repair loop — delivery plan" added (RL1 proposals in the assessment ~1 wk; RL2 calculation repair ~2 wks; RL3 visual alternatives and fallback ~2–3 wks), sequenced with X12, AI06.1–AI06.3 and AI08; links X07, AI03, G05, AI05, G01, G03 | `V3_ENTERPRISE_MASTER_PLAN.md` section 6 |
| 2026-10-04 | Release 22 deployed and verified (8346dea7…, 63 files, 0 mismatches, config/grants unchanged; 355/355 V3 tests). A second run of release22 with previous = release21 stopped at preflight as designed (live already release 22; the live download matched the release 22 manifest 63/63). Demo prep: deck v18 (Impact 24/25; backup A6 on X03), talk track v18, then a full word-for-word talk track v19 + cue card; D06 snapshot | `reports/ui01_console_20261003/release22/`; `reports/demo_snapshot_20261005/`; demo output folder v18 files |
| 2026-10-04 | Release 21 verified (dc7fc70d…, 63 files, config/grants unchanged): readable migration report (verdict, tiles, what needs attention, one sheet table, AI cost; full evidence collapsed). Release 22 candidate: Data model progress order fix; Diagnose with Cortex on per-sheet differences (X09). X03 root cause confirmed (per-customer vs per-event grain) → X12. Cortex diagnosis tested on X03: wrong lead, right first check → AI06.1–AI06.3 plan. V3 tests 354/355 (1 env-only) | `V3_FIX_JOURNAL.md` (REPORT-01, UI-LOG-01, AI06-UI); `reports/ui01_console_20261003/release21/`; `Claude outputs/X03_days_to_2nd_purchase_diagnosis.sql` |
| 2026-10-04 | X11 built: Cortex tokens and cost recorded per call (show_details), shown on the Cortex AI page and in the migration report; release 19 verified (8936a64f..., 62 files, 0 mismatches) | `reports/ai_usage_20261004/` |
| 2026-10-04 | X10 built: single-table Semantic View for Ask the data (user request: questions on any dashboard's data). Relationship path unchanged (shared helpers refactored, 16/16 semantic tests); new tests/test_v3_single_semantic.py; bundle unchanged when no single-table view | `reports/single_table_sv_20261004/` |
| 2026-10-04 | Rounds 8–17: Semantic View from Tableau relationships (automatic in the run, verified equal, dashboard reads through it, Cortex glossary, readable names); Ask the data (Cortex Analyst) in console and deployed apps; published-dashboard scoping; per-sheet comparison fixes (dates, KPI tiles, rank tables, signs, totals, scatter); one-button Validation with numbered tabs; image snapshot fix (no data); Deploy page with app naming; auto-detect existing tables. Gap versus the original documented; Priority 1A added. Live build f639199f… (release18) | `reports/ui01_console_20261004_semantic/checkpoint.md`; `reports/ui01_console_20261003/release10`–`release18` |
| 2026-10-03 | Round 4: user asked for original-style migration report, sheet-level validation, image compare, visible Cortex by step and roadmap AI. Built v3_ai.py + v3_evidence.py, Validation tabs, Cortex AI page, Start new migration, pre-filled site URL; candidate d7781953… (V3 303 run / 302 pass, 1 env-only) | `reports/ui01_console_20261003/round4/` |
| 2026-10-03 | Round 3: deploy now calls the original pipeline.deploy_streamlit_app; original architecture board reused with corrected labels; candidate dc8fe6e0… (291/292 tests, 1 env-only) | `reports/ui01_console_20261003/checkpoint.md` |
| 2026-10-03 | Release 1 verified on Windows (83d8160b…, 54 files, config/grants unchanged). Round 2 built: Deploy page, live Tableau comparison (SALES 5/5 on captured reference), V3 architecture, standalone Preview view; 292/293 tests (1 env-only) | `reports/ui01_console_20261003/checkpoint.md` |
| 2026-10-03 | D01 done; UI01 Monday slice built (original console on V3 engine) with 7 tests; release scripts (F04 partial); candidate 83d8160b… awaiting Windows checks and deploy | `reports/ui01_console_20261003/checkpoint.md` |
| 2026-10-03 | Documentation consolidated: six status files frozen with a banner, AGENTS.md end-of-session rule narrowed to this file + journal + evidence, standard prompt rewritten. Added UI01 after comparing the original `pipeline_app.py` (8 pages, 5-stage run) with the V3 single-page workbench | `reports/doc_consolidation_20261003/` |
| 2026-10-03 | Read the unique content of all 16 files in the standard prompt's reading list; added cohort, Cortex context, stale-record warning, historical estimate, V2 path, R00 and AI03.3 | This session |
| 2026-10-03 | Session protocol added: AGENTS.md now requires reading this file first and updating section 0 and this log at session end | `AGENTS.md` |
| 2026-10-03 | Tracker created from the existing V3 documents and a re-verification of the deployed build (hash match; 268/273 tests pass on Streamlit 1.52.2 with 4 environment-related failures and 1 resolved by a missing package) | This session; `reports/original_presentation_20261002/binding_final/` |
