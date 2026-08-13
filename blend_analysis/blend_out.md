<!-- Slide number: 1 -->

BLEND360
Data & Analytics · Snowflake Modernization Demo
TABLEAU TO STREAMLIT-IN-SNOWFLAKE
Modernizing BI
on Snowflake
Unified BI operating model

SIMPLIFY
TRUST
EVOLVE
Unified BI operating model
Automated migration validation
AI-assisted applications
OBJECTIVE  —  Modernize suitable Tableau workloads into governed, AI-ready Streamlit applications in Snowflake while reducing platform duplication and preserving trusted business outcomes.
Sharath Kumar  |  Snowflake modernization demo

### Notes:
Timing: 1 minute. Open with modernization, not tool replacement. The goal is to move suitable BI workloads onto a governed, extensible Snowflake application foundation.

<!-- Slide number: 2 -->
BLEND360
BI MODERNIZATION · 02
The data platform modernized, but BI still operates separately
Snowflake is the governed system of record; Tableau remains a second platform, control plane and cost base.

CURRENT OPERATING MODEL
THE RESULT

Separate cost
$

Snowflake
Tableau
Tableau licenses, infrastructure or cloud subscription
+
Data + governance
BI consumption
Duplicated operations
?

Administration, access, monitoring and deployment
Two platforms to license, secure, administer, monitor and release.
Data movement
?

Extract creation, refresh schedules and failure handling
Logic duplication
?

Business calculations repeated across ELT and dashboards

This is the situation the modernization program is designed to change.
Limited extensibility
?

Additional applications needed beyond dashboard interaction
02 / 13

### Notes:
Timing: 3 minutes. Explain the situation before mentioning the accelerator. Do not attack Tableau; describe the operational duplication created when the data platform and BI platform evolve separately. Replace generic cost language with client figures when available.

<!-- Slide number: 3 -->
BLEND360
BI MODERNIZATION · 03
Modernization creates value beyond visualization
The opportunity is larger than replacing a visualization tool.

?
?
?
?

Platform economics
Delivery speed
Governance
AI-ready experiences
LOWER DUPLICATE COST
SHORTER CHANGE CYCLES
FEWER CONTROL GAPS
NEW CLIENT EXPERIENCES
Potentially reduce Tableau licenses and duplicated platform support for migrated users.
Build on governed Snowflake data using reusable Python and Streamlit components.
Reuse Snowflake RBAC, masking policies, semantic assets and controlled deployment.
Extend migrated apps with CoCo and add natural-language analytics through Cortex Analyst.

Measure the business case  —  baseline licenses, platform support, extract operations and dashboard change lead time before the pilot.
03 / 13

### Notes:
Timing: 3 minutes. Leadership should hear business outcomes; Snowflake should hear platform consolidation; BI developers should hear faster iteration and extensibility. License savings are potential until client-specific TCO is calculated.

<!-- Slide number: 4 -->
BLEND360
AI-ASSISTED BI LIFECYCLE · 04

VISION · SNOWFLAKE COCO
The migrated application becomes an AI-assisted product, not a static replica
Natural-language enhancement changes the economics of maintaining and extending BI applications.
1
2
3
4
5

Migrate
Validate
Deploy
Enhance
Revalidate
Tableau to
Streamlit
Visual, data,
formulas
Governed
SiS app
CoCo
instructions
Controlled
release

COCO / CORTEX CODE
CORTEX ANALYST
Generates reviewable application changes from natural-language requirements.
Adds governed natural-language questions over semantic views.

“Add a region filter to every chart, preserve existing calculations, and create a customer-detail view on selection.”
Every AI-assisted change returns through regression and validation before release — CoCo lowers dependence on specialized coding skills, but production changes are never unreviewed.
04 / 13

### Notes:
Timing: 3 minutes. This is a primary modernization advantage, but say clearly this is where the roadmap is headed next, not a capability wired into the accelerator's current five stages. Say that CoCo lowers dependence on specialized coding skills; do not promise that production applications can be changed without engineering review. Every AI-assisted change returns through regression and validation.

