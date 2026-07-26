@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %ERRORLEVEL%==0 (
    py -3 "%~dp0apply_update.py"
) else (
    python "%~dp0apply_update.py"
)

set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
    echo Update completed. Use RUN_ALL.bat in the radio program folder.
) else (
    echo Update failed. The original database backup is under database\backup.
)
echo.
pause
exit /b %RC%
