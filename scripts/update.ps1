# update.ps1 — Update the kiro compression proxy to the latest version.
#
# Upgrades headroom-ai, re-downloads source from GitHub, then restarts
# the proxy task so new compression logic takes effect immediately.

#Requires -Version 5.1
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$PROXY_DIR  = Join-Path $env:USERPROFILE '.kiro-proxy'
$VENV_DIR   = Join-Path $PROXY_DIR '.venv'
$SRC_DIR    = Join-Path $PROXY_DIR 'src'
$TASK_NAME  = 'kiro-proxy'
$HEALTH_URL = "http://127.0.0.1:9090/health"
$GITHUB_RAW = 'https://raw.githubusercontent.com/DrewGitsIt/headroom-kiro-proxy/main'

$SOURCE_FILES = @(
    'src/connect_proxy.py',
    'src/proxy.py',
    'src/interceptor.py',
    'src/stats.py',
    'src/reporter.py',
    'src/handler.py',
    'src/applet.py'
)

$MGMT_SCRIPTS = @('update.ps1', 'uninstall.ps1', 'kiro-proxy.ps1')

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
function Write-Ok   { param([string]$msg) Write-Host "  [ok] $msg" -ForegroundColor Green }
function Write-Warn { param([string]$msg) Write-Host "  [!]  $msg" -ForegroundColor Yellow }
function Write-Fail {
    param([string]$msg)
    Write-Host ""
    Write-Host "  [FAIL] $msg" -ForegroundColor Red
    Write-Host ""
    exit 1
}

# ---------------------------------------------------------------------------
# Helper: download, bypassing any proxy
# ---------------------------------------------------------------------------
function Download-File {
    param([string]$Url, [string]$Dest)
    try {
        Invoke-WebRequest -Uri $Url -OutFile $Dest -UseBasicParsing -NoProxy
    } catch {
        Write-Fail "Failed to download: $Url`n  Error: $_"
    }
}

# ---------------------------------------------------------------------------
# Helper: wait for proxy health endpoint
# ---------------------------------------------------------------------------
function Wait-ProxyUp {
    param([int]$MaxSeconds = 12)
    $deadline = (Get-Date).AddSeconds($MaxSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $resp = Invoke-WebRequest -Uri $HEALTH_URL -UseBasicParsing -TimeoutSec 2 -ErrorAction SilentlyContinue
            if ($resp.StatusCode -eq 200) { return $true }
        } catch { }
        Start-Sleep -Seconds 1
    }
    return $false
}

# ---------------------------------------------------------------------------
# Pre-flight: require existing install
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "kiro-proxy updater (Windows)" -ForegroundColor Cyan
Write-Host ""

if (-not (Test-Path $PROXY_DIR)) {
    Write-Fail "$PROXY_DIR not found. Run the installer first:`n  irm https://raw.githubusercontent.com/DrewGitsIt/headroom-kiro-proxy/main/scripts/install.ps1 | iex"
}

$venv_pip    = Join-Path $VENV_DIR 'Scripts\pip.exe'
$venv_python = Join-Path $VENV_DIR 'Scripts\python.exe'

if (-not (Test-Path $venv_pip)) {
    Write-Fail "Venv not found at $VENV_DIR. Run the installer first."
}

# ---------------------------------------------------------------------------
# 1. Upgrade headroom-ai
# ---------------------------------------------------------------------------
Write-Host "  Upgrading headroom-ai..." -ForegroundColor DarkGray
& $venv_pip install --quiet --upgrade "headroom-ai"
if ($LASTEXITCODE -ne 0) { Write-Fail "headroom-ai upgrade failed. Check your internet connection." }
Write-Ok "Upgraded headroom-ai"

# ---------------------------------------------------------------------------
# 2. Re-download source files
# ---------------------------------------------------------------------------
foreach ($rel_path in $SOURCE_FILES) {
    $url  = "$GITHUB_RAW/$rel_path"
    $dest = Join-Path $PROXY_DIR ($rel_path -replace '/', '\')
    $parent = Split-Path $dest -Parent
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    Download-File -Url $url -Dest $dest
    Write-Ok "Updated $rel_path"
}

foreach ($script in $MGMT_SCRIPTS) {
    $url  = "$GITHUB_RAW/scripts/$script"
    $dest = Join-Path $PROXY_DIR $script
    Download-File -Url $url -Dest $dest
    Write-Ok "Updated scripts/$script"
}

# ---------------------------------------------------------------------------
# 3. Restart the scheduled task
# ---------------------------------------------------------------------------
$task = Get-ScheduledTask -TaskName $TASK_NAME -ErrorAction SilentlyContinue
if ($null -eq $task) {
    Write-Warn "Scheduled task '$TASK_NAME' not found. The proxy may not auto-start."
    Write-Warn "Re-run the installer to register it."
} else {
    Stop-ScheduledTask  -TaskName $TASK_NAME -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    Start-ScheduledTask -TaskName $TASK_NAME
    Write-Ok "Restarted proxy task"

    if (Wait-ProxyUp -MaxSeconds 12) {
        Write-Ok "Proxy is running"
    } else {
        Write-Warn "Proxy did not respond within 12 seconds."
        Write-Warn "Check logs: kiro-proxy logs"
    }
}

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Update complete." -ForegroundColor Green
Write-Host "  Compression active with latest logic."
