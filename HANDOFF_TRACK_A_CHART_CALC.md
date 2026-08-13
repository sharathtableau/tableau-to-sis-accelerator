# Handoff Brief — Track A: Chart & Calc Engine Backlog

> ### 👉 New to this project? Read `START_HERE.md` first.
> It explains in plain English what this project does, how the pipeline
> works, and what the vocabulary means (*IR*, *gate*, *pill*, *extract*,
> *BLOCKED vs FAIL*). This brief assumes all of it. Fifteen minutes there
> saves an afternoon of guessing here.

*(Prepared 2026-08-13 for onboarding a new engineering resource. Scope: the
post-MVP chart-type and table-calc backlog from `MVP_ACCELERATOR_SCOPE.md`'s
"Remaining" section, filtered to the items that are purely additive engine
work — new chart renderers and calc translation, not validation/data-model
correctness. Read `ARCHITECTURE.md` §1–4 first for how the pipeline and
`engine.py` fit together before touching anything below.)*

## How this codebase works (read before writing code)

- **Deterministic core, zero AI at conversion time.** `tableau_parser.py`
  parses XML → IR JSON. `codegen.py` embeds the IR into a generated
  `app_<book>.py`. `engine.py` is the shared runtime every generated app
  imports (`engine.run(IR)`) — this is where almost all of Track A's work
  happens.
- **Chart renderers follow one naming convention**: `engine.py` has one
  `r_<kind>(s, where)` function per chart kind (`r_bar`, `r_pie`, `r_scatter`,
  `r_treemap`, `r_gantt`, `r_dots`, `r_table`, ... — see `grep -n "^def r_"
  engine.py` for the full current list, ~19 kinds). A new chart type means a
  new `r_<kind>` function plus a dispatch entry in `render_sheet`
  (`engine.py:2720`).
- **Calc translation lives in `calc_translator.py`.** Table calcs currently
  refused (never guessed) are gated by the `_UNSUPPORTED` regex at line 118:
  `LOOKUP|FIRST|LAST|RUNNING_\w+|TOTAL|...`. `WINDOW_SUM/AVG/MIN/MAX/COUNT/
  MEDIAN` and `RANK*` are already translated (line ~240-280) as the reference
  pattern for how a table calc becomes a SQL window function.
- **Nothing is silently dropped.** Every unsupported construct must produce a
  named finding (see `findings.py`), never a blank chart or a guessed number.
  This is the project's core trust invariant — don't break it for the sake of
  shipping a chart type.
- **The loop per new capability** (from `NEW_CHAT.md`'s own stated process,
  still current): implement → `python tests/test_regression.py` (85 gates,
  must stay green) → add/extend a gate proving the new capability with teeth
  (revert the fix, confirm the gate catches it) → update
  `MVP_ACCELERATOR_SCOPE.md`'s status column + `status_config.json`'s roadmap
  entry in the **same change** (see `CLAUDE.md`-equivalent project discipline:
  the tracker is hand-maintained and drifts if not updated with the feature).
- **Test against the real corpus, not just synthetic fixtures.** Workbooks
  live under `Workbooks/`. `report.py <book>.twbx` gives fidelity %,
  converted/partial/failed sheet counts, and calc-drop findings — run it
  before and after your change on any workbook you touch.

## Priority-ordered task list

