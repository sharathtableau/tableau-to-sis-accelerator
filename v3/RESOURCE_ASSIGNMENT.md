# Resource Assignment — Assignable Ticket Sheet

> **Archived assignment sheet (2026-09-10).** This file describes an earlier parallel staffing plan and must not drive a new session. Use [`CURRENT_STATUS.md`](CURRENT_STATUS.md) for active priorities.

*(2026-08-13. Hand this file to both resources. Full technical context per
track: `HANDOFF_TRACK_A_CHART_CALC.md` and `HANDOFF_TRACK_B_VALIDATION.md` —
each resource reads their own brief in full before starting. Spreadsheet
version with the same content, one tab per resource:
`Tableau_to_SiS_Task_Assignment.xlsx` — keep the two in sync.)*

---

## Dependency tiers — how independently each resource can work

Every task below carries a **tier**, verified 2026-08-13 against the actual
repo contents rather than assumed:

| Tier | Meaning | Cost to Sharath |
|---|---|---|
| **1 — INDEPENDENT** | Fully self-contained. Corpus workbooks are in `Workbooks/`, parsed IR fixtures are committed, and the regression suite runs **entirely offline** on DuckDB + fake Snowflake sessions (proven by a full 81-gate run with **no credentials**). Hand over the repo and the resource is productive immediately. | none |
| **2 — ONE-TIME UNBLOCK** | Needs ONE thing from you, once — then fully offline. Either a 2-minute decision (D1/D2/D3) or a single exported sample file. | ~30 min total |
| **3 — NEEDS YOUR ACCOUNT** | Genuinely needs Snowflake/Snowsight and can't be delegated without credentials. This is about **diagnosis** — *deploying* a finished fix also needs Snowflake, but that batches: both resources build and gate-lock offline, you deploy in one pass. | account access |

**Summary (updated 2026-08-13, after D1/D2 were decided): 15 of 18 tasks are
now tier 1.** Resource 1 has **10.5 of its 12.5 days** fully independent (only
A8 needs the Snowflake account). **Track B is now fully unblocked: B3 and B5
are CLOSED (2026-08-13), and B4 became tier 1** once the Tableau PAT turned out
to be already set and real REST fixtures were committed. E1/E2 are withdrawn.

**Both resources can start immediately.**

---

## ✅ DECISIONS — MADE 2026-08-13. B1 and B6 are unblocked.

