@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0.."

where py >nul 2>nul
if not errorlevel 1 (
    py -3 "app\radio_batch.py" self-test
) else (
    python "app\radio_batch.py" self-test
)
set "RC=%ERRORLEVEL%"
exit /b %RC%
