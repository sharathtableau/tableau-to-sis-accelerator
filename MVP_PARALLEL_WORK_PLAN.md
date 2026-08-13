# Work Plan — Completed & Remaining
*(Prepared 2026-07-21, refocused 2026-07-22. Companion to MVP_ACCELERATOR_SCOPE.md.
Manager-facing — see MVP_PARALLEL_WORK_PLAN.html for the visual version — NOTE: that HTML
export is a manually-maintained snapshot last regenerated 2026-07-22 and has NOT been kept
in sync with this .md since; treat this .md as the current source of truth.
Updated 2026-07-28 to reflect everything shipped since the original plan: R1/R3/R4/R5/R6/
R9/R10 and the non-star-joins half of R7 have all closed, live-verified. Work packages 6 and
7 below are rewritten to match; packages 1/2/3/5 and part of 4 are unchanged from 2026-07-22.)*

## Completed (MVP — all engineering done)
No hours claimed here — these are simply **done**, verified by a 60-gate regression suite +
numeric harness, and deployed live to Snowsight:

- ✅ Data-routing correctness fix (the "open landmine")
- ✅ Custom SQL execution
- ✅ Union support
- ✅ Live connection support (proven live on Snowflake)
- ✅ Superstore Measure-Swap workbook assessed (numerically exact)
- ✅ ~6 real bugs found + fixed on live Snowsight testing (region-filter, param/filter,
  bar-chart, top-N-vs-context, stale-filter)

**MVP = 5 of 6 done.** The only remaining MVP item is your own **Snowsight sign-off** (a
review, not development).

## Completed since this plan was written (2026-07-25/26/28) — newly-requested roadmap
Also done, verified, and live — none of this was in scope when the plan above was written:

- ✅ **R3** — auto-point an extract-based workbook at its already-existing Snowflake table
  (no decode, no copy); live-verified on a real Snowsight upload.
- ✅ **R4** — Snowsight staged-app UX polish (branded 5-stage rail, calc pass/fail tally,
  balloons removed) — folded into the Blend console reskin.
- ✅ **R5** — human-gated in-app Deploy button (ships the generated app through the Snowpark
  session, no CLI).
- ✅ **R6** — in-app Cortex Analyst chat ("ask your data"), user-confirmed working live.
- ✅ **R7 (non-star joins half)** — snowflake-schema join chains + blend link-field
  extraction from the workbook's own XML; blend AUTO-MATERIALIZATION deliberately left open
  (a query-time link, not a SQL join — guidance + reviewable remodel SQL only, by design).
- ✅ **R9** — a live connection joining multiple tables, found and fixed a worse silent-wrong-
  answer bug along the way; live-verified against the real account.
- ✅ **R10** — multi-table extract auto-bind to pre-existing separate Snowflake tables;
  live-verified in Snowsight, including a second real sequencing bug found + fixed live.
- ✅ **R1** — pull a workbook from Tableau Server/Cloud, closed **2026-07-28**, live-verified
  through the actual deployed Snowsight app (not just offline): PAT sign-in, a project/
  workbook browse UI with search (replacing the need to paste a link), the SiS-specific
  Secret + External Access Integration wiring, and a real end-to-end conversion whose numbers
  matched Tableau's own ground truth exactly. Blend360-branded console UI reskin — done + live
  (delivered R4 as a side effect).

---

## Remaining work — estimated at our pace
**Resources:** Person A **3 hrs/day** + Person B **2 hrs/day** = **5 hrs/day combined.**
A "day" below = one working day of focused output (~5 combined hours). Splitting the two
people across independent packages keeps both busy and avoids editing the same files — it
does **not** exceed 5 hrs/day combined, so the calendar is driven by total effort.

