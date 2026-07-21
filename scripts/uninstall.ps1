# uninstall.ps1 — Remove the kiro compression proxy completely.
#
# Reverses everything install.ps1 did:
#   1. Stops and unregisters the Task Scheduler job
#   2. Removes HTTPS_PROXY, SSL_CERT_FILE, NODE_EXTRA_CA_CERTS from user env
#   3. Removes ~/.kiro-proxy from user PATH
#   4. Deletes the ~/.kiro-proxy directory (includes venv, source, certs, logs)
#
# After uninstall, kiro-cli connects directly to AWS (no compression).

#Requires -Version 5.1
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'   # Don't abort on non-critical cleanup errors

$PROXY_DIR = Join-Path $env:USERPROFILE '.kiro-proxy'
$TASK_NAME = 'kiro-proxy'

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
Write-Host ""
Write-Host "kiro-proxy uninstaller (Windows)" -ForegroundColor Cyan
Write-Host ""
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 1. Stop and unregister the scheduled task
# ---------------------------------------------------------------------------
$task = Get-ScheduledTask -TaskName $TASK_NAME -ErrorAction SilentlyContinue
if ($task) {
    # Stop the running instance first; ignore errors if already stopped.
    Stop-ScheduledTask -TaskName $TASK_NAME -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TASK_NAME -Confirm:$false -ErrorAction SilentlyContinue
    Write-Ok "Stopped and removed scheduled task '$TASK_NAME'"
} else {
    Write-Ok "Scheduled task '$TASK_NAME' not found (already removed)"
}

# ---------------------------------------------------------------------------
# 2. Remove user-scope environment variables
# ---------------------------------------------------------------------------
$env_vars_to_remove = @('HTTPS_PROXY', 'SSL_CERT_FILE', 'NODE_EXTRA_CA_CERTS')
foreach ($var_name in $env_vars_to_remove) {
    $current = [Environment]::GetEnvironmentVariable($var_name, 'User')
    if ($null -ne $current) {
        [Environment]::SetEnvironmentVariable($var_name, $null, 'User')
        # Clear from current process too
        [System.Environment]::SetEnvironmentVariable($var_name, $null, 'Process')
        Write-Ok "Removed $var_name from user environment"
    } else {
        Write-Ok "$var_name was not set (skipping)"
    }
}

# ---------------------------------------------------------------------------
# 3. Remove ~/.kiro-proxy from user PATH
# ---------------------------------------------------------------------------
$raw          = [Environment]::GetEnvironmentVariable('PATH', 'User')
$current_path = if ($raw) { $raw } else { '' }
$path_parts   = $current_path -split ';' | Where-Object { $_ -ne '' }
$filtered     = $path_parts | Where-Object { $_ -ne $PROXY_DIR }

if ($filtered.Count -lt $path_parts.Count) {
    $new_path = $filtered -join ';'
    [Environment]::SetEnvironmentVariable('PATH', $new_path, 'User')
    Write-Ok "Removed $PROXY_DIR from user PATH"
} else {
    Write-Ok "$PROXY_DIR was not in user PATH (skipping)"
}

# ---------------------------------------------------------------------------
# 4. Delete the ~/.kiro-proxy directory
# ---------------------------------------------------------------------------
if (Test-Path $PROXY_DIR) {
    try {
        Remove-Item -Path $PROXY_DIR -Recurse -Force
        Write-Ok "Deleted $PROXY_DIR"
    } catch {
        Write-Warn "Could not fully delete $PROXY_DIR: $_"
        Write-Warn "You may need to delete it manually."
    }
} else {
    Write-Ok "$PROXY_DIR does not exist (already removed)"
}

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Uninstall complete." -ForegroundColor Green
Write-Host ""
Write-Host "  kiro-cli now connects directly to AWS (no compression proxy)."
Write-Host "  Open a new terminal for env var changes to take effect."
Write-Host ""
Write-Host "  To reinstall:"
Write-Host "  irm https://raw.githubusercontent.com/DrewGitsIt/headroom-kiro-proxy/main/scripts/install.ps1 | iex"
