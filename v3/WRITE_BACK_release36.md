# Release36 — EN41 data blending (written back 13 Nov 2026, not deployed)

On top of release35 (live, 3ad2e2f0…). Originals in `before_release36/` (`RELEASE36_BEFORE_MD5.txt` = release35 md5s,
checked); written files match `RELEASE36_AFTER_MD5.txt`.

| File | New md5 | What |
|---|---|---|
| tableau_parser.py | 4adebe03ceeba1167010294d63101633 | `blend_fields`: per datasource, the display names of its dimensions (caption, else name; metadata-only text/date columns by local name) -> ir['blend_fields'] |
| v3_sheet_plan.py | 7ad158c99bcfecf8d52a1fa7e2a8a427 | `_blend`: every secondary with custom links (workbook relationship for the pair) or Tableau's automatic links (same display name, exact); `compile_blend`: the primary through the ordinary compiler (sets, LODs, windows, filters), each secondary aggregated at its active links and LEFT JOINed, a secondary with no link at all aggregated over all its rows; a set in IN/OUT mode headed IN/OUT(<set caption>) |
| v3_exact_loop.py | dd205b1aa7cb5af2237d520c486114bd | uses a field's Tableau header when the plan carries one |
| tests/test_v3_dm5_blends.py | 03aa2472797e93e1c43a54b6c0866a04 | rewritten from Tableau's capture (was: unlinked secondaries refuse) |

Gates (13 Nov): cloud V3 1001 tests (9 known fixture errors), regression identical, captures Superstore 19 + Forecast
partly, R1 13/13, R1-V2 13/13, census 54 verified unchanged; corpus FAILED 2 -> 1 (Globalsales Details now runs, 49 rows;
left: Regional download); device: touched V3 tests pass; full regression 161/166 in the VM, the 5 failing are VM-only
(same 5 fail with the release35 files: plotly missing, ECommerce fixtures).
Deploy: `release\release_v3.cmd reports\cortex_lab_20261005\release36 reports\cortex_lab_20261005\release35\deployment\candidate\candidate_build.json`
