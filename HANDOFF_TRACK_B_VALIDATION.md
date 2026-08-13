# Handoff Brief — Track B: Validation & Data-Model Correctness

*(Prepared 2026-08-13 for onboarding a new engineering resource. Scope: the
open items from the 2026-08-07 → 2026-08-12 validation arc plus the residual
data-model parser gap. These are correctness/trust items, not new features —
they live in a different set of files from Track A and can run fully in
parallel. Read `ARCHITECTURE.md` §15–19 in order first; that is the entire
design history of the code you'll be touching, most of it written in the last
week.)*

## How this part of the codebase works (read before writing code)

The validation system proves a migrated dashboard is faithful by comparing
**three independent legs** per chart, at the chart's own displayed grain:

| Leg | Where it comes from | Module |
|---|---|---|
| **Tableau** | Real REST pull — crosstab CSV + view screenshot | `tableau_server.py` |
| **Streamlit** | The app's OWN captured chart dataframe (never a re-run of the backend SQL — that would be circular) | `headless_render.capture_sheet_chart` → `validation_adapter.py` |
| **Backend** | A scoped SQL query against the real table | `validation_adapter.py` |

- `validation_report.py` is a **vendored comparison engine** (user-supplied
  originally, then patched — see `status_config.json`'s R12 entry for the four
  bugs fixed inside it). It decides PASS/FAIL/BLOCKED. Treat its strictness
  semantics as deliberate: **do not loosen a check unilaterally.**
- `validation_adapter.py` + `validation_evidence_bridge.py` are the adapters
  feeding it. `deep_validation.py` orchestrates the pack and renders the UI
  (`render_proof_first_validation` is the only validation panel today).
- `app_screenshot.py` (2026-08-10) launches the **generated app locally**
  (`streamlit run` + real Chromium via Playwright) and screenshots each
  dashboard tab. This REPLACED a headless re-render that was caught
  *flattering the migration* — see §18. **Do not reintroduce a second
  renderer for the image leg.**
- **Core trust invariants — breaking these is worse than leaving a bug open:**
  - `BLOCKED` (never measured) and `FAIL` (proven wrong) must never look the
    same. A missing proof cannot become a pass.
  - Never guess a match. Where the code refuses (an ambiguous crosstab, an
    unresolvable column), that refusal is the feature.
  - "No proof, no pass" for the visual claim itself.

## Priority-ordered task list

### 1. `order_match` chart-level FAIL — **DECIDED 2026-08-13, ready to build**

> **SHARATH'S RULING (D1): order is a structural failure ONLY for charts where
> display order carries meaning — rank tables and top-N. Everywhere else it
> becomes an informational flag, not a FAIL.**
>
> Implement exactly that. Do **not** delete the check, and do **not** weaken it
> for ranked lists: a genuinely mis-ordered top-10 must still fail. Your gate
> must prove BOTH directions — a ranked chart with shuffled rows still FAILs,
> a grouped bar chart with different row order does not.

`validation_report.py:198` — `order_match = sources["tableau"].order ==
sources["streamlit"].order`, and line 206 makes it a `structural_failure`.
Live on Regional Analysis, **both fully-evidenced charts read chart-level FAIL
while every individual cell passes**, purely from row *sequence*.
- Exact row order is meaningful for a **ranked list** (display order carries
  visual meaning); it is **not** meaningful for a grouped bar chart.
- This was deliberately **flagged, not patched**, because it changes the
  vendored engine's own structural-failure semantics — a bigger and more
  contestable call than what was asked.
- **Action:** get the product decision (likely: make `order_match` count as a
  structural failure only for chart kinds where order is semantic, e.g. rank
  tables / top-N, and demote it to an informational flag elsewhere). Then
  implement with a gate proving BOTH sides — a ranked list still fails on
  wrong order, a grouped bar chart no longer does.
- **Do not simply delete the check.**

### 2. Global Sales Dashboard View2 crash — unscoped, investigate first
Opened during 2026-08-11/12 screenshot testing; its View2 dashboard **crashed
the re-render-based capture**. The conversation moved to the real-screenshot
rewrite (§18) before it was root-caused. **Never diagnosed, never fixed.**
- Note this crashed the *old* capture path. First question to answer: does it
  still reproduce under `app_screenshot.capture_app`, or was it specific to the
  now-retired `headless_render` image path? That answer decides whether this is
  a live bug or already moot.
