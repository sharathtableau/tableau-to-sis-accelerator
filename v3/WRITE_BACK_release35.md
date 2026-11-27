# Release35 — fiscal dates, Show Missing Values, forecasts, Cortex first translator (written back 13 Nov 2026, not deployed)

Written to `tableau_accelerator_v3_cortex` on top of release34 (also not deployed yet); originals in `before_release35/`
(`RELEASE35_BEFORE_MD5.txt` = release34 md5s, checked), written files verified against `RELEASE35_AFTER_MD5.txt`.

| File | New md5 | What |
|---|---|---|
| v3_sheet_plan.py | 7e66a86ba8989eb3234c3bb7c82045ac | EN17 fiscal year / quarter, Tableau week and weekday; DENS Show Missing Values padding; FC forecast spec + estimates; estimates drawn dashed |
| v3_relationship_query.py | 5c70126383c7228a8d1167e5a4a1fc2d | EN17 fiscal start passed to date parts |
| engine.py | 4dd922f52163cf72463a12651213c452 | EN17 fiscal start; finding "forecast-approximated" |
| backend.py | f22b17204ed24ed9e789afbe5235a0d0 | local preview: EQUAL_NULL macro |
| v3_exact_loop.py | 0af0efa521e4016f3682f25b63ec3d37 | FC: app estimates compared apart; mean difference from Tableau reported; never Verified |
| v3_console.py | f3c3c0835266b13b4080860479f70f1b | CF05 block in Inventory > Calculations (run, referee, approve) |
| v3_forecast.py (new) | 89079a5945ffebb8652b7c695e51082b | FC: Holt-Winters in numpy driven by the forecast specification |
| v3_first_translator.py (new) | 4c5a3b18650ee1ca8193ea9ee2a53f45 | CF05: Cortex first translator, engine / Tableau referee, approved apply |
| tests/test_v3_en17_fiscal.py (new) | c3acbd74a8667d38643795cb9c3fe40d | |
| tests/test_v3_dens_show_missing.py (new) | e5a6ba3f5b55dde671dd4745b4610f7b | |
| tests/test_v3_forecast.py (new) | e59e9ec838d1e4165db1380343e70417 | |
| tests/test_v3_cf05_first_translator.py (new) | cbcc2d00f48ba04dba2023b84300d2b2 | |
| tests/test_v3_exact.py | fde09ba76a29908371e236bfb47fef99 | Forecast row names the approximation |
| snowflake.yml | 09faf61b6abec48862119d07f0b4ec37 | v3_forecast.py and v3_first_translator.py added to the 4 artifact lists (first deploy attempt stopped at the bundle-completeness check); before = 4621e7f10c3c02c8c0a4af75ec8a6b02 |
| tests/test_regression.py | 9d7119874c0a117851d01f7158218969 | 3 exact-SQL assertions updated to release34's catalogue (INT truncates, FIND -> POSITION, LEN -> LENGTH); each test's purpose kept. Second deploy attempt stopped here |

Contents and evidence: master plan section 6.0.12 and V3_FIX_JOURNAL.md (13 Nov, release35).
Deploy (after release34, or instead of it — release35 contains release34):
`release\release_v3.cmd reports\cortex_lab_20261005\release35 reports\cortex_lab_20261005\release33\deployment\candidate\candidate_build.json`
After deploy: run CF05 live on a workbook with captured Tableau values (Inventory > Calculations > "Translate with Cortex and
referee"); confirm the GOLD CRM Sankey against a capture.
