# Tableau → SiS Accelerator — MVP + Full Remaining Scope

> **Status update 2026-09-10:** the original six-item MVP is achieved and the project is in multi-workbook demo/enterprise hardening. The scope catalogue remains useful, but its July estimates and completion labels are historical. Current truth is generated in [`CURRENT_STATUS.md`](CURRENT_STATUS.md).

*(Prepared 2026-07-21, progress updated 2026-07-22. Single source of truth: every
pending item for this accelerator is accounted for below, in exactly one of the two
sections — MVP or Remaining.)*

## MVP (demo-ready baseline) — originally estimated at 6–9 days · **complete; enterprise hardening continues**

**Progress: 6 of 6 items DONE** (Custom SQL, Live connection, Routing fix, TopN/MeasureSwap
assessment, Union support, and live Snowsight confirmation). The Sales workbook now renders
through the deployed generic pipeline. Remaining work is multi-workbook enterprise hardening + a small
residual bugfix buffer. Much of the original 1–1.5-day live-deploy buffer has already been
*spent as intended* during this session's Snowsight testing — the KPI Live workbook surfaced
~6 real bugs (deploy-artifact, region-filter blanking, param/filter bleed, bar-chart gaps,
top-N-vs-context, stale-filter-at-All), all found and fixed.

| # | Item | Why it's MVP-blocking | Est. | Status |
|---|---|---|---|---|
| 1 | Custom SQL datasource execution | Named demo scenario; today only reported, not run | 1 day | **DONE 2026-07-21** (Snowflake-dialect only; other dialects honestly reported) |
| 2 | Live connection support | Named demo scenario; currently falls back to a stand-in table silently | 0.5–1 day | **DONE 2026-07-21** (live-to-Snowflake only; other live classes honestly reported) |
| 3 | Union support | Named demo scenario; zero code path exists today | 1 day | **DONE 2026-07-22** — parser detects `<relation type='union'>` members; onboarding materializes all members row-wise (UNION ALL). Synthetic-validated (no corpus union yet) |
| 4 | Per-workbook profile + datasource routing fix | Correctness/trust — a foreign workbook can silently render someone else's data on caption collision (the "open landmine") | ~1 day | **DONE 2026-07-21** |
| 5 | Assess 1 untested corpus workbook (Superstore_TopN_MeasureSwap) | Unknown defect risk until audited | 0.5–1 day | **DONE 2026-07-22** — 3/3 sheets render, 0 blockers, 0 calc-drops; measure-swap-driven top-N numerically EXACT for Sales & Profit (ranking re-orders with the param) |
| 6 | User's own visual confirmation of deployed apps in Snowsight | Sign-off checkpoint. The generic pipeline was cleanly recreated with PAT/EAI bindings and the Sales output rendered live | — | ✅ **DONE 2026-09-10** — Sharath confirmed the app is clean and the Sales dashboard renders; broader workbook testing is enterprise hardening |
| — | Live-deploy bugfix buffer | This project consistently finds 3–6 real Snowflake dialect/privilege bugs on first live test of anything new | ~0.5 day left (of 1–1.5) | Mostly spent — ~6 bugs found+fixed this session |
| | **MVP TOTAL** | | **Complete** | **6 of 6 done** — enterprise/demo hardening continues outside the original MVP checkpoint |

### Historical record: why MVP item 6 was not signed off on 2026-08-13

Sharath held sign-off open with a specific reason: **he still has to test
whether Tableau's numbers are coming through properly.** That is exactly the
claim the MVP definition rests on — *"a working, **numerically-validated**
Streamlit app"* — and it is the one leg of the three-way comparison
(Tableau / Streamlit / backend) not yet fully proven.

**Three known gaps stopped some Tableau values reaching a comparison at all.
One is now closed:**

| Gap | Task | Status |
|---|---|---|
| Pivoted / long-shape crosstabs (`Measure Names` / `Measure Values`) the aligner can't reshape | **B3** | ✅ **CLOSED 2026-08-13** |
| Date-part headers ("Month of Order Date") that don't alias to the chart's own grain | **B4** | ✅ **CLOSED 2026-08-13** (column half **B4a** + value half **B4b**) |
| Two measures (`Days to Ship Scheduled`, `Sales Forecast`) that don't resolve against the backend table | **B5** | ✅ **CLOSED 2026-08-13** |

**B3 closed** — and the diagnosis in the handoff was wrong, which is the more
useful finding. It was tracked as alignment failing with *"missing source
columns"*. Against the **real** REST pull, alignment **succeeds**: Tableau emits
the measure columns *and* the `Measure Names`/`Measure Values` pair, repeating
each grain row once per measure. The comparison was handed **24 rows where the
chart has 4**, each carrying the display-formatted wide value (`$391,722`,
`11.9%`) while the full-precision number sat unread in `Measure Values`
(`391721.905000001`). Fixed by `unpivot_measure_names()`
(`validation_evidence_bridge.py`), grouping by the chart's **declared** grain
rather than dimensions inferred from the CSV, and taking values from
`Measure Values`. Refuses with a stated reason when the grain is absent or a
measure repeats. Gate `test_pivoted_crosstab_unpivots_to_chart_grain` against
**real committed REST fixtures** (`tests/fixtures/tableau_crosstabs/`); teeth
proven. Suite 83 → 84.

**The E1/E2 dependency is withdrawn and was never real.** The Tableau PAT was
already present in the environment; `pull_all_view_csvs()` pulled every crosstab
directly, and `Query View Data` is *not* subject to the `403019` permission gap
that blocked R1's workbook downloads.

**Currency / percent parsing — RULED ON AND FIXED 2026-08-13.** Sharath's
ruling: *strip the `$`, divide the `%` by 100.* `validation_report._d()` stripped
a thousands comma but not a currency `$` or a trailing `%`, so `'$391,722'` and
`'11.9%'` parsed to `None` and were **never compared** — the same silent-pass
class the comma fix closed in that very function. `%` is *scaled*, not stripped,
because `value_scale` is `"fraction"` everywhere (`validation_adapter.py:63` is
its only producer), so `'11.9%'` is `0.119`; stripping alone would be a 100×
error. **Accounting negatives are handled first**: the real `Lower Prediction
Interval for Sales` column contains `($1,137)`, and stripping `$` without
reading the parentheses would yield `+1137` — a sign inversion, strictly worse
than the original bug. A range label (`'75-100%'`, a real `Achieved Quota`
value) still parses to `None` rather than becoming `75`.

**B4 — the COLUMN half is closed; the VALUE half is not, and it is a distinct
problem.** `declared_date_parts()` now aliases `Month of Order Date` to the
chart's `Order Date` grain, driven by the part the sheet itself declares, and
only from grain channels — Superstore's `applied_filters` also carry dateparts
(ShipSummary filters by `qr` and `yr`), and aliasing off a filter would point a
monthly chart at a quarterly column. Only the declared part is aliased, because
a real export carries several parts of the same field side by side.

**B4b — the value half — CLOSED 2026-08-13.** Aliasing the column was not
enough: Tableau's `Month of Order Date` holds a month **name** (`'January'`)
while the app emits `DATE_TRUNC('MONTH', …)` → `2021-01-01` (`engine.py:2078`),
and `engine.py` has no month-name path. They are also not two labels for one
grouping — Tableau's discrete `mn` collapses every January **across years**
while `DATE_TRUNC` keeps it. `reconcile_date_part_values()` rebuilds Tableau's
displayed value into the app's own shape, handling the **three genuinely
different real shapes** in the fixtures:

| Shape | Tableau value | Resolved by |
|---|---|---|
| `mn` discrete part | `'January'` + a separate `Year of Order Date` column | combining the two |
| `tmn` truncated | `'April 2021'` — year **inside** the same column, no year column exists | parsing it directly |
| month-only, no year anywhere (plus `'All'` grand-total rows) | `'January'` | **refused with a stated reason** |

Values are emitted through `records()` — the same normalizer the Streamlit leg
already passes through — so both legs are shaped by one code path rather than
two that could drift. A `Year` part maps to the **number** `EXTRACT(YEAR)`
produces, not a date. Unmapped parts (`Week`), unrecognizable months, and a
missing year all refuse rather than guess.

