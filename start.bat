@echo off
rem Grant Reporting Assistant V.1.0 - starts the local app and opens the browser.
rem Setup runs on first use, and again whenever this folder moves or a new version
rem changes the requirements. The Python environment lives outside this folder.
setlocal
cd /d "%~dp0"
set "VENV=%LOCALAPPDATA%\GrantReportingAssistant\venv"
set "STAMP=%VENV%\gra-setup.txt"

if not exist "%VENV%\Scripts\python.exe" goto setup
if not exist "%STAMP%" goto setup
cd > "%VENV%\gra-check.txt"
type pyproject.toml >> "%VENV%\gra-check.txt"
fc /b "%STAMP%" "%VENV%\gra-check.txt" >nul 2>&1 || goto setup
goto run

:setup
python -c "import sys; raise SystemExit(sys.version_info < (3, 11))" >nul 2>&1
if errorlevel 1 (
  echo Python 3.11 or newer is required: https://www.python.org/downloads/
  echo When installing, tick "Add python.exe to PATH", then run start.bat again.
  pause
  exit /b 1
)
echo Setting up. This takes a minute...
if not exist "%VENV%\Scripts\python.exe" python -m venv "%VENV%"
if not exist "%VENV%\Scripts\python.exe" (echo Setup failed: could not create the Python environment. & pause & exit /b 1)
"%VENV%\Scripts\python.exe" -m pip install --quiet --upgrade pip
"%VENV%\Scripts\python.exe" -m pip install --quiet -e .
if errorlevel 1 (
  echo Setup failed. Check the internet connection and try again.
  pause
  exit /b 1
)
cd > "%STAMP%"
type pyproject.toml >> "%STAMP%"

:run
echo Grant Reporting Assistant is running at http://127.0.0.1:8765/
echo Close this window to stop it.
"%VENV%\Scripts\python.exe" -m gra.server
if errorlevel 1 pause
