# Resource Assignment — Assignable Ticket Sheet
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

**Summary: 11 of 18 tasks are tier 1.** Resource 1 has **10.5 of its 12.5
days** fully independent (only A8 needs your account). **Resource 2 is the one
that needs you** — only B2/B7/B8 are immediately independent, but making
decisions **D1 + D2** and exporting **two sample crosstabs** converts four more
tasks to fully independent, leaving only B5 account-bound. That is the
highest-leverage 30 minutes available on this handoff.

---

## ⚠️ THREE DECISIONS ONLY YOU CAN MAKE — make these before day one

These block or mis-shape real tickets. None is an engineering call; each
changes numbers or verdicts the client sees.

| # | Decision | Why it can't be delegated | Blocks |
|---|---|---|---|
| **D1** | **`order_match`**: should an exact row-ORDER mismatch fail a chart that isn't a ranked list? Today it does — so on Regional Analysis, two charts read chart-level **FAIL while every single cell passes**. *Recommended: order is semantic only for ranked/top-N charts; elsewhere demote to an informational flag.* | Changes the vendored comparison engine's strictness — i.e. changes what "validated" means in a client deliverable | **B1** |
| **D2** | **`Discount` = AVG or SUM?** `profile_superstore.py:20` curates `"Discount": AVG(DISCOUNT)`, which silently overrides the workbook's own declared `agg=sum`. Validation caught this live and **Tableau itself settled it via REST: Tableau 0.2/0.8/0.4, app 0.1/0.2/0.1, backend 0.2/0.8/0.4 — the migrated app is wrong.** Not fixed, because AVG is legitimately correct on *other* sheets. | The fix changes numbers the **deployed** app renders | **B6** |
| **D3** | **Is the Track A backlog still the priority order you want?** The list below follows `MVP_ACCELERATOR_SCOPE.md`. Rich tooltips (A9) is marked "user deprioritized" — confirm it stays last. | Sequencing/business call | A-track ordering |

### Plus two file exports — ~15 minutes, unblocks two more tasks

Neither is a decision; both are artifacts that simply don't exist locally.
I checked `tests/fixtures/` and every `reports/` pack: **no live Tableau
crosstab CSV is saved anywhere in the repo**, and there is **no fixture for
the pivoted shape at all**. Without these, B3/B4 can't be worked offline.

| # | Export | Unblocks |
|---|---|---|
| **E1** | One Tableau crosstab CSV in the **pivoted / long shape** (`Measure Names` / `Measure Values` columns) | **B3** |
| **E2** | One Tableau crosstab CSV whose header carries a **date-part grouping** (e.g. "Month of Order Date") | **B4** |

Drop both into `tests/fixtures/` and they become permanent regression
fixtures, not just a one-time unblock.

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
**Start on B2 — it is tier 1 and needs nothing from you.** B1/B6 open the
moment D1/D2 land; B3/B4 open as soon as you export two sample crosstabs.
Primary files: `validation_report.py`, `validation_adapter.py`, `deep_validation.py`, `app_screenshot.py`

| ID | Task | Pri | Est | Dep | Tier | Needs you? | Done when |
|---|---|---|---|---|---|---|---|
| **B1** | **`order_match` semantics** (`validation_report.py:198`) — implement D1's decision | **P0** | small | **D1** | 🟡 2 | **Decision D1** (~2 min) — work is fully offline | Gate proves BOTH sides: ranked list still fails on wrong order, grouped bar no longer does. **Do not just delete the check** |
| **B2** | **Global Sales Dashboard View2 crash** — crashed the capture during 08-11/12 testing, **never root-caused**. First question: does it still reproduce under `app_screenshot.capture_app`, or was it specific to the retired `headless_render` path? | **P0** | unscoped | — | 🟢 1 | **No** — `Globalsalesdashboard.twbx` is in `Workbooks/`, IR already parsed; reproduces locally | Either fixed, or proven moot w/ evidence. This is the **3rd corpus workbook** — highest-value generalization proof available |
| **B3** | **Pivoted/long crosstab alignment** — Tableau REST sometimes returns `Measure Names`/`Measure Values` shape the aligner can't reshape | **P1** | unscoped | — | 🟡 2 | **ONE sample CSV.** Confirmed: no saved live crosstab and **no fixture for the pivoted shape** exists anywhere (checked `tests/fixtures/` + every `reports/` pack) | Reshape works AND `_assign_dashboard_csv_by_header` (`deep_validation.py:96`) still resolves uniquely — subset matching was proven ambiguous live, don't regress that |
| **B4** | **Date-part column aliasing** — "Month of Order Date" doesn't alias to the chart's own "Order Date" grain | **P1** | unscoped | — | 🟡 2 | **ONE sample CSV** with a date-part header. None saved locally | Verified against a real pulled crosstab, not an assumed header format |
| **B5** | **Two unresolved backend measures** — `Days to Ship Scheduled`, `Sales Forecast` don't resolve against the backend table | **P1** | unscoped | — | 🔴 3 | **Snowflake** — can't be determined from workbook XML alone | Diagnose which of three causes (calc gap / column-mapping gap / absent from table) — each has a different fix |
| **B6** | **`Discount` AVG-vs-SUM app bug** (`profile_superstore.py:20`) — implement D2's decision | **P1** | small | **D2** | 🟡 2 | **Decision D2** (~2 min) — numbers already proven live; fix is local (deploy later needs Snowflake) | Numbers match Tableau's REST-confirmed 0.2/0.8/0.4, without breaking sheets where AVG is correct |
| **B7** | **`source_tables()` legacy-join misreport** — reports a single legacy-joined object as a "3-table data model". **Reason string only; behavior is already correct** | P2 | small | — | 🟢 1 | **No** — pure XML parsing, no runtime or data | Skip relations nested in another relation's `<clause>` subtree |
| **B8** | **Multi-fact/cyclic refusal proof** — logic is built, only ever proven on synthetic fixtures. Build a real workbook via `tests/make_datamodel_workbooks.py` | P2 | small | — | 🟢 1 | **No** — generator script is local, refusal path is offline logic | Refusal fires correctly on real XML. Expected outcome: a passing proof, **not** a fix |

**Track B effort deliberately not totaled.** B2–B5 are "investigate, then
fix." **Resource 2's day-one deliverable is a real estimate for B2–B5**,
reported back before committing to a timeline.

**Track B is the dependency-heavy track.** Only B2/B7/B8 are tier 1. Your two
decisions (D1, D2) plus two exported sample crosstabs move B1, B3, B4 and B6
to fully independent — leaving **B5 as the only account-bound item**.

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
   **Verified green 2026-08-13**: 82 gates defined, 81 auto-run all passing,
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
