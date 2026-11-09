@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Create the Python environment first. See README.ja.md.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" cart_station.py gui %*
if errorlevel 1 pause