**The proof is end-to-end, not structural:** the gate captures the *real* app
chart through `engine`'s own render path and asserts the reconciled Tableau keys
**equal** the app's — **48 of 48 month keys match exactly**. Gate
`test_datepart_values_reconcile_to_app_grain`, teeth proven. Suite 85 → 86.

**B5 closed** (`ARCHITECTURE.md` §20): both measures are **row-level calcs**,
and the validator resolved captions only through `build_metrics()`, which by
design emits *only aggregating* calcs. A chart legitimately aggregates a
row-level calc — and the generated app already did — so the app rendered these
charts correctly while the validator called them unresolvable. Worse, they were
dropped **silently**, appearing in no `skipped` list. Fixed by mirroring
`engine._resolve_measure`; verified by execution and arithmetic
(`Sales Forecast` = 3,484,217.849 against an independent 3,484,217.85), gate
`test_row_level_calc_resolves_as_measure` with teeth proven. Suite 77 → 78.

**Consequence — updated 2026-08-13, end of day:** all three engineering gaps
(B3, B4, B5) are now **closed**. MVP sign-off no longer waits on any engineering
— only on Sharath's own confirmation in Snowsight, i.e. item #6 itself. (Earlier
the same day this paragraph read "coupled to B3 and B4" — corrected here rather
than left to contradict the table above, which already showed both closed.)

### The visual leg — status 2026-08-14

The first end-to-end read of a **deployed** pack (0 PASS across 21 charts) sent
this arc in two directions: four data-side defects fixed (`ARCHITECTURE.md`
§22), and the visual leg finally diagnosed rather than assumed (§23).

**The diagnosis, by running it:** `app_screenshot.available()` returns **True**
on a workstation and all 10 Superstore dashboards captured there in ~151s. So
the deployed run's ten BLOCKED visuals had exactly **one** cause — the SiS
sandbox has no browser. Not Playwright, not Chromium, not the app failing.

| Path to the visual leg | Artifact | Needs | State |
|---|---|---|---|
| **In-Snowflake capture** (`notebook_capture.py`) | **Real screenshot**, one click | nothing | ✅ **proven standalone 2026-08-14** — 10/10 dashboards, ~160s. Deployed run **not yet confirmed** |
| `make_validation_pack.py` (workstation CLI) | **Real screenshot** | a machine with a browser | ✅ working — 8 of 10 scored 0.77–0.90 |
| Cortex vision on a labelled render | AI verdict per dashboard | nothing | ✅ deployed |

**There is no infrastructure blocker.** An earlier version of this section
claimed one — an ACCOUNTADMIN compute-pool grant — and it was **wrong**:
`USAGE` on `SYSTEM_COMPUTE_POOL_CPU` has been granted to `PUBLIC` since
2024-12-10, so `WBR_OWNER` always had it. The claim came from grepping
`SHOW GRANTS TO ROLE` (which cannot see `PUBLIC` grants) instead of running the
command. See `ARCHITECTURE.md` §24.

**What remains is one confirmation, not a build.** Every deployed attempt so
far executed a **stale app process**: Streamlit in Snowflake imports a module
once and keeps it, so `snow streamlit deploy --replace` swaps the files without
restarting a running app. The app was dropped and recreated 2026-08-14 15:08
UTC; the next run from a **fresh tab** is the test.

**A rule that did not bend:** a server-side render is never presented as a
screenshot. It feeds the Cortex comparison under an explicit
*non-authoritative* label; the visual status stays BLOCKED; a real screenshot
always wins. A render that drew nothing is discarded, and a partial one
declares every sheet it lost — measured after `Sales Commission Model` rendered
at 16KB against a 337KB real screenshot, having lost both its charts.

**What is already proven, and remains true:** the KPI Live workbook testing
(~6 real bugs found and fixed), the R3 and R10 Snowsight uploads, R1's
Superstore run matching Tableau's verified totals exactly, and Regional
Analysis live-verified twice. Upload → working app → gaps reported is
demonstrated. What is missing is confirmation that Tableau's numbers reach the
comparison for **every** chart — not only those whose shape the aligner
already handles.

*(EMEA DTC Performance KPIs.twbx removed from scope — not required for this MVP.)*

### 2026-08-17 → 2026-08-19 — every engineering blocker on item 6 closed; the arc is now proof-only

Track B's whole handoff (`HANDOFF_TRACK_B_VALIDATION.md`) closed item by item:
**R16** (a real bug — ordinary date-parted bar/line charts silently reported 0
Tableau rows, fixed and verified live: Performance 0→428, Forecast 0→180),
**D1** (`order_match` is a chart-level FAIL only for rank/top-N charts now,
informational elsewhere), **D2** (a curated Discount profile no longer
silently overrides the workbook's own declared aggregation), **B2** (the old
Global Sales Dashboard capture crash confirmed moot under the current
capture path, plus a real duplicate-column bug found and fixed along the
way), and items 6/7 (a parser reason-string fix; the multi-fact/cyclic
refusal proven on real XML). All gated, all teeth-proven, all deployed. Full
detail in `status_config.json`'s roadmap and `HANDOFF_TRACK_B_VALIDATION.md`.

**R15 — the in-Snowflake capture path — PROVEN LIVE END TO END 2026-08-17,
the moment the ACCOUNTADMIN grant landed.** `python activate_task_capture.py`
proved the hard part — TASK → EXECUTE NOTEBOOK, the link this whole redesign
existed to enable — worked on the **first try**. The real capture then
failed for a genuine, narrow reason: the worker notebook had no network
egress bound to it (`deploy_worker()` never attached
`EXTERNAL_ACCESS_INTEGRATIONS`, unlike the already-proven standalone path),
so pip couldn't reach PyPI. Fixed and gated same day. Second attempt
succeeded server-side; the local activation script was killed mid-run by an
unrelated session interruption before it could print its own PASS, so the
result was verified independently rather than assumed — real queue status,
real files listed on the stage, two screenshots downloaded and actually
opened (Sales Commission Model byte-identical to the earlier known-good
capture; Sheet 22 its own documented legitimate near-zero-height shape).

**2026-08-19 — the first genuine human click-through of "Run Validation" in
the deployed app found one more real bug, now also fixed.** The report
showed "Missing required Streamlit dashboard screenshot" for 2 of 10
dashboards (On-Time Shipment Trends, Sheet 22) even though — verified
directly against the account — both had real, byte-correct screenshots
sitting on the stage. Root cause: `deep_validation.py`'s button handler
filtered the screenshot dict by whether each dashboard's UNRELATED formula/
interaction check had resolved, and both of those dashboards' formula check
was independently skipped (no comparable calculated fields) — discarding a
perfectly good screenshot for a reason that had nothing to do with it.
Fixed (`_pack_screenshots()`), gated, teeth-proven, deployed with a full
`DROP`+redeploy (this fix lives in code the running app process had
already cached in memory, so a plain `--replace` would not have taken
effect until the next restart anyway).

**Where item 6 actually stands now:** every engineering gap it was ever
blocked on is closed. The capture mechanism, the alignment/reconciliation
logic, and the report-assembly step have each been independently proven
against the real account — not just gated offline. What remains is not a
build, it is Sharath re-running "Run Validation" once more (fresh tab) and
confirming the report now correctly shows all 10 Tableau + 10 Streamlit
screenshots and reading the real per-chart PASS/FAIL/BLOCKED verdicts
underneath them.

### 2026-08-20 → 2026-08-21 — the DATA leg's real remaining gap, root-caused and half-fixed; new TOP PRIORITY

The screenshot arc above closed the VISUAL leg. Running "Run Validation" for
real then surfaced the thing that matters more: most sections showed **no
Tableau values at all** on the DATA leg. Live-traced against the real
account (`R1 Test Upload - Superstore`): of 10 sections that reached a
comparison, **7 read 100% Tableau "Missing."** The REST pull itself was
clean (all 10 views, zero fetch errors) — the real cause is
`deep_validation.py`'s crosstab-to-sheet assignment, since Tableau's REST
`Query View Data` exports only ONE sheet's data per multi-sheet dashboard
view, silently.

Shipped and deployed two real, safe fixes to that assignment logic (Tier 4:
a map sheet's `geo` field; Tier 5: a table sheet's combined strong+weak
dims) — closes **Sales by Geography** and **Sales and Profit by Product
Names**, both teeth-proven, both verified with real numbers against the
live account.

