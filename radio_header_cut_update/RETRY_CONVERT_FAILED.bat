@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %ERRORLEVEL%==0 (
    py -3 "%~dp0header_cut_processor.py" --mode retry-failed
) else (
    python "%~dp0header_cut_processor.py" --mode retry-failed
)
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%
