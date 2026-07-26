@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0.."

if "%~1"=="" (
    echo Usage: batch\migrate_existing_sqlite.bat FULL_PATH_TO_OLD_DB
    echo Example: batch\migrate_existing_sqlite.bat "C:\old_radio\database\radio_catalog.db"
    exit /b 64
)

where py >nul 2>nul
if not errorlevel 1 (
    py -3 "app\radio_batch.py" migrate-sqlite --source "%~1"
) else (
    python "app\radio_batch.py" migrate-sqlite --source "%~1"
)
set "RC=%ERRORLEVEL%"
exit /b %RC%
