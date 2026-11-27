# Practical Demo Architecture

**Project:** Tableau to Streamlit-in-Snowflake Accelerator  
**Owner:** Sharath Kumar  
**Documented:** 2026-09-11  
**Implementation start:** 2026-09-12  
**Status:** Isolated V2 implementation started; V1 deployment frozen  
**Target:** Snowflake demonstration build  
**Long-term companion:** [`AI_ASSISTED_ACCELERATOR_ARCHITECTURE_AND_DEMO_PLAN.md`](AI_ASSISTED_ACCELERATOR_ARCHITECTURE_AND_DEMO_PLAN.md)

## 1. Decision

The demonstration will not use a temporary architecture that must be replaced afterward. It will implement the first working vertical slice of the intended enterprise architecture.

The architecture is hybrid:

- Deterministic code remains authoritative for Tableau metadata, data identity, calculations, datasource relationships, filter scope, security, and application generation.
- Cortex provides structured assessment and discrepancy analysis for ambiguous or low-confidence mappings.
- Automated validation decides whether the migration is successful.
- Post-demo work extends the same architecture with bounded automatic repair. It does not replace the demo architecture.

The distinction presented to Snowflake is **implementation maturity**, not a future architecture change.

### 1.1 Demo isolation and UI decision (2026-09-11)

- The existing app `WBR_DB.PUBLIC.TABLEAU_TO_SIS_PIPELINE_DEMO` is a protected V1 baseline.
- Practical architecture work is implemented as the separate app `WBR_DB.TABLEAU_ACCELERATOR_V2.TABLEAU_TO_SIS_AI_ACCELERATOR_DEMO`.
- V2 uses its own schema, stage, persistence tables, deployment manifest, and main file.
- The current workbench navigation, styling, page structure, and migration experience remain the UI baseline because that experience has already been demonstrated internally.
- Architecture capabilities are added behind that UI and as evidence panels within its existing Inventory and Validation surfaces; this is not a UI redesign.
- The existing Tableau External Access Integration, PAT secret, and warehouse may be bound to V2, but V2 setup must not rotate, recreate, or alter those shared account objects.

## 2. Architecture

```text
Tableau Cloud / workbook upload
                |
                v
      1. Deterministic extraction
  workbook metadata, fields, calculations,
  filters, parameters, layouts, datasources
                |
                v
       2. Canonical migration IR
  Semantic IR + Visual IR + Interaction IR
       evidence and confidence included
                |
                v
      3. Compatibility assessment
 supported / partial / unsupported / blocked
                |
                v
      4. Deterministic app generation
     governed Streamlit-in-Snowflake app
                |
                v
       5. All-dashboard validation
 data + charts + layout + filters + parameters
                |
                v
       6. Cortex migration critic
 structured discrepancies and recommendations
                |
                v
  PASS / NEEDS REVIEW / BLOCKED / UNSUPPORTED
                |
                v
       7. Approved Snowflake deployment
```

Post-demo, this exact flow gains an additional bounded loop:

```text
Cortex recommendation
        -> constrained IR override
        -> rerender
        -> revalidate
        -> accept or reject
```

## 3. Architectural principles

### 3.1 Deterministic correctness

The following must never be guessed by an LLM:

- physical tables and columns;
- datasource routing;
- joins, relationships, unions, and blends;
- aggregations and calculation dependencies;
- filter scope and target worksheets;
- parameter datatype and domain;
- security objects, roles, secrets, and network integrations;
- production deployment commands.

### 3.2 Constrained AI

Cortex must return schema-constrained findings and recommendations. It must not directly generate or execute unrestricted production Python or SQL during the demo.

### 3.3 Evidence before success

A migration is not successful because an application file was generated or the first dashboard opened. All expected dashboards must be assessed and critical behavior must be verified.

### 3.4 Honest states

Every migration ends in exactly one state:

- **PASS:** Meets the acceptance threshold and has no hard blocker.
- **NEEDS REVIEW:** Generated successfully but does not have enough evidence or fidelity for automatic approval.
- **BLOCKED:** A critical data, runtime, filter, or deployment defect prevents use.
- **UNSUPPORTED:** The workbook contains a construct for which no safe equivalent or fallback is available.

## 4. Demo components

### Component A — Workbook intake

Inputs:

- Tableau Cloud/Server workbook selection; or
- uploaded `.twb`/`.twbx` workbook.

Outputs:

- workbook hash and migration run ID;
- Tableau workbook/project/view identifiers when available;
- extraction and connectivity status.

### Component B — Deterministic compatibility assessment

Display before conversion:

- dashboard and worksheet counts;
- datasource and relationship inventory;
- calculated field, filter, and parameter counts;
- detected chart and interaction classes;
- supported, partial, unsupported, and ambiguous constructs;
- required Snowflake objects and privileges;
- predicted conversion confidence;
- blockers and fallbacks.

The assessment must be stored with the migration run.

### Component C — Canonical migration IR

The existing intermediate representation becomes an explicit, versioned contract with four sections:

1. **Semantic IR:** fields, measures, calculations, aggregations, datasources, joins, and date semantics.
2. **Visual IR:** marks, shelves, axes, labels, colors, formatting, sorting, legends, and chart family.
3. **Layout IR:** dashboard containers, zones, visibility, geometry, order, and size relationships.
4. **Interaction IR:** filters, parameters, scopes, targets, navigation, and supported actions.

Every important IR node should include:

- source evidence;
- mapping confidence;
- support level;
- findings;
- fallback, if applicable.

For the demo vertical slice, the existing IR may be wrapped and versioned incrementally. A full rewrite is not required.

### Component D — Streamlit generator

The generator consumes the canonical IR and produces the Streamlit-in-Snowflake application through approved renderers.

Rules:

- no workbook-name-specific branches;
- no silent calculation or worksheet drops;
- one sheet failure cannot crash unrelated sheets;
- ambiguous datasource routing blocks instead of guessing;
- unsupported behavior receives a visible finding;
- generated applications carry the accelerator/run version.

### Component E — All-dashboard validation gate

Validation covers every expected dashboard, not only the initially selected tab.

Required checks:

- application starts without an exception;
- expected dashboard list matches the published workbook views;
- every critical worksheet renders non-empty marks;
- generated chart queries compile and execute;
- critical KPIs reconcile with Tableau or independently queried source evidence;
- visible filters expose valid values and change their intended targets;
- filters do not affect unrelated datasources or worksheets;
- visible parameters update intended charts, values, or titles;
- major layout groups and relative dimensions are preserved;
- visible labels and legends do not materially overlap or clip;
- deployed behavior matches the verified build.

#### Deferred validation UX contract for the next accelerator version

The present demo application remains unchanged. For the later product version,
the approved client-facing validation outputs are:

1. the proof-first HTML report rendered inline;
2. **Save a copy** for that same self-contained HTML report; and
3. the structured migration report plus its PDF download.

These are intentionally retained because they serve different users: the HTML
contains detailed chart-level proof, while the structured/PDF report supports
executive review and sign-off. They must nevertheless be projections of one
stored validation result—not independently computed verdicts.

Future UI simplification applies to the surrounding tools, not these three
outputs. Notebook downloads should move under **Advanced / engineering
artifacts**. The crosstab uploader should remain available as an evidence
fallback but appear only when Tableau REST evidence is unavailable or
incomplete. Raw JSON, issue CSVs, comparison CSVs, screenshots, and secondary
engine reports remain in the audit pack without becoming competing primary
download choices.

### Component F — Cortex migration critic

This is the first genuine AI component to implement for the demo architecture.

Inputs should be limited to relevant evidence:

- workbook compatibility assessment;
- normalized IR fragments;
- parser/runtime findings;
- Tableau dashboard screenshot;
- generated Streamlit screenshot;
- chart/data validation summary;
- allowed discrepancy categories.

Cortex returns structured JSON, not free-form code.

Proposed response contract:

```json
{
  "dashboard": "Metric Summary",
  "status": "NEEDS_REVIEW",
  "confidence": 0.91,
  "discrepancies": [
    {
      "category": "LAYOUT",
      "severity": "HIGH",
      "component": "KPI Summary",
      "evidence": "Six Tableau KPI cards were detected; generated output contains a different grouping.",
      "recommended_action": "Preserve the two-row, three-column layout container.",
      "safe_for_override": true
    }
  ]
}
```

Allowed discrepancy categories:

