# Enterprise Accelerator Hardening Plan

**Project:** Tableau to Streamlit-in-Snowflake Accelerator  
**Owner:** Sharath Kumar  
**Created:** 2026-09-11  
**Target:** Consistently produce at least an 80% functional and visual match for supported Tableau workbook patterns, while reporting unsupported constructs honestly.

## 1. Purpose

The accelerator must not consider a migration successful merely because it generated Python files or opened a Streamlit page. A successful migration must preserve the workbook's important data, chart semantics, dashboard structure, formatting, and interactions closely enough to be usable.

The immediate objective is not universal pixel-perfect Tableau reproduction. The objective is an enterprise-safe conversion process that:

- Produces at least an 80% functional and visual match for supported workbook patterns.
- Protects data correctness as the highest-priority requirement.
- Detects unsupported or ambiguous Tableau features before deployment.
- Fails transparently and locally instead of breaking the entire application.
- Generates evidence showing what passed, what was approximated, and what remains unsupported.
- Improves through generic feature-class rules rather than workbook-specific exceptions.

## 2. Definition of migration success

Every generated workbook must receive an automated acceptance score.

| Category | Weight | Required behavior |
|---|---:|---|
| Data correctness | 35% | Aggregates, dimensions, filters, row counts, and calculation results match Tableau evidence. |
| Chart semantics | 25% | Mark type, shelves, aggregation, sorting, layering, axes, and encoding roles are preserved. |
| Dashboard layout | 20% | Containers, grouping, ordering, relative widths/heights, and major information hierarchy are retained. |
| Formatting | 10% | Titles, labels, number formats, colors, legends, and visibility settings are materially similar. |
| Interactions | 10% | Important filters, parameters, navigation, and supported actions operate correctly. |

A migration passes only when all of the following are true:

- Overall weighted score is at least **80%**.
- Data-correctness score is at least **95%**.
- No dashboard crashes during startup or navigation.
- No critical chart is blank.
- No critical calculated field is silently dropped.
- Every unsupported construct has a visible finding and an explicit fallback or blocker.
- The generated application is verified in its target Snowflake environment.

An application below the threshold must be labelled **Needs review** or **Blocked**, never **Complete**.

## 3. Engineering principles

### 3.1 Fix feature classes, not workbooks

Production code must never branch on a workbook, dashboard, or worksheet name to correct rendering behavior.

Every newly discovered issue must produce:

1. A clearly identified Tableau feature class.
2. A minimal XML or workbook regression fixture.
3. A generic parser or calculation-translation rule.
4. A generic rendering rule when applicable.
5. A positive test proving the feature works.
6. A negative/control test proving unrelated behavior is unchanged.
7. Evidence from the reported workbook and, when possible, another workbook.

Examples of valid feature classes include customized KPI labels, duplicate-measure dual axes, parameter-swapped measures, nested containers, dashboard filters, reference lines, table calculations, maps, sets, and dashboard actions.

### 3.2 Read authored Tableau metadata

The converter should use Tableau's declared XML metadata wherever it exists. It should not infer presentation from workbook names or field-name keywords when an authored definition is available.

Metadata that must be retained in the intermediate representation includes:

- Dashboard container hierarchy and geometry
- Tiled versus floating placement
- Worksheet mark layers and pane order
- Rows, columns, Text, Color, Size, Shape, Detail, and Tooltip shelves
- Aggregations and calculated-field dependencies
- Number and date formats
- Customized label templates and run-level styling
- Axis titles, visibility, synchronization, and reversal
- Worksheet and dashboard titles, including parameter tokens
- Filters, parameters, domains, and control placement
- Legends and dashboard navigation objects
- Datasource, table, relationship, join, blend, and custom-SQL dependencies
- Unsupported extensions, actions, or presentation constructs

### 3.3 Never fail silently

Missing metadata, unresolved calculations, failed queries, or unsupported marks must create structured findings. A sheet-level problem must not prevent independent sheets from rendering.

## 4. Required accelerator workstreams

### Workstream A — Pre-migration compatibility assessment

Before app generation, inspect the workbook and produce:

- Workbook and dashboard inventory
- Datasource and connectivity inventory
- Detected feature classes
- Supported, partial, and unsupported features
- Untranslated or ambiguous calculations
- Required Snowflake objects and privileges
- Predicted fidelity score and confidence
- Critical blockers and available fallbacks

#### Acceptance criteria

- A user can see risks before starting deployment.
- Unsupported extensions and calculations cannot disappear silently.
- The assessment result is stored with the migration run.
- The pipeline can block clearly non-viable migrations before creating an app.

### Workstream B — Validated intermediate representation

Define and validate a formal IR schema between Tableau parsing and Streamlit rendering.

The validator must check:

- Every layout node has a recognized node type.
- Container nodes have valid children.
- Sheet leaves reference existing worksheets.
- Every displayed measure and dimension resolves to SQL or an explicit finding.
- Layer order and duplicate-measure panes are preserved.
- Formatting and visibility metadata uses normalized values.
- Filter and parameter references resolve to known fields or definitions.
- Datasource routing resolves to a verified Snowflake object or a declared materialization step.