- An earlier draft of the status docs wrongly claimed this workbook was tested
  and fixed — it was not (corrected in `NEW_CHAT.md`'s top entry). Treat the
  workbook as genuinely unproven.
- This is the **third corpus workbook**; Superstore and Regional Analysis are
  both proven. Getting a third through end-to-end is the highest-value
  generalization proof available.

### 3. Pivoted / long-shape Tableau crosstab alignment
Tableau's REST crosstab sometimes returns a long/pivoted shape (`Measure
Names` / `Measure Values` columns) instead of a wide one. The column aligner
doesn't reshape it, so those charts lose their Tableau leg entirely.
- **Files:** `validation_adapter.py` (the aligner), `deep_validation.py`
  (`_assign_dashboard_csv_by_header`, line 96 — the exact-header-set matcher).
- **Careful:** the existing exact-set matcher was chosen over a subset match
  *because subset matching was found genuinely ambiguous live* (View2's real
  3-column header subset-matched two different sheets). Any reshape must
  preserve that unambiguity — reshaping changes the header set the matcher
  sees, so re-verify the matcher still resolves uniquely afterward.

### 4. Date-part column-name aliasing
Date-part-grouped Tableau headers (e.g. `"Month of Order Date"`) don't alias
to the chart's own `Order Date` grain, so those columns never compare.
- Related existing machinery: `parity._sheet_pill_captions` (the declared
  field-set scan) and the engine's own date-part handling (`_part_num`,
  `engine.py:583`; date-part filters already translate to `EXTRACT`).
- Should be a contained mapping fix, but confirm against a real pulled
  crosstab rather than assuming the header format.

### 5. Two unresolved backend measures
`Days to Ship Scheduled` and `Sales Forecast` don't resolve against the
backend table, so their charts can't complete the backend leg. Diagnose
whether these are calc-translation gaps, column-mapping gaps, or genuinely
absent from the loaded table — the three have very different fixes.

### 5b. `Discount` AVG-vs-SUM — a REAL bug in the shipped app — **DECIDED 2026-08-13**

`profile_superstore.py:20` curates `"Discount": {"sql": "AVG(DISCOUNT)"}`.
Because `Discount` is a **raw column**, that curated AVG silently overrides the
workbook's own declared `agg=sum`. The validation pack caught it and Tableau
itself settled it via REST:

| | Tableau | App | Backend |
|---|---|---|---|
| Product Detail Sheet `Discount` | 0.2 / 0.8 / 0.4 | **0.1 / 0.2 / 0.1** | 0.2 / 0.8 / 0.4 |

Tableau and the backend agree; **the migrated app is wrong.** It went unfixed
because AVG is legitimately correct on *other* sheets, so a blind swap to SUM
trades one wrong number for another.

> **SHARATH'S RULING (D2): respect the workbook's declared aggregation.** A
> curated profile entry applies only where the workbook itself declares that
> same aggregation; otherwise the workbook's declared agg wins.

This is deliberately the structural fix, not a one-line swap — it prevents the
whole bug class (any curated entry silently overriding a raw column's declared
agg) rather than just this measure.

- **File:** `profile_superstore.py`, plus wherever the profile curation is
  applied (`engine.py`'s measure resolution — `resolve_measure` /
  `_resolve_measure`, `engine.py:218-255`).
- **Done when:** Product Detail Sheet's Discount matches Tableau's
  0.2/0.8/0.4, **and** the sheets where AVG is correct still read AVG, with a
  gate covering both. Note this changes numbers the DEPLOYED app renders, so
  it needs a redeploy and a mention in the tracker.

### 6. `source_tables()` misreports a legacy-joined object — parser accuracy, low urgency
`DATA_MODEL_STATUS.md` §3.7. `tableau_parser.source_tables()` uses
`ds.findall(".//relation[@type='table']")`, which walks nested relations
buried inside a **legacy `<relation type='join'>` clause tree**. On
`Globalsalesdashboard.twbx` this reports a single already-resolved legacy-
joined object as a *"3-table data model"*.
- **Practical impact today: none** — the `bindable=False` outcome happens to
  be the same conclusion a correct read reaches. **Only the reason string is
  misleading.**
- **Fix:** detect and skip relations nested inside another relation's
  `<clause>` subtree.
- Genuinely untested and separate: whether a workbook using
  `<relation type='join'>` as its ONLY structure (a real pre-2020.2 file, no
  object-model graph) parses/flattens correctly for an extract. No such corpus
  workbook exists.

### 7. Multi-fact / cyclic graph refusal — proof only, no code expected
`DATA_MODEL_STATUS.md` §3.4/§3.5. The refusal logic is **built**; it has only
ever been proven on synthetic fixtures. Build a real workbook (two fact tables
sharing one dim, or a 3-table loop) via `tests/make_datamodel_workbooks.py`
and confirm the refusal fires correctly on real XML. Expected outcome: a
passing proof, not a fix.

### 8. R9 live multi-table join — UI upload confirmation only
Code-complete, gated (`test_r9_live_multitable_join`), and verified against
the real account through `pipeline.onboard()` directly. The only missing step
is uploading `Workbooks/R9_Live_Join_Orders_Product_Category.twb` through the
deployed `pipeline_demo` Streamlit UI — the same final confirmation R3 and R10
already passed. **Zero dev work; a 10-minute task for whoever has Snowsight
access.**

## Ground rules
- **Never let a missing proof become a pass.** The `BLOCKED` vs `FAIL`
  distinction was hard-won (a wholly-absent source used to report FAIL, i.e.
  "proven wrong", when the honest answer was "never measured").
- **Never guess a match.** Where the code refuses, understand *why* it refuses
  before changing it — at least two refusals in this codebase exist because a
  looser version was tried and found genuinely ambiguous against live data.
- **Don't reintroduce a second renderer** for the image leg. If a screenshot
  can't be taken, report BLOCKED with the reason.
- Regression suite must stay green after every change, and any fix needs a
  gate with **proven teeth** (revert the fix, confirm the gate goes red) —
  this project does that consistently and the gates have caught real drift.
- **Update `status_config.json` + `MVP_ACCELERATOR_SCOPE.md` in the same
  change as the fix.** `test_tracker_consistency` enforces part of this
  mechanically, but only for under-statement of a fixed list — the rest is
  discipline.
- Packs must be generated **from a workstation**, not from inside the deployed
  SiS app (no browser there, so the visual leg is always BLOCKED). This is a
  known, accepted cost, not a bug to fix.

**Total: items 1–5 are the real work** (unscoped until #1's decision and #2's
diagnosis land — deliberately not given day estimates, since three of them are
"investigate, then fix"). Items 6–8 are small/proof-only and can be slotted in
as filler.
