@echo off
cd /d "%~dp0"
echo Pushing to github.com/sharathtableau/tableau-to-sis-accelerator ...
echo.
git push -u origin main
echo.
if %ERRORLEVEL%==0 (
    echo SUCCESS - pushed.
    echo https://github.com/sharathtableau/tableau-to-sis-accelerator
) else (
    echo FAILED - exit code %ERRORLEVEL%
)
echo.
pause
