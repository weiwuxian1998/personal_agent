@echo off
rem Double-click to start the adapter. The window can be closed after "Started" appears.
rem The adapter hosts the Web UI (port 8765) + REST/WS for the console frontend.
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0adapter.ps1" -Action start
echo.
pause