| # | Work package | What's in it | Est. days | Suggested owner |
|---|---|---|---|---|
| 1 | **Cortex arc** (the differentiator) | run calc-fallback across the corpus, deploy E-Commerce semantic view + align YAML, extract blend linking-fields, wire fallback into the demo's Stage 5 | ~3.5 d | Person A |
| 2 | **Chart types** | bins, histogram, box-whisker, bullet, true matrix/pivot | ~6.5 d | split A/B |
| 3 | **Table-calc & LOD edge cases** | native Measure Names/Values swap, RUNNING/LOOKUP/TOTAL/FIRST/LAST, FIXED-LOD-ignores-dimension-filters | ~5 d | Person A |
| 4 | **Data-model completeness** | non-star join → Snowflake views (**DONE 2026-07-26 via R7**), blend link-extraction (**DONE 2026-07-26 via R7**), blend AUTO-MATERIALIZATION (~1–1.5 d remaining, deliberately left as guidance-only so far — see R7), data-source-level filters (not started), external live-source migration kit (not started) | ~5 d *(ORIGINAL ESTIMATE — now overstated: 2 of 4 sub-items are done; the original figure was never broken out per sub-item, so an exact residual can't be computed — treat as an upper bound, true remaining is likely less)* | Person B |
| 5 | **Polish & automation** | rich tooltips, reserved-word column risk, map/choropleth fallback, layout cosmetics, `convert.py --deploy` | ~4.5 d | Person B |
| 6 | **Tableau-integration & validation deepening** *(newly requested 2026-07-22)* | ~~R1 pull-a-workbook-from-Tableau-Server/Cloud-by-link~~ **DONE 2026-07-28**, live-verified through the deployed Snowsight app (a project/workbook browse UI with search, SiS Secret + External Access Integration wiring, real end-to-end numeric match). ~~R3 auto-point to an existing Snowflake table~~ **DONE 2026-07-26**, live-verified. **REMAINING:** R2 Cortex per-section end-to-end Tableau-vs-Streamlit validation (extends `parity.py` Stage 5; Cortex is comparison-only, gated, never source of truth) — now unblocked to start on real Tableau values since R1 landed | ~2–3 d *(R2 only — was ~3.5–5 d)* | Person A |
| 7 | **Semantic layer + ask-your-data + Snowsight UX** *(newly requested; DONE 2026-07-25/26)* | **DONE:** honest Stage 3 (3a Data Model + 3b optional Cortex), **#1 data-model view** (scope A live + scope B star case LIVE-PROVEN), **R5 human-gated Deploy button**, **R6 in-app Cortex Analyst chat**, **R4 Snowsight UX polish**, **R7 non-star joins** (blend link-extraction also done; only blend auto-materialization remains, counted under package 4 above to avoid double-counting) | **0 d — fully done** *(was ~3–4 d)* | — |
| 8 | **Cortex vision screenshot validation** *(new, 2026-07-26 — not in the original plan)* | Upload Tableau dashboard screenshots → `AI_COMPLETE` (vision) extracts each dashboard's KPI values → diff against the app's real SQL-computed values → pass/fail per KPI, execution-gated. Real token counts replace today's mocked-zero Run Center panels. Blocking first step needs a live session (verify Cortex vision input is available on the account) — cannot be built or estimated more precisely offline. Not started. | ~2–3 d | Person A or B |
| | **TOTAL REMAINING** | | **~29.5–32 days** *(down from ~30.5–33; package 4's ~5 d is a stale upper bound so the true number likely sits toward the low end; R8 added since it postdates this plan)* | |

**Time to finish everything remaining:** ~29.5–32 days of effort ≈ **~147.5–160 combined hours**
≈ **~29.5–32 working days (~6 weeks)** at the combined pace (3 + 2 hrs/day).
Of this, the previously-scoped backlog (packages 1/2/3/5, plus package 4's likely-overstated
remainder) is ~24.5–26 days and the newly-requested packages add ~5–6 days: R2 (~2–3 d, package
6) + R8 (~2–3 d, package 8) — R1/R3/R4/R5/R6/R7-joins/R9/R10 have all shipped; package 7 is
fully done.

*(Out of scope, not counted: dashboard actions / cross-highlighting / stories; the
"Drill: Product" invented control is a product decision, not engineering.)*

## Suggested split (keeps both people busy, no file conflicts)
- **Person A** (engine-heavy): Cortex arc → Table-calc/LOD edge cases → shares chart types → **R2 Cortex per-section validation** (now unblocked — R1's real Tableau values landed 2026-07-28) → **R8 Cortex vision** (needs a live-session probe first).
- **Person B** (data & polish): Data-model completeness (now mostly just blend auto-materialization + data-source filters + the live-source kit) → Polish/automation → shares chart types.
- ~~**R1 (Tableau-Server ingestion)**~~ **DONE 2026-07-28** — live-verified through the deployed Snowsight app, including a project/workbook browse UI with search (not just a pasted link).
- ~~**R3 (data-model table reuse)**~~ **DONE 2026-07-26** — live-verified.
- ~~**R4 (Snowsight UX polish)**~~ **DONE 2026-07-26** — folded into the Blend console reskin.
- **R5 (human-gated Deploy button)** — **DONE 2026-07-25**: deploys via the Snowpark session (`session.file.put` → `CREATE OR REPLACE STREAMLIT`), no CLI; the confirmed path is the local-connected/hosted session, fully-qualified with no session-context `USE`.
- ~~**R6 (in-app Cortex Analyst chat)**~~ **DONE 2026-07-25** — live, user-confirmed.
- ~~**R7 (non-star joins + blend link-extraction)**~~ **DONE 2026-07-26** — only blend auto-materialization remains (~1–1.5 d, deliberately deferred, not a gap).
- ~~**R9/R10 (multi-table auto-bind, live + extract)**~~ **DONE 2026-07-26** — both live-verified.

## If you want a shorter first milestone
Doing just the **Cortex arc (package 1, ~3.5 days)** completes this folder's distinctive
value — the AI-assist layer proven end-to-end across the corpus and wired into the live
demo — without waiting on the full backlog. That's the recommended next target.
