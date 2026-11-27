# Standard new-session prompt (updated 3 October 2026)

Copy everything inside the block into a new session. Previous version: reports/doc_consolidation_20261003/before/NEW_SESSION_PROMPT_TABLEAU_ACCELERATOR.md.

```
Continue the company production AI-assisted Tableau-to-Streamlit-in-Snowflake accelerator.

V3 folder (work here only): C:\Users\SharathKumarKammari\Downloads\Tableau to SiS_Cowork - Cortex\tableau_accelerator_v3
Original accelerator (functional and UI baseline, read-only): C:\Users\SharathKumarKammari\Downloads\Tableau to SiS_Cowork - Cortex
V2 (preserve, do not edit): ...\Tableau to SiS_Cowork - Cortex\tableau_sis_accelerator_v2
Demo folder (only when the focus is a demo): C:\Users\SharathKumarKammari\OneDrive - Blend 360\Documents\ChatGPT\Tableau to Sis(New)\demo_monday_20261005\output

GOAL
An enterprise product that reliably migrates varied, previously unfamiliar Tableau workbooks into Streamlit in Snowflake. The original accelerator's UI and end-to-end functionality are the baseline; V3 supplies the corrected engine (structural identity, verified binding, typed semantics, clear refusals, governed execution, independent evidence). One successful workbook, a good preview or a higher chart count does not mean completion.

START
1. Follow AGENTS.md. Read V3_ENTERPRISE_MASTER_PLAN.md first, especially section 0 "Next session starts here".
2. Verify the current state: rebuild the package (v3_package.workbench_bundle) and compare the build SHA256 with the live build in section 0; check git status and destination hashes before edits.
3. Work on the item named in section 0, or "Today's focus" below. One item per session unless I say otherwise.
4. Read only the reference documents, contracts, original/V3 source and evidence that the item links to. Other V3_*.md status files are frozen history.

RULES
- Preserve the original, V2, concurrent changes and existing deployments. No new product version.
- Check the original implementation before building a replacement; reuse working generic behaviour behind V3 validation. No blanket legacy fallback.
- No workbook/sheet/file-name or known-value dispatch. Reproduce the defect, apply the smallest generic fix, add renamed and independently shaped fixtures.
- AI proposes bounded, structured plans; deterministic validation, verified bindings and independent comparison decide. Never execute model-generated SQL/Python, invent bindings or reference data, or let AI change a verdict.
- Converter edits: run tests/test_regression.py before and after, all V3 tests, and relevant corpus/render checks. Keep evidence categories separate (numeric, local render, live Cortex, Snowflake execution, Tableau parity, security, AI quality/cost, whole workbook).
- An item is DONE only when its "Done when" evidence exists; otherwise PARTIAL with what remains.
- Python: C:\Users\SharathKumarKammari\python.exe. If a step must run on my Windows machine (git commit, snow deploy, Snowflake sign-in), give me the complete command (cd /d to the folder && command) and continue with everything else.

DEPLOYMENT
Deployment authorized this session: <NO | YES — existing V3 target WBR_DB.TABLEAU_ACCELERATOR_V3.TABLEAU_TO_SIS_V3_CANDIDATE after checks pass>

Today's focus: <blank = continue section 0, or an item ID such as F06 or UI01>

END OF SESSION
Update V3_ENTERPRISE_MASTER_PLAN.md (item rows, section 0, change log), append one V3_FIX_JOURNAL.md entry, and store evidence in a dated reports/ folder. Then summarise: implemented, verified, blocked/unverified, next item, records updated.
```
