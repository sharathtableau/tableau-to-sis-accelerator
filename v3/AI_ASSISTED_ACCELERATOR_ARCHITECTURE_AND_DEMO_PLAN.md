# AI-Assisted Tableau-to-Streamlit Accelerator Architecture and Demo Plan

**Project:** Tableau to Streamlit-in-Snowflake Accelerator  
**Owner:** Sharath Kumar  
**Created:** 2026-09-11  
**Status:** Parked architecture proposal plus immediate demo execution plan  
**Related document:** [`ENTERPRISE_ACCELERATOR_HARDENING_PLAN.md`](ENTERPRISE_ACCELERATOR_HARDENING_PLAN.md)

## 1. Decision

Use two deliberately separate tracks:

1. **Demo stabilization now.** Freeze the architecture, select a representative demo portfolio, validate every dashboard tab and interaction, and make only regression-tested feature-class corrections.
2. **Hybrid accelerator after the demo.** Evolve the current deterministic converter into a governed compiler with an AI planning and repair loop, a restricted dashboard DSL, and a mandatory evidence-based acceptance gate.

The demo track must not introduce a large autonomous-code-generation redesign immediately before the Snowflake demonstration. The post-demo design must not continue the current pattern of adding workbook-specific behavior to a growing renderer.

## 2. Problem statement

The current accelerator is primarily deterministic:

```text
Tableau XML/TWBX -> parser -> intermediate representation -> Python/Streamlit renderer
```

This works well when an uploaded workbook uses Tableau constructs already represented in the parser and renderer. A new workbook can expose a different combination of:

- datasource scoping;
- calculated-field identity;
- Measure Names/Measure Values behavior;
- nested or floating layout zones;
- table calculations and LOD expressions;
- parameter and filter placement;
- mark layers, custom labels, actions, extensions, or navigation.

When those constructs are missing or ambiguous, a deterministic renderer either guesses incorrectly, drops information, or fails. Fixing the visible workbook alone gives the appearance of progress without establishing portfolio-level reliability.

The solution is not to discard deterministic code. Data identity, aggregation, filter scope, and security must remain deterministic. The solution is to separate factual extraction from presentation decisions and use AI only inside a constrained, testable repair process.

## 3. Product contract for “80%”

The accelerator must not promise 80% compatibility with literally every Tableau workbook. Some workbooks contain extensions, external scripts, unsupported live sources, complex action chains, or presentation behavior that Streamlit in Snowflake cannot reproduce.

The defensible contract is:

> For a workbook that passes automated eligibility assessment, the accelerator produces at least an 80% functional and visual fidelity score, with at least 95% data correctness, no critical runtime errors, and explicit reporting of every approximation or unsupported feature.

### 3.1 Proposed score

| Area | Weight | Required evidence |
|---|---:|---|
| Data correctness | 35% | Tableau, generated chart data, and Snowflake aggregates reconcile within declared tolerances. |
| Chart semantics | 25% | Chart family, grain, aggregation, shelves, sorting, layering, axes, and encodings match. |
| Dashboard layout | 20% | Major containers, order, relative position, width, height, and information hierarchy match. |
| Interactions | 15% | Every visible filter and parameter changes the intended target sheets and does not affect unrelated sheets. |
| Styling | 5% | Material colors, number formats, labels, legends, borders, and visibility rules match. |

### 3.2 Hard blockers

An aggregate score cannot hide a critical failure. The migration fails if any of these are true:

- a visible dashboard or critical worksheet is missing;
- the application or a dashboard raises a runtime/SQL error;
- a material KPI is incorrect;
- a visible filter does nothing or queries the wrong datasource;
- a calculation is silently removed or replaced by an invented value;
- unsupported functionality is presented as successfully converted;
- the deployed Snowflake application behaves differently from the verified build.

## 4. Target hybrid architecture

