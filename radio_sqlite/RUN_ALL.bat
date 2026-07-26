@echo off
setlocal
call "%~dp0batch\03_run_all.bat"
exit /b %ERRORLEVEL%
