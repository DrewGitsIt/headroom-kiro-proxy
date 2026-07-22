# install.ps1 — Install the kiro compression proxy on Windows.
#
# One-command install:
#   irm https://raw.githubusercontent.com/DrewGitsIt/headroom-kiro-proxy/main/scripts/install.ps1 | iex
#
# What this does:
#   1. Verifies Python 3.10+ and kiro-cli are present
#   2. Baseline-tests kiro-cli (without proxy)
#   3. Creates a venv at %USERPROFILE%\.kiro-proxy\.venv
#   4. Installs headroom-ai, cryptography, certifi, boto3 into the venv
#   5. Downloads proxy source files from GitHub
#   6. Generates CA cert + host cert chain inline (no external script)
#   7. Registers a Task Scheduler job (auto-start on logon, restart on failure)
#   8. Sets user-scope env vars: HTTPS_PROXY, SSL_CERT_FILE, NODE_EXTRA_CA_CERTS
#   9. Adds ~/.kiro-proxy to user PATH
#  10. End-to-end kiro-cli test through the proxy

#Requires -Version 5.1
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
$PROXY_DIR      = Join-Path $env:USERPROFILE '.kiro-proxy'
$VENV_DIR       = Join-Path $PROXY_DIR '.venv'
$SRC_DIR        = Join-Path $PROXY_DIR 'src'
$LOGS_DIR       = Join-Path $PROXY_DIR 'logs'
$PROXY_PORT     = 9090
$TASK_NAME      = 'kiro-proxy'
$HEALTH_URL     = "http://127.0.0.1:$PROXY_PORT/health"
$GITHUB_RAW     = 'https://raw.githubusercontent.com/DrewGitsIt/headroom-kiro-proxy/main'

# Source files to download from GitHub
$SOURCE_FILES = @(
    'src/connect_proxy.py',
    'src/proxy.py',
    'src/interceptor.py',
    'src/stats.py',
    'src/reporter.py',
    'src/handler.py',
    'src/applet.py'
)

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
function Write-Step  { param([int]$n, [string]$msg) Write-Host "" ; Write-Host "[step $n] $msg" -ForegroundColor Cyan }
function Write-Ok    { param([string]$msg) Write-Host "  [ok] $msg" -ForegroundColor Green }
function Write-Warn  { param([string]$msg) Write-Host "  [!]  $msg" -ForegroundColor Yellow }
function Write-Fail  {
    param([string]$msg)
    Write-Host ""
    Write-Host "  [FAIL] $msg" -ForegroundColor Red
    Write-Host ""
    exit 1
}

# ---------------------------------------------------------------------------
# Helper: download a file, bypassing any system proxy
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
# Helper: find Python 3.10+
# Returns the path to the python executable, or $null if not found.
# ---------------------------------------------------------------------------
function Find-Python {
    $candidates = @('py', 'python3', 'python')
    foreach ($cmd in $candidates) {
        try {
            # 'py -3 --version' works on Windows Launcher; others use --version directly
            $args_list = if ($cmd -eq 'py') { @('-3', '--version') } else { @('--version') }
            $ver_output = & $cmd @args_list 2>&1
            # Output is like "Python 3.12.1"
            if ($ver_output -match 'Python (\d+)\.(\d+)') {
                $major = [int]$Matches[1]
                $minor = [int]$Matches[2]
                if ($major -ge 3 -and $minor -ge 10) {
                    # Resolve to full path using 'where' (Windows) or 'which'
                    $resolved = (Get-Command $cmd -ErrorAction SilentlyContinue)
                    if ($resolved) {
                        return [pscustomobject]@{
                            Path    = $resolved.Source
                            Version = "$major.$minor"
                            Cmd     = $cmd
                        }
                    }
                }
            }
        } catch {
            # This candidate does not exist; try the next one.
            continue
        }
    }
    return $null
}

