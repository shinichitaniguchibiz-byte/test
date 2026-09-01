@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %ERRORLEVEL%==0 (
    py -3 "%~dp0nhk_download.py"
) else (
    python.exe "%~dp0nhk_download.py"
)

set "RESULT=%ERRORLEVEL%"
echo.
echo EXIT_CODE=%RESULT%
echo.
pause
exit /b %RESULT%