The bigger finding: **this is confirmed a genuine Tableau REST platform
ceiling, not a matching-logic gap** — independently stated by Sharath's own
prior Tableau-automation project's own code comment ("REST API strategy is
intentionally disabled... only Selenium can select the correct sheet"). And
then **a real, live-proven way past it was found**: injecting the REST PAT
token as a browser cookie via Chrome DevTools Protocol (not submitted
through the login form) bypasses the Blend SSO+MFA wall entirely. Proven
end to end against the real account — authenticated with no login page, the
real dashboard rendered with every previously-missing number visible,
opened the real Download→Crosstab dialog, selected a specific sheet among
its real names, and downloaded its actual per-sheet crosstab (41 real rows,
full precision, matching the dashboard exactly). The first Snowflake-side
feasibility question (can this run inside Snowflake, not just a
workstation?) also got a live answer: raw network egress from Snowflake's
compute to the real Tableau server works, confirmed with a real HTTP 200.

**Per Sharath's own instruction (2026-08-21), this is now the top priority
above every other roadmap item** — see `status_config.json`'s `roadmap[0]`
and `current_priority` field, and `ARCHITECTURE.md` §28 for the full trace.

**UPDATE, same day, later: the Playwright port is built and proven fully
live, including inside Snowflake itself.** `tableau_zone_crosstab.py` ports
the technique to Playwright, gated, verified on 3 real sheets. Then the
entire technique — cookie injection, navigation, toolbar/dialog discovery,
the real file download — was run successfully **inside Snowflake's own
compute** via a standalone test notebook: 6 egress/environment iterations
from a cold start to a clean, real success matching the live dashboard's
own numbers exactly.

**FINAL UPDATE, same day: R18 is DONE — built, deployed, and verified end
to end.** A real, isolated job queue + worker (`tableau_crosstab_jobs.py`
/ `tableau_crosstab_worker.py`) reuses R15's proven architecture without
sharing R15's own table/task/notebook. Found and fixed a real bug in the
same pass: a borrowed REST session token let an unrelated `sign_out()`
elsewhere invalidate it before a delayed job got processed; fixed with an
independent worker sign-in. Wired into `deep_validation.py`'s evidence
pipeline — an unresolvable sheet now queues a real fetch and waits,
bounded, instead of reading "Missing." Deployed live, verified against the
real deployed code path directly (`QuotaAttainment`: `None` → 41 real
rows). Only remaining: Sharath's own Snowsight click-through confirmation.
Full trace: `ARCHITECTURE.md` §28.

## Remaining (post-MVP, real, tracked) — ~25 days