<!-- Slide number: 5 -->
BLEND360
ACCELERATOR DEMO · 05
One workbook moves through five controlled stages
Each stage leaves an inspectable artifact before the next stage proceeds.

01
02
03
04
05
Discovery
Parsing
Data model
App creation
Validation
?
?
?
?
Sources, extracts
and tables
Workbook to
migration IR
Relations and optional
semantic view
Generated Streamlit
application
Visual, data and
formula evidence

?  Human deployment gate
The application is deployed only after findings and validation are visible.
05 / 13

### Notes:
Timing: 3 minutes. This slide replaces the conflicting three-stage and seven-stage descriptions in earlier material. During the live demo, narrate the same five labels shown here.

<!-- Slide number: 6 -->
BLEND360
ACCELERATOR DEMO · 06
Visual parity is reviewed at dashboard level
The first check is immediate: does the migrated tab preserve the same analytical composition?

TABLEAU
STREAMLIT IN SNOWFLAKE
=

[ Insert dashboard screenshot ]
[ Insert dashboard screenshot ]

layout hierarchy

KPI presence

chart type

filters

labels

visible grain
Review evidence  —  visual similarity alone is not a pass; it is the first evidence layer.
06 / 13

### Notes:
Timing: 2 minutes. Visual similarity alone is not a pass. It is the first evidence layer. Point out that the chart colors can differ while data, hierarchy and interaction behavior still require independent checks. Replace the placeholder panes with the real Tableau vs Streamlit comparison screenshot before presenting.

<!-- Slide number: 7 -->
BLEND360
ACCELERATOR DEMO · 07
A PASS is earned with chart-grain numbers and formulas
The displayed visual grain determines the evidence: product rows for a ranked product table, not a dashboard total.
DATA PROOF
FORMULA PROOF
| Product | Tableau | Streamlit | Backend | Diff | Result |
| --- | --- | --- | --- | --- | --- |
| Canon imageCLASS 2200 | $61,600.00 | $61,599.82 | $61,599.82 | $0.18 | PASS |
| Fellowes PB500 | $27,454.00 | $27,453.78 | $27,453.78 | $0.22 | PASS |
| Cisco TelePresence | $22,638.00 | $22,638.28 | $22,638.28 | $0.28 | PASS |

Profit Ratio
TABLEAU
SUM([Profit]) / SUM([Sales])
SNOWFLAKE SQL
SUM(PROFIT) / NULLIF(SUM(SALES), 0)

Semantically equivalent + null-safe
The three displayed rows are representative slide evidence; the downloadable report retains the complete result set and every mismatch.

Absolute and relative tolerance are recorded in the downloadable report. Currency tolerance is expressed in currency, not points.
07 / 13

### Notes:
Timing: 4 minutes. The three displayed rows are representative slide evidence; the downloadable report retains the complete result set and every mismatch. Explain absolute and relative tolerance in the report and that currency tolerance must be expressed in currency, not points.

<!-- Slide number: 8 -->
BLEND360
ACCELERATOR DEMO · 08
Modernize the workloads that benefit; retain the workloads that do not
A selective portfolio approach protects value and avoids forcing every Tableau use case into Streamlit.

STRONG MIGRATION CANDIDATES

REVIEW CAREFULLY OR RETAIN
?
?

Snowflake-centric dashboards
Broad self-service authoring
?
?

Operational analytics applications
Dashboard actions and stories
?
?

Custom workflows and write-back
Specialist Tableau visualizations
?
?

Cortex-enabled experiences
Pixel-specific publications
?
?

High duplicated-platform overhead
Unsupported live-source patterns

Unsupported constructs receive a named outcome before deployment; they are never silently dropped.
08 / 13

### Notes:
Timing: 2 minutes. This is the only main-deck boundary slide. Position the program as selective modernization, not a universal Tableau replacement. Unsupported constructs are surfaced before deployment.

<!-- Slide number: 9 -->
BLEND360
ACCELERATOR DEMO · 09  ·  CORE PRESENTATION ENDS HERE
A controlled pilot should test trust, not just conversion speed
Proposed decision: approve a representative workbook pilot with explicit acceptance criteria.

