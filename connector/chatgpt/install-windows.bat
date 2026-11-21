@echo off
rem Installs Grant Reporting Assistant for the ChatGPT desktop app (Windows).
rem Finds Python 3.11 or newer and runs the installer next to this file.
setlocal
title Grant Reporting Assistant for ChatGPT
set "HERE=%~dp0"
set "PY="
py -3 -c "import sys; raise SystemExit(sys.version_info < (3, 11))" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys; raise SystemExit(sys.version_info < (3, 11))" >nul 2>&1 && set "PY=python"
if not defined PY (
  echo.
  echo Grant Reporting Assistant needs Python 3.11 or newer, which was not found on this computer.
  echo.
  echo The Python download page will open now. Download Python, run its installer, and on its first
  echo screen tick "Add python.exe to PATH" before you click "Install Now". Then run this file again.
  echo.
  start "" "https://www.python.org/downloads/"
  pause
  exit /b 1
)
%PY% "%HERE%files\install.py" %*
echo.
pause
