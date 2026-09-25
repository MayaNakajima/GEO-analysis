@echo off
rem ============================================================
rem  GEO-analysis : regenerate analysis.html with the latest CSVs,
rem  copy it to share_dirs (Box) and open it in the browser.
rem  Just double-click this file.
rem ============================================================
setlocal
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"

set "PY="
if exist "C:\work\anaconda_install\python.exe" set "PY=C:\work\anaconda_install\python.exe"
if not defined PY if exist "%USERPROFILE%\anaconda3\python.exe" set "PY=%USERPROFILE%\anaconda3\python.exe"
if not defined PY (where py >nul 2>nul && set "PY=py")
if not defined PY set "PY=python"

echo Python: %PY%
"%PY%" generate.py --open %*
if errorlevel 1 (
  echo.
  echo [ERROR] Failed to generate analysis.html. See the message above.
  pause
  exit /b 1
)

echo.
echo Done. This window will close in 5 seconds.
timeout /t 5 >nul
endlocal
