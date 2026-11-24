@echo off
rem Internal launcher used by gateway.ps1 (WMI-created, detached from the terminal).
rem Keep this file free of interactive prompts: its console is not visible.
rem Force UTF-8 so the log keeps a single encoding even when started from cmd.

setlocal
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
for %%I in ("%~dp0..") do set "PROJECT_DIR=%%~fI"
cd /d "%PROJECT_DIR%"

rem gateway.ps1 passes the interpreter as the first argument; fall back to PATH
rem only when this wrapper is launched manually.
set "PYTHON_EXE=%~1"
if not defined PYTHON_EXE set "PYTHON_EXE=python"

"%PYTHON_EXE%" -u main_gateway.py 1>> "%PROJECT_DIR%\logs\gateway-live.log" 2>&1
