@echo off
rem Internal launcher used by gateway.ps1 (WMI-created, detached from the terminal).
rem Keep this file free of interactive prompts: its console is not visible.
rem Force UTF-8 so the log keeps a single encoding even when started from cmd.
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "D:\personal_agent"
"C:\Users\34897\miniconda3\envs\feishu\python.exe" -u main_gateway.py 1>> "D:\personal_agent\logs\gateway-live.log" 2>&1
