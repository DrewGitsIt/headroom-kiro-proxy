# Spec: Windows Support for kiro-proxy

## Goal

Enable the same `kiro-proxy` compression pipeline on Windows. Users get a one-command install (PowerShell equivalent of `curl | bash`) that sets up the proxy, auto-starts on login, and delivers ~40-55% token savings.

## Scope

Phase 1 (headless): Proxy + CLI + auto-start. No tray applet.
Phase 2 (follow-up): System tray applet with `pystray`.

## Current State

The proxy core is already cross-platform:
- `connect_proxy.py` — pure asyncio, no platform-specific code
- `handler.py` — pure Python + headroom-ai (which ships Windows wheels)

What's macOS-only:
- `install.sh` (bash)
- LaunchAgents + `launchctl`
- `rumps` (menu bar)
- `openssl` CLI for cert generation
- `kiro-proxy` CLI (bash script)
- `.zshrc` env var injection

## Architecture

```
User runs: iwr -useb https://...install.ps1 | iex
  → Creates venv at %USERPROFILE%\.kiro-proxy\.venv
  → pip install headroom-ai
  → Generates CA cert (via Python cryptography lib)
  → Registers Task Scheduler job (auto-start, restart on failure)
  → Sets user env vars (HTTPS_PROXY, SSL_CERT_FILE, NODE_EXTRA_CA_CERTS)
  → Runs end-to-end test
```

## Component Mapping

| macOS | Windows | Notes |
|-------|---------|-------|
| `install.sh` | `install.ps1` | PowerShell 5.1+ (ships with Win10+) |
| `uninstall.sh` | `uninstall.ps1` | |
| `update.sh` | `update.ps1` | |
| `scripts/kiro-proxy` (bash) | `kiro-proxy.ps1` | Or compile to `.exe` via PyInstaller for PATH convenience |
| LaunchAgents | Task Scheduler XML | `Register-ScheduledTask` cmdlet |
| `launchctl setenv` | `[Environment]::SetEnvironmentVariable(..., "User")` | Persists in registry |
| `.zshrc` block | User env vars in registry | No shell rc file needed |
| `openssl` CLI | Python `cryptography` package | Eliminate external dep |
| `rumps` | `pystray` (Phase 2) | Skip for Phase 1 |
| `/usr/local/bin/kiro-proxy` symlink | Add `%USERPROFILE%\.kiro-proxy` to PATH | |

## Detailed Work Items

### 1. Cert Generation in Python (shared, both platforms)

Replace `generate-ca.sh` with a Python script `generate_certs.py` that uses the `cryptography` package:

```python
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
```

Generates:
- CA cert + key (10-year, `CN=kiro-proxy CA`)
- Host cert for `runtime.us-east-1.kiro.dev` signed by CA
- CA bundle: `certifi.where()` contents + our CA cert

This also benefits macOS (removes `openssl` CLI dependency).

**Effort: 0.5 day**

### 2. `install.ps1`

Steps:
1. Check Python 3.10+ on PATH (`py -3 --version` or `python3 --version`)
2. Check `kiro-cli` exists
3. Baseline kiro-cli test
4. Create venv: `py -3 -m venv $env:USERPROFILE\.kiro-proxy\.venv`
5. `pip install headroom-ai cryptography certifi`
6. Download source from GitHub (with `-NoProxy`)
7. Run `generate_certs.py`
8. Register Task Scheduler job (see below)
9. Set user env vars:
   ```powershell
   [Environment]::SetEnvironmentVariable("HTTPS_PROXY", "http://127.0.0.1:9090", "User")
   [Environment]::SetEnvironmentVariable("SSL_CERT_FILE", "$certBundle", "User")
   [Environment]::SetEnvironmentVariable("NODE_EXTRA_CA_CERTS", "$caCert", "User")
   ```
10. Add `%USERPROFILE%\.kiro-proxy` to user PATH
11. End-to-end test

**Execution policy note:** The `iwr | iex` pattern bypasses execution policy for the invoked script. But if install.ps1 calls other `.ps1` files, those need `-ExecutionPolicy Bypass`. Handle via:
```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

**Effort: 2 days**

### 3. Task Scheduler Registration

```powershell
$action = New-ScheduledTaskAction `
    -Execute "$env:USERPROFILE\.kiro-proxy\.venv\Scripts\python.exe" `
    -Argument "connect_proxy.py --port 9090" `
    -WorkingDirectory "$env:USERPROFILE\.kiro-proxy\src"

$trigger = New-ScheduledTaskTrigger -AtLogon
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Seconds 10) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable

Register-ScheduledTask `
    -TaskName "kiro-proxy" `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -RunLevel Limited
```

Equivalent to macOS LaunchAgent with `KeepAlive: true`.

**Effort: 0.5 day**

### 4. `kiro-proxy.ps1` CLI

Subcommands: `status`, `logs`, `enable`, `disable`, `restart`, `update`, `uninstall`

```powershell
# status → curl http://127.0.0.1:9090/stats and format
# enable → Enable-ScheduledTask -TaskName "kiro-proxy"
# disable → Disable-ScheduledTask -TaskName "kiro-proxy"
# restart → Stop-ScheduledTask + Start-ScheduledTask
# logs → Get-Content -Tail 50 ~/.kiro-proxy/logs/proxy.err
```

**Effort: 1 day**

### 5. `uninstall.ps1`

1. `Stop-ScheduledTask -TaskName "kiro-proxy"`
2. `Unregister-ScheduledTask -TaskName "kiro-proxy" -Confirm:$false`
3. Remove user env vars
4. Remove `~\.kiro-proxy` from PATH
5. Remove `~\.kiro-proxy\` directory

**Effort: 0.5 day**

### 6. Testing

- Windows 10 + Windows 11
- Python 3.10, 3.11, 3.12, 3.13, 3.14
- kiro-cli through proxy (TLS handshake, compression)
- Task Scheduler restart-on-failure
- Clean uninstall + reinstall cycle

**Effort: 2 days**

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| Windows Defender flags the script | PowerShell scripts are not flagged; only `.exe` files trigger SmartScreen |
| Python not on PATH | Check for `py` launcher (standard on Windows), fail early with install instructions |
| kiro-cli on Windows doesn't respect `NODE_EXTRA_CA_CERTS` | Test early — if not, fall back to adding CA to Windows cert store (`certutil -addstore Root`) |
| User doesn't have Python 3.10+ | Installer prints link to python.org, exits cleanly |
| Proxy log path issues (backslashes) | Use `pathlib.Path` in connect_proxy.py (already mostly used) |

## Open Questions

1. Do we want a single cross-platform `applet.py` (with `pystray` on Windows, `rumps` on macOS), or separate files?
2. Should we migrate cert generation to Python on macOS too (unify codepath), or leave `openssl` for macOS?
3. Do Windows users at the company have `kiro-cli` installed? What's the prerequisite state?
4. Is there a corporate proxy on the Windows machines that we'd need to chain through?

## Estimated Total Effort

| Item | Days |
|------|------|
| Cert generation in Python | 0.5 |
| install.ps1 | 2 |
| Task Scheduler setup | 0.5 |
| kiro-proxy.ps1 CLI | 1 |
| uninstall.ps1 + update.ps1 | 1 |
| Testing | 2 |
| Documentation | 0.5 |
| **Total** | **~8 days** |

Phase 2 (pystray applet): additional 2 days.
