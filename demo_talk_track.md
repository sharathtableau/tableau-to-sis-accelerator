# Talk track Tableau to Streamlit-in-Snowflake demo

Twelve slides, ten minutes of deck, a live pass through Snowsight in the middle, and two appendix slides to hold in reserve for questions. Timings are rough. Talk slower than feels natural. It always feels slower to you than it sounds to the room.

---

## How to open, before slide 1 even loads

Don't open with the deck. Open with a sentence.

"Thanks for the time. What I'm going to show you isn't a mockup. It's a working accelerator that takes a real Tableau workbook and turns it into a governed Streamlit app running inside Snowflake, and I can prove every number it produces. Let's get into it."

Then click to slide 1.

---

## Slide 1: Cover

"This is the pitch in one line. We move BI workloads off Tableau and onto Snowflake itself, not by replacing the visuals, but by rebuilding them natively where the data already lives. Three things drive this: simplify the operating model, earn trust through automated validation, and evolve into AI-assisted applications. I'll show you all three today."

*(30 seconds. Don't linger here.)*

---

## Slide 2: Current operating model

"Here's the situation most of you already live with. Snowflake holds the data and the governance. Tableau sits next to it as a second platform, with its own licenses, its own admin, its own deployment cycle. Every extract is a copy of data that already exists somewhere better. Every calculation gets written twice, once in your ELT and once again in a Tableau formula.

That's not a knock on Tableau. It's just what happens when the data platform and the BI platform grow up separately. This program exists to close that gap."

*(2 minutes.)*

---

## Slide 3: Why Streamlit in Snowflake, not plain Streamlit

"Before I show you how this works, I want to answer the question I already know is coming. Why not just run Streamlit on its own, outside Snowflake?

Four reasons. Egress: a standalone app has to pull data out of Snowflake on every query. Ours doesn't, because it runs next to the data. Governance: our app runs as a Snowflake role, so RBAC, masking, and audit apply automatically. We don't rebuild any of that. Infrastructure: there's no VM, no container, nothing to patch. And this is the one that matters most in this room: Cortex becomes a local call. The moment you take Streamlit outside Snowflake and still want to use Cortex, you're calling back into the platform you just left.

And to be fair, if none of that applied, if the data lived somewhere else and there was no governance need, plain Streamlit would honestly be the simpler choice. This case only holds because the data's already here."

*(2 minutes. Say the "to be fair" part out loud. It's the sentence that makes the rest of the slide credible.)*

---

## Slide 4: Value beyond visualization

"So the value isn't just swapping one chart tool for another. Four places this actually pays off: platform economics, since fewer Tableau licenses and less duplicated support cut real cost. Delivery speed, because you're building on Snowflake data with Python and Streamlit components you can reuse. Governance, since you inherit RBAC and masking instead of rebuilding them. And AI readiness, because a migrated app can be extended with CoCo and opened up to natural-language questions through Cortex Analyst.

Before we run a pilot, we'd want to baseline your current licenses, support cost, and how long a dashboard change usually takes today. That's how we prove the business case with your numbers, not ours."

*(2 minutes.)*

---

## Slide 5: AI-assisted BI lifecycle

"One thing I want to flag honestly before we move past this: this slide is where we're headed, not something wired into the accelerator today.

Once a workbook is migrated, we don't treat it as finished. It becomes something you keep improving with natural language, through CoCo, and query directly through Cortex Analyst. Someone could type 'add a region filter to every chart, keep the existing calculations, and add a customer detail view on selection,' and CoCo drafts that change for review. It doesn't ship on its own. Every change still goes through validation before release. The point isn't that AI replaces engineering judgment. It's that it lowers how much specialized coding you need to keep iterating."

*(2 minutes. Say "where we're headed, not built today" plainly. Don't let this slide get mistaken for a live capability.)*

---

## Slide 6: Five controlled stages

"Now let's get into how the migration actually works. One workbook moves through five stages: discovery, where we find the sources and load the tables. Parsing, where the workbook becomes an intermediate model. Data modeling, where we build the relationships and, where it makes sense, a semantic view. App creation, where that model becomes a real Streamlit app. And validation, where we prove it's right.

Each stage leaves something you can inspect. Nothing moves forward silently. And the app only deploys after a person looks at the findings and the validation results."

*(2 minutes.)*

---

## Slide 7: Platform architecture

"This is what that actually looks like end to end."

*(Walk the diagram left to right with your hand, not just your eyes.)*

"Discovery and ingestion on the left pull from Tableau Server or your existing Snowflake tables. The conversion layer turns the workbook into an IR, then generates the app. Cortex sits here, in the middle, inside your account, and it only proposes. It never gets the final word. Everything it suggests has to run against real data, and then a person signs off. On the right, the semantic and serving layer is where your roles, your masking, your audit already apply, because nothing left the account. And at the bottom, every migrated dashboard goes through three independent checks: does the data match, does the formula still mean the same thing, and does it still look like Tableau.

That's not marketing. That's the system running right now."

*(2 to 3 minutes. This is the technical proof point. Slow down here even if the room looks impatient.)*

---

## → Switch to the live app here

Close the deck, or minimize it, and open Snowsight.

Upload a workbook live. Narrate the five stages as they run, using the same five words from slide 6: discovery, parsing, data model, app creation, validation. Let stage five actually finish and show the real pass count on screen, not a screenshot of one.

Say something like: "I'm not going to pre-load this. I want you to watch it happen." Then let it happen. Don't fill the silence while it runs.

When it's done, go back to the deck.

---

## Slide 8: Visual parity

"So here's what you just watched, laid out for review. This is the first check: does the migrated dashboard keep the same shape as the original? Same layout, same KPIs, same chart type, same filters.

I want to be upfront that visual similarity by itself isn't a pass. The colors can differ and that's fine. This is the first layer of evidence, not the whole proof."

*(1 minute. This slide is deliberately modest. Don't oversell it.)*

---

## Slide 9: Data and formula proof

"This is the layer that actually earns a pass. Every number is compared at the grain it's actually displayed at. If a chart shows product-level totals, we check product rows, not a dashboard grand total."

*(Point at the table.)*

"Canon imageCLASS: sixty-one thousand six hundred dollars in Tableau, sixty-one thousand five ninety-nine eighty-two in Streamlit and in the backend. Eighteen cents apart. That's a pass. Every mismatch like it shows up in a downloadable report, not just these three rows."

*(Point at the formula comparison.)*

"And on the formula side, Tableau's sum of profit over sum of sales becomes sum of profit over null-if sum of sales, zero, in Snowflake SQL. Same math, just protected against a divide by zero.

That's what we mean by proof. Not 'it looks right.' It's checked."

*(3 minutes. This is the slide that turns skepticism into trust. Don't rush the numbers.)*

---

## Slide 10: Next steps

"So here's what I'd propose. Pick one workload, ideally one your team nominates rather than one we picked to look good. We run it live, in your account, stage by stage, exactly like what you just saw. Then we review the evidence together before anyone calls it done.

What we need from you is simple: one workload, and one account team willing to sit through that with us."

*(2 minutes. Stop talking after the ask. Let someone in the room answer it.)*

---

## Appendix: hold these two, don't present them

**Slide 11, Cortex boundary:** pull this up only if someone asks where exactly Cortex fits and where it doesn't. The short version, if you need it without the slide: "Cortex touches two things, the hard calculations our rules can't translate and the semantic layer. It never writes the app, and it never overrides a check that already failed."

**Slide 12, evidence contract:** pull this up only if someone asks what's actually inside the report they'd get to keep. The short version: "Every dashboard gets the same six things: a summary verdict, a visual comparison, the chart-level data check, the formula mapping, an interaction check, and the raw artifacts underneath all of it, so nothing in that report is a claim you have to take on faith."

---

## If something breaks mid-demo

It might. Say so, plainly, and keep going. "That's a real finding, not a scripted one, this is exactly the kind of thing Stage 5 is built to catch before it reaches you." A visible failure that gets caught by the tool is a better argument for the tool than a demo that never hits one.
