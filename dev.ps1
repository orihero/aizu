<#
.SYNOPSIS
    dev.ps1 - start every app for local development with one command (Windows).

.DESCRIPTION
    Windows counterpart to dev.sh. Launches, and tears both down on Ctrl+C:
      1. bridge - the engine's JSON API (aizu) on 127.0.0.1:8765, via
                  engine/scripts/dev_panel.py (auto-restarts on engine/aizu/*.py edits)
      2. panel  - the admin-panel Vite dev server on http://localhost:5173,
                  which proxies /api -> the bridge. http://localhost:5173/ serves
                  the marketing landing page (public/index.html); the React SPA
                  lives at http://localhost:5173/app/.

    The engine *run* itself (the live crawl) is NOT started here - it is launched
    from the panel's Run button or `aizu run`. This script only brings up the two
    long-lived dev servers you need to use the panel.

    Both servers share this console, so their logs interleave unprefixed (unlike
    dev.sh, which pipes each through sed). Ctrl+C stops both.

    Keep this file ASCII-only: Windows PowerShell 5.1 parses .ps1 files as the
    system ANSI codepage, and a UTF-8 dash or ellipsis decodes into a curly quote
    that PowerShell treats as a string delimiter.

.EXAMPLE
    .\dev.ps1
.EXAMPLE
    .\dev.ps1 -BridgePort 8770
.EXAMPLE
    .\dev.ps1 -Database other.db
#>
[CmdletBinding()]
param(
    [int]    $BridgePort = $(if ($env:BRIDGE_PORT) { [int]$env:BRIDGE_PORT } else { 8765 }),
    [string] $BridgeHost = $(if ($env:BRIDGE_HOST) { $env:BRIDGE_HOST } else { '127.0.0.1' }),
    [int]    $PanelPort  = $(if ($env:PANEL_PORT)  { [int]$env:PANEL_PORT }  else { 5173 }),
    # Not named -Db: that collides with the `db` alias of the -Debug common
    # parameter that [CmdletBinding()] adds, and PowerShell refuses to bind.
    [string] $Database   = $(if ($env:DB)          { $env:DB }               else { 'aizu.db' })
)

$ErrorActionPreference = 'Stop'

# --- Configuration -----------------------------------------------------------
$RepoRoot   = $PSScriptRoot
$EngineDir  = Join-Path $RepoRoot 'engine'
$PanelDir   = Join-Path $RepoRoot 'admin-panel'
$VenvPython = Join-Path $EngineDir '.venv\Scripts\python.exe'
$ViteJs     = Join-Path $PanelDir 'node_modules\vite\bin\vite.js'

# --- Output helpers ----------------------------------------------------------
function Write-Info { param([string]$Message) Write-Host "[dev] $Message" -ForegroundColor Green }
function Stop-Dev   { param([string]$Message) Write-Host "[dev] error: $Message" -ForegroundColor Red; exit 1 }

# --- Preflight checks --------------------------------------------------------
if (-not (Test-Path $VenvPython)) {
    Stop-Dev "engine venv not found at $VenvPython - create it: python -m venv engine\.venv; engine\.venv\Scripts\pip install -e engine"
}
if (-not (Test-Path (Join-Path $PanelDir 'node_modules'))) {
    Stop-Dev "panel deps not installed - run: cd admin-panel; npm install"
}
if (-not (Test-Path $ViteJs)) {
    Stop-Dev "vite not found at $ViteJs - run: cd admin-panel; npm install"
}
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Stop-Dev "node not found on PATH - install Node.js"
}

# --- Process helpers ---------------------------------------------------------
# taskkill /T kills a whole process tree - a single-level Stop-Process is not
# enough here, since dev_panel.py respawns the bridge on edits and vite spawns
# esbuild. Routed through cmd so taskkill's success/failure chatter is discarded
# without tangling with PowerShell's native-command error stream.
function Invoke-TaskKill {
    param($TargetPid)
    try {
        Start-Process -FilePath $env:ComSpec `
                      -ArgumentList '/c', "taskkill /PID $TargetPid /T /F >nul 2>&1" `
                      -NoNewWindow -Wait -ErrorAction SilentlyContinue | Out-Null
    } catch { }
}

# --- Free any port still held by a previous run ------------------------------
# The #1 cause of "Address already in use" here is a stale bridge/panel from an
# earlier run that didn't tear down cleanly. Kill whatever is listening before
# we try to bind it ourselves.
function Clear-Port {
    param([int]$Port, [string]$Label)

    $held = @()
    try {
        $held = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
                  Select-Object -ExpandProperty OwningProcess -Unique)
    } catch {
        # Get-NetTCPConnection is absent on some SKUs - fall back to netstat.
        $held = @(netstat -ano -p tcp |
                  Select-String ":$Port\s+\S+\s+LISTENING\s+(\d+)\s*$" |
                  ForEach-Object { $_.Matches[0].Groups[1].Value } |
                  Select-Object -Unique)
    }
    # Never kill ourselves (PID 0 is the system idle process).
    $held = @($held | Where-Object { $_ -and [int]$_ -ne 0 -and [int]$_ -ne $PID })
    if (-not $held) { return }

    Write-Info "port $Port ($Label) held by stale process(es) [$($held -join ' ')] - killing"
    foreach ($stale in $held) { Invoke-TaskKill -TargetPid $stale }
    Start-Sleep -Seconds 1
}

Clear-Port -Port $BridgePort -Label bridge
Clear-Port -Port $PanelPort  -Label panel

# --- Process management ------------------------------------------------------
$script:Bridge = $null
$script:Panel  = $null

function Stop-Tree {
    param($Proc)
    if (-not $Proc) { return }
    try { if ($Proc.HasExited) { return } } catch { return }
    Invoke-TaskKill -TargetPid $Proc.Id
}

function Invoke-Cleanup {
    Write-Host ''
    Write-Info 'shutting down...'
    Stop-Tree $script:Panel
    Stop-Tree $script:Bridge
    try { [Console]::TreatControlCAsInput = $false } catch { }
    Write-Info 'stopped.'
}

try {
    # Take Ctrl+C as ordinary input so the console does not signal the children
    # behind our back - we stop them explicitly, in order, below. Fails when
    # there is no interactive console (CI, redirected stdin); that is fine, the
    # wait loop below degrades to a plain poll.
    $InteractiveConsole = $true
    try { [Console]::TreatControlCAsInput = $true } catch { $InteractiveConsole = $false }

    # --- Launch --------------------------------------------------------------
    Write-Info "starting bridge (engine API) on ${BridgeHost}:${BridgePort}  db=$Database"
    $script:Bridge = Start-Process -FilePath $VenvPython `
        -ArgumentList 'scripts\dev_panel.py', '--db', $Database, '--host', $BridgeHost, '--port', $BridgePort `
        -WorkingDirectory $EngineDir -NoNewWindow -PassThru

    Write-Info "starting panel (Vite dev server) - proxies /api -> :$BridgePort"
    $script:Panel = Start-Process -FilePath 'node' `
        -ArgumentList $ViteJs, '--port', $PanelPort `
        -WorkingDirectory $PanelDir -NoNewWindow -PassThru

    Write-Info "both servers up - panel: http://localhost:$PanelPort/ (landing) and http://localhost:$PanelPort/app/ (SPA). Ctrl+C to stop everything."

    # Block while both children live; the moment either dies, fall through so
    # the finally block stops the survivor.
    while (-not $script:Bridge.HasExited -and -not $script:Panel.HasExited) {
        if ($InteractiveConsole -and [Console]::KeyAvailable) {
            $key = [Console]::ReadKey($true)
            if (($key.Modifiers -band [ConsoleModifiers]::Control) -and $key.Key -eq 'C') { break }
        }
        Start-Sleep -Milliseconds 200
    }
} finally {
    Invoke-Cleanup
}
