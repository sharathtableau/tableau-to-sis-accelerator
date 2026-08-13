# The 4-workbook trust demo — deploy + run steps

Goal: show the accelerator convert 4 real Tableau workbooks live, stage by
stage, ending in a validation that PROVES the numbers — not a canned demo.

**The 4 workbooks** (chosen because they're proven 100% clean — see
"Verified state" below):

| Workbook | Sheets | Notes |
|---|---|---|
| `Workbooks/Superstore.twbx` | 20 | Multi-datasource (Orders + Sales Commission + Sales Target), what-if params |
| `Workbooks/E-Commerce (Software) Sales Dashboard VOTD.twbx` | 50 | The frontier workbook — relationship extract (3 tables), 88 calcs, table-calc engine |
| `Workbooks/Regional Analysis.twbx` | 6 | Relationship extract (3 tables: Orders + People + Returns) |
| `Workbooks/World Indicators.twbx` | 9 | Date-part filters, choropleth map |

---

## What the demo shows (the app: `pipeline_app.py`)

One Streamlit app. Upload a `.twb`/`.twbx`, watch 5 stages run live:

1. **Discovery** — datasources found, data extracted, tables loaded
2. **Parsing** — workbook → IR: sheets, chart kinds, calcs, filters
3. **Data Model & Semantic Layer** — two honest sub-steps: **3a Data Model**
   shows the workbook's Tableau join/relationship graph and, when the tables
   exist separately in Snowflake, deploys a real `CREATE VIEW` replicating them
   (a flattened extract is labelled honestly, not faked); **3b Cortex layer**
   *(optional)* — Tableau calcs → a native `CREATE SEMANTIC VIEW`, only when
   there are metrics to expose, skipped-if-exists (**the Cortex element** —
   what Cortex Analyst / Snowflake Intelligence query)
4. **App Creation** — IR → generated Streamlit app, rendered live inline
5. **Validation** — every measure proven two independent ways (app's own SQL
   vs a direct source read) or, for calculated fields, execution-gated +
   cross-checked against known Tableau figures. Ends in a downloadable
   `.ipynb` validation notebook — the artifact you can hand someone as proof.

After the 5 stages: a **human-gated "🚀 Deploy to Snowflake" button** actually
ships the generated app to Streamlit-in-Snowflake (via the Snowpark session, no
CLI), and a **"💬 Ask your data"** panel lets you query the deployed semantic
view in plain English through Cortex Analyst.

---

## Deploy to Snowsight (one time)

Prereqs: `snow` CLI installed, connection configured (`snow connection add`
if you haven't; this project's connection is named `wbr`). CSV/Excel-based
workbooks (Superstore, E-Commerce) load their tables in-app on upload;
hyper-ONLY workbooks (Regional Analysis, Global Sales) need the one-time
`preload_demo.py` step first (see "`.hyper` extracts" under "Run the demo").

```bash
snow streamlit deploy pipeline_demo --replace --connection wbr
```

This reads the `pipeline_demo` entity in [snowflake.yml](snowflake.yml) —
uploads `pipeline_app.py` (main file) plus `pipeline.py`, `parity.py`,
`cortex_semantic.py`, `tableau_parser.py`, `init_workbook.py`,
`calc_translator.py`, `codegen.py`, `engine.py`, `backend.py`, `config.py`,
`findings.py`, `profile_superstore.py`, `environment.yml` (pins
`streamlit=1.52.2` — without this SiS falls back to a pre-1.23 default and
breaks). No `datasources.json` is pre-seeded: the app is workbook-agnostic —
deploy once, demo any book, `config.DATASOURCES` gets repointed per upload.

App name in Snowsight: **`TABLEAU_TO_SIS_PIPELINE_DEMO`**
(Snowsight → Projects → Streamlit).

**First SSO note:** `snow` may open a browser window for identity-provider
login on first use in a session — approve it, the CLI resumes automatically.

---

## Run the demo

1. Open `TABLEAU_TO_SIS_PIPELINE_DEMO` in Snowsight.
2. Upload one of the 4 workbooks (drag `.twbx` onto the uploader).
3. Narrate each stage as it appears — they render progressively, same as
   local (native Streamlit `st.spinner`/`st.progress`, not a batch dump at
   the end).
4. At Stage 3, point out: the `CREATE SEMANTIC VIEW` DDL shown is **actually
   executed** against the account (when running in Snowflake) — say so, then
   optionally show it in a worksheet: `DESCRIBE SEMANTIC VIEW
   WBR_DB.PUBLIC.<STEM>_SEMANTIC;`
5. At Stage 5, the payoff: **"Measures PASS X/X, 0 bugs"** — this is the
   trust proof. Download the `.ipynb` and mention it's a reviewable artifact,
   not a claim.