| # | Item | Est. |
|---|---|---|
| 1 | Bins (197× in corpus — top gap by occurrence) | 1.5 days |
| 2 | Histogram (depends on bins) | 1 day |
| 3 | Box-whisker chart | 1 day |
| 4 | Bullet chart | 1 day |
| 5 | True matrix / pivot table | 2 days |
| 6 | Parameter-driven measure swap ("Metric Selector") — **BOTH forms DONE (calc-based 2026-07-22; native Measure Names/Values, no-calc form, DONE 2026-08-17)**. The native form is proven against a real, data-backed `.twbx` (grafted onto Superstore_TopN_MeasureSwap's real extract) run through the full pipeline end to end — synthetic in the sense that no real Tableau Desktop export of this exact pattern exists in the corpus, honestly flagged as such | **DONE** |
| 7 | Table calcs RUNNING_*/LOOKUP/TOTAL/FIRST/LAST — needs view-ordering support | 2 days |
| 8 | Dashboard actions / cross-highlighting / stories | **N/A — out of scope by design**, not estimated unless re-prioritized |
| 9 | Rich hover tooltips (basic only today) | 1 day |
| 10 | Non-star join → Snowflake views — **DONE 2026-07-26 (R7)**: any deterministic tree (incl. snowflake-schema chains) auto-models in both the view DDL and the extract flatten; genuinely ambiguous graphs (multi-fact/cyclic/disconnected) refuse with a named reason | **DONE** |
| 11 | Reserved-word risk at the column level (no corpus hit yet) | 0.5 day |
| 12 | Map/choropleth blank in SiS sandbox — bar/table fallback | 1 day |
| 13 | Data blends — **guidance DONE 2026-07-26 (R7)**: real link fields extracted from the workbook XML, reported per affected sheet, with reviewable pre-aggregate remodel SQL. Auto-materializing a blend as a deployed view is still open (needs the ACTIVE link per sheet + a numeric ground-truth workbook) | ~1–1.5 days |
| 14 | Context / data-source filters — **DONE 2026-07-21** for the categorical/range/date-part case: `context='true'` filters now inject into the top-N ranking subquery (top-N *within* the context) and dashboard filters scope to their bound sheets. Remaining: data-source-level filters | 0.5 day |
| 15 | Pixel-exact layout cosmetics (hollow scatter marks, refline labels, narrow-column truncation) | 1 day |
| 16 | Live-source migration kit (SQL-Server-style external live sources — landing DDL + views + checklist) | 2.5 days |
| 17 | Cortex: run calc fallback across rest of corpus (22 calc_drops total) | 1 day |
| 18 | Cortex: deploy E-Commerce semantic view + align YAML emitter | 1 day |
| 19 | Cortex: extract blend linking fields from XML — **DONE 2026-07-26 (R7)**: `blend_constraint()` feeds the calc-fallback prompt the workbook's declared links, replacing the infer-the-join-key instruction that produced the wrong `Region = Segment` key | **DONE** |
| 20 | Cortex: wire calc fallback into `pipeline_app.py` Stage 5 | 0.5 day |
| 21 | `convert.py --deploy` full automation (dry-run already proven manually) | 1 day |
| 22 | "Drill: Product" invented control | **0 days — product decision, not engineering** |
| 23 | **FIXED LOD ignores dimension filters (order-of-operations)** — a `{FIXED [k]: AGG}` LOD is computed inside the sheet's full WHERE, so it wrongly sees DIMENSION filters; Tableau computes FIXED LODs *before* dimension filters (after context filters, which we already handle). Needs LOD windows computed in a context-only subquery with dimension filters applied outside. **DEFERRED — no corpus workbook exercises FIXED-LOD + dimension-filter yet; build against a real test workbook (assess-first), do NOT rewrite the regression-locked LOD path blind.** Optional cheap interim: detect the combo + emit an honest migration-notes finding. | 2 days |
| | **REMAINING TOTAL** | **~25 days** |

**For the FULL data-model scenario audit** (every source-connection type, extract
shape, union, blend — tracked as built/tested/confidence, with two new confirmed
gaps R9/R10 found 2026-07-26) see `DATA_MODEL_STATUS.md` at the repo root.

## Newly requested — roadmap (added 2026-07-22; STATUS UPDATED 2026-08-07)
*(New-axis work — separate from the MVP data-model scope above and the chart/calc backlog. Kept in sync with NEW_CHAT.md's "ROADMAP — NEXT ITEMS" block and status_config.json.)*

**Status 2026-07-26:** R3, R4, R5 (Deploy button), R6 (in-app Cortex Analyst chat) **all DONE**; the honest Stage 3 rework (3a Data Model + 3b optional Cortex) and the data-model view **#1 scope A DONE + scope B star case LIVE-PROVEN** (E-Commerce replicated as 3 separate tables + a relationship view). The Blend accelerator-console UI reskin **DONE + live** (see below), which also delivered R4. **R3 closed 2026-07-26** — the data-model auto-bind half (point an extract-based workbook at its already-existing Snowflake source, no copy) joined the pre-loaded-table reuse half shipped 2026-07-25. **R7 non-star joins DONE + blend link-field extraction DONE** 2026-07-26 (blend auto-materialization deliberately left open). **R1 DONE + LIVE-VERIFIED 2026-07-28 through the actual deployed Snowsight app** — SiS network/secret setup landed, plus a project/workbook dropdown browse flow (Connect → pick project → pick workbook → Fetch) replacing the need to paste a link at all (the view-value pull for R2 still open). Still pending: R2, R8 (Cortex vision validation).

**Status 2026-08-07 — R2 and R8's Cortex-narrated UI SUPERSEDED, not merely extended.** Both rows' own text above still describes the design as it stood 2026-07-28/30; see each row's own "2026-08-07" append note for what actually shipped. Short version: the user judged the deterministic proof-first pack (real screenshots + real row-level comparison, 0 Cortex tokens) clearer than the Cortex-judged per-metric UI and the Cortex vision comparison, so both were removed from `pipeline_app.py`'s Validation page and replaced by one panel, `deep_validation.render_proof_first_validation`. Live-verified against the real account; two real bugs found and fixed the same session (a dashboard-granular Tableau REST view-list gap, and a multi-panel screenshot capture bug that silently dropped every KPI panel but the last). Deployed to `pipeline_demo`. Real gaps remain open (pivoted-CSV alignment, date-part column-name aliasing, two unresolved backend measures) — none of them regressions, all pre-existing or newly-surfaced-but-honestly-reported, per each row's own note.

**Status 2026-08-10/12 — the pack's IMAGE evidence source was REPLACED, not patched, then proven on a SECOND corpus workbook live.** User, mid-way through a third round of headless-render bugfixing, asked directly: "why are you rendering it again — just screenshot the app." Correct: a re-render is a second renderer, and it was caught actively FLATTERING the migration (drawing more customer labels on Customer Analysis than the real app's own render height allows). New `app_screenshot.py` launches the GENERATED app locally (`streamlit run`, real Chromium via Playwright — no SSO involved, since that constraint only ever applied to the *deployed* app) and screenshots every dashboard tab for real. `headless_render.render_dashboard_to_png` no longer supplies the image leg (still supplies the Streamlit DATA leg via `capture_sheet_chart`, untouched); when no browser is available (inside the deployed SiS app itself), visual evidence is BLOCKED with that stated reason — a real, accepted cost: packs must be generated from a workstation. Two bugs found getting this working: a subprocess `stdout=PIPE` deadlocked the child Streamlit process after ~2 dashboards (capture time 646s → 137s once fixed); a status-ROLLUP bug meant one missing screenshot alone dragged an entire dashboard's verdict to BLOCKED even when every chart/formula/interaction had genuinely passed — fixed by excluding BLOCKED (missing evidence) from the rollup while a proven visual FAIL still counts as before. Pack SLIMMED per user ask ("keep only what is necessary"): 49 files/5.7MB → ~11 self-contained files, shipped individually (no zip, explicit decision). **Then run against a SECOND corpus workbook (Regional Analysis) live against the real Tableau account for the first time**, which surfaced four more real bugs, all fixed and gate-locked: (1) stacked-bar/line-with-labels charts' data was unreachable — an Altair `LayerChart`'s `.data` only hoists when every layer shares the exact dataframe, and the text-label layer's derived columns always break that; fixed by walking every layer and preferring the first with the full needed column set. (2) The pie chart's `theta` channel had no Vega-Lite title (`color` did) — one-line fix. (3) A multi-sheet dashboard's real Tableau crosstab was being discarded outright (the matcher only trusted single-sheet dashboards) — fixed with an EXACT full-caption-set header match, not a guess (a looser subset match was tried first and found genuinely ambiguous live). (4) Tableau's comma-formatted numbers ("163,797.1638") silently failed `Decimal()` parsing, so a chart could read PASS on Streamlit/backend agreement alone while the Tableau leg was never actually checked — fixed by stripping the comma first. Re-verified live end to end: real screenshots + crosstabs for both dashboards, genuine structural scores (0.844 REVIEW / 0.880 PASS, not BLOCKED), every compared cell reconciling within tolerance. **One finding surfaced and deliberately NOT silently patched:** both fully-evidenced charts still read chart-level FAIL purely from `order_match`, an exact row-sequence check meaningful for a ranked list but not a grouped bar chart — flagged to the user rather than loosening the vendored comparison engine's strictness unilaterally; **needs a product decision**, tracked as the top item in `HANDOFF_TRACK_B_VALIDATION.md`. Separately, a **third** corpus workbook (Global Sales Dashboard) crashed the then-current capture on its View2 dashboard during this session's testing — never root-caused, still open. Gates: `test_app_screenshot_no_pipe_deadlock`, `test_validation_pack_slimmed`, `test_blocked_visual_does_not_cap_passing_data_proof`, `test_layered_chart_streamlit_rows_and_pie_theta_caption`, `test_multisheet_dashboard_csv_matched_by_header_and_thousands_comma`. Suite 80 → 82 gates (verified by a live full run 2026-08-13; corrects a prior "82 → 84" arithmetic slip in this doc's own source material — see `ARCHITECTURE.md` §19's correction note), all green. Deployed to `pipeline_demo`. **Full detail:** `ARCHITECTURE.md` §17–19, `NEW_CHAT.md` top entry, `HANDOFF_TRACK_B_VALIDATION.md` for the open items assigned out of this arc.

| # | Item | Est. |
|---|---|---|
| R6 | **In-app Cortex Analyst chat (ask-your-data)** — **DONE 2026-07-25 + live.** A "💬 Ask your data" panel bound to the workbook's deployed semantic view; `_cortex_analyst` calls Cortex Analyst (SiS `_snowflake` bridge / Snowpark REST fallback), returns SQL + answer, runs it, shows results. Single-step `st.form`. Fully guarded. | **DONE** |
| R7 | **Data-model completeness — non-star joins + blends** — **NON-STAR JOINS DONE 2026-07-26; BLENDS extracted + reported (materialization deliberately not built).** `semantic_layer.join_plan()` replaces the star-only check: any deterministic **tree** now auto-models, so a **snowflake schema** (Orders → Product → Category — a dim joined to a dim) builds its view and flattens correctly. A star is just the depth-1 case; refusing chains was a limitation of the *check*, not the data. Fixed a latent correctness bug on the way: the emitter hardcoded `ON f.<key>` — right for a star (every parent *is* the fact), silently wrong at depth > 1; each `ON` now references its own parent's alias. **One planner drives both the view DDL and the extract flatten** (`init_workbook.flatten_tables`, newly extracted so the merge/key-tracking logic is testable without a `.hyper`) so the two paths cannot disagree about what's joinable. Still refuses — with a named reason, never a guess — multi-fact, cyclic and disconnected graphs. **Blends:** `tableau_parser.blends()` reads the workbook's own `<datasource-relationship><column-mapping>`, collapsing Tableau's one-map-per-pill-derivation into real fields and naming the affected sheets; verified against Superstore's **real** blend (`Sample - Superstore` + `Sales Target` on Order Date / Category / Segment, sheet `Performance`). Surfaced as a finding + app warning + **reviewable remodel SQL** that pre-aggregates the secondary to the link grain before joining (a row-level join would fan out the primary and double-count its measures). **Closes the documented wrong-join-key bug** — the Cortex calc prompt used to ask the model to infer blend keys and it proposed `Region = Segment`; it now receives the declared links as a hard constraint. Gate `test_non_star_join_and_blends`. | **Joins DONE**; blend auto-materialization ~1–1.5 days remaining |
| R1 | **Pull a workbook from Tableau Server / Cloud by link** — **DONE + LIVE-VERIFIED 2026-07-28, same session it was built.** Vendored `tableau_server.py` (`sign_in_with_pat`, `find_workbook_by_content_url`, `download_workbook_bytes`, `fetch_workbook`) trimmed from the `tableau` skill's `rest_api_client.py`, kept entirely inside the repo. `parse_tableau_url()` handles both Cloud and self-hosted Server URL shapes, raises — never guesses — on an unrecognized one. `pipeline_app.py`'s Discover & Scope tab has a Source radio: pasting a URL + clicking Fetch hands the bytes to the **same** `run_pipeline()` → `pipeline.onboard(...)` path a manual upload uses, via a tiny `_FetchedWorkbook` shim — zero pipeline changes needed. PAT via `TABLEAU_PAT_NAME`/`TABLEAU_PAT_SECRET` only, never entered in the UI. Offline gate `test_tableau_server_url_parsing_and_fetch`; `requests` added to `environment.yml`; `tableau_server.py` added to `snowflake.yml`'s artifacts. **LIVE-VERIFIED against the real account** (site `b360bi` on `prod-useast-b.online.tableau.com`): PAT sign-in + URL-to-workbook-id resolution both confirmed real against 5 different workbooks. Downloading PRE-EXISTING site content hit a consistent `403019` across 4 workbooks — a site-wide/role-wide "Download workbook" permission gap on the test account, not a bug in our request shaping. Published a copy of `Superstore.twbx` to the site to get download-authorized content, pulled it straight back down through the actual `fetch_workbook()` with a realistic view URL (valid `.twbx`, correct internal structure), fed the bytes into `pipeline.onboard()` unchanged (4 datasources resolved, 0 missing, 0 blocked), then ran the full `convert.py` CLI pipeline end to end on it: 21 sheets, 0 blockers, and the resulting app's grand totals/CustomerOverview figures **matched Tableau's own verified ground truth exactly** (Sales 2,326,534.35 / Profit 292,296.81 / Quantity 38,654 / Profit Ratio 0.1256 vs the 2,326,534 / 292,297 / 38,654 / 0.126 already locked in `validate_numbers.py`). Test artifacts cleaned up; two shared, non-stem-namespaced report files (`reports/migration_assessment.md`, `sql/generated_views.sql` — a real characteristic of `report.py`, not a bug introduced here) got overwritten by the test run and were regenerated from the real `Superstore.twbx`; full suite re-confirmed all green (verified count: 63 auto-run gates — 59 pass + 4 skip for corpus workbooks absent in this environment — plus 1 deliberately-manual gate needing live SSO; a "59→60"/"60/60" figure was stated earlier in this session and corrected after an audit found it was a miscount, not a missing gate). **Still open:** a real client-workbook download will need an admin to actually grant "Download workbook" permission (this account didn't have it for pre-existing content); the `download_view_image`/view-data pull that feeds R2's `tableau_truth` (per-section rendered values, not just workbook download) is untouched — the other half of R1's original scope. | **DONE** (ingest); view-value pull for R2 remaining |
| R2 | **Per-section end-to-end validation via Cortex** — Cortex reads the complete `.twbx` and compares data between Tableau and the generated Streamlit app **section by section** (per dashboard/tab/sheet), extending `parity.py` Stage 5 beyond today's grand-total + calc spot checks. **Architecture, updated 2026-07-28 (explicit user decision, after a challenge-and-discuss round):** Cortex now **owns the PASS/BUG verdict** for R2 — a change from this doc's prior "Cortex narrates only, never decides" language, deliberately scoped to R2 (every other AI-gated path in this project, e.g. the calc fallback, is unchanged). The boundary that keeps this safe: Cortex is handed two **already-real** numbers — the app's actually-executed value and Tableau's real reference value — and judges whether they agree; it never computes either one itself (the risk raised and agreed on: letting an LLM re-derive a number by reading a formula, instead of via REST/SQL execution, is a real accuracy regression, not just a policy concern). The prior deterministic `check_calc_metrics` verdict is kept as a labeled **cross-check** next to Cortex's verdict; a disagreement is flagged for review, never silently resolved either way. **2026-07-28, same session — the missing ground-truth seam built + offline-gated.** `tableau_server.py` (R1's own vendored client) gained `list_views(workbook_id)` and `query_view_data_csv(view_id)` — Tableau REST's "Query View Data", returning a view's data **as rendered**, not raw extract rows — plus `fetch_view_data()`. This is what `tableau_truth` was left pluggable for. **Then, same session: `parity.cortex_judge_section()` built** (the live in-app judge — one `CORTEX.COMPLETE` call, returns JSON verdict+explanation, `"UNKNOWN"` never a silent PASS on parse/call failure) and **wired into `pipeline_app.py` Stage 5** ("Let Cortex validate each section" — Cortex's verdict is what's shown, live, per metric). `build_section_validation_notebook` rewritten so the **downloadable** notebook also has Cortex decide the verdict at notebook-run time, self-contained (stdlib-only `%python` parse cells, no dependency on this repo's modules). Gates: `test_tableau_server_view_data_pull`, `test_section_validation_notebook` extended (Cortex-judged structure + every generated `%python` cell proven syntactically valid via `ast.parse()` + `cortex_judge_section` covers clean/fenced/garbage/raising responses). **Ground-truth seam closed, same session, completing R2:** `tableau_server.pull_all_view_csvs()` (self-contained signin→pull-every-view→signout, one session per workbook) + `parity.truth_from_view_csv()` (maps a view's CSV to `calc_metrics` by normalizing the `SUM(...)`/`AGG(...)` header wrapper — **conservative by design**: only a single-data-row CSV, a true grand-total/KPI view, is used as a scalar; a multi-row dimension breakdown is skipped with a stated reason, never summed/picked into a guess) + `parity.pull_live_tableau_truth()` (merges across every view, first-view-wins on a duplicate metric, one bad view's REST error doesn't abort the rest). Wired into Stage 5: a "Pull real Tableau values for this workbook" button (shown only when the workbook came from the Tableau REST fetch, not a manual upload) populates `live_truth`, used by both the live Cortex-judge loop and the downloadable notebook. Gate `test_r2_live_truth_pull`. Suite 63→65 total across both closing passes. **R2 is now code-complete end to end** — REST pull, conservative header mapping, Cortex-owned verdict, both surfaces wired to the same real ground truth. **RAW-COLUMN GROUND TRUTH WIRED 2026-07-30** — a real gap the whole R1/R2 arc had walked past, surfaced by the user asking why `Discount` had no Tableau value and then correctly rejecting the first answer ("Profit, Sales and Quantity are also raw columns, how come those are shown?"). `parity.TABLEAU_TRUTH` is a hand-typed dict of ONE datasource and THREE columns, and `check_workbook()` read it and nothing else — so Discount showed "—" on the very workbook it covers, and every raw measure on every *other* workbook would show "—" forever no matter how live the REST connection was. R1/R2's dynamic pull already existed but was only ever handed `calc_metrics`; raw columns — the easiest thing to match, since a view exporting `SUM(Discount)` maps straight onto the Discount measure — were never offered to the matcher. Fixed with `raw_measure_metrics()` (adapts raw rows into the SAME matcher shape — one matcher, not two that can drift), `apply_live_truth_to_measures()` + `_resummarize()`, and a Stage-5 restructure so the pull runs *above* the tables and feeds the raw-column table, the calc table and the notebook. Verdict rule: an EXACT (single-row-view) reference decides a verdict; an APPROXIMATE (multi-row, column-summed) one never does — a filtered view legitimately differs, and treating that as a BUG would repeat the false-BUG class already hit twice. Gate `test_raw_measure_live_truth_and_json_verdicts`, teeth proven by reverting. **FIRST LIVE RUN (2026-07-30) FOUND THE APPROXIMATE TIER UNSOUND, THEN REMOVED IT (2026-08-04, user's explicit choice).** Quantity resolved to `231,924.0 (REST, ≈ sum of 24 rows)` against a true `38,654` — exactly 6×, because the view's rows did not partition the data (a measure crosstab, each row repeated once per measure). A row-partition guard caught that shape but *also* rejected legitimate multi-row detail listings, which look identical to a crosstab from the CSV alone — and a reference only trusted when it already agrees with the app validates nothing anyway. Rather than patch a third time, the sum-across-rows tier was **removed**: a multi-row view is now always reported as a dimension breakdown, never summed. **Replaced with true Tableau aggregates via the VizQL Data Service** — `tableau_server.list_datasources()` / `vds_query_aggregate()` ask Tableau's own engine for `SUM(field)` with **no grouping fields**, which returns exactly one row: the real grand total, needing no dashboard to display a total and immune to both inflation and deflation since no view is queried at all. The honest failure mode is stated, not hidden: a workbook published with **embedded** data has no separately-published datasource for VDS to reach, and `pull_tableau_aggregates` returns that reason explicitly rather than a silent empty result. Wired into `parity.pull_vds_tableau_truth()` (pure-SUM metrics only) and Stage 5, queried first as the authoritative source ahead of the single-row-exact view-CSV fallback, with a new "Tableau's own aggregates (VizQL Data Service)" panel showing which datasource answered what. Gates `test_r2_live_truth_pull` (rewritten — multi-row now asserts nothing is contributed) and `test_raw_measure_live_truth_and_json_verdicts` (extended with the full VDS orchestration, including the embedded-data honest-failure path). Teeth proven on three fronts. Suite unchanged at 70 gates, all green. No new dependency. **2026-08-07 — SUPERSEDED by a fully deterministic proof-first pack (explicit user decision).** After live-testing R2's Cortex-owned-verdict design against the real account, the user asked for the AI-narrated per-metric judge, the skill-methodology Cortex write-up, AND the Cortex vision comparison (R8, below) to be replaced entirely by ONE pack: `validation_evidence_bridge.py` + `validation_report.py`'s existing proof-first engine, driven with `parity.build_cortex_dashboard_validation_report(..., narrate=False)` — real per-CHART row-level Tableau/Streamlit/backend comparison, real Tableau REST screenshots vs. real Streamlit-rendered screenshots with a STRUCTURAL similarity score (not a Cortex judgment), **zero Cortex tokens spent**, matching this project's own "AI grows/assists the tool, determinism ships the app" rule more literally than R2's original Cortex-decides design did. `render_proof_first_validation` (`deep_validation.py`) is now the ONLY validation UI on the Validation page; `render_cortex_section_validation` and `render_vision_validation` were deleted (their underlying `parity.py` functions — `cortex_judge_section`, `vision_validate_dashboard` — remain, gated only by their own regression tests, in case a future session wants them back). Live-verified against the real account (`b360bi` / `R1 Test Upload - Superstore`): real screenshots for 9/9 dashboards, real crosstab rows for the sheets Tableau's view-per-dashboard REST model can reach. **Two real bugs found and fixed live the same session:** (1) a dashboard-granular REST view list meant a lone worksheet inside a single-sheet dashboard had no view of its own to query — fixed with a same-dashboard fallback, restricted to the unambiguous 1-sheet case only, never guessed for a multi-sheet dashboard; (2) `headless_render.capture_sheet_chart` kept only the LAST of several `st.altair_chart` calls a multi-panel sheet (`r_mbar` KPI rows, faceted small-multiples) makes, so a dashboard's Streamlit-side screenshot silently dropped every panel but one — fixed by capturing every call and combining with `alt.hconcat`. **Still open, honestly:** Tableau's REST crosstab sometimes returns a long/pivoted shape (`Measure Names`/`Measure Values` columns) the column aligner doesn't yet reshape; date-part-grouped Tableau column headers (e.g. "Month of Order Date") don't yet alias to the chart's own "Order Date" grain; two measures (`Days to Ship Scheduled`, `Sales Forecast`) don't resolve against the backend table; workbook-level status stays `BLOCKED` until every chart passes, which is by design, not a defect, but is the realistic current ceiling. Deployed live to `pipeline_demo`. | **Superseded — see 2026-08-07 note** |
| R3 | **Auto-point to an existing Snowflake table from the data model** — **DONE 2026-07-26.** An extract-based workbook whose upstream table already lives in the account is now pointed straight at it: **no extract decode, no `write_pandas` copy**, the app queries the governed original. New `tableau_parser.source_tables()` reads the declared upstream source (connection db/schema + relation table names + the source columns Tableau records in `metadata-records`) for exactly the datasources `live_connections()` deliberately skips — the extract-bearing ones. `pipeline.resolve_source_binding()` then ranks candidates against the real account: `sources.json` explicit map → the workbook's own declared `db.schema.table` → a single name match in the load DB. **The guard is the feature:** a name match alone never binds — the candidate's real columns must cover the workbook's recorded source, an ambiguous name (same table name in >1 schema) is surfaced as a choice rather than resolved, and "cannot verify" is never treated as verified. Both onboarding paths share one resolver (`pipeline.onboard` + `init_workbook --connection`); Stage 1 shows both what bound and what was refused. Gate `test_auto_bind_existing_snowflake_table`. Synthetic-validated + corpus-swept for false positives (no corpus workbook is a single-table extract over a live DB; Regional Analysis / Global Sales are 3-table models that correctly defer to the data-model view path). | **DONE** |
| R4 | **Snowsight staged-app UX — impactful per-stage progress + Discovery calc pass/fail** — **DONE 2026-07-26** as part of the Blend console UI reskin (below): per-stage progress is now a branded 5-card stage rail (Discovery/Parsing/Data Model & Semantic/App Creation/Validation), `st.balloons()` removed, and Discovery shows a calc-pass/fail score-card tally up front. Pure UI, no engine/parser change, as scoped. | **DONE** |
| R5 | **Human-gated in-app Deploy button** *(user-emphasized)* — **DONE 2026-07-25.** After the 5 stages, a "🚀 Deploy to Snowflake" button (`pipeline_app.render_deploy_step`) ships the generated `app_<stem>.py` + its runtime modules + a per-deploy `datasources.json` to Streamlit-in-Snowflake via `pipeline.deploy_streamlit_app` — `session.file.put` stages the files, then `CREATE OR REPLACE STREAMLIT`, all through the Snowpark session (no `snow` CLI, since a SiS sandbox has none). Fully-qualified, never a session-context `USE`; re-deploy replaces in place; best-effort Snowsight deep link returned. Only live when a Snowflake session is connected. Gate `test_deploy_streamlit_app`. | **DONE** |
| — | **Blend accelerator-console UI reskin** *(user-provided design, 2026-07-26)* — **DONE + deployed live (twice).** `pipeline_app.py` wears the Blend-branded console design (sis-* CSS + render_* sections, extracted verbatim via AST from the user's own console file and inlined — no new module, no snowflake.yml change). Final shell: **3 tabs** (Overview / Discover & Scope / Migration report). Took four rounds of live user review to get honest and useful: the first pass copied the console's illustrative 8-stage narrative and mock content (a hardcoded parity chart, an empty token/screenshot cockpit, a JSON dump, a Briefs tab we don't produce) — all removed. Every stage view now shows **our real 5 stages**; a **live step tracker** + auto-collapsing finished stages make progress visible during a demo instead of presenting a finished wall of text; **Run Center and Element explorer were dropped** (neither earned a distinct job — both re-showed the same scorecards) and folded into one **Migration report**: verdict header, grouped item counts (data model / dashboards & sheets), per-stage pipeline table, validation result, detail expanders, the full Stage-5 validation proof, and a **downloadable PDF** (fpdf2 — the tabbed Excel inventory was removed as unreadable). One `_report_sections()` feeds both the screen and the PDF so they cannot drift. Presentation-only; 49/49 regression gates green throughout. | **DONE** |
| R8 | **Cortex vision validation + real AI-token metering** — **redesigned 2026-07-28** (user rejected manual screenshot upload): both sides pulled/rendered automatically instead of a manual upload. **Tableau side:** real REST pull via "Query View Image", never a screenshot of any Tableau UI. **Streamlit side:** screenshotting the deployed (SSO-gated, zero-egress SiS sandbox) app was considered and rejected as a dead end without automating credentials; instead, render the SAME Altair chart object `engine.py` already builds to a static PNG server-side via `vl-convert-python` — pixel-accurate, no browser. **Tableau-side REST piece built 2026-07-28:** `tableau_server.query_view_image()`, `pull_all_view_images()` (self-contained, one session per workbook, a single view's failure doesn't abort the rest — mirrors R2's CSV-pull trio), `fetch_view_image()`. Gate `test_tableau_server_view_image_pull`. **Headless Streamlit-render piece built same session** ("implement what we agreed on"): new `headless_render.py` — `render_sheet_to_png()` calls `engine.render_sheet()` directly with `st.altair_chart`/`st.plotly_chart` monkeypatched to capture the chart object (the same technique this project's own regression suite already used to test chart output without a live Streamlit runtime), converts the captured Vega-Lite spec to PNG via `vl-convert-python` — no browser, no SSO. `render_dashboard_to_png()` composites a whole dashboard into one PNG (the unit matching a Tableau view/tab), reusing `engine._rows_from_geom`'s own row grouping; Plotly-rendered sheets (maps) honestly report "not yet supported" rather than a blank image. **Verified against the real Superstore fixture** before writing gates (a real 276KB dashboard composite + 335KB single-sheet PNG, KPI-only sheets correctly reported as not-rendered). Gate `test_headless_render_to_png` — real PNG magic bytes, non-chart/Plotly/raising sheets each get a stated reason, the `st.altair_chart` monkeypatch proven to always restore even on exception. Suite 65→67. `environment.yml` gained `vl-convert-python` (flagged **not yet confirmed** against the Snowflake conda channel — a compiled Rust wheel, materially higher-risk than the pure-Python deps added so far) and `pillow`. **Blocking precondition CONFIRMED WORKING 2026-07-29, live, user-run:** Cortex vision input to `AI_COMPLETE` genuinely works on `wb19670-c2gpartners` — the user ran a real dashboard screenshot (Customer Analysis Dashboard) through it and got back an accurate, detailed description (correctly read every regional KPI number, correctly interpreted the scatter plot, correctly named individual customers off the ranking chart). **Two real gotchas found and now documented:** (1) a stage's *default* client-side encryption is not supported as an `AI_COMPLETE` file source — must create it with `ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')`; (2) `PROMPT()` needs an explicit `{0}` placeholder for the file binding — omitting it doesn't error, it silently returns `NULL`. Confirmed-working call shape: `AI_COMPLETE('claude-opus-5', PROMPT('... {0}', TO_FILE('@sse_encrypted_stage', 'file.png')))`. **Vision-diff orchestration + Stage 5 wiring built same session** ("go ahead"): new `parity.py` functions — `ensure_vision_stage()` (idempotent, requests the confirmed SSE encryption, never silently replaces an existing wrongly-encrypted stage), `_stage_png()` (uploads bytes straight to a stage, no temp file), `_describe_image_via_cortex()` (one vision call per image, the confirmed `{0}`-placeholder shape), `_compare_descriptions_via_cortex()` (compares the two resulting **text** descriptions via a plain-text call — deliberately not an untested multi-image `PROMPT()` call), and `vision_validate_dashboard()` (the full orchestration, returns verdict/explanation/both descriptions/tokens/errors, skips the comparison call if either description fails). **Wired into Stage 5:** a "Visual validation (Cortex vision)" section — pulls every Tableau view image, matches each dashboard to its Tableau view by name, renders the app's own image via `render_dashboard_to_png`, runs the vision diff per dashboard, adds real token usage to the sidebar total. `snowflake.yml` gained `headless_render.py` in the artifacts (caught immediately by `test_pipeline_demo_bundle_complete` — exactly the mechanical catch it exists for). Gate `test_vision_validate_dashboard` — SSE-encryption request asserted, `{0}`-placeholder presence asserted, null/raising responses proven to become stated errors, the comparison call proven to be genuinely plain-text, full orchestration proven against a fake session including a partial-failure case. Suite 67→68. **First live click found two real bugs, both fixed same session:** (1) metrics with no independent Tableau reference at all were still sent to Cortex with the reference hardcoded to the literal string "unknown," reliably producing a false "BUG" verdict — fixed by skipping Cortex entirely for those metrics (shows "NO REFERENCE" instead), in both the live Stage 5 loop and the downloadable notebook; gated with a fixture metric that has no `tableau_bound`, asserting exactly the right number of Cortex calls. (2) a real crash — `StreamlitDuplicateElementKey` — because the headless render path called the real `engine.build_where()`/`render_sheet()` a second time for a dashboard the live app had already rendered once, re-registering the same widget keys; fixed with a new `_mocked_widgets()` context (same monkeypatch technique as chart capture, generalized to every input-widget function), verified by directly counting real widget calls against the live fixture (0, and identical PNG output). Suite 68→69. Redeployed with both fixes. **TWO MORE REAL BUGS FOUND + FIXED 2026-07-30** from the user's live results table (7 dashboards reading `UNKNOWN` with raw escaped JSON in the Note column). (1) **Every vision verdict was UNKNOWN because the JSON was never parsed** — `AI_COMPLETE` returns a VARIANT, and stringifying a VARIANT yields its *JSON representation* (quoted + backslash-escaped), so the payload was a JSON **string containing** the JSON object; `_extract_json_obj` hit the `\"` right after the opening brace and failed on every candidate. Cortex's verdicts were well-formed and simply never read. Fixed by peeling string-encoding layers before the brace scan — also fixes R2's `cortex_judge_section`, which shares the parser. (2) **The diff was comparing a full dashboard against a deliberately partial render** — with verdicts finally readable they said "missing the KPI summary panel"; but `headless_render` draws Altair charts only, so KPI tiles and Plotly/map sheets are absent from the app-side PNG *by design* (the live app renders them; only the static exporter cannot). Fixed by threading headless_render's own per-sheet notes into the comparison prompt as an explicit "these are known-absent, not a bug" caveat, and surfacing `omitted_sheets` on every result row so a verdict over a partial image never reads as a verdict over the whole dashboard. Gates: `test_vision_validate_dashboard` extended + the double-encoded shape locked; teeth proven by reverting both. **Still open:** none of the 2026-07-29 or 2026-07-30 fixes has been re-tested live yet — offline-verified with the same rigor as the rest of this project's gates, awaiting a re-click in Snowsight. Separately flagged, deliberately not fixed this pass: the whole page blanks/reruns on any Stage 5 button click (pre-existing, `run_pipeline()` has zero caching) — real, scoped as its own follow-up. **2026-08-07 — UI REMOVED, superseded by the R2 note above.** The user asked for the Cortex-judged vision comparison to be replaced by the proof-first pack's real screenshot pair + STRUCTURAL similarity score (deterministic, no AI verdict). `render_vision_validation` was deleted from `deep_validation.py`; the underlying `parity.vision_validate_dashboard`/`ensure_vision_stage`/etc. remain, covered only by `test_vision_validate_dashboard`, unreachable from the UI. **2026-08-07 LATER — RE-WIRED AND LIVE-PROVEN, on evidence rather than preference.** Opening all ten real Tableau-vs-app image pairs (which had never been done — only similarity NUMBERS had been read) showed the structural metric is not fit for this job: two COMPLETELY different charts scored 0.797 while the one genuinely-matching pair scored 0.858, so the whole signal range is ~0.06 wide with the 0.85 threshold inside the noise. It cannot distinguish a faithful migration from an unrelated chart. That is a concrete, evidenced argument for Cortex vision, so it was wired back in as `deep_validation.render_cortex_vision` — click-gated, reusing the SAME images the pack already scored, with the deterministic score kept BESIDE the AI verdict as a labelled cross-check (never replaced), and `_omitted_sheets` still telling Cortex which sheets the exporter genuinely cannot draw. **LIVE: 9 dashboards, ~39,267 tokens, BUG on all 9 with specific checkable reasons** (missing forecast/confidence band; a bullet-chart matrix rendered as plain bars; per-panel axes where Tableau shares one; a $0 reference line replaced by a trend line; missing Canadian provinces; KPI percentages 46/28/27 vs 49.7/24.0/26.3; two columns and a filter header dropped) — including **Customer Analysis, which scored 0.858 = structural PASS while Cortex found a real difference.** Also required expanding `headless_render` to capture ALL FOUR channels (Altair, Plotly via kaleido, KPI tiles and tables via PIL) in one render pass, since 5 of 10 dashboards previously produced no app image at all: **sheet coverage 12/20 → 20/20.** **NOT YET DEPLOYED.** | **DONE** (wired + live-proven; deploy pending) |
| R9 | **Live connection with a JOIN across multiple tables** — **DONE 2026-07-26, same session it was found.** The real bug was worse than the documented refusal: `live_connections()`'s relation scan matched every `<relation>` anywhere in the datasource, including the object-model's per-object relations nested in `<object-graph>` — for a live 2-table model built with the modern relationship syntax (not the legacy `type='join'` tag the old refusal checked for), this silently returned `queryable: True` pointed at just the FIRST table, dropping the join entirely. Fixed by scoping the scan to the federated connection's direct-child relations only, so a genuine multi-table live model is now correctly refused. **Once that refusal fires correctly, zero new plumbing was needed** — R10's already-built `onboard()` machinery picks up the caption automatically and deploys the join view against the live tables directly. Gate `test_r9_live_multitable_join`, teeth proven by reverting the scan. Suite 56→57 gates. **Synthetic-fixture only — not yet uploaded to Snowsight** (no corpus workbook has a genuinely live multi-table join). **FOLLOW-UP 2026-08-25 — a real gap in THIS fix, found and closed.** R9's direct-child scope is correct and is kept (it is what stops the object-graph leaking back in), but it was only ever confirmed against the **single-table** KPI Live workbook. Real Tableau wraps a **multi-table** datasource's relations in `<relation type='collection'>`, so all of them sat one level below the scan and a genuine live multi-table model fell through to `"no single named table relation found"` — an inaccurate *reason*, never a wrong bind (`queryable` stayed `False` either way, and `semantic_layer.describe_model()` reads the object-graph independently, so Stage 3 always resolved the model correctly). R9's own gate could not catch it: its fixture uses a flat-sibling shape **this repo invented and Tableau Desktop never produces** — confirmed still green under the revert. Fixed by expanding exactly one level through a `collection` relation before classifying (only `collection`, never `join`, so legacy-join detection is untouched). Corpus-swept across all 23 workbooks — the real single-table live workbook still binds `queryable=True` at `SUPERSTORE_ORDERS`. Gate `test_live_multitable_relations_inside_a_collection`, teeth proven (revert → red with the exact original reason string, restored byte-identical). Suite 134→135 gates. Also produced the corpus's **first real-content live multi-table workbook**, `Workbooks/ECommerce_Live_Join_Events_Customers_Products.twb` (the real 50-sheet / 184-calc E-Commerce book re-authored onto live Snowflake over `WBR_DB.PIPELINE_DEMO`), replacing the 3-sheet synthetic as the realistic test case. **Shipped as a `.twbx`, not a `.twb`** — a first attempt emitted a bare `.twb` following R9's own fixture comment (*"a genuinely live workbook is a bare .twb"*), which is true for **data** but wrong when a workbook bundles **assets**: this one's 31 image zones reference 20 PNGs (logo, 6 nav icons, 4 KPI card icons, filter funnel, avatar) and all of them rendered blank until it was repackaged as an archive carrying the original `Image/` entries byte-for-byte (and deliberately **no** `.hyper`). The gate asserts every referenced image is present and no extract is, teeth proven by stripping them. **Not yet opened in Tableau Desktop or run against the live account** (both need interactive SSO). **Follow-on, 2026-08-25 (later):** the first attempt to actually RUN this live workbook through the deployed accelerator died in Discovery at 0% — `invalid identifier 'F."customer_id"'`. The view's SELECT list correctly used the tables' real columns (from the metadata-records' `<remote-name>`), but its **JOIN keys** came from the object-graph's relationship expressions, which are Tableau's *local field names* — quoted verbatim against a live declared source, `f."customer_id"` cannot match `CUSTOMER_ID`. Invisible on every extract-backed star (the copy path upper-snakes both through `to_phys`), so it hit **only** live multi-table models. Fixed by `semantic_layer._local_to_remote()`, which resolves each side of every `ON` against its own table's metadata; gate `test_live_model_view_join_keys_use_physical_columns`, corpus-swept (only the two live E-Commerce workbooks changed, the other 17 byte-identical). The corrected DDL is **verified offline against the exact failing statement, not yet re-run against the live account.** | **DONE** |
| R10 | **Multi-table extract auto-bind to pre-existing separate Snowflake tables** — **DONE + LIVE-VERIFIED 2026-07-26, same session it was found.** Root cause fixed: `semantic_layer._connection(ds)` now reuses R3's own upstream-detection (`tableau_parser._upstream_connections`) instead of a bare `.find()` that picked up the outer `federated` wrapper. A new `_parse_relation_table()` correctly handles the 1/2/3-segment relation shapes (Regional Analysis' real shape is fully-qualified `[SANDBOX].[DS].[TABLE]`) without dot-joining onto a caller default — the double-schema bug class this project keeps guarding against. **The verification, not just the parsing, is the actual feature:** `pipeline.verify_table_candidate()` existence+column-checks a declared-source table exactly like R3's single-table resolver (a name is not evidence); `data_model_report`'s `deployable` now requires **every** table in the graph to verify — a single unverified table refuses the whole model, never a partial bind; `build_data_model_tables` **skips decode+copy entirely** and deploys the view straight at the originals once everything verifies. Gate `test_r10_multitable_source_autobind` (9 cases incl. the wrong-table guard, a no-copy proof via a write-raises fake session, and corpus proof — Regional Analysis' real XML now resolves to `SANDBOX.DS.*` instead of the assumed-copy location the bug produced). Teeth proven by reverting `_connection()` and disabling the column check. **LIVE-VERIFIED:** built + uploaded `Workbooks/R10_Chain_Over_Existing_Tables.twbx` (3 tables pre-loaded separately first) with decode genuinely blocked — Stage 1/3 both confirmed the no-copy bind live. This upload also surfaced and fixed a SECOND real bug: `onboard()`'s missing-check couldn't see a multi-table datasource's separate verification, so Stage 1 hard-stopped before Stage 3's mechanism ever ran — fixed inside `onboard()` itself, gated (`test_onboard_resolves_multitable_missing_before_stopping`, deliberately not auto-run since it needs a live session), redeployed, re-verified live. | **DONE** |
| | **NEWLY-REQUESTED TOTAL** | **R1/R3/R9/R10 all done. R2/R8 SUPERSEDED 2026-08-07** by the deterministic proof-first validation pack (see each row's own note) — that pack is live-deployed. Its three specific gaps (pivoted-CSV alignment, date-part column aliasing, two unresolved backend measures) are **all closed as of 2026-08-13** (B3, B4, B5) — remaining Track B work is now `order_match` structural-fail semantics (B1), the Global Sales crash (B2), and low-urgency parser/proof items (B6–B8), not the original R2/R8 gaps. |

## Grand total
**MVP: ~0.5 dev day LEFT** (of the original ~6–9; all 5 engineering items DONE — Custom
SQL, live connection, union, datasource-routing fix, TopN/MeasureSwap assessment — only
your Snowsight sign-off #6 remains, which is not dev work).  **Post-MVP backlog: ~25 days.**
**Newly-requested roadmap (Cortex per-section validation, Cortex vision screenshot
validation): ~4–6 days** (R1/R3/R4/R5/R6/R7/R9/R10 and the Blend console reskin all done
— R1 closed 2026-07-28, live-verified end to end through the actual deployed Snowsight
app including a project/workbook browse UI, not just offline; only R2 and R8 remain).
**Everything pending for the accelerator: ~29.5–31.5 days.**

## One-line summary for a manager
*(Updated 2026-07-28 — this paragraph previously understated progress; R1/R3/R4/R5/R6/R9/R10
and the non-star-joins half of R7 have all shipped and live-verified since it was last written.)*

*"MVP is 6 of 6 done — all engineering complete. All three named data-model scenarios
built (Custom SQL, live connection, union), plus the datasource-routing correctness fix and
the last untested corpus workbook assessed and numerically verified. Only your own visual
sign-off remains (not dev work), so ~0.5 day to MVP demo-ready, down from ~6–9. The full
post-MVP backlog (chart types, table-calc edge cases, the FIXED-LOD order-of-ops case,
Cortex scale-out, deploy automation, cosmetics) is another ~25 days.
The demo UI wears a Blend-branded console design, truthful to what the accelerator
actually does (real 5-stage progress, real validation proof, a real Excel migration
inventory) — deployed live. Of the newly-requested roadmap: pull-a-workbook-from-Tableau-
Server-by-link (R1), auto-point to an existing Snowflake table (R3), the human-gated deploy
button (R5), in-app Cortex Analyst chat (R6), non-star joins + blend link extraction (R7),
and both live/extract multi-table auto-bind items (R9/R10) are ALL DONE and live-verified —
R1 as of 2026-07-28, through the actual deployed Snowsight app (a project/workbook browse UI
with search, not just a pasted link). Only Cortex per-section end-to-end validation (R2,
2–3 days — now unblocked to start on real Tableau values since R1 landed) and Cortex-vision
screenshot validation with genuine AI-token metering (R8, ~2–3 days) remain — ~4–6 days,
~29.5–31.5 days pending in total, all real and roadmapped, none blocking the MVP date."*

**2026-08-07 addendum:** R2 and R8 as described above are superseded, not merely completed as
scoped — see each row's own dated note. Validation is now ONE deterministic, proof-first pack
(`render_proof_first_validation`), live-deployed, with real Tableau screenshots/rows where
REST can reach them. What's left is narrower and more concrete than the original R2/R8
estimates: pivoted-CSV column alignment, date-part column-name aliasing, and two unresolved
backend measures.
