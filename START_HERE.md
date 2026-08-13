# Start here — what this project is, in plain English

*Read this before any other document. It takes about 15 minutes and assumes you
know nothing about this project. Everything else in the repo is reference
material that will make sense once you've read this.*

---

## 1. What problem does this solve?

A company has dashboards built in **Tableau**. They want those same dashboards
running **inside Snowflake** instead, as **Streamlit** apps.

Why they'd want that:
- No Tableau licence needed for viewers
- The data never leaves Snowflake (no copying it out to a separate tool)
- It's cheaper to run

The catch: rebuilding a dashboard by hand takes days or weeks, and you can
never quite prove the new version shows the same numbers as the old one.

**This project automates that.** You give it a Tableau file. It gives you back
a working Streamlit app — plus a report that proves the numbers match.

---

## 2. What does it actually produce?

Give it: `Superstore.twbx` (a Tableau file)

You get back:
1. **A working Streamlit app** — the same charts, filters and dropdowns, running
   as Python code
2. **A report** listing anything it couldn't convert (nothing is ever silently
   skipped)
3. **Proof the numbers match** — it queries Tableau for its real numbers, queries
   the new app for its numbers, and compares them cell by cell

Point 3 is the part clients actually care about. Anyone can generate a chart
that *looks* similar. Proving `Total Sales` is exactly `2,326,534.35` in both
systems is the hard part, and it's most of what this codebase does.

---

## 3. How it works — the five steps

A Tableau `.twb` file is just **XML** (structured text). A `.twbx` is a **zip**
containing that XML plus the data. So the whole pipeline is: read the XML,
understand it, and write out equivalent Python.

```
   Superstore.twbx
        │
        ▼
  1. ONBOARD      Unzip it. Pull out the data (CSV/Excel/Tableau's own
                  ".hyper" format) and load it into Snowflake as tables.
        │
        ▼
  2. PARSE        Read the XML and write down everything found — every chart,
                  every formula, every filter, every colour — into one JSON
                  file. That JSON is called the IR (see glossary).
        │
        ▼
  3. ASSESS       Compare "what the workbook declares" against "what we
                  captured". Anything we can't handle gets REPORTED, never
                  silently dropped.
        │
        ▼
  4. GENERATE     Turn the IR into Python: a Streamlit app that draws those
                  charts by running SQL against the Snowflake tables.
        │
        ▼
  5. VALIDATE     Prove it. Pull Tableau's real numbers, run the new app's
                  numbers, compare every row of every chart.
```

**The most important thing to understand: steps 1-4 involve NO AI.** It is
ordinary, deterministic code — read XML, write Python. Same input always gives
the same output. AI (Snowflake Cortex) is used only in a few optional, clearly
fenced-off places. If you're wondering "how does it know what chart to draw?",
the answer is always "there's a rule in the code for that", never "the model
figured it out".

---

## 4. The vocabulary you need

You'll hit these words constantly. None is complicated, but nothing in the repo
defines them.

### Tableau words

| Word | Means |
|---|---|
| **.twb** | A Tableau workbook file. It's XML — you can open it in a text editor |
| **.twbx** | The same thing zipped up *with the data included* |
| **Worksheet** (or **sheet**) | **One chart.** "Sales by Region" is a sheet |
| **Dashboard** | **A page holding several sheets**, arranged in a layout |
| **Pill** | A field dragged onto a chart. Tableau draws them as little rounded tags — hence "pill". Whether a pill is a *measure* (a number to add up) or a *dimension* (a category to group by) determines what chart gets drawn |
| **Calc** / **calculated field** | A formula, e.g. `Profit / Sales` |
| **LOD** | "Level Of Detail" — a Tableau formula like `{FIXED [Region]: SUM([Sales])}`, meaning "total sales per region, regardless of what else is on the chart" |
| **Table calc** | A formula that works *across rows* — a running total, a rank, "% of total" |
| **Extract** | The data is **copied inside** the Tableau file (a `.hyper` file) |
| **Live connection** | The data **stays in a database**; Tableau queries it each time |
| **Blend** | Two separate data sources joined *at the moment the chart is drawn*, rather than in the database |

### This project's own words

| Word | Means |
|---|---|
| **IR** | **Intermediate Representation.** A big JSON file listing everything found in the workbook — charts, formulas, filters, colours. It's the hand-off point: the parser writes it, the code generator reads it. When you see `superstore_ir.json`, that's a parsed workbook |
| **Gate** | **A test.** This project calls its tests "gates" because they gate whether work counts as done. There are 82 of them in `tests/test_regression.py` |
| **Teeth** | A gate has "teeth" if it actually fails when the bug comes back. Standard practice here: undo your fix, confirm the gate goes red, redo your fix. A gate that passes either way is worthless |
| **Corpus** | The collection of real Tableau workbooks used for testing, in `Workbooks/` |
| **The pack** / **validation pack** | The bundle of evidence proving a migration is correct: screenshots, per-chart number comparisons, a summary report |
| **Finding** | A reported problem or limitation. "Reported, never silently dropped" is the core rule |
| **Stage 1-5** | The five pipeline steps above, as shown in the app's UI |
| **SiS** | Streamlit in Snowflake — a Streamlit app hosted inside Snowflake |
| **R1, R3, R7...** | Roadmap item numbers from earlier work. R-numbers are just labels; don't read meaning into the ordering |