6. Repeat for the other 3 workbooks, or take questions between each.

**`.hyper` extracts** (Regional Analysis and Global Sales are hyper-ONLY;
E-Commerce/World Indicators also ship one): `tableauhyperapi` isn't in
Snowflake's Anaconda channel, so a `.hyper` cannot be decoded inside a
Streamlit-in-Snowflake sandbox. Such a workbook must be onboarded **once from
a laptop** — the decode runs there — into the same schema the demo app reads:

```bash
python preload_demo.py "Workbooks/Regional Analysis.twbx"   # --connection wbr
python preload_demo.py "Workbooks/Globalsalesdashboard.twbx"
```

`preload_demo.py` reuses the exact same load logic (`pipeline.load_into_snowflake`)
the app expects, so the tables land in `WBR_DB.PIPELINE_DEMO` with the exact
names the sheets + semantic view look for. **Re-upload the same workbook in
Snowsight afterwards and the app reuses those tables automatically** (Stage 1
reports `existing (pre-loaded)`). If you upload a hyper-only workbook that was
NOT pre-loaded, Stage 1 stops cleanly with this exact remediation instead of
cascading `does not exist` errors through the later stages (intentional —
never silently guessed, never a wall of tracebacks).

---

## Run it locally first (recommended before presenting)

```bash
streamlit run pipeline_app.py --server.port 8510
```

or via the project's launch config: the `pipeline-ui` entry in
`.claude/launch.json`. By default a local run uses DuckDB and Stage 3 only
shows the semantic-view DDL (no session to deploy it).

### One-upload migration incl. `.hyper` workbooks (local → Snowflake)

Because a `.hyper` can't be decoded inside Snowflake, the way to migrate a
hyper-only workbook (Regional Analysis, Global Sales) in a **single upload** is
to run this same app locally and let it push to Snowflake:

1. `streamlit run pipeline_app.py --server.port 8510`
2. In the sidebar, tick **"Push to Snowflake on upload"** (connection defaults
   to `wbr`). First upload opens a browser for SSO.
3. Upload any workbook. One upload now does it all: decode the `.hyper` **here**
   (laptop has the Hyper engine) → load tables into `WBR_DB.PIPELINE_DEMO` →
   deploy the `CREATE SEMANTIC VIEW` → render + validate.

The migrator is a tool that runs outside; the migrated output (tables,
semantic view) lands inside Snowflake — the SiS story is intact. The
Snowsight-hosted copy still handles CSV/Excel-sourced workbooks live, but
cannot decode a `.hyper` (hard platform limit — no Hyper engine in the sandbox).

---

## Verified state (re-run before presenting to catch drift)

```bash
python tests/test_regression.py
```

Full regression suite green as of 2026-07-25 (run `python weekly_status.py` for
the live gate count), including:
- `test_pipeline_reuses_preloaded_table` — the hyper-only-workbook fix: a
  datasource whose `.hyper` can't be decoded in Snowflake reuses a pre-loaded
  table if one exists, else is flagged MISSING so Stage 1 stops cleanly.
- `test_parity_validation` — runs the actual Stage-5 logic against all 4 demo
  workbooks, asserts **0 bugs**. This is the same check the live demo shows.
- `test_cortex_semantic_generation` / `test_cortex_calc_fallback_guards` —
  the Cortex-layer trust scaffolding (deterministic, offline-testable parts).
- `test_datepart_member_as_full_date` — the fix that made World Indicators
  demo-clean (a date-part filter whose members are full dates used to crash
  render).

If this suite is red, **do not present** — fix first. That's the whole point
of the validation stage: no claim without a check behind it.

---

## Data (already done for these 4 workbooks; re-run only if data changes)

```bash
python load_snowflake.py --database WBR_DB
```

Loads all tracked corpus datasources (11 tables) to `WBR_DB.PUBLIC`, writes
`datasources.deploy.json`. Verify row counts independently before trusting a
fresh load:

```sql
SELECT SUM(SALES), SUM(PROFIT), SUM(QUANTITY) FROM WBR_DB.PUBLIC.SAMPLE_SUPERSTORE;
-- expect 2,326,534 | 292,297 | 38,654
```

---

## If something breaks mid-demo

- **A stage errors on a workbook you haven't tried before**: that's real —
  the app shows the traceback in an expander rather than a blank screen.
  Screenshot it, don't paper over it; every gap here becomes a construct rule
  (this project's whole discipline).
- **Validation shows a BUG row**: read the "value / error" column — usually a
  wrong-table routing issue or a genuinely untranslated calc. This is the
  system working, not failing — surfacing a real problem before a client
  would have found it in production.
