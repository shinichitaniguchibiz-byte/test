@echo off
setlocal
cd /d "%~dp0"
set "VERIFY_SCRIPT=%~dp0verify_header_cut.py"
if not exist "%VERIFY_SCRIPT%" set "VERIFY_SCRIPT=%~dp0radio_header_cut_update\verify_header_cut.py"
if not exist "%VERIFY_SCRIPT%" (
    echo RESULT=FAIL
    echo ERROR=verify_header_cut.py was not found.
    pause
    exit /b 1
)
where py >nul 2>nul
if %ERRORLEVEL%==0 (
    py -3 "%VERIFY_SCRIPT%"
) else (
    python "%VERIFY_SCRIPT%"
)
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%