5–8
100%
0
1
workbooks across simple,
moderate and complex patterns
unsupported constructs
surfaced before deployment
unexplained numeric
mismatches at accepted tolerance
reviewable evidence
package per workbook
PILOT OUTPUT

DECISION REQUIRED
•
Converted SiS app
Select the pilot workbooks, Snowflake environment, and acceptance owners.
•
Migration findings
•
Validation report
Ask Snowflake to help select the environment and verify the native deployment pattern; ask BI owners to approve semantic and visual evidence.
•
Coverage backlog
09 / 13

### Notes:
Timing: 3 minutes. Close on a decision, not a generic next-step list. Ask Snowflake to help select the environment and verify the native deployment pattern; ask BI owners to approve semantic and visual evidence.

<!-- Slide number: 10 -->
BLEND360

APPENDIX

Appendix: the corpus tests different migration risks
| Workbook | Fidelity | Primary stress case | Current evidence |
| --- | --- | --- | --- |
| Regional Analysis | 100% | Three-table relationship extract | Assessed clean |
| World Indicators | 99% | Date-part filters + map | Regression corpus |
| Superstore | 96% | Multi-datasource + parameters | 20 sheets |
| Global Sales | 95% | Four datasources | 14 of 15 |
| E-Commerce Software Sales | 97% | Table calcs + 50 sheets | Deployed to SiS |
| Top-N Measure Swap | 100% | Parameter-driven ranking | Numerically exact |
Fidelity is workbook-specific and reflects the recorded project corpus, not a blanket SLA.
10 / 13

### Notes:
Use only when asked about breadth. Explain that corpus fidelity is a directional product metric and must not replace workbook-level acceptance evidence.

<!-- Slide number: 11 -->
BLEND360

APPENDIX

Appendix: Cortex assists only where its output can be governed

DETERMINISTIC CORE
OPTIONAL CORTEX ASSISTANCE
1
?

Workbook parser
Semantic view generation
2
?

Intermediate representation
Guarded calculation proposal
+
3
?

Calculation rules
Narrative validation summary
4

Code generation

Execution-gated

Human-reviewed
5

Numeric validation
Cortex cannot convert a deterministic mismatch into a PASS.
No model decision is needed for the standard path.
11 / 13

### Notes:
Use when Snowflake asks where Cortex adds value. The concise answer: Cortex enriches the governed semantic experience and can propose translations for unsupported calculations, but deterministic checks retain authority.

<!-- Slide number: 12 -->
BLEND360

APPENDIX

Appendix: every dashboard tab receives the same evidence contract

1
2
3

Summary

Visual

Chart data
Dashboard and sheet verdicts with reasons
Tableau and Streamlit tab comparison when source capture is available
Every displayed key compared across Tableau, Streamlit and backend

4
5
6

Formulas

Interactions

Artifacts
Tableau calculation mapped to generated Snowflake SQL
Filters, parameters, sorting and tooltip behavior checked
Full rows, mismatches, queries and notebook retained for audit
The HTML report is the readable entry point; the notebook and row-level artifacts provide the complete proof set.
12 / 13

### Notes:
Use when the audience asks what is inside the generated report. Stress that the HTML is the readable entry point while the notebook and row-level artifacts provide the complete proof set.

<!-- Slide number: 13 -->
BLEND360

APPENDIX

Appendix: how the pipeline is actually built
One deterministic pipeline; Cortex is an opt-in branch, never a dependency in it.

1 · PARSE
2 · TRANSLATE
3 · GENERATE
?
?
workbook ? IR
? Snowflake SQL, by rule
? verified app.py

RENDERING ENGINE  —  19 chart types · layout · filters · live parameters

LOCAL · DUCKDB
SNOWFLAKE · SiS
CORTEX AI · OPT-IN
identical SQL, for build and testing
identical SQL, inside the account, governed by the app's own role
hard calcs · semantic view · ask-your-data
execution-tested,
then human-reviewed
Because no AI writes the app, the same workbook produces the same result every run.
13 / 13

### Notes:
New appendix slide. Use if a Snowflake engineer asks how the pipeline actually works end to end. Emphasize determinism: Cortex sits as a dashed, opt-in branch, never in the critical conversion path.
