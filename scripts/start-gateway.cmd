@echo off
rem Double-click to start the gateway. The window can be closed after READY.
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0gateway.ps1" -Action start
echo.
pause