### 1. Bins — 1.5 days (top corpus gap by occurrence: 197× across the corpus)
Tableau bins are FLOOR-style numeric bucketing (`floor(field / binsize) *
binsize`), distinct from the *categorical*-bin grouping already handled in
`tableau_parser.py` around line 2017 (`class="categorical-bin"` — that's user
GROUPS, already done, don't confuse the two). This is the numeric `<bin>`
construct. Needed as its own capability AND because histogram (#2) depends on
it.
- **Files:** `tableau_parser.py` (parse `<bin>` / binned-field calc XML into
  an IR-carried bin expression), `calc_translator.py` (bin → SQL `FLOOR`
  expression), `engine.py` (a binned dimension needs to render as a
  continuous-looking axis with discrete buckets).
- **Done when:** a real corpus workbook using bins (check `Globalsalesdashboard.
  twbx` and others — `grep -rn "<bin " Workbooks/*.twb` after unzipping .twbx)
  renders the binned chart with numerically correct bucket boundaries, gated
  by a new `test_*` in `tests/test_regression.py`.

### 2. Histogram — 1 day (depends on #1)
A histogram is a bar chart over a binned continuous measure with COUNT/COUNTD
as the aggregate. Once bins exist, this is mostly wiring: a new `r_histogram`
in `engine.py`, dispatched off the existing bin detection.
- **Done when:** a histogram sheet renders with correct bucket counts against
  a real or purpose-built test workbook.

### 3. Box-whisker chart — 1 day
New `r_boxwhisker` in `engine.py`. Altair has native `mark_boxplot()` — check
whether Tableau's box-plot semantics (quartile method, outlier display) match
Altair's default before assuming a 1:1 mapping.
- **Done when:** min/Q1/median/Q3/max match a hand-computed reference on a
  test workbook.

### 4. Bullet chart — 1 day
New `r_bullet` in `engine.py`. Needs the qualitative-range bands + target
marker semantics Tableau bullet charts carry in their XML — check how
reference lines (`_refline_rule`, `engine.py:981`) are already parsed, since
a bullet's target line is structurally similar.

### 5. True matrix / pivot table — 2 days
Distinct from the existing `r_table`/`_rank_table` (`engine.py:1802`), which
is a flat crosstab. A true matrix needs row+column dimension crossing with
subtotals. Likely the most structurally different of this list — budget time
to read `_rank_table` and `r_table` fully before starting, since a naive
implementation will duplicate a lot of existing formatting/glyph logic
(`_ascii_glyphs`, `_table_to_png` if this also needs to render into the
validation screenshot pipeline — check with Track B before assuming it does).

### 6. Native Measure Names/Values selector — 1 day
The calc-based form (a CASE calc on a parameter driving which measure ranks/
plots) is **already done and numerically verified** on
`Superstore_TopN_MeasureSwap.twbx`. What's missing is Tableau's *native*
Measure Names/Values shelf construct (no user-written calc) — the parameter
is already captured, only the switch-wiring from the native shelf token to
the same rendering path is the gap.
- **Files:** `tableau_parser.py` (detect the native Measure Names/Values
  shelf pattern), `engine.py` (route it to the same code path the calc-based
  form already uses).

### 7. Table calcs RUNNING_*/LOOKUP/TOTAL/FIRST/LAST — 2 days
Currently in the refusal regex (`calc_translator.py:118`). These need
view-ordering support — SQL window functions with an explicit `ORDER BY`
derived from the sheet's own sort context, which today is only tracked for
display purposes, not fed into calc SQL generation.
- **Start here:** read how `RANK`/`RANK_DENSE`/`RANK_UNIQUE` were done
  (`calc_translator.py:240-280`, gated by `test_table_calc_engine` or
  equivalent — check `status_config.json`'s "Table-calc engine" roadmap
  entry, marked done, for the exact gate name) — RUNNING_*/LOOKUP/TOTAL are
  the same family, just needing the sheet's manual/computed sort order as an
  explicit window `ORDER BY` instead of RANK's implicit one.
- **Done when:** a RUNNING_SUM or LOOKUP calc against a real corpus workbook
  matches Tableau's own displayed running total exactly.

### 8. Rich hover tooltips — 1 day (deprioritized by user — take last)
`status_config.json` roadmap entry: "custom tooltip fidelity (155× in
corpus, cosmetic) — user deprioritized; take later." Confirm still
deprioritized before picking this up.

### 9. Map/choropleth blank-in-sandbox fallback — 1 day
Plotly-rendered map sheets are known to render blank inside the SiS sandbox
(a Plotly-in-Streamlit-in-Snowflake limitation, not a parsing bug). Needs a
bar/table fallback presentation when the sandbox can't render Plotly, with an
honest finding stating why.

### 10. Pixel-exact layout cosmetics — 1 day
Hollow scatter marks, reference-line labels, narrow-column truncation. Lowest
priority in this track — pure polish, do last if time allows.

## Ground rules
- **Never guess a chart type or calc translation.** If Tableau's XML for a
  construct is ambiguous or under-specified, report it as a finding — do not
  invent default behavior. This project's entire trust model depends on
  "reported, never silently dropped" staying true (`ARCHITECTURE.md` §1).
- **85 gates must stay green** (`python tests/test_regression.py`) after every
  change, and any new capability needs its own gate with proven teeth (revert
  your fix, confirm the gate fails).
- **Update `MVP_ACCELERATOR_SCOPE.md` + `status_config.json` in the same
  commit as the feature**, not as a follow-up — this project's tracker has
  drifted before when that discipline slipped.
- If you're unsure whether something is Track A or Track B scope (e.g. a
  chart rendering correctly but the *validation* of it failing), that's a
  Track B question — flag it rather than guessing at ownership.

**Total: ~12.5 engineering days across 10 items**, bins → histogram first
(dependency order), then whichever of box-whisker/bullet/matrix/native-
selector/table-calcs the assigned resource is most comfortable with in
parallel, tooltips and cosmetics last.
