# kiro-proxy.ps1 — CLI for the kiro compression proxy (Windows).
#
# Installed to %USERPROFILE%\.kiro-proxy\kiro-proxy.ps1 and available on PATH.
#
# Subcommands:
#   status      Show health and live compression stats
#   logs        Tail the proxy error log (last 50 lines, then follow)
#   enable      Enable the scheduled task (re-enable compression)
#   disable     Disable the scheduled task (temporarily stop compression)
#   restart     Stop and start the proxy task
#   update      Download latest source and restart (invokes update.ps1)
#   uninstall   Complete removal (invokes uninstall.ps1)

#Requires -Version 5.1
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

$PROXY_DIR  = Join-Path $env:USERPROFILE '.kiro-proxy'
$PROXY_PORT = 9090
$TASK_NAME  = 'kiro-proxy'
$STATS_URL  = "http://127.0.0.1:$PROXY_PORT/stats"
$HEALTH_URL = "http://127.0.0.1:$PROXY_PORT/health"
$LOG_ERR    = Join-Path $PROXY_DIR 'logs\proxy.err'
$LOG_OUT    = Join-Path $PROXY_DIR 'logs\proxy.log'

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
function Write-Ok   { param([string]$msg) Write-Host "  [ok]  $msg" -ForegroundColor Green }
function Write-Warn { param([string]$msg) Write-Host "  [!]   $msg" -ForegroundColor Yellow }
function Write-Err  { param([string]$msg) Write-Host "  [err] $msg" -ForegroundColor Red }
function Write-Item { param([string]$key, [string]$val) Write-Host ("  {0,-22} {1}" -f $key, $val) }

