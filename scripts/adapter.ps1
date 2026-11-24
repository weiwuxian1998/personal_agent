#requires -Version 5.1
<#
Manual adapter lifecycle helper: start / stop / restart / status / logs.

Detachment: WMI Win32_Process.Create spawns python.exe directly (no cmd.exe /c),
so the child belongs to WmiPrvSE.exe (Windows service), not our terminal job.
Closing the terminal does NOT kill it.

Port: protocol_adapter.py defaults to 8765 (see --port arg).
#>

[CmdletBinding()]
param(
    [ValidateSet('start', 'stop', 'restart', 'status', 'logs')]
    [string]$Action = 'status',

    [int]$Tail = 40,

    [switch]$Follow
)

$ErrorActionPreference = 'Stop'

$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

# Python resolution order:
#   1. .env WORKER_PYTHON (absolute path, explicit config)
#   2. Common conda env probe (find one that can import fastapi; feishu env preferred)
#   3. PATH fallback (works only when PATH points to the right env)
function Resolve-PythonExe {
    param([string]$ProjectRoot)

    # --- 1. .env WORKER_PYTHON ---
    try {
        $out = python -c "import sys; sys.path.insert(0, r'$ProjectRoot'); from config import settings; print(settings.worker_python or '')" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out -and $out.Trim()) { return $out.Trim() }
    } catch {}

    # --- 2. Common conda env probe ---
    $probeDirs = @(
        "$env:USERPROFILE\miniconda3\envs\feishu\python.exe",
        "$env:USERPROFILE\anaconda3\envs\feishu\python.exe",
        "$env:USERPROFILE\miniconda3\envs\assistant\python.exe",
        "$env:USERPROFILE\anaconda3\envs\assistant\python.exe"
    )
    foreach ($candidate in $probeDirs) {
        $exists = Test-Path $candidate
        if (-not $exists) { continue }
        & $candidate -c "import fastapi" 2>$null
        if ($LASTEXITCODE -eq 0) { return $candidate }
    }

    # --- 3. PATH fallback ---
    $candidate = (Get-Command python -ErrorAction SilentlyContinue).Source
    if ($candidate) { return $candidate }

    throw "Python not found. Set WORKER_PYTHON in .env or add python to PATH."
}

$PythonExe = Resolve-PythonExe -ProjectRoot $ProjectDir
$LogFile   = Join-Path $ProjectDir 'logs\adapter-live.log'
$PidFile   = Join-Path $ProjectDir 'logs\adapter.pid'

function Get-Adapters {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and $_.CommandLine -like '*protocol_adapter.py*' -and
            $_.ExecutablePath -and $_.ExecutablePath -ieq $PythonExe
        }
}

function Show-Status {
    $procs = @(Get-Adapters)
    'Project     : {0}' -f $ProjectDir
    'Log file    : {0}' -f $LogFile

    if ($procs.Count -eq 0) {
        'Process     : NOT RUNNING'
        return
    }

    'Process     : {0} instance(s)' -f $procs.Count
    foreach ($p in $procs) {
        '  pid={0} start={1}' -f $p.ProcessId, $p.CreationDate
    }

    $port = netstat -ano 2>$null | Select-String ':8765' | Select-String 'LISTENING'
    if ($port) { 'Port 8765   : LISTENING' } else { 'Port 8765   : NOT LISTENING' }

    if (Test-Path $LogFile) {
        '--- last {0} log lines ---' -f $Tail
        Get-Content -Path $LogFile -Tail $Tail -Encoding UTF8
    }
}

function Stop-Adapter {
    $procs = @(Get-Adapters)
    if ($procs.Count -eq 0) {
        'Adapter is not running.'
        Remove-Item $PidFile -ErrorAction SilentlyContinue
        return
    }
    foreach ($p in $procs) {
        'Stopping pid={0} ...' -f $p.ProcessId
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 1
    $left = @(Get-Adapters)
    Remove-Item $PidFile -ErrorAction SilentlyContinue
    if ($left.Count -eq 0) { 'Adapter stopped.' }
    else { "WARNING: still alive: $($left.ProcessId -join ',')"; exit 1 }
}

function Start-Adapter {
    $existing = @(Get-Adapters)
    if ($existing.Count -gt 0) {
        'Refusing to start: adapter already running (pid={0}). Stop it first.' -f ($existing.ProcessId -join ',')
        exit 2
    }

    New-Item -ItemType Directory -Force -Path (Split-Path $LogFile) | Out-Null

    # cmd.exe /c wrapper (gateway.ps1 同款): WMI creates cmd.exe which spawns python.exe.
    # This keeps the process alive and handles stdout/stderr redirection via adapter-run.cmd.
    $runner = Join-Path $ProjectDir 'scripts\adapter-run.cmd'
    $result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine      = ('cmd.exe /c ""{0}" "{1}""' -f $runner, $PythonExe)
        CurrentDirectory = $ProjectDir
    }
    if ($result.ReturnValue -ne 0) {
        'Failed to create process via WMI (ReturnValue={0}).' -f $result.ReturnValue
        exit 1
    }

    # Wait for process to appear
    $deadline = (Get-Date).AddSeconds(15)
    $proc = $null
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        $proc = Get-Adapters | Select-Object -First 1
        if ($proc) { break }
    }
    if (-not $proc) {
        'Failed: adapter process did not appear within 15s.'
        exit 1
    }

    @(
        ('pid={0}' -f $proc.ProcessId)
        ('start={0}' -f $proc.CreationDate)
    ) | Set-Content -Path $PidFile -Encoding ASCII

    # Wait for port to bind
    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        $port = netstat -ano 2>$null | Select-String ':8765' | Select-String 'LISTENING'
        if ($port) { break }
    }

    if ($port) {
        'Started pid={0} on http://127.0.0.1:8765/' -f $proc.ProcessId
    } else {
        'Started pid={0} but port 8765 not listening yet (uvicorn still starting?).' -f $proc.ProcessId
    }
}

switch ($Action) {
    'start'   { Start-Adapter }
    'stop'    { Stop-Adapter }
    'restart' { Stop-Adapter; Start-Adapter }
    'status'  { Show-Status }
    'logs' {
        if (-not (Test-Path $LogFile)) { 'No log file yet: {0}' -f $LogFile; exit 1 }
        if ($Follow) { Get-Content -Path $LogFile -Tail $Tail -Wait -Encoding UTF8 }
        else { Get-Content -Path $LogFile -Tail $Tail -Encoding UTF8 }
    }
}
