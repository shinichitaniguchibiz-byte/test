@echo off
rem HEADER_CUT_WRAPPER_V1
setlocal
cd /d "%~dp0"

call "%~dp0RUN_DOWNLOAD_ONLY.bat"
set "DOWNLOAD_RC=%ERRORLEVEL%"

where py >nul 2>nul
if %ERRORLEVEL%==0 (
    py -3 "%~dp0header_cut_processor.py" --mode pending
) else (
    python "%~dp0header_cut_processor.py" --mode pending
)
set "CONVERT_RC=%ERRORLEVEL%"

echo.
echo DOWNLOAD_RESULT=%DOWNLOAD_RC%
echo CONVERSION_RESULT=%CONVERT_RC%

if not "%CONVERT_RC%"=="0" (
    echo One or more header-cut operations failed.
    echo Fix the cause shown in recordings.status_description,
    echo then run RETRY_CONVERT_FAILED.bat.
    pause
    exit /b %CONVERT_RC%
)

if not "%DOWNLOAD_RC%"=="0" (
    echo The download process reported an error.
    echo Successfully downloaded files were still converted.
    pause
    exit /b %DOWNLOAD_RC%
)

echo All download and header-cut operations completed.
pause
exit /b 0