# ---------------------------------------------------------------------------
# Helper: wait for proxy health endpoint
# ---------------------------------------------------------------------------
function Wait-ProxyUp {
    param([int]$MaxSeconds = 15)
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
# Helper: set a persistent user-scope environment variable
# Also exports it into the current process so the end-to-end test works.
# ---------------------------------------------------------------------------
function Set-UserEnv {
    param([string]$Name, [string]$Value)
    [Environment]::SetEnvironmentVariable($Name, $Value, 'User')
    [System.Environment]::SetEnvironmentVariable($Name, $Value, 'Process')
}

# ---------------------------------------------------------------------------
# Helper: add a directory to the user PATH (idempotent)
# ---------------------------------------------------------------------------
function Add-ToUserPath {
    param([string]$Dir)
    $raw     = [Environment]::GetEnvironmentVariable('PATH', 'User')
    $current = if ($raw) { $raw } else { '' }
    $parts = $current -split ';' | Where-Object { $_ -ne '' }
    if ($parts -notcontains $Dir) {
        $new_path = ($parts + $Dir) -join ';'
        [Environment]::SetEnvironmentVariable('PATH', $new_path, 'User')
        $env:PATH = "$env:PATH;$Dir"
        Write-Ok "Added $Dir to user PATH"
    } else {
        Write-Ok "$Dir already in user PATH"
    }
}

# ---------------------------------------------------------------------------
# Main installation sequence
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "kiro-proxy installer (Windows)" -ForegroundColor Cyan
Write-Host ""

# ---------------------------------------------------------------------------
Write-Step 1 "Checking prerequisites"
# ---------------------------------------------------------------------------

$py = Find-Python
if ($null -eq $py) {
    Write-Fail "Python 3.10+ not found. Install from https://python.org/downloads/ then re-run this installer.`n  Tip: ensure 'Add Python to PATH' is checked during installation."
}
Write-Ok "Found Python $($py.Version) at $($py.Path)"

if (-not (Get-Command 'kiro-cli' -ErrorAction SilentlyContinue)) {
    Write-Fail "kiro-cli not found on PATH. Install kiro-cli first, then re-run this installer."
}
Write-Ok "Found kiro-cli"

# ---------------------------------------------------------------------------
Write-Step 2 "Testing kiro-cli baseline (without proxy)"
# ---------------------------------------------------------------------------

# Temporarily clear proxy env vars for the baseline test
$saved_https_proxy   = $env:HTTPS_PROXY
$saved_ssl_cert_file = $env:SSL_CERT_FILE
$env:HTTPS_PROXY     = ''
$env:SSL_CERT_FILE   = ''

$baseline_ok = $false
try {
    $baseline_output = & kiro-cli chat --no-interactive "respond with only the word: ok" 2>&1
    $baseline_ok = $true
    Write-Ok "kiro-cli baseline test passed"
} catch {
    Write-Warn "kiro-cli failed (exit code $LASTEXITCODE). Output: $baseline_output"
    Write-Fail "kiro-cli must work before installing the proxy. Fix kiro-cli first."
} finally {
    # Restore in case they were set
    if ($saved_https_proxy)   { $env:HTTPS_PROXY   = $saved_https_proxy }
    if ($saved_ssl_cert_file) { $env:SSL_CERT_FILE = $saved_ssl_cert_file }
}

# ---------------------------------------------------------------------------
Write-Step 3 "Creating venv and installing Python dependencies"
# ---------------------------------------------------------------------------

New-Item -ItemType Directory -Force -Path $PROXY_DIR | Out-Null
New-Item -ItemType Directory -Force -Path $SRC_DIR   | Out-Null
New-Item -ItemType Directory -Force -Path $LOGS_DIR  | Out-Null

if (Test-Path (Join-Path $VENV_DIR 'Scripts\python.exe')) {
    Write-Ok "Existing venv found at $VENV_DIR, reusing"
} else {
    Write-Host "  Creating venv at $VENV_DIR ..." -ForegroundColor DarkGray
    & $py.Path -m venv $VENV_DIR
    if ($LASTEXITCODE -ne 0) { Write-Fail "Failed to create Python venv." }
    Write-Ok "Created venv"
}

$venv_pip    = Join-Path $VENV_DIR 'Scripts\pip.exe'
$venv_python = Join-Path $VENV_DIR 'Scripts\python.exe'

Write-Host "  Installing headroom-ai, cryptography, certifi, boto3 (may take 30-60s)..." -ForegroundColor DarkGray
& $venv_pip install --quiet --upgrade pip 2>$null | Out-Null
& $venv_pip install --quiet "headroom-ai>=0.31.0" "cryptography>=42.0.0" "certifi>=2024.1.1" "boto3>=1.34.0"
if ($LASTEXITCODE -ne 0) { Write-Fail "pip install failed. Check your internet connection." }
Write-Ok "Installed headroom-ai, cryptography, certifi, boto3"

# ---------------------------------------------------------------------------
Write-Step 4 "Downloading proxy source from GitHub"
# ---------------------------------------------------------------------------

foreach ($rel_path in $SOURCE_FILES) {
    $url  = "$GITHUB_RAW/$rel_path"
    $dest = Join-Path $PROXY_DIR ($rel_path -replace '/', '\')
    # Ensure parent directory exists (src/ subfolder)
    $parent = Split-Path $dest -Parent
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    Download-File -Url $url -Dest $dest
    Write-Ok "Downloaded $rel_path"
}

# Also download the management scripts
$mgmt_scripts = @('update.ps1', 'uninstall.ps1', 'kiro-proxy.ps1')
foreach ($script in $mgmt_scripts) {
    $url  = "$GITHUB_RAW/scripts/$script"
    $dest = Join-Path $PROXY_DIR $script
    Download-File -Url $url -Dest $dest
    Write-Ok "Downloaded scripts/$script"
}

# ---------------------------------------------------------------------------
Write-Step 5 "Generating TLS certificates"
# ---------------------------------------------------------------------------

# Generate CA + host cert inline using the cryptography library (no external file needed).
# This mirrors what install.sh does on macOS with openssl commands.
$cert_gen_script = @"
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from cryptography import x509
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

proxy_dir = Path(sys.argv[1])
proxy_dir.mkdir(parents=True, exist_ok=True)

# --- CA cert (self-signed) ---
ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'kiro-proxy CA')])
now = datetime.now(timezone.utc)
ca_cert = (
    x509.CertificateBuilder()
    .subject_name(ca_name)
    .issuer_name(ca_name)
    .public_key(ca_key.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(now)
    .not_valid_after(now + timedelta(days=3650))
    .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
    .add_extension(x509.KeyUsage(
        digital_signature=False, content_commitment=False, key_encipherment=False,
        data_encipherment=False, key_agreement=False, key_cert_sign=True,
        crl_sign=True, encipher_only=False, decipher_only=False
    ), critical=True)
    .sign(ca_key, hashes.SHA256())
)

# --- Host cert (signed by CA) ---
host_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
host_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'runtime.us-east-1.kiro.dev')])
host_cert = (
    x509.CertificateBuilder()
    .subject_name(host_name)
    .issuer_name(ca_name)
    .public_key(host_key.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(now)
    .not_valid_after(now + timedelta(days=825))
    .add_extension(x509.SubjectAlternativeName([
        x509.DNSName('runtime.us-east-1.kiro.dev'),
        x509.DNSName('*.kiro.dev'),
    ]), critical=False)
    .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
    .sign(ca_key, hashes.SHA256())
)

# --- Write files ---
(proxy_dir / 'ca-key.pem').write_bytes(
    ca_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
)
(proxy_dir / 'ca-cert.pem').write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
(proxy_dir / 'key.pem').write_bytes(
    host_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
)
(proxy_dir / 'cert.pem').write_bytes(host_cert.public_bytes(serialization.Encoding.PEM))

# --- CA bundle: system roots + our CA ---
import certifi
system_roots = Path(certifi.where()).read_bytes()
(proxy_dir / 'ca-bundle.pem').write_bytes(system_roots + b'\n' + ca_cert.public_bytes(serialization.Encoding.PEM))

print('Certificates generated successfully.')
"@

$cert_gen_tmp = Join-Path $PROXY_DIR '_cert_gen.py'
Set-Content -Path $cert_gen_tmp -Value $cert_gen_script -Encoding UTF8

& $venv_python $cert_gen_tmp $PROXY_DIR
if ($LASTEXITCODE -ne 0) { Write-Fail "Certificate generation failed. Check Python output above." }
Remove-Item -Path $cert_gen_tmp -Force -ErrorAction SilentlyContinue

$ca_cert_path   = Join-Path $PROXY_DIR 'ca-cert.pem'
$ca_bundle_path = Join-Path $PROXY_DIR 'ca-bundle.pem'

if (-not (Test-Path $ca_cert_path)) {
    Write-Fail "ca-cert.pem not found after cert generation. Something went wrong."
}
if (-not (Test-Path $ca_bundle_path)) {
    Write-Fail "ca-bundle.pem not found after cert generation. Something went wrong."
}
Write-Ok "Certificates ready in $PROXY_DIR"

# ---------------------------------------------------------------------------
Write-Step 6 "Registering Task Scheduler job"
# ---------------------------------------------------------------------------

$proxy_script = Join-Path $SRC_DIR 'connect_proxy.py'
$log_out      = Join-Path $LOGS_DIR 'proxy.log'
$log_err      = Join-Path $LOGS_DIR 'proxy.err'

# Remove stale task if it exists
$existing = Get-ScheduledTask -TaskName $TASK_NAME -ErrorAction SilentlyContinue
if ($existing) {
    Stop-ScheduledTask -TaskName $TASK_NAME -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TASK_NAME -Confirm:$false
    Write-Ok "Removed previous scheduled task"
}

# Build the action: run python with connect_proxy.py, stdout/stderr to files
# We use cmd /c to redirect output because Task Scheduler actions don't support
# shell redirection natively.
$action_cmd  = $venv_python
$action_args = "`"$proxy_script`" --port $PROXY_PORT >> `"$log_out`" 2>> `"$log_err`""

# Wrap in cmd /c so redirection works
$action = New-ScheduledTaskAction `
    -Execute 'cmd.exe' `
    -Argument "/c `"$action_cmd`" $action_args" `
    -WorkingDirectory $SRC_DIR

$trigger = New-ScheduledTaskTrigger -AtLogon

# Run as current user, restart on failure (up to 3 times, 30s interval)
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable

$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName  $TASK_NAME `
    -Action    $action `
    -Trigger   $trigger `
    -Settings  $settings `
    -Principal $principal `
    -Force | Out-Null

Write-Ok "Registered scheduled task '$TASK_NAME' (runs at logon)"

# Start it now
Start-ScheduledTask -TaskName $TASK_NAME
Write-Ok "Started proxy"

# Wait for health endpoint
if (Wait-ProxyUp -MaxSeconds 15) {
    Write-Ok "Proxy is running on 127.0.0.1:$PROXY_PORT"
} else {
    Write-Warn "Proxy did not respond within 15 seconds."
    Write-Warn "Check logs with: kiro-proxy logs"
    Write-Warn "  Log file: $log_err"
    # Don't hard-fail — the task is registered and will retry.
}

# ---------------------------------------------------------------------------
Write-Step 7 "Setting environment variables"
# ---------------------------------------------------------------------------

Set-UserEnv -Name 'HTTPS_PROXY'          -Value "http://127.0.0.1:$PROXY_PORT"
Set-UserEnv -Name 'SSL_CERT_FILE'        -Value $ca_bundle_path
Set-UserEnv -Name 'NODE_EXTRA_CA_CERTS'  -Value $ca_cert_path

Write-Ok "HTTPS_PROXY          = http://127.0.0.1:$PROXY_PORT"
Write-Ok "SSL_CERT_FILE        = $ca_bundle_path"
Write-Ok "NODE_EXTRA_CA_CERTS  = $ca_cert_path"

# ---------------------------------------------------------------------------
Write-Step 8 "Adding kiro-proxy to PATH"
# ---------------------------------------------------------------------------

Add-ToUserPath -Dir $PROXY_DIR

# ---------------------------------------------------------------------------
Write-Step 9 "End-to-end verification through proxy"
# ---------------------------------------------------------------------------

# env vars are already set in the current process by Set-UserEnv
try {
    $proxy_output = & kiro-cli chat --no-interactive "respond with only the word: ok" 2>&1
    Write-Ok "kiro-cli works through the proxy"
} catch {
    Write-Warn "kiro-cli test through proxy failed (exit code $LASTEXITCODE)."
    Write-Warn "This may be a transient startup issue. Try: kiro-proxy status"
    Write-Warn "If problems persist: kiro-proxy logs"
}

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "=== Installation complete ===" -ForegroundColor Green
Write-Host ""
Write-Host "  Proxy:  127.0.0.1:$PROXY_PORT (running, auto-starts at logon)"
Write-Host "  Venv:   $VENV_DIR"
Write-Host "  Logs:   $LOGS_DIR"
Write-Host ""
Write-Host "  IMPORTANT: Open a new terminal for env vars to take effect."
Write-Host ""
Write-Host "  kiro-proxy status      Show health and compression stats"
Write-Host "  kiro-proxy logs        Tail proxy logs"
Write-Host "  kiro-proxy disable     Temporarily stop compression"
Write-Host "  kiro-proxy enable      Re-enable compression"
Write-Host "  kiro-proxy update      Pull latest compression logic"
Write-Host "  kiro-proxy uninstall   Clean removal"
Write-Host ""
Write-Host "  All kiro-cli sessions will now be compressed (~40-55% savings)."
Write-Host "  Other tools (git, curl, npm, winget) are unaffected."
