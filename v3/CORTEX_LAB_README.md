# Cortex lab (test copy of V3) — created 5 Oct 2026

Purpose: build and prove the Cortex-forward features without touching the stable folder
(`..\tableau_accelerator_v3`, = live release 22) or its app.

- Deploys to its own app: `WBR_DB.TABLEAU_ACCELERATOR_V3.TABLEAU_TO_SIS_V3_CORTEX`.
- Its Semantic Views are named `..._SVLAB_...` (and `..._CORTEX` for the Cortex-designed view).
- When naming apps on the Deploy page, add a `_LAB` suffix so stable apps are never replaced.
- First deploy: `release\release_v3.cmd reports\cortex_lab_20261005\release1 NEW`
  Later deploys: pass the previous lab release's `deployment\candidate\candidate_build.json`.
- Tracker and journal stay in the stable folder (V3_ENTERPRISE_MASTER_PLAN.md, V3_FIX_JOURNAL.md).

Lab-only features: v3_prove.py (cause proof), v3_cortex_model.py (Cortex-designed semantic model),
v3_cortex_calc.py (Cortex second translator), AISQL calculation inventory and inline Cortex panels in
v3_console.py, confidence cap in v3_ai.py, formula capture in v3_local.py.
