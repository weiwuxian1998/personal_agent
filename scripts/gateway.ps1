#requires -Version 5.1
<#
Manual gateway lifecycle helper: start / stop / restart / status / logs.

Detachment (why it survives closing this window):
  Our terminal runs inside a Windows job object; children started with
  Start-Process / & inherit that job and die when the window closes.
  `start` asks the WMI provider (WmiPrvSE.exe, a Windows service)
  to create the process via Win32_Process.Create. The new process belongs to
  WmiPrvSE, not to our terminal/job, so closing the window does NOT kill it.

No scheduled task, no autostart, no watchdog: the gateway only runs after you
start it manually.

Instance detection:
  Feishu delivers each event to exactly ONE of the websocket connections of the
  same app. Two gateways => messages split => some are never answered.
  `start` refuses when a `python ... main_gateway.py` process already exists.
#>

[CmdletBinding()]
param(
    [ValidateSet('start', 'stop', 'restart', 'status', 'logs', 'rotate')]
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
# This avoids the trap where `python -c ...` uses PATH's base env python and
# settings.python_exe() also resolves to that same (wrong) python.
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

$PythonExe  = Resolve-PythonExe -ProjectRoot $ProjectDir
$LogFile    = Join-Path $ProjectDir 'logs\gateway-live.log'
$PidFile    = Join-Path $ProjectDir 'logs\gateway.pid'

function Get-Gateways {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and $_.CommandLine -like '*main_gateway.py*' -and
            $_.ExecutablePath -and $_.ExecutablePath -ieq $PythonExe
        }
}

function Get-ParentName([int]$ProcessId) {
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if ($parent) { return $parent.Name } else { return 'unknown' }
}

function Get-ConnectionState([int]$GatewayPid) {
    if (-not (Test-Path $LogFile)) { return @{ Ready = $false; Line = '' } }
    $lines = Select-String -Path $LogFile -Pattern ('pid={0} channel=|connected to wss' -f $GatewayPid)
    $lastStart = $null
    $lastConn = $null
    foreach ($l in $lines) {
        if ($l.Line -like ('*pid={0} channel=*' -f $GatewayPid)) { $lastStart = $l.LineNumber; $lastConn = $null }
        elseif ($l.Line -like '*connected to wss*') { $lastConn = $l }
    }
    if ($lastConn) { return @{ Ready = $true; Line = $lastConn.Line } }
    return @{ Ready = $false; Line = '' }
}

function Get-RedisState {
    $script = @'
import json
try:
    import redis, config
    from bus.envelope import OUTBOX_STREAM, ROUTER_STREAM
    r = redis.from_url(config.settings.redis_url, decode_responses=True, socket_timeout=3)
    def dump(name):
        try:
            groups = r.xinfo_groups(name)
            return {"stream": name, "xlen": r.xlen(name), "groups": groups}
        except Exception as exc:
            return {"stream": name, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps({"outbox": dump(OUTBOX_STREAM), "router": dump(ROUTER_STREAM)},
                     ensure_ascii=False, default=str))
except Exception as exc:
    print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
'@
    $raw = $script | & $PythonExe - 2>&1
    return ($raw | Out-String).Trim()
}