| # | Decision | **Sharath's ruling** | Unblocked |
|---|---|---|---|
| **D1** | `order_match`: should a row-ORDER mismatch fail a chart that isn't a ranked list? | **Order is a structural failure ONLY for ranked / top-N charts.** Everywhere else it becomes an informational flag. Do not delete the check; do not weaken it for ranked lists. Gate must prove both directions. | **B1 → READY** |
| **D2** | `Discount` = AVG or SUM? (`profile_superstore.py:20` overrides the workbook's declared `agg=sum`; Tableau REST proves the app is wrong — Tableau 0.2/0.8/0.4 vs app 0.1/0.2/0.1) | **Respect the workbook's declared aggregation.** A curated profile entry applies only where the workbook declares that same agg; otherwise the workbook wins. Structural fix, not a one-line swap — it kills the whole bug class. | **B6 → READY** |
| **D3** | Is the Track A backlog order still right? | Not formally answered; the `MVP_ACCELERATOR_SCOPE.md` order stands. A9 (tooltips) remains last. Resource 1 is unaffected — A1 was never blocked. | A-track order stands |

Full rationale for D1/D2 is written into `HANDOFF_TRACK_B_VALIDATION.md`
(items 1 and 5b) — Resource 2 should read it there, not re-derive it.

### ~~Still outstanding — two file exports~~ — CORRECTED 2026-08-13

**This was wrong, and it's worth reading why.** E1/E2 were written up as "Sharath
manually exports two CSVs from Tableau." That framing missed something already
in the repo: **`tableau_server.py` can pull crosstabs over the REST API
itself** (`pull_all_view_csvs`, `fetch_view_data`, `query_view_data_csv`) —
built for R1/R2 and live-verified against the real account on 2026-08-11/12.

Better still, the pivoted shape B3 must handle **is a shape the REST pull
itself returns** — `ARCHITECTURE.md:1373`: *"Tableau's REST crosstab sometimes
returns a long/pivoted shape (`Measure Names`/`Measure Values` columns)."* A
hand-export could easily have produced a *different* shape and sent B3 off
building against a format the pipeline never actually sees.

**The real dependency is a Tableau PAT, not a manual export** — and one is
needed for Track B anyway. With it, Resource 2 pulls whatever crosstabs they
need, as often as they need, against real workbooks:

```python
from tableau_server import pull_all_view_csvs
csvs = pull_all_view_csvs(server_url, site_content_url, workbook_id,
                          token_name, token_secret)   # PAT via env vars
```

Credentials come from `TABLEAU_PAT_NAME` / `TABLEAU_PAT_SECRET`. **Never** put
a token in a file — a hardcoded PAT has already leaked once in this project.

| # | Was | Now |
|---|---|---|
| **E1 → B3** | Sharath exports a pivoted CSV | Resource 2 pulls it via REST once they have a PAT |
| **E2 → B4** | Sharath exports a date-part CSV | Same |

**Still worth committing the pulled CSVs into `tests/fixtures/`** once obtained,
so the gates are reproducible offline and don't need live credentials to run.

**Net effect:** B3/B4 move from *"blocked on Sharath"* to *"blocked on Tableau
access"*, which is the same thing Track B needed regardless. Sharath's only
action is issuing the PAT.

---

## RESOURCE 1 → Track A: Chart & Calc Engine
**Effectively self-sufficient: 10.5 of 12.5 days are tier 1. Can start
immediately on A1 with nothing from you.** Only A8 needs your Snowflake
account; A9 needs a one-line priority confirmation.
Primary files: `engine.py`, `calc_translator.py`, `tableau_parser.py` (render/parse paths)

| ID | Task | Pri | Est | Dep | Tier | Needs you? | Done when |
|---|---|---|---|---|---|---|---|
| **A1** | **Bins** — numeric FLOOR binning (`floor(f/size)*size`). NOT the `categorical-bin` grouping already done at `tableau_parser.py:2017`. Top corpus gap: **197 occurrences** | **P0** | 1.5 d | — | 🟢 1 | **No** | A real corpus workbook using bins renders with numerically correct bucket boundaries + new gate |
| **A2** | **Histogram** — bar chart over a binned measure w/ COUNT agg. New `r_histogram` in `engine.py` | **P0** | 1 d | **A1** | 🟢 1 | **No** | Bucket counts correct vs. hand-computed reference |
| **A3** | **Table calcs** RUNNING_*/LOOKUP/TOTAL/FIRST/LAST. Currently refused at `calc_translator.py:118`. Needs sheet sort order fed into window `ORDER BY` | **P1** | 2 d | — | 🟢 1 | **No** | A RUNNING_SUM matches Tableau's own displayed running total exactly |
| **A4** | **Native Measure Names/Values selector** — calc-based form already DONE + verified; only the no-calc native shelf wiring is missing (param already captured) | **P1** | 1 d | — | 🟢 1 | **No** | Native measure-swap workbook renders + switches correctly |
| **A5** | **Box-whisker chart** — Altair has `mark_boxplot()`; verify Tableau's quartile/outlier semantics match before assuming 1:1 | P2 | 1 d | — | 🟢 1 | **No** | Min/Q1/median/Q3/max match hand-computed reference |
| **A6** | **Bullet chart** — reuse reference-line parsing (`engine.py:981`), structurally similar to a bullet's target | P2 | 1 d | — | 🟢 1 | **No** | Bands + target marker render correctly |
| **A7** | **True matrix / pivot** — row+column crossing w/ subtotals. Distinct from existing `r_table`/`_rank_table` | P2 | 2 d | — | 🟢 1 | **No** | Read `_rank_table` (`engine.py:1802`) fully first — naive impl duplicates existing formatting logic |
| **A8** | **Map/choropleth SiS fallback** — Plotly maps render blank in the sandbox; needs bar/table fallback + honest finding | P2 | 1 d | — | 🔴 3 | **Snowflake** — the blank map IS a sandbox-only platform limit; can't be reproduced locally | Fallback renders w/ stated reason, never a blank |
| **A9** | **Rich hover tooltips** (155× corpus, cosmetic) | P3 | 1 d | **D3** | 🟡 2 | **Decision D3** — work itself is local | Confirm still deprioritized before starting |
| **A10** | **Pixel-exact cosmetics** — hollow scatter marks, refline labels, column truncation | P3 | 1 d | — | 🟢 1 | **No** | Take last |

**Track A total: ~12.5 days — of which 10.5 days are tier 1 (fully
independent).** Only hard task dependency is A1 → A2; the only item needing
your account is A8.

---

## RESOURCE 2 → Track B: Validation & Data-Model Correctness
**B3, B4 (a and b) and B5 are all CLOSED (2026-08-13)**, and D1/D2/D4 released
B1, B6 and the parsing fix. **Every one of the three gaps that blocked MVP item
6 is now closed.** What remains is B1, B2, B6, B7 and B8 — all tier 1, none
needing anything from Sharath.
Primary files: `validation_report.py`, `validation_adapter.py`, `deep_validation.py`, `app_screenshot.py`

| ID | Task | Pri | Est | Dep | Tier | Needs you? | Done when |
|---|---|---|---|---|---|---|---|
| **B1** | **`order_match` semantics** (`validation_report.py:198`) — implement D1's ruling: structural FAIL only for ranked/top-N charts | **P0** | small | — | 🟢 1 | **No — D1 DECIDED** | Gate proves BOTH sides: ranked list still fails on wrong order, grouped bar no longer does. **Do not just delete the check** |
| **B2** | **Global Sales Dashboard View2 crash** — crashed the capture during 08-11/12 testing, **never root-caused**. First question: does it still reproduce under `app_screenshot.capture_app`, or was it specific to the retired `headless_render` path? | **P0** | unscoped | — | 🟢 1 | **No** — `Globalsalesdashboard.twbx` is in `Workbooks/`, IR already parsed; reproduces locally | Either fixed, or proven moot w/ evidence. This is the **3rd corpus workbook** — highest-value generalization proof available |
| ~~**B3**~~ | ~~**Pivoted/long crosstab alignment**~~ — ✅ **CLOSED 2026-08-13.** Real REST fixtures now committed at `tests/fixtures/tableau_crosstabs/`. The tracked diagnosis was wrong: alignment *succeeded* and delivered 24 rows where the chart has 4, at display-rounded precision. Fixed by `unpivot_measure_names()`; gate `test_pivoted_crosstab_unpivots_to_chart_grain`, teeth proven | — | — | — | ✅ | **No** | **DONE** |
| **B4a** | ~~**Date-part column aliasing**~~ — ✅ **CLOSED 2026-08-13.** `declared_date_parts()` aliases "Month of Order Date" to the chart's `Order Date` grain, from the sheet's declared part, grain channels only (a filter's datepart must never alias). Gate `test_datepart_alias_and_formatted_number_parsing`, teeth proven 3 ways | — | — | — | ✅ | **No** | **DONE** |
| ~~**B4b**~~ | ~~**Date-part VALUE reconciliation**~~ — ✅ **CLOSED 2026-08-13.** `reconcile_date_part_values()` rebuilds Tableau's displayed value into the app's shape, handling all three real shapes (`'January'`+year column; `'April 2021'` with the year inside; month-only-with-no-year → **refused**). Proven end to end: the gate captures the **real** app chart and asserts **48 of 48** month keys match exactly. Gate `test_datepart_values_reconcile_to_app_grain`, teeth proven | — | — | — | ✅ | **No** | **DONE** |
| ~~**B5**~~ | ~~**Two unresolved backend measures**~~ — ✅ **CLOSED 2026-08-13** (a row-level calc used as a chart measure could never resolve; `parity._resolve_measure_sql` now mirrors `engine._resolve_measure`). Gate `test_row_level_calc_resolves_as_measure` | — | — | — | ✅ | **No** | **DONE** |
| **B6** | **`Discount` AVG-vs-SUM app bug** (`profile_superstore.py:20`) — implement D2's ruling: curated entry applies only where the workbook declares that agg | **P1** | small | — | 🟢 1 | **No — D2 DECIDED** (deploy later needs Snowflake) | Numbers match Tableau's REST-confirmed 0.2/0.8/0.4, without breaking sheets where AVG is correct |
| **B7** | **`source_tables()` legacy-join misreport** — reports a single legacy-joined object as a "3-table data model". **Reason string only; behavior is already correct** | P2 | small | — | 🟢 1 | **No** — pure XML parsing, no runtime or data | Skip relations nested in another relation's `<clause>` subtree |
| **B8** | **Multi-fact/cyclic refusal proof** — logic is built, only ever proven on synthetic fixtures. Build a real workbook via `tests/make_datamodel_workbooks.py` | P2 | small | — | 🟢 1 | **No** — generator script is local, refusal path is offline logic | Refusal fires correctly on real XML. Expected outcome: a passing proof, **not** a fix |

**Track B effort deliberately not totaled.** **B2 is the only remaining
"investigate, then fix" item** — B3, B4a, B4b and B5 all closed 2026-08-13.
Resource 2's day-one deliverable is a real estimate for B2.

### Added 2026-08-14 — from the first real read of a deployed pack

| ID | Task | Pri | Tier | Needs you? | State |
|---|---|---|---|---|---|
| **B9** | **Server-side exporter cannot capture some sheets that the real app draws.** `Sales Commission Model` rendered at 16KB vs a 337KB real screenshot, losing both charts; the same sheets carry **no data leg**, which is why that dashboard reports 0/4 charts passed. Fixing it improves the render **and** the row evidence | **P1** | 🟢 1 | **No** | **OPEN — highest-value remaining thread** |
| **B10** | **Visual capture inside Snowflake** — `notebook_capture.py`: a container-runtime notebook installs Playwright + Chromium at run time, starts the generated app and captures it with the existing `app_screenshot.capture_app`. **Proven standalone: 10/10 dashboards, ~160s** (`ARCHITECTURE.md` §24) | P1 | 🟢 1 | **No — no admin needed** | ⚠️ **built + deployed; ONE deployed run still unconfirmed** (every attempt so far hit a stale app process) |
| — | Four trust/scoping defects from the deployed run (empty-vs-absent verdict, discarded reasons, Tableau-leg scoping, pivoted CSV assignment) | — | — | — | ✅ **fixed + gated 2026-08-14** |

**B10 has no blocker — that earlier claim was wrong.** It said `WBR_OWNER`
needed an ACCOUNTADMIN compute-pool grant. In fact `USAGE` on
`SYSTEM_COMPUTE_POOL_CPU` has been granted to **`PUBLIC` since 2024-12-10**;
every role has always had it. The mistake was grepping `SHOW GRANTS TO ROLE`
(which cannot see `PUBLIC` grants) and asserting a platform limit from a grants
listing instead of running the command.

> **Rule this bought:** a permissions blocker requires a **real command
> returning a real error**. Without a run, the honest word is *untested*, not
> *impossible*.

**What B10 still needs is one confirmed deployed run.** Every attempt so far
executed a **stale app process** — Streamlit in Snowflake imports a module once
and keeps it, so `snow streamlit deploy --replace` swaps the stage files
without restarting a running app. Two reports came back identical for exactly
that reason while the fixed file was already live. To force new code: **`DROP
STREAMLIT`, redeploy, open a fresh tab.**

Meanwhile real screenshots are also available from `make_validation_pack.py` on
a workstation (8 of 10 dashboards scored 0.77–0.90), and the deployed app has
Cortex vision over a **labelled non-authoritative** server-side render.

**Track B is no longer the dependency-heavy track.** B3 and B5 are closed,
D1/D2 released B1 and B6, and the committed REST fixtures make B4 tier 1.
**No Track B task now needs account access or a file from Sharath.**

### ✅ Decision D4 — currency/percent parsing — MADE 2026-08-13, implemented

> **SHARATH'S RULING (D4): strip the `$`, divide the `%` by 100.**

`validation_report._d()` stripped a thousands comma but not a currency `$` or a
trailing `%`, so `'$391,722'` and `'11.9%'` parsed to `None` and were **never
compared** — the same silent-pass class the comma fix closed in that very
function. `%` is *scaled*, not stripped, because `value_scale` is `"fraction"`
everywhere, so `'11.9%'` is `0.119`; a plain strip would be a 100× error.

**Accounting negatives were the real trap and are handled first.** The live
`Lower Prediction Interval for Sales` column contains `($1,137)`. Stripping `$`
without reading the parentheses yields `+1137` — a **sign inversion**, i.e. a
silently *wrong* comparison instead of an honestly missing one, which is worse
than the bug being fixed. A range label (`'75-100%'`, a real `Achieved Quota`
value) still parses to `None` rather than becoming `75`.

---

## Not assigned to either resource — you or whoever has Snowsight access

| ID | Task | Est |
|---|---|---|
| **S1** | **R9 UI upload confirmation** — upload `Workbooks/R9_Live_Join_Orders_Product_Category.twb` through the deployed `pipeline_demo` app. Code-complete, gated, already verified against the real account via `pipeline.onboard()`; this is the same final UI confirmation R3 and R10 already passed | ~10 min, no dev work |
| **S2** | **MVP sign-off (#6)** — your own visual confirmation in Snowsight. The last open MVP item; not dev work | — |
| **S3** | **Confirm R8 deploy status** — the Cortex-vision re-wire was marked "NOT YET DEPLOYED" 2026-08-07, and `ARCHITECTURE.md:1579` notes one such banner is itself stale. Needs a look at what's actually live in `pipeline_demo` before anyone trusts either claim | 15 min |

---

## ⚠️ The one real collision risk between tracks

Both tracks can touch **`engine.py`**:
- Track A owns it for new chart renderers (its main file).
- Track B may need it for **channel captions** — `engine.py` doesn't set real
  Tableau captions as chart axis/tooltip titles, documented as the *"biggest
  lever"* for unblocking validation. The 08-11/12 pie-`theta` fix was exactly
  this, in `engine.r_pie`.

**Rule: Track B does not edit `engine.py` without telling Track A first.** A
caption-title change also alters the shipped app's visuals, so it needs its
own decision — it is not a validation-tooling side effect.

Secondary overlap: both touch `tableau_parser.py`, but different functions
(Track A: render/parse paths; Track B: `source_tables()` / data-model). Low
risk, still worth a heads-up.

---

## Day-one setup for both resources
1. This file + their own track brief, read in full.
2. `ARCHITECTURE.md` — Track A reads §1–4 minimum; Track B reads §15–19.
3. Run the suite before touching anything, to confirm a clean baseline:
   ```bash
   python tests/test_regression.py
   ```
   **Verified green 2026-08-14**: 93 gates defined, 92 auto-run all passing,
   1 deliberately manual (needs a live session). Must stay green before
   either resource calls anything done, and every fix needs a gate with
   **proven teeth** (revert the fix, confirm the gate goes red).
4. **⚠️ This working directory is NOT under version control.** With two
   people editing, set up git first or partition file ownership manually —
   there is no branch isolation and no undo if two edits collide.
5. Both must update `status_config.json` + `MVP_ACCELERATOR_SCOPE.md` **in
   the same change as the feature/fix**, not as a follow-up.
   `test_tracker_consistency` enforces part of this mechanically; the rest is
   discipline, and this project's trackers have drifted before when it
   slipped.

## Non-negotiable trust invariants (both tracks)
- **`BLOCKED` (never measured) and `FAIL` (proven wrong) must never look the
  same.** A missing proof cannot become a pass.
- **Never guess.** Where the code refuses — an ambiguous crosstab, an
  unresolvable column, a multi-fact graph — that refusal *is* the feature. At
  least two refusals here exist because a looser version was tried and found
  genuinely ambiguous against live data.
- **Nothing is silently dropped.** Every unsupported construct produces a
  named finding, never a blank chart or a guessed number.
