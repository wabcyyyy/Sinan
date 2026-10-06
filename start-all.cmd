@echo off
setlocal
cd /d "%~dp0"

if not exist "%~dp0start-all.ps1" (
  echo [ERROR] start-all.ps1 not found next to this script.
  echo Run from the repo root, or double-click start-all.cmd.
  pause
  exit /b 1
)

echo Starting Sinan (redis :6380 / api :8000 / frontend :5173)...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-all.ps1"
set ERR=%ERRORLEVEL%
echo.
if not "%ERR%"=="0" (
  echo [ERROR] launcher exited with code %ERR%
) else (
  echo Web UI: http://localhost:5173
)
pause
exit /b %ERR%