function Show-Status {
    $procs = @(Get-Gateways)
    'Project     : {0}' -f $ProjectDir
    'Log file    : {0}' -f $LogFile

    if ($procs.Count -eq 0) {
        'Process     : NOT RUNNING'
        'Connection  : n/a'
        return
    }

    'Process     : {0} instance(s)' -f $procs.Count
    foreach ($p in $procs) {
        '  pid={0} start={1} parent={2}({3})' -f `
            $p.ProcessId, $p.CreationDate, $p.ParentProcessId, (Get-ParentName $p.ParentProcessId)
    }

    $conn = Get-ConnectionState $procs[0].ProcessId
    if ($conn.Ready) {
        'Connection  : ESTABLISHED'
        '  {0}' -f $conn.Line
    } else {
        'Connection  : NOT established yet (or disconnected). Startup imports lark_oapi (~50s).'
    }

    'Redis       :'
    try { Get-RedisState } catch { '  (redis check failed: {0})' -f $_.Exception.Message }

    if (Test-Path $LogFile) {
        '--- last {0} log lines ---' -f $Tail
        Get-Content -Path $LogFile -Tail $Tail -Encoding UTF8
    }
}

function Stop-Gateway {
    $procs = @(Get-Gateways)
    if ($procs.Count -eq 0) {
        'Gateway is not running.'
        Remove-Item $PidFile -ErrorAction SilentlyContinue
        return
    }
    foreach ($p in $procs) {
        'Stopping pid={0} (parent {1}) ...' -f $p.ProcessId, (Get-ParentName $p.ParentProcessId)
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 1
    $left = @(Get-Gateways)
    Remove-Item $PidFile -ErrorAction SilentlyContinue
    if ($left.Count -eq 0) {
        'Gateway stopped.'
    } else {
        'WARNING: still alive: {0}' -f ($left.ProcessId -join ',')
        exit 1
    }
}

function Invoke-LogRotation {
    try {
        & $PythonExe (Join-Path $ProjectDir 'log_rotation.py')
    } catch {
        'WARNING: log rotation failed: {0}' -f $_.Exception.Message
    }
}

function Start-Gateway {
    $existing = @(Get-Gateways)
    if ($existing.Count -gt 0) {
        'Refusing to start: {0} gateway instance(s) already running (pid={1}).' -f `
            $existing.Count, ($existing.ProcessId -join ',')
        'Two gateways split Feishu websocket traffic, so some messages would never be answered.'
        'Run stop-gateway.cmd first if you really want to restart.'
        exit 2
    }

    New-Item -ItemType Directory -Force -Path (Split-Path $LogFile) | Out-Null

    Invoke-LogRotation

    $runner = Join-Path $ProjectDir 'scripts\gateway-run.cmd'
    $result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine      = ('cmd.exe /c ""{0}" "{1}""' -f $runner, $PythonExe)
        CurrentDirectory = $ProjectDir
    }
    if ($result.ReturnValue -ne 0) {
        'Failed to create process via WMI (ReturnValue={0}).' -f $result.ReturnValue
        exit 1
    }

    $deadline = (Get-Date).AddSeconds(30)
    $proc = $null
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        $proc = Get-Gateways | Select-Object -First 1
        if ($proc) { break }
    }
    if (-not $proc) {
        'Failed: gateway process did not appear within 30s. See {0}' -f $LogFile
        exit 1
    }

    $parentName = Get-ParentName $proc.ParentProcessId
    @(
        ('pid={0}' -f $proc.ProcessId)
        ('start={0}' -f $proc.CreationDate)
        ('parent={0} {1}' -f $proc.ParentProcessId, $parentName)
    ) | Set-Content -Path $PidFile -Encoding ASCII

    'Started pid={0} (parent={1}); this window can be closed now.' -f $proc.ProcessId, $parentName
    'Waiting for the Feishu websocket (first start imports lark_oapi, ~50s) ...'

    $deadline = (Get-Date).AddSeconds(150)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
        $conn = Get-ConnectionState $proc.ProcessId
        if ($conn.Ready) {
            'READY: {0}' -f $conn.Line
            return
        }
    }
    'WARNING: no websocket line within 150s. Check {0}' -f $LogFile
    Show-Status
    exit 1
}

switch ($Action) {
    'start'   { Start-Gateway }
    'stop'    { Stop-Gateway }
    'restart' { Stop-Gateway; Start-Gateway }
    'status'  { Show-Status }
    'logs' {
        if (-not (Test-Path $LogFile)) { 'No log file yet: {0}' -f $LogFile; exit 1 }
        if ($Follow) { Get-Content -Path $LogFile -Tail $Tail -Wait -Encoding UTF8 }
        else { Get-Content -Path $LogFile -Tail $Tail -Encoding UTF8 }
    }
    'rotate'  { Invoke-LogRotation }
}