```text
Tableau workbook + Tableau data/screenshots
                    |
                    v
       A. Deterministic workbook extraction
   XML, datasources, fields, calculations, zones,
   filters, parameters, actions, formats, metadata
                    |
                    v
          B. Validated canonical model
       Semantic IR + Visual IR + Interaction IR
       Every node includes evidence + confidence
                    |
          +---------+----------+
          |                    |
          v                    v
  High-confidence rules   Low-confidence planner
  compile immediately     AI proposes DSL mapping
          |                    |
          +---------+----------+
                    v
          C. Restricted dashboard DSL
  Approved charts, cards, tables, filters, layout,
  formatting, navigation, queries, and fallbacks
                    |
                    v
       D. Deterministic Streamlit generation
                    |
                    v
      E. Execute, capture, compare, and score
                    |
              score below 80%
                    v
       F. AI proposes a bounded IR override
       rerender -> revalidate, maximum 3 passes
                    |
                    v
       PASS / NEEDS REVIEW / UNSUPPORTED
```

## 5. Responsibilities by layer

### 5.1 Deterministic extraction and semantic compiler

This layer remains authoritative for facts and must never delegate them to an LLM:

- physical datasource, table, and column identity;
- relationships, joins, unions, blends, and custom SQL;
- field datatype, role, aggregation, aliases, and calculation dependencies;
- filter field, scope, domain, context, order of operations, and target sheets;
- parameter datatype, domain, placement, and dependent calculations;
- dashboard membership, zones, containers, geometry, and visibility;
- worksheet shelves, mark layers, axes, sorting, labels, legends, and formatting.

It produces a versioned canonical IR. Invalid or incomplete IR is rejected before application runtime.

### 5.2 Confidence classifier

Every worksheet, calculation, filter, and layout group receives a confidence level:

- **Exact:** metadata maps to a proven handler with regression coverage.
- **High:** supported feature combination with complete metadata.
- **Medium:** deterministic mapping exists but visual or interaction intent is ambiguous.
- **Low:** unknown feature combination or missing metadata; AI planning is required.
- **Unsupported:** no safe Streamlit-in-Snowflake equivalent exists.

The classifier drives routing. AI is not invoked for already-proven exact mappings.

### 5.3 Restricted dashboard DSL

AI must not directly generate arbitrary production Python. It proposes a schema-validated declarative specification such as:

```json
{
  "component": "horizontal_bar",
  "datasource": "media",
  "dimension": "channel",
  "measure": {"field": "spend", "aggregation": "sum"},
  "labels": {"visible": true, "position": "inside"},
  "filters": ["channel", "month"],
  "layout": {"row": 1, "column": 1, "width": 4, "height": 2},
  "confidence": 0.91
}
```

The DSL should contain a finite component catalogue and strict field references. The deterministic runtime validates and renders it. Unsupported properties cannot become executable code.

### 5.4 AI migration planner

For each low-confidence component, provide the model only the relevant evidence:

- normalized worksheet/dashboard XML fragment;
- canonical IR fragment;
- verified datasource schema;
- translated calculation dependencies;
- Tableau screenshot and generated screenshot;
- supported DSL schema and component catalogue;
- retrieved examples of similar successful feature patterns;
- current validation findings.

The response must be structured JSON containing the proposed mapping, evidence, confidence, expected differences, and fallback. Snowflake `AI_COMPLETE` supports schema-constrained structured output and is suitable for this planner.

### 5.5 Repair worker

The repair worker executes outside the normal end-user render request. It may:

1. inspect the workbook and IR;
2. run read-only Snowflake queries;
3. generate a candidate DSL override;
4. compile and render the candidate application;
5. capture every dashboard tab;
6. exercise visible filters and parameters;
7. compare Tableau and Streamlit evidence;
8. accept or reject the candidate;
9. repeat for at most three passes.

It must not modify the shared generic engine during a migration. A successful correction becomes a versioned workbook-level IR overlay. Only a separately reviewed, regression-tested feature rule may later be promoted into the generic engine.

### 5.6 Validation and release gate

Validation is part of conversion, not an optional activity after deployment:

- compile every generated query;
- reconcile critical KPIs and chart aggregates;
- render every dashboard, not only the initial tab;
- ensure every critical worksheet produces marks;
- exercise every visible filter and parameter;
- compare layout structure and images;
- calculate the weighted score;
- preserve evidence and findings with the run;
- prevent production deployment when hard blockers remain.

