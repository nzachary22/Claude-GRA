@echo off
rem Grant Reporting Assistant V.1.0 - starts the local app and opens the browser.
rem First run creates a private Python environment outside this folder.
setlocal
set "HERE=%~dp0"
set "VENV=%LOCALAPPDATA%\GrantReportingAssistant\venv"

if not exist "%VENV%\Scripts\python.exe" (
  echo Setting up for first use. This takes a minute...
  python -m venv "%VENV%" || (echo Python 3.11 or newer is required: https://www.python.org/downloads/ & pause & exit /b 1)
  "%VENV%\Scripts\python.exe" -m pip install --quiet --upgrade pip
  "%VENV%\Scripts\python.exe" -m pip install --quiet -e "%HERE%." || (echo Setup failed. & pause & exit /b 1)
)

echo Grant Reporting Assistant is running at http://127.0.0.1:8765/
echo Close this window to stop it.
"%VENV%\Scripts\python.exe" -m gra.server
