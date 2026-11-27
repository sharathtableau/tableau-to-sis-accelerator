"""
Runs the dashboard-validation SKILL's methodology (comparison table +
diagnostic-on-anomaly + confirmed-bug-vs-intentional-difference + explicit
verdict, every section) against the live `wbr` account, Superstore.twbx.

Applies the skill's STRUCTURE to this workbook's real schema, not the WBR
retail mart's CHANNEL/TIME_FLAG/YEAR_FLAG/PLAN_LE columns (which don't exist
here) -- per the skill's own rule: confirm real schema, never invent one.

Two deliverables, both with REAL results + REAL Cortex-authored content:
  1. <stem>_dashboard_validation.ipynb
  2. <stem>_dashboard_validation.html

Run once, locally, against a live session:  python scripts_run_live_section_validation.py
"""
import backend
import config
import parity
import pipeline
import tableau_parser as TP

STEM = "superstore"
BOOK = "Superstore.twbx"

print("Parsing", BOOK, "...")
ir = TP.build_ir(f"Workbooks/{BOOK}")
print(f"  {len(ir['dashboards'])} dashboards, {len(ir.get('calcs', {}))} calcs")

config.DATASOURCES["Sample - Superstore"]["table"] = "WBR_DB.PUBLIC.ORDERS_SAMPLE_SUPERSTORE"
config.DATASOURCES["Sales Commission"]["table"] = "WBR_DB.PUBLIC.SALES_COMMISSION"
print("Datasources repointed at WBR_DB.PUBLIC (live account).")

print("Opening a real Snowpark session (connection 'wbr') ...")
session = pipeline.snow_session("wbr")
backend.set_session(session)
print("  connected.")

print("Running the skill-driven dashboard validation -- one live query + one "
      "RICH skill-driven Cortex call per section ...")
sections, rollup = parity.build_cortex_dashboard_validation_report(ir, session, BOOK)

total_tokens = rollup.get("tokens", 0)
for sec in sections:
    if sec.get("skipped"):
        print(f"  [skip] {sec['title']}: {sec['skipped']}")
    else:
        total_tokens += sec.get("cortex_tokens", 0)
        ok = sec.get("cortex_report") is not None
        print(f"  [{'ok' if ok else 'FAIL'}] {sec['title']}: "
              f"~{sec.get('cortex_tokens', 0)} tokens"
              + ("" if ok else f" -- {sec.get('cortex_error')}"))

print(f"\nRollup: {'ok' if rollup.get('text') else 'FAIL — ' + str(rollup.get('error'))}")
print(f"Total estimated tokens this run: ~{total_tokens}")

nb_str = parity.dashboard_validation_report_to_notebook(sections, rollup, BOOK)
with open(f"reports/{STEM}_dashboard_validation.ipynb", "w", encoding="utf-8") as f:
    f.write(nb_str)
print(f"Wrote reports/{STEM}_dashboard_validation.ipynb")

html_str = parity.dashboard_validation_report_to_html(sections, rollup, BOOK)
with open(f"reports/{STEM}_dashboard_validation.html", "w", encoding="utf-8") as f:
    f.write(html_str)
print(f"Wrote reports/{STEM}_dashboard_validation.html")
print("\nDone.")