#### Validation deliverable contract (parked for the future version)

Decision recorded 2026-09-11: do not simplify validation to a single output
format. The future accelerator must preserve these three client-facing
capabilities:

1. an inline, self-contained proof-first HTML report in the Validation tab;
2. one **Save a copy** action that downloads the exact HTML bytes displayed
   inline; and
3. a structured migration report with a PDF download for executive review,
   audit circulation, and sign-off.

The proof-first HTML report is the detailed evidence surface. The migration
report/PDF is its concise governance and executive summary; it is not a second
validation authority. Both formats must consume the same immutable validation
result, run ID, evidence manifest, tolerances, and final decision so their
PASS/REVIEW/BLOCKED states cannot drift.

The future version should simplify only the surrounding engineering controls:

- move validation notebooks and SECTION notebooks into a collapsed
  **Advanced / engineering artifacts** area, or expose them only in a
  diagnostic build;
- show manual Tableau crosstab upload only when the live evidence pull is
  unavailable or identifies unresolved worksheets;
- do not expose README, summary JSON, issue CSV, per-chart CSV, or duplicate
  HTML downloads as separate primary buttons;
- keep raw artifacts in the persisted evidence pack for audit and automation,
  even when they are not presented as first-class UI downloads;
- allow AI Review to annotate the common validation result, but never let it
  silently replace the deterministic verdict.

This is a post-demo/future-version requirement. It does not authorize changes
to the currently deployed accelerator UI.

## 6. Snowflake capability mapping

| Requirement | Snowflake capability | Intended use |
|---|---|---|
| Structured AI planning | Cortex `AI_COMPLETE` structured outputs | Generate schema-valid DSL proposals and discrepancy classifications. |
| Visual comparison | Multimodal Cortex model invocation | Compare Tableau and Streamlit screenshots and return structured differences. |
| Governed orchestration | Cortex Agents | Plan and call approved custom tools for inspection, rendering, and validation. |
| Isolated repair execution | Cortex Agent code execution/Coding Agent or controlled notebook/container worker | Generate and test candidates outside the live application request. Treat preview capabilities cautiously. |
| Business semantic model | Snowflake semantic views | Store governed dimensions, facts, metrics, time dimensions, relationships, and filters. |
| Reusable SQL evidence | Cortex Analyst Verified Query Repository | Preserve approved business questions and SQL used for semantic validation. |
| Pattern memory | Snowflake tables and optionally Cortex Search | Retrieve previously proven feature-class mappings and failure resolutions. |
| External Tableau access | External Access Integration plus Snowflake Secret | Govern Tableau REST/API access without putting credentials into application code. |
| Application UI | Streamlit in Snowflake | Render the approved DSL using supported native components. |
| Run history and evidence | Snowflake tables, stages, tasks, and event/log data | Persist reproducible migration artifacts, scores, screenshots, and diagnostics. |

### Relevant Snowflake references

