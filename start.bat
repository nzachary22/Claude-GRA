@echo off
rem Grant Reporting Assistant V.1.2 - starts the local app and opens the browser.
rem Setup runs on first use, and again whenever this folder moves or a new version
rem changes the requirements. The Python environment lives outside this folder.
rem Setup also puts a "Grant Reporting Assistant" shortcut on the desktop and in
rem the Start menu, so later the app starts from there.
setlocal
title Grant Reporting Assistant
cd /d "%~dp0"
set "GRA_DIR=%CD%"
if /i not "%GRA_DIR:\AppData\Local\Temp\=%"=="%GRA_DIR%" (
  echo This is running from inside the zip file.
  echo First unzip it: right-click the zip, choose "Extract All...", pick a folder
  echo such as Documents, then double-click start.bat in the extracted folder.
  pause
  exit /b 1
)
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
call :shortcuts

:run
echo Grant Reporting Assistant is running at http://127.0.0.1:8765/
echo Close this window to stop it.
"%VENV%\Scripts\python.exe" -m gra.server
if errorlevel 1 pause
exit /b

:shortcuts
rem Desktop and Start menu shortcuts pointing at this folder. Rewritten on every
rem setup, so they follow the folder if it moves. A failure here is not fatal.
powershell -NoProfile -NonInteractive -Command "$ErrorActionPreference='Stop'; $d=$env:GRA_DIR; $w=New-Object -ComObject WScript.Shell; foreach($f in [Environment]::GetFolderPath('Desktop'),[Environment]::GetFolderPath('Programs')){ $s=$w.CreateShortcut((Join-Path $f 'Grant Reporting Assistant.lnk')); $s.TargetPath=(Join-Path $d 'start.bat'); $s.WorkingDirectory=$d; $s.IconLocation=(Join-Path $d 'gra\static\icon.ico')+',0'; $s.Description='Start the Grant Reporting Assistant'; $s.Save() }" >nul 2>&1
if errorlevel 1 (
  echo Could not add the desktop shortcut. Start the app with start.bat in this folder instead.
) else (
  echo Added a "Grant Reporting Assistant" shortcut to the desktop and Start menu.
)
exit /b 0
