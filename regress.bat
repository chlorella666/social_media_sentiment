@echo off
rem One-click regression: unit tests + golden lexicon gate.
rem NOTE: keep this file pure ASCII. cmd.exe parses .bat with the OEM
rem codepage (GBK on zh-CN), so non-ASCII text here becomes mojibake.
rem Chinese user-facing output is handled by Python (run_regression.py).
cd /d "%~dp0"
python tests\run_regression.py %*
set RC=%ERRORLEVEL%
echo.
if %RC%==0 (
    echo Regression PASSED - all green.
) else (
    echo Regression FAILED - see report above.
)
pause
exit /b %RC%