- [AI_COMPLETE structured outputs](https://docs.snowflake.com/en/user-guide/snowflake-cortex/complete-structured-outputs)
- [Cortex Agents](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-agents)
- [Cortex Agent code execution](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-agents-code-execution-tool)
- [Cortex Coding Agent](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-agents-coding-agent)
- [Cortex Analyst Verified Query Repository](https://docs.snowflake.com/user-guide/snowflake-cortex/cortex-analyst/verified-query-repository)
- [Semantic view YAML specification](https://docs.snowflake.com/en/user-guide/views-semantic/semantic-view-yaml-spec)
- [Streamlit in Snowflake limitations](https://docs.snowflake.com/en/developer-guide/streamlit/limitations)
- [External network access in Streamlit in Snowflake](https://docs.snowflake.com/en/developer-guide/streamlit/features/external-access)

## 7. Approaches considered

| Approach | Benefit | Problem | Decision |
|---|---|---|---|
| Continue deterministic rules only | Predictable and testable | Coverage grows slowly; unknown combinations repeatedly fail | Retain as the trusted compiler, but not as the entire product |
| Ask an LLM to generate a full Streamlit app | Flexible and fast for a prototype | Hallucinated fields/SQL, inconsistent code, security and maintainability risk | Reject for production |
| Embed or iframe the original Tableau view | High visual similarity | Not a migration; external embedding/scripts are constrained by Streamlit-in-Snowflake CSP | Reject as the core approach |
| Screenshot fallback | Preserves appearance | Static and non-interactive; cannot satisfy functional fidelity | Permit only as a clearly labelled evidence/fallback view |
| Hybrid compiler + constrained AI repair | Preserves deterministic correctness while adapting to unknown presentation patterns | Requires DSL, confidence model, automated evaluation, and controlled worker | Recommended target architecture |

## 8. Post-demo implementation roadmap

### Phase H1 — Canonical IR and eligibility assessment

**Objective:** Know whether a workbook is safely convertible before deployment.

- Version the Semantic, Visual, Layout, and Interaction IR schemas.
- Validate every IR node.
- Add confidence and evidence to every mapping.
- Produce a preflight compatibility report.
- Mark unsupported extensions, actions, sources, and calculations explicitly.
- Prevent clearly non-viable migrations from being labelled complete.

**Exit criterion:** Every workbook receives an eligibility verdict and predicted fidelity range before application generation.

### Phase H2 — Restricted dashboard DSL

**Objective:** Separate interpretation from rendering.

- Define supported component schemas.
- Build a deterministic DSL validator and renderer.
- Express existing chart handlers through the DSL.
- Add versioned workbook-level override support.
- Prohibit arbitrary SQL or Python in AI responses.

**Exit criterion:** Existing golden workbooks render through the DSL without lowering their approved baselines.

### Phase H3 — Automated acceptance harness

**Objective:** Make “80%” an enforceable release decision.

- Capture all dashboard tabs in both Tableau and Streamlit.
- Reconcile chart data and critical totals.
- Test filters, parameters, and navigation.
- Calculate the weighted score and hard blockers.
- Persist artifacts by workbook hash, accelerator version, and run ID.
- Generate the inline/downloadable proof-first HTML and the structured
  migration PDF from the same immutable acceptance result.
- Keep raw machine evidence persisted while limiting the primary UI to the
  approved HTML and migration-report outputs.
- Prevent deployment below the approved threshold.

**Exit criterion:** A migration cannot be marked complete without evidence for every dashboard.

### Phase H4 — Cortex planner and visual critic

**Objective:** Resolve ambiguous mappings without unrestricted code generation.

- Implement structured-output prompts for component classification and DSL repair.
- Add multimodal screenshot discrepancy classification.
- Retrieve similar proven mappings from the pattern catalogue.
- Require confidence, evidence, and explicit fallback in every response.
- Evaluate prompt/model versions against the golden portfolio.

**Exit criterion:** The AI planner improves low-confidence workbook scores without reducing data correctness or existing baselines.

### Phase H5 — Controlled agentic repair loop

**Objective:** Automatically improve candidates below 80%.

- Expose read-only inspection, SQL, render, capture, comparison, and candidate-write tools.
- Run in an isolated worker with bounded iterations and time/cost limits.
- Validate every candidate before accepting it.
- Store successful changes as workbook-level overlays.
- Promote repeated patterns into the generic engine only through normal review and regression tests.

**Exit criterion:** Eligible portfolio workbooks reach the target threshold automatically or return an honest review package.

### Phase H6 — Portfolio SLA and productization

**Objective:** Replace anecdotal workbook success with measurable coverage.

- Maintain 20–30 structurally diverse golden workbooks.
- Publish supported feature classes and known limits.
- Track first-pass success, repair success, elapsed time, Cortex cost, and regressions.
- Define supported workbook tiers and service-level objectives.
- Package the accelerator with least-privilege Snowflake roles, secrets, telemetry, and upgrade controls.

**Exit criterion:** Release readiness is decided by portfolio metrics, not by one demonstration workbook.

## 9. Immediate Snowflake demo plan

The demo should use the existing verified deterministic engine. The hybrid redesign remains parked until after the demonstration.

### 9.1 Freeze the demo scope

Select three to five workbooks representing different but currently supported structures:

1. **Sales Dashboard:** standard KPI, bar, combo, scatter, and table behavior.
2. **BDO Pre-Optimization:** multiple datasources, parameters, KPI sparklines, funnel, trends, and filters.
3. **Sample Superstore:** broad standard Tableau coverage and established regression evidence.
4. Optionally one table/filter-heavy workbook.
5. Optionally one live Snowflake or relationship-model workbook.

Do not introduce an unassessed workbook during the live presentation. A new workbook can be shown only after it passes the same rehearsal checklist.

### 9.2 Demo acceptance checklist per workbook

- Tableau workbook can be discovered or uploaded.
- All expected dashboards are listed; empty authoring sheets are absent.
- Every dashboard opens without an exception.
- Every critical worksheet renders non-empty marks.
- Critical KPIs match Tableau/source evidence.
- Every visible filter has at least two values and changes an intended target.
- Filters do not change unrelated datasource sheets.
- Parameter changes update the intended chart/title/metric.
- Major layout grouping resembles Tableau at the demo resolution.
- Labels and legends do not materially overlap or clip.
- Generated application deploys into Snowflake.
- A fresh browser session opens the deployed app successfully.
- Tableau secret and External Access Integration remain bound after recreation.
- A rollback/redeploy procedure is rehearsed.

### 9.3 Demo narrative

The demonstration should make three claims, each backed by visible evidence:

1. **Automated intake:** Select a Tableau workbook directly from Tableau Cloud or upload it.
2. **Governed conversion:** The accelerator parses workbook semantics, resolves Snowflake data, generates the Streamlit application, and reports unsupported constructs.
3. **Verified output:** Open multiple dashboard tabs, change filters, show key values, and display the validation evidence/deployment result.

Do not claim universal Tableau compatibility or pixel-perfect reproduction. Recommended wording:

> The accelerator automates migration for supported Tableau feature classes, validates every dashboard, and reports unsupported or approximate constructs. We are extending it into an AI-assisted repair architecture to broaden first-pass coverage while keeping data correctness governed and testable.

### 9.4 Demo change-control rule

Until the demonstration:

- no workbook-name branches;
- no production change without a regression fixture;
- no deployment without the full suite passing;
- no success declaration based on one tab;
- no simultaneous edits to the same core files without coordination;
- no new large framework or agent runtime in the demo build;
- clean recreation when deployed module caching can retain stale code;
- preserve the last known-good deploy bundle and object definition.

### 9.5 Proposed execution sequence

1. Confirm the final demo workbook list.
2. Run each through a clean migration using the deployed accelerator.
3. Capture every dashboard tab at the presentation resolution.
4. Exercise and record every visible filter and parameter.
5. Compare critical KPIs and chart totals with Tableau.
6. Classify failures by feature class and fix only P0 demo blockers generically.
7. Run all regression gates after each correction.
8. Perform a clean Snowflake deployment.
9. Repeat the entire demo once from a fresh browser session.
10. Freeze the build and keep a known-good fallback recording/screenshots.

## 10. Definition of demo-ready

The accelerator is ready for the Snowflake demonstration when:

- the selected portfolio completes the checklist above;
- all regression gates pass;
- no selected workbook contains a visible runtime error, missing critical chart, incorrect critical KPI, or non-functional filter;
- the deployed application is verified from a fresh session;
- the presenter has a rehearsed path and known-good backup;
- product limitations and the post-demo hybrid roadmap are stated honestly.

## 11. First action after the demo

Begin Phase H1, not the Cortex agent itself. A reliable AI repair system depends on a validated canonical IR, confidence labels, and an automated acceptance scorer. Adding an agent before those contracts exist would automate guessing rather than automate migration.

The first post-demo deliverable should therefore be:

> A preflight report that classifies every workbook feature, predicts conversion risk, validates the IR, and refuses to label a migration complete without an all-dashboard score.
