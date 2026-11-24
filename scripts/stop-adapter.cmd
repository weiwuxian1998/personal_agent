@echo off
rem Double-click to stop the adapter (port 8765).
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0adapter.ps1" -Action stop
echo.
pause
