@echo off
rem Internal launcher used by adapter.ps1 (WMI-created, detached from the terminal).
rem Keep this file free of interactive prompts: its console is not visible.

setlocal
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
for %%I in ("%~dp0..") do set "PROJECT_DIR=%%~fI"
cd /d "%PROJECT_DIR%"

rem adapter.ps1 passes the interpreter as the first argument; fall back to PATH.
set "PYTHON_EXE=%~1"
if not defined PYTHON_EXE set "PYTHON_EXE=python"

"%PYTHON_EXE%" -u protocol_adapter.py 1>> "%PROJECT_DIR%\logs\adapter-live.log" 2>&1
