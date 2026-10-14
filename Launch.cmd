@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto venv
where py >nul 2>&1
if errorlevel 1 goto missing_python
rem Version probes must never trigger either launcher's install-on-demand feature.
set "PYTHON_MANAGER_AUTOMATIC_INSTALL=false"
set "PYLAUNCHER_ALLOW_INSTALL="
set "PYLAUNCHER_ALWAYS_INSTALL="
py -3.12 --version >nul 2>&1
if not errorlevel 1 goto python312
py -3.13 --version >nul 2>&1
if not errorlevel 1 goto python313
goto missing_python

:venv
".venv\Scripts\python.exe" scripts\launch.py %*
goto finished

:python312
py -3.12 scripts\launch.py %*
goto finished

:python313
py -3.13 scripts\launch.py %*
goto finished

:missing_python
echo Install Python 3.12 or 3.13 with the py launcher, or create .venv\Scripts\python.exe. See docs\LAUNCHERS.md. >&2
set "VOICE_EXIT_CODE=1"
goto pause_if_interactive

:finished
set "VOICE_EXIT_CODE=%errorlevel%"
:pause_if_interactive
if "%~1"=="" pause
exit /b %VOICE_EXIT_CODE%
