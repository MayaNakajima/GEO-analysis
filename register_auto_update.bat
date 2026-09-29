@echo off
rem ============================================================
rem  GEO-analysis : register the auto-update task (double-click me)
rem  Every day from 14:30, hourly: regenerate analysis.html and copy
rem  it to Box only when monitoring produced new data.
rem  Remove the task:   register_auto_update.bat -Unregister
rem  (ASCII only on purpose)
rem ============================================================
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\register_auto_update.ps1" %*
echo.
pause