- `DATA`
- `CALCULATION`
- `DATASOURCE_ROUTING`
- `FILTER`
- `PARAMETER`
- `CHART_TYPE`
- `ENCODING`
- `LAYOUT`
- `LABEL`
- `FORMAT`
- `INTERACTION`
- `UNSUPPORTED`

For the demo, Cortex recommendations are advisory and visible. They do not automatically modify the production application.

### Component G — Migration decision and deployment

The validation service combines deterministic findings and the Cortex critique into one decision.

Deployment is enabled only when:

- no hard blocker exists;
- the selected demo workbook meets its approved baseline;
- critical data checks pass;
- all dashboards start successfully;
- accelerator regression gates pass;
- the target Snowflake object and security bindings are valid.

## 5. Demo fidelity contract

The architecture should calculate a weighted score:

| Area | Weight |
|---|---:|
| Data correctness | 35% |
| Chart semantics | 25% |
| Dashboard layout | 20% |
| Interactions | 15% |
| Styling | 5% |

For the demonstration portfolio:

- target overall score: at least 80%;
- required data correctness: at least 95%;
- no critical runtime, data, blank-chart, or filter blocker;
- missing evidence is `NOT TESTED`, never an assumed pass.

The first demo implementation does not have to fully automate the score if the existing validation evidence cannot yet support every category. It must, however, show honest category statuses and prevent unsupported claims.

## 6. What will be implemented on 2026-09-12

### P0 — Required vertical slice

1. Add an explicit architecture/version marker to every migration run.
2. Add or expose the compatibility assessment before conversion.
3. Normalize the existing IR into the four documented sections without breaking current consumers.
4. Attach support level and confidence to detected feature classes.
5. Make the all-dashboard acceptance result visible in the pipeline.
6. Implement a schema-constrained Cortex migration critic.
7. Persist the critic response and deterministic validation evidence by run ID.
8. Present one final `PASS`, `NEEDS REVIEW`, `BLOCKED`, or `UNSUPPORTED` state.
9. Preserve the current deterministic application generator and deployment path.
10. Add regression tests for the new contracts before deployment.

### P1 — Implement if P0 remains stable

- Display the weighted score using available evidence.
- Add a workbook-level dashboard matrix.
- Add direct links from a discrepancy to the affected dashboard/sheet.
- Add a downloadable assessment/validation report.
- Add model/version/token/cost metadata for Cortex calls.
- Add a feature-pattern lookup from previously verified migrations.

### Explicitly deferred until after the demo

- automatic AI modification of generated applications;
- autonomous production-code generation;
- automatic promotion of workbook overrides into the generic engine;
- multi-pass AI repair and rerender loop;
- Cortex Agent/Coding Agent as a mandatory production dependency;
- universal support claims for arbitrary Tableau extensions and actions;
- large renderer or IR rewrites that risk the known-good demo build.
- validation-page information architecture described in Component E,
  including advanced-artifact grouping and conditional crosstab fallback;
- unifying the HTML and migration-PDF generators on one immutable validation
  result while preserving both approved presentation formats.

## 7. Implementation sequence

### Step 1 — Baseline and safety

- Record the current deployed object and version.
- Run the complete regression suite.
- Confirm the current Tableau secret and External Access Integration.
- Preserve a known-good deploy bundle.

### Step 2 — Contracts

- Define the compatibility assessment schema.
- Define the canonical IR version wrapper.
- Define confidence/support enums.
- Define the Cortex critic input and output JSON schemas.
- Define final migration decision rules.

### Step 3 — Deterministic assessment

- Reuse existing parser inventories and audit coverage.
- Calculate feature support and confidence.
- Detect hard blockers before generation.
- Persist the assessment with the run.

### Step 4 — Cortex critic

- Implement the Snowflake Cortex call with structured output.
- Keep prompt inputs scoped to the active workbook/dashboard.
- Validate the response against the schema.
- Treat unavailable, invalid, or low-confidence AI output as advisory failure, not pipeline failure.
- Record model and response metadata.

### Step 5 — UI integration

Add four visible stages to the migration workbench:

1. **Assess**
2. **Convert**
3. **Validate**
4. **AI Review**

The existing deployment remains a controlled action after those stages.

### Step 6 — Verification

- Add unit and integration gates.
- Run the selected demo workbook portfolio.
- Capture every dashboard tab.
- Exercise every visible filter and parameter.
- Compare critical values.
- Run the full regression suite.
- Perform a clean Snowflake deployment and fresh-session verification.

