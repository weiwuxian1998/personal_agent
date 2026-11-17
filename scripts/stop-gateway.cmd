@echo off
rem Double-click to stop the gateway (only processes running main_gateway.py).
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0gateway.ps1" -Action stop
echo.
pause
