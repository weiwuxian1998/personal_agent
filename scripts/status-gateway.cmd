@echo off
rem Double-click to show gateway status, feishu connection, redis lag/pending and log tail.
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0gateway.ps1" -Action status
echo.
pause