#### Acceptance criteria

- Invalid IR is rejected before runtime.
- Errors such as `KeyError: children` cannot reach the generated application.
- IR validation reports the exact dashboard, sheet, node, or field responsible.

### Workstream C — Generic chart and calculation handlers

Maintain a handler registry for supported feature classes rather than a chain of workbook-dependent conditions.

Priority handlers:

1. KPI and BAN cards, including customized labels and conditional deltas
2. Standard bars, lines, areas, pies, donuts, scatterplots, tables, and maps
3. Dual-axis and layered charts
4. Parameter-driven measure and dimension swaps
5. Reference lines and bands
6. Date grains, fiscal periods, and relative-date filters
7. Top-N, sets, groups, and context-filter order of operations
8. Table calculations and supported level-of-detail calculations
9. Floating/tiled container composition
10. Dashboard navigation and supported actions

#### Acceptance criteria

- Every handler has an isolated regression fixture.
- Every handler has a declared support level: full, partial, fallback, or unsupported.
- New handlers cannot reduce existing corpus scores below their approved baselines.

### Workstream D — Graceful degradation

When exact conversion is unavailable, use an explicit, deterministic fallback.

| Failure | Enterprise fallback |
|---|---|
| Unsupported chart mark | Render a basic chart or crosstab with a visible finding. |
| Unresolved calculated field | Show the calculation, dependent sheets, and blocker; do not substitute a guessed value. |
| Failed worksheet query | Render a sheet-level error card while continuing other sheets. |
| Unsupported dashboard action | Preserve the source/target sheets and report the missing action. |
| Ambiguous datasource routing | Stop deployment and request an explicit mapping. |
| Missing Tableau formatting | Use a neutral default and record an approximation. |

#### Acceptance criteria

- One broken sheet cannot crash the whole dashboard.
- A fallback is never presented as an exact match.
- Critical data uncertainty blocks deployment.

### Workstream E — Automated validation and 80% gate

Validation must be part of the normal pipeline, not an optional activity after deployment.

For every dashboard:

1. Execute every generated chart query.
2. Capture generated Streamlit chart data.
3. Pull Tableau crosstab or view data where available.
4. Compare Tableau, Streamlit, and backend aggregates.
5. Capture Tableau and Streamlit dashboard images.
6. Evaluate chart semantics and layout structure.
7. Test placed filters and parameters.
8. Calculate the weighted fidelity score.
9. Generate a consolidated migration report.
10. Block production deployment if mandatory gates fail.

#### Validation report and download policy

The enterprise version retains three approved client-facing capabilities:

- the complete proof-first HTML report displayed inline;
- one download of the exact same self-contained HTML report; and
- a structured migration report with PDF export.

The HTML report and migration PDF have different presentation purposes but
must read the same persisted validation result. Neither report may recompute or
reinterpret the final decision independently. Both must identify the workbook,
run ID, accelerator version, evidence timestamp, tolerances, and final
PASS/REVIEW/BLOCKED/UNSUPPORTED state.

Engineering notebooks, raw JSON, issue CSVs, chart-comparison CSVs, and
individual screenshots remain auditable artifacts rather than primary download
buttons. Place notebooks behind an advanced diagnostic surface. Reveal manual
crosstab upload only when automated Tableau evidence is unavailable or a sheet
remains unresolved. AI commentary may be included as advisory evidence but
cannot override the deterministic release gate.

#### Acceptance criteria

- The report contains a score and evidence for every dashboard.
- The inline HTML, downloaded HTML, and migration PDF reference the same run
  and cannot present conflicting final decisions.
- Missing evidence produces `Not tested` or `Blocked`, never an assumed pass.
- Data mismatches identify the affected field, calculation, aggregation, filter state, and sheet.
- The deployment button is disabled when mandatory gates fail.

### Workstream F — Golden workbook portfolio

Build a permanent set of 20–30 small, diverse Tableau workbooks. Each workbook should isolate one or more feature classes.

Required coverage includes:

- KPI dashboard
- Executive dashboard with nested containers
- Operational dashboard with tables
- Parameter and filter dashboard
- Dual-axis dashboard
- Table-calculation dashboard
- LOD calculation dashboard
- Top-N and context-filter dashboard
- Map dashboard
- Multiple-datasource relationship workbook
- Blend workbook
- Live Snowflake workbook
- Custom SQL workbook
- Device-layout workbook
- Dashboard actions/navigation workbook

The Sales, BDO Pre-Optimization, Superstore, Regional Analysis, Global Sales, E-Commerce, and Fil Test workbooks should remain part of the broader regression corpus.

#### Acceptance criteria

- Every production issue becomes a permanent regression fixture.
- Baseline fidelity is stored per workbook and dashboard.
- CI fails when a supported feature or approved score regresses.

### Workstream G — Observability and reproducibility

Persist the following for every migration run:

- Run ID and timestamps
- Source workbook name and content hash
- Tableau site, project, workbook, and view identifiers
- Accelerator version and deployment version
- Parsed feature inventory
- Datasource-resolution decisions
- Calculation translation findings
- Query and render results per sheet
- Validation evidence and fidelity scores
- Generated app object and URL
- Deployment status and failure reason

#### Acceptance criteria

- A failed run can be reproduced using its workbook hash and engine version.
- Logs never contain PAT secrets or other credentials.
- Users can download a concise migration report without reading raw logs.

### Workstream H — Release and deployment discipline

The project currently has a shared and heavily modified worktree. Enterprise releases require a controlled process.

Required controls:

- One release branch or reviewed release commit
- Clear ownership for overlapping changes
- Clean diff review before deployment
- Full regression execution
- Versioned deployment bundle
- Snowflake object verification after deployment
- Verification that the Tableau EAI and PAT secret remain bound
- Smoke test of the live application
- Last-known-good rollback bundle
- Release notes describing feature classes changed

#### Acceptance criteria

- No deployment comes from an unknown mixture of edits.
- Every deployed version can be identified and rolled back.
- Deployment success includes a live-page smoke test, not only successful file upload.

## 5. Demo-readiness plan

### 5.1 Select the demo portfolio

Choose four to six representative workbooks before the demonstration:

1. One known-good baseline workbook
2. Sales Dashboard
3. BDO Pre-Optimization Dashboard
4. A filter/parameter-heavy workbook
5. A table or operational workbook
6. One moderately complex workbook to demonstrate transparent assessment

Do not introduce an untested workbook during the primary demonstration flow.

### 5.2 Validate every demonstration workbook

For each workbook, record:

- Tableau source screenshot
- Generated Streamlit screenshot
- Data-validation result
- Overall fidelity score
- Known approximations
- Expected migration duration
- Required Snowflake objects
- Recovery procedure

### 5.3 Freeze the demonstration release

After the selected workbooks pass:

- Stop non-critical feature work.
- Run the complete regression suite.
- Deploy a versioned bundle.
- Verify Tableau authentication and Snowflake external access.
- Rehearse the complete workflow at least twice.
- Retain a known-good generated app for connectivity contingencies.

### 5.4 Recommended demonstration statement

> The accelerator performs automated Tableau workbook discovery, conversion, Snowflake-native deployment, and validation. Supported workbook patterns target at least 80% visual and functional fidelity, with strict data-correctness checks. Unsupported constructs are identified before deployment and reported transparently.

Do not claim universal or pixel-perfect conversion.

## 6. Prioritized implementation backlog

### P0 — Must complete before the Snowflake demonstration

- [ ] Select and freeze the demonstration workbook portfolio.
- [ ] Run every selected workbook through the deployed pipeline.
- [ ] Add the weighted 80% acceptance score to the migration result.
- [ ] Enforce the 95% minimum data-correctness gate.
- [ ] Block completion when critical charts are blank or calculations unresolved.
- [ ] Produce one consolidated report per workbook.
- [ ] Fix only reusable feature-class failures found by the demo portfolio.
- [ ] Run the complete regression suite after every engine change.
- [ ] Create a versioned demo release and rollback bundle.
- [ ] Rehearse the complete Snowflake workflow twice.

### P1 — Enterprise reliability

- [ ] Implement formal IR schema validation.
- [ ] Add sheet-level exception isolation and fallbacks.
- [ ] Add migration-run persistence and version metadata.
- [ ] Add automatic live application smoke testing.
- [ ] Add baseline fidelity regression checks.
- [ ] Expand the golden portfolio to at least 20 feature-diverse workbooks.
- [ ] Add controlled release branches and reviewed deployment manifests.

### P2 — Broader Tableau coverage

- [ ] Expand supported dashboard actions.
- [ ] Improve floating-object and device-layout handling.
- [ ] Expand table-calculation and LOD coverage.
- [ ] Improve maps, rich tooltips, annotations, and extensions handling.
- [ ] Add non-Snowflake live-source replication patterns.
- [ ] Improve typography, collision handling, and responsive layout fidelity.

## 7. Definition of enterprise-ready

The accelerator can be described as enterprise-ready only when:

- At least 20 structurally different golden workbooks are continuously tested.
- Supported-pattern workbooks consistently score at least 80%.
- Critical data comparisons meet the 95% minimum.
- Unsupported features are discovered before deployment.
- A sheet failure cannot crash an entire dashboard.
- Every migration produces an evidence-backed report.
- Releases are versioned, reviewable, reproducible, and reversible.
- Secrets remain in Snowflake Secret objects or approved environment mechanisms and never appear in code, logs, reports, or generated files.
- The deployed app and its Tableau connectivity are automatically smoke-tested.

## 8. Immediate next action

Run the selected demonstration portfolio through `WBR_DB.PUBLIC.TABLEAU_TO_SIS_PIPELINE_DEMO`, capture the results, and create a feature-class issue list ordered by impact on the weighted acceptance score. The next engineering change should address the highest-impact generic failure and add a regression fixture before deployment.