# ---------------------------------------------------------------------------
# Subcommand: status
# ---------------------------------------------------------------------------
function Invoke-Status {
    Write-Host ""
    Write-Host "kiro-proxy status" -ForegroundColor Cyan
    Write-Host ""

    # Task Scheduler state
    $task = Get-ScheduledTask -TaskName $TASK_NAME -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Err  "Scheduled task '$TASK_NAME' not registered."
        Write-Warn "Run the installer to set it up:"
        Write-Host "  irm https://raw.githubusercontent.com/DrewGitsIt/headroom-kiro-proxy/main/scripts/install.ps1 | iex"
        Write-Host ""
        return
    }

    $task_state = $task.State          # Ready, Running, Disabled, etc.
    $task_color = switch ($task_state) {
        'Running'  { 'Green'  }
        'Ready'    { 'Yellow' }
        'Disabled' { 'Red'    }
        default    { 'Gray'   }
    }
    Write-Host "  Task state:            " -NoNewline
    Write-Host $task_state -ForegroundColor $task_color

    # Last run result
    $task_info = Get-ScheduledTaskInfo -TaskName $TASK_NAME -ErrorAction SilentlyContinue
    if ($task_info) {
        $last_run    = if ($task_info.LastRunTime -and $task_info.LastRunTime -ne [DateTime]::MinValue) { $task_info.LastRunTime.ToString('u') } else { 'never' }
        $last_result = $task_info.LastTaskResult
        Write-Item "  Last run:" $last_run
        if ($last_result -ne 0 -and $last_result -ne 267009) {
            # 267009 = SCHED_S_TASK_RUNNING (still running — not an error)
            Write-Warn "  Last result code: $last_result (non-zero may indicate a crash)"
        }
    }

    Write-Host ""

    # HTTP health check
    $proxy_up = $false
    try {
        $resp = Invoke-WebRequest -Uri $HEALTH_URL -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
        if ($resp.StatusCode -eq 200) { $proxy_up = $true }
    } catch { }

    if ($proxy_up) {
        Write-Host "  HTTP health:           " -NoNewline
        Write-Host "up" -ForegroundColor Green
    } else {
        Write-Host "  HTTP health:           " -NoNewline
        Write-Host "not responding" -ForegroundColor Red
        Write-Warn "Proxy is not accepting connections on port $PROXY_PORT."
        if ($task_state -eq 'Disabled') {
            Write-Warn "Task is disabled. Run: kiro-proxy enable"
        } elseif ($task_state -eq 'Ready') {
            Write-Warn "Task is registered but not running. Run: kiro-proxy restart"
        }
        Write-Warn "Check logs: kiro-proxy logs"
        Write-Host ""
        return
    }

    # /stats endpoint
    try {
        $resp   = Invoke-WebRequest -Uri $STATS_URL -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
        $stats  = $resp.Content | ConvertFrom-Json

        Write-Host ""
        Write-Host "  Compression stats:" -ForegroundColor Cyan

        $req_total   = if ($null -ne $stats.requests_total)  { $stats.requests_total  } `
                       elseif ($null -ne $stats.requests)    { $stats.requests         } `
                       else { 'n/a' }
        $bytes_in    = if ($null -ne $stats.bytes_in)        { $stats.bytes_in         } `
                       elseif ($null -ne $stats.bytes_received) { $stats.bytes_received } `
                       else { 0 }
        $bytes_out   = if ($null -ne $stats.bytes_out)       { $stats.bytes_out        } `
                       elseif ($null -ne $stats.bytes_sent)  { $stats.bytes_sent       } `
                       else { 0 }
        $uptime_s    = if ($null -ne $stats.uptime_seconds)  { $stats.uptime_seconds   } else { 0 }

        Write-Item "  Requests proxied:" $req_total

        if ($bytes_in -gt 0 -and $bytes_out -gt 0) {
            $saved_pct = [math]::Round((1 - $bytes_out / $bytes_in) * 100, 1)
            $saved_kb  = [math]::Round(($bytes_in - $bytes_out) / 1024, 1)
            Write-Item "  Bytes in:"   "$([math]::Round($bytes_in / 1024, 1)) KB"
            Write-Item "  Bytes out:"  "$([math]::Round($bytes_out / 1024, 1)) KB"
            Write-Item "  Saved:"      "$saved_kb KB ($saved_pct%)"
        }

        if ($uptime_s -gt 0) {
            $uptime_fmt = if ($uptime_s -ge 3600) {
                "{0}h {1}m" -f [math]::Floor($uptime_s / 3600), [math]::Floor(($uptime_s % 3600) / 60)
            } elseif ($uptime_s -ge 60) {
                "{0}m {1}s" -f [math]::Floor($uptime_s / 60), ($uptime_s % 60)
            } else {
                "${uptime_s}s"
            }
            Write-Item "  Uptime:" $uptime_fmt
        }

    } catch {
        Write-Warn "Could not parse /stats: $_"
    }

    Write-Host ""
}

# ---------------------------------------------------------------------------
# Subcommand: logs
# ---------------------------------------------------------------------------
function Invoke-Logs {
    if (-not (Test-Path $LOG_ERR)) {
        Write-Warn "Log file not found: $LOG_ERR"
        Write-Warn "Has the proxy run at least once? Try: kiro-proxy restart"
        return
    }
    Write-Host ""
    Write-Host "kiro-proxy logs (last 50 lines, then following...)" -ForegroundColor Cyan
    Write-Host "(Ctrl-C to stop)" -ForegroundColor DarkGray
    Write-Host ""
    Get-Content -Path $LOG_ERR -Tail 50 -Wait
}

# ---------------------------------------------------------------------------
# Subcommand: enable
# ---------------------------------------------------------------------------
function Invoke-Enable {
    $task = Get-ScheduledTask -TaskName $TASK_NAME -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Err "Scheduled task '$TASK_NAME' not found. Run the installer first."
        exit 1
    }
    Enable-ScheduledTask -TaskName $TASK_NAME | Out-Null
    Start-ScheduledTask  -TaskName $TASK_NAME
    Write-Ok "Enabled and started task '$TASK_NAME'"
    Write-Ok "Compression proxy is active."
}