## 8. Demo portfolio

Recommended initial portfolio:

1. **Sales Dashboard Ver 01** — standard KPI, bars, combo chart, scatter, and table.
2. **BDO Pre-Optimization Dashboard** — multiple datasources, parameters, KPI sparklines, funnel, trend analysis, and scoped filters.
3. **Sample Superstore** — broad standard Tableau feature coverage and established validation evidence.
4. Optional table/filter-heavy workbook after rehearsal.
5. Optional live Snowflake or relationship-model workbook after rehearsal.

No unassessed workbook should be introduced during the live presentation.

## 9. Snowflake presentation narrative

Recommended explanation:

> The accelerator uses deterministic compilation for data identity, calculations, relationships, filter scope, and security because those cannot be safely guessed. It produces a canonical migration specification and generates a governed Streamlit-in-Snowflake application. Every dashboard is validated for data, chart semantics, layout, and interactions. Cortex analyzes low-confidence or failed mappings and returns schema-constrained recommendations. The current release keeps those recommendations human-reviewed; the next implementation phase applies validated IR overrides and automatically reruns the same acceptance process.

This is the permanent architecture. Post-demo development increases automation and feature coverage without replacing its components or trust boundaries.

## 10. Expected Snowflake questions

### Why not use AI for the entire conversion?

An LLM must not guess business calculations, aggregations, joins, filter scope, or physical data identity. Deterministic compilation protects correctness; AI handles ambiguous presentation mappings and diagnosis.

### What happens when a new workbook is uploaded?

The workbook is assessed before conversion. Supported constructs compile through proven handlers. Ambiguous constructs are marked low-confidence and sent to the Cortex critic. Unsupported constructs are reported before deployment.

### What happens when the result is below 80%?

It is marked `NEEDS REVIEW` or `BLOCKED`, and the report identifies the affected dashboards and recommended corrections. After the demo phase, the controlled repair worker will propose an IR override, rerender, and revalidate.

### Does the accelerator support every Tableau workbook?

It assesses every workbook. The fidelity commitment applies only to workbooks that pass eligibility assessment. Unsupported constructs are never silently presented as converted.

### How does the accelerator improve over time?

Repeated successful corrections are converted into generic feature-class handlers, added to the golden workbook portfolio, and protected by regression gates. Workbook-specific Python branches are prohibited.

### Why Snowflake Cortex?

It allows governed structured reasoning over migration metadata and evidence inside Snowflake. It can later orchestrate controlled tools while Snowflake continues to govern the underlying data and application objects.

## 11. Demo change control

Until the Snowflake demonstration:

- no workbook-name-specific production behavior;
- no large framework rewrite;
- no AI-generated code applied automatically;
- no success declaration from one dashboard tab;
- no production change without regression coverage;
- no deployment without the complete suite passing;
- no simultaneous uncoordinated edits to core files;
- preserve a known-good bundle and redeployment procedure;
- verify the live application from a fresh browser session.

## 12. Demo-ready exit criteria

The architecture and build are demo-ready when:

- the four visible stages—Assess, Convert, Validate, and AI Review—are operational;
- the Cortex critic returns validated structured findings or an honest unavailable state;
- all selected demo workbooks pass their rehearsed dashboard checks;
- critical KPIs and filters are proven;
- all regression gates pass;
- the Snowflake deployment and Tableau connectivity survive clean recreation;
- the architecture narrative matches the code actually being demonstrated;
- the post-demo repair loop is clearly an extension of the same contracts.

## 13. Post-demo continuation

The first post-demo enhancement is the constrained repair loop:

1. Convert a Cortex recommendation into a candidate IR override.
2. Validate the override schema and field references.
3. Render the candidate in an isolated worker.
4. Re-run data, visual, and interaction validation.
5. Accept only measurable improvement with no correctness regression.
6. Repeat for at most three passes.
7. Store the approved override with the workbook hash and accelerator version.

This continuation adds automation without changing the architecture demonstrated to Snowflake.

In the same post-demo track, implement the deferred validation UX contract from
Component E. Treat the current inline HTML, HTML copy, and migration PDF as
compatibility requirements; simplify only redundant engineering-facing
controls and ensure every report format cites the same run ID and final
decision.