---

## 5. The three rules that matter most

This project's whole value is that its output is **trustworthy**. A client is
going to retire a real Tableau dashboard based on what it says. So these three
rules outrank speed, elegance, and shipping:

### Rule 1 — "I couldn't measure this" must never look like "this is correct"

Every chart check ends in one of these:
- **PASS** — checked, matches
- **FAIL** — checked, does NOT match (a real bug)
- **BLOCKED** — could not check at all (no screenshot, no Tableau data, etc.)

**BLOCKED is not a pass.** If we couldn't verify something, the report must say
so plainly. Turning a BLOCKED into a PASS to make a report look green is the
worst thing you can do in this codebase.

### Rule 2 — Never guess

When the code can't work something out with certainty, it **refuses and says
why**. It does not pick the most likely answer.

Real example: some charts can't be matched to their Tableau data because two
different charts look equally plausible. The code refuses both rather than
picking one. Someone previously tried the "smarter" guess-the-closest-match
approach — it silently matched the *wrong* chart, and the report confidently
showed wrong numbers. **A refusal you can see beats a guess you can't.**

So when you find code that seems needlessly strict — that's usually deliberate.
Check *why* before loosening it.

### Rule 3 — Nothing is silently dropped

If the converter meets something it can't handle, it writes a **finding** that
appears in the report. It never produces a blank chart or a made-up number.

---

## 6. What you'll actually do day one

1. **Set up** — follow `ONBOARDING.md`. Clone the repo (into a *short* folder
   path — this matters on Windows), install dependencies, run two build commands.
2. **Run the tests** — `python tests/test_regression.py`. Takes a few minutes,
   should end with `ALL REGRESSION TESTS PASSED`. This proves your setup works
   before you change anything.
3. **Read your task brief** — you're on Track A or Track B (below).
4. **Pick your first task** — it's already chosen for you.
5. **Make the change, add a gate, prove the gate has teeth, re-run the tests.**

### Which track am I on?

**Track A — Chart & Calc Engine.** You're **adding new capability**. There are
Tableau chart types and formulas the converter doesn't understand yet, so it
currently reports them as gaps. You'll teach it. Mostly one file: `engine.py`.
Your first task is **bins** (a Tableau feature that buckets numbers into ranges
— "0-10", "10-20"). It's the single most common thing the converter can't do.

**Track B — Validation & Correctness.** You're **fixing what's already built**.
The system that proves migrations are correct works, but has known gaps and one
bug. Your first task is a **crash**: one dashboard (Global Sales) crashes the
screenshot step, and nobody has worked out why yet.

---

## 7. Where everything lives

Read these in order. Don't start at the bottom.

| Order | File | What it's for |
|---|---|---|
| 1 | **`START_HERE.md`** | This file |
| 2 | **`ONBOARDING.md`** | Setup: clone, install, build, run the tests |
| 3 | **`HANDOFF_TRACK_A_...`** or **`..._TRACK_B_...`** | Your task brief, in detail |
| 4 | `RESOURCE_ASSIGNMENT.md` | The full ticket list, priorities, who does what |
| — | `ARCHITECTURE.md` | Deep reference. **Don't read it front to back** — 1,800 lines. Look things up in it |
| — | `DATA_MODEL_STATUS.md` | Reference: which data setups are supported and how well proven |
| — | `NEW_CHAT.md` | The project diary. Useful for "why on earth is it like this?" |

**A warning about the reference docs:** `ARCHITECTURE.md` and `NEW_CHAT.md` were
written as a running log for people already deep in the project. They're dense,
they assume the vocabulary above, and they're long. That's fine — they're
lookup material, not reading material. Use `Ctrl-F`.

---

## 8. Some honest context about this codebase

- **It is unusually well tested.** 82 gates, and most exist because a real bug
  got through once. If a gate looks paranoid, it's because something broke.
- **The comments are long and explain *why*.** That's deliberate. When you fix
  something subtle, explain the reasoning the same way.
- **Some things are deliberately not done.** "We don't support X" is often a
  considered decision, not an oversight. `DATA_MODEL_STATUS.md` marks which is
  which.
- **The trackers are updated by hand.** When you finish something, update
  `status_config.json` and `MVP_ACCELERATOR_SCOPE.md` *in the same commit*.
  They've drifted before.
- **Ask rather than guess.** If a task seems to require a product decision —
  "should this count as an error or not?" — that's a question for Sharath, not
  a judgement call for you. Two such decisions were already made this way and
  are written into your brief.

---

## 9. If you get stuck

- **Setup problems** → `ONBOARDING.md` section 2 (the short-path issue catches
  everyone on Windows) and section 4 (two build steps people skip)
- **"What does this word mean?"** → the glossary above
- **"Why is the code like this?"** → search `NEW_CHAT.md` for the feature name
- **"Is this supported?"** → `DATA_MODEL_STATUS.md`
- **"Should I change this behaviour?"** → if it changes a number a client sees,
  ask first