# ---------------------------------------------------------------------------
# Subcommand: disable
# ---------------------------------------------------------------------------
function Invoke-Disable {
    $task = Get-ScheduledTask -TaskName $TASK_NAME -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Err "Scheduled task '$TASK_NAME' not found."
        exit 1
    }
    Stop-ScheduledTask    -TaskName $TASK_NAME -ErrorAction SilentlyContinue
    Disable-ScheduledTask -TaskName $TASK_NAME | Out-Null
    Write-Ok "Disabled task '$TASK_NAME'"
    Write-Ok "kiro-cli will now connect directly to AWS (uncompressed)."
    Write-Warn "HTTPS_PROXY is still set in your env. Start a new terminal to see the effect."
}

# ---------------------------------------------------------------------------
# Subcommand: restart
# ---------------------------------------------------------------------------
function Invoke-Restart {
    $task = Get-ScheduledTask -TaskName $TASK_NAME -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Err "Scheduled task '$TASK_NAME' not found. Run the installer first."
        exit 1
    }
    Stop-ScheduledTask  -TaskName $TASK_NAME -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    Start-ScheduledTask -TaskName $TASK_NAME
    Write-Ok "Restarted task '$TASK_NAME'"

    # Wait for health
    $deadline = (Get-Date).AddSeconds(12)
    $up = $false
    while ((Get-Date) -lt $deadline) {
        try {
            $resp = Invoke-WebRequest -Uri $HEALTH_URL -UseBasicParsing -TimeoutSec 2 -ErrorAction SilentlyContinue
            if ($resp.StatusCode -eq 200) { $up = $true; break }
        } catch { }
        Start-Sleep -Seconds 1
    }
    if ($up) {
        Write-Ok "Proxy is responding on port $PROXY_PORT"
    } else {
        Write-Warn "Proxy did not respond within 12 seconds."
        Write-Warn "Check logs: kiro-proxy logs"
    }
}

# ---------------------------------------------------------------------------
# Subcommand: update
# ---------------------------------------------------------------------------
function Invoke-Update {
    $update_script = Join-Path $PROXY_DIR 'update.ps1'
    if (-not (Test-Path $update_script)) {
        Write-Err "update.ps1 not found at $update_script."
        Write-Warn "Downloading it from GitHub..."
        try {
            Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/DrewGitsIt/headroom-kiro-proxy/main/scripts/update.ps1' `
                -OutFile $update_script -UseBasicParsing -NoProxy
        } catch {
            Write-Err "Download failed: $_"
            exit 1
        }
    }
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $update_script
}

# ---------------------------------------------------------------------------
# Subcommand: uninstall
# ---------------------------------------------------------------------------
function Invoke-Uninstall {
    $uninstall_script = Join-Path $PROXY_DIR 'uninstall.ps1'
    if (-not (Test-Path $uninstall_script)) {
        Write-Err "uninstall.ps1 not found at $uninstall_script."
        Write-Warn "Downloading it from GitHub..."
        try {
            Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/DrewGitsIt/headroom-kiro-proxy/main/scripts/uninstall.ps1' `
                -OutFile $uninstall_script -UseBasicParsing -NoProxy
        } catch {
            Write-Err "Download failed: $_"
            exit 1
        }
    }
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $uninstall_script
}

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
function Show-Usage {
    Write-Host ""
    Write-Host "Usage: kiro-proxy <command>" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  status      Show proxy health and compression stats"
    Write-Host "  logs        Tail proxy logs"
    Write-Host "  enable      Enable compression (re-enable scheduled task)"
    Write-Host "  disable     Disable compression (stop scheduled task)"
    Write-Host "  restart     Restart the proxy"
    Write-Host "  update      Update to latest version"
    Write-Host "  uninstall   Remove kiro-proxy completely"
    Write-Host ""
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
$subcommand = if ($args.Count -gt 0) { $args[0].ToLower() } else { '' }

switch ($subcommand) {
    'status'    { Invoke-Status    }
    'logs'      { Invoke-Logs      }
    'enable'    { Invoke-Enable    }
    'disable'   { Invoke-Disable   }
    'restart'   { Invoke-Restart   }
    'update'    { Invoke-Update    }
    'uninstall' { Invoke-Uninstall }
    default     { Show-Usage       }
}
