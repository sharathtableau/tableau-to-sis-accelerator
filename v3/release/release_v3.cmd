@echo off
rem Checks, preflight, deploy and verify for the existing V3 target.
rem Usage:  release\release_v3.cmd reports\<evidence_folder> [previous candidate_build.json] [full]
setlocal
set PY=C:\Users\SharathKumarKammari\python.exe
set EVID=%~1
set PREV=%~2
if "%PREV%"=="" set PREV=reports\ui01_console_20261003\release\deployment\candidate\candidate_build.json
if "%EVID%"=="" (echo Usage: release\release_v3.cmd reports\evidence_folder & exit /b 2)
cd /d "%~dp0.."
if not exist "%EVID%" mkdir "%EVID%"
set MODE=%~3
if /i "%MODE%"=="full" goto gates
echo [1/5] Gates: skipped when this exact code already passed them (add "full" as a third argument to force)...
"%PY%" release\gate_fingerprint.py check "%EVID%" && goto preflight
:gates
echo [1-2/5] Gates in parallel: regression, corpus, XML coverage, V3 tests...
"%PY%" release\run_gates.py "%EVID%" || (echo GATES FAILED - see %EVID%\gates_summary.txt & exit /b 1)
:preflight
echo [3/5] Preflight and rollback backup...
"%PY%" release\preflight.py "%EVID%" "%PREV%" || (echo PREFLIGHT FAILED & exit /b 1)
echo [4/5] Deploy...
"%PY%" release\deploy.py "%EVID%" || (echo DEPLOY FAILED - see %EVID%\deployment\deploy.log & exit /b 1)
echo [5/5] Verify live hashes, configuration and grants...
"%PY%" release\verify.py "%EVID%" || (echo VERIFY FAILED & exit /b 1)
echo [6/6] Smoke test: every page of the deployed files opens without error (EN10)...
"%PY%" release\smoke.py "%EVID%" || (echo SMOKE FAILED - see %EVID%\deployment\smoke.json & exit /b 1)
echo RELEASE VERIFIED
