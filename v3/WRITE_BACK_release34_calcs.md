# Release34 — calculations area (C14), written back 13 Nov 2026 (not deployed)

Written to `tableau_accelerator_v3_cortex`; device "before" md5s matched release33 first; originals in `before_release34/`
(`RELEASE34_BEFORE_MD5.txt`), written files verified against `RELEASE34_AFTER_MD5.txt`.

| File | New md5 | Before (release33) |
|---|---|---|
| calc_translator.py | 14660126d3ab9e0086ec6e56816ea01f | ec5632186eb5f65ea5761843bf9382cf |
| v3_sheet_plan.py | 19788912db1a70f9e1a183ebdc7c84d9 | 3a038ff846d476c3cbad80782306cf71 |
| backend.py | 8ef215d7c7cccd97e7f6efaee284c762 | b6af6e9de98552d53620e5620413b9b9 |
| field_identity.py | 2b6c6fc38af299888e5ebe61ef5507d4 | 76a0dda45a43b595d1bed8e4a933e857 |
| engine.py | 23c3a107b2758d15af8def42078c391a | de7021a9bc21189a3b153b672c08bd85 |
| tests/test_v3_c14_functions.py (new) | 53db5fb105d82e5e7b27d5389171f58d | — |
| tests/test_v3_c15_table_calcs.py (new) | 1566bf6ca0c5a8869a55cff5ae314373 | — |
| tests/test_v3_c6_table_functions.py | 8f12ffd97245c04abbf0c286a766878d | f924fa6909cca8568f1c483dd7f2d70a (SIZE() now translates) |
| tests/test_v3_c11_lod_grain.py | a4c923762d1b12dbdb163e2fbb172a51 | dcea7b8cfe21ba9ab80e1a8b1d22792f (general INCLUDE / EXCLUDE shapes) |

Contents and evidence: master plan section 6.0.11 and V3_FIX_JOURNAL.md (13 Nov, release34).
Deploy: `release\release_v3.cmd reports\cortex_lab_20261005\release34 reports\cortex_lab_20261005\release33\deployment\candidate\candidate_build.json`
Confirm with a capture: Hospitality rank sheets (RANK now follows each pill's Compute Using).
Still open in calculations: CF05 Cortex first translator, EN17 fiscal-year date parts, forecasts, densification.
