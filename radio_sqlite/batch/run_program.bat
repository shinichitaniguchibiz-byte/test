@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0.."

if "%~1"=="" (
    echo Usage: batch\run_program.bat PROGRAM_ID_OR_ABBREVIATION
    echo Example: batch\run_program.bat ESE
    exit /b 64
)

where py >nul 2>nul
if not errorlevel 1 (
    py -3 "app\radio_batch.py" run --program "%~1" --retry-errors
) else (
    python "app\radio_batch.py" run --program "%~1" --retry-errors
)
set "RC=%ERRORLEVEL%"
exit /b %RC%
