# kiro-proxy — Cut kiro-cli token costs by ~50%

A local compression proxy that strips old screenshots, truncates stale tool results, and compresses verbose assistant turns from your kiro-cli conversation history — before it reaches the LLM. Works with kiro-cli chat, kiro-cli acp, and Kiro IDE.

## Install

```bash
curl -sSL --noproxy '*' https://raw.githubusercontent.com/DrewGitsIt/headroom-kiro-proxy/main/scripts/install.sh | bash
```

Then open a new terminal (or `source ~/.zshrc`). That's it — all kiro sessions are now compressed.

> **Trust model:** This downloads and executes scripts from this repo over HTTPS. You're trusting this GitHub account and GitHub's transport security. Review the [install script](scripts/install.sh) before running if you prefer.

## What it does

- Runs a local HTTPS proxy on `127.0.0.1:9090`
- Only intercepts traffic to `runtime.us-east-1.kiro.dev` (everything else passes through unchanged)
- Strips base64 images from old turns (~35% savings)
- Truncates tool results in old turns (~15% savings)
- Protects recent messages (last 8 turns are never touched)
- Auto-starts on login, auto-restarts on crash

## What it does NOT do

- Modify your system keychain
- Install system-wide proxy settings (only env vars in your shell profile)
- Affect git, curl, npm, brew, AWS CLI, or any other tool
- Touch response data (responses stream back unchanged)

## Lifecycle

```bash
# Check health
~/.kiro-proxy/doctor.sh

# Update to latest compression logic
~/.kiro-proxy/update.sh

# Uninstall (clean removal, restores everything)
~/.kiro-proxy/uninstall.sh
```

## How it works

```
kiro-cli (with HTTPS_PROXY + SSL_CERT_FILE)
  → mitmdump on 127.0.0.1:9090 (--allow-hosts runtime\.us-east-1\.kiro\.dev)
    → kiro traffic: intercept, compress request body, forward
    → all other traffic: transparent CONNECT tunnel (no TLS interception)
```

The proxy CA is only trusted by processes that have `SSL_CERT_FILE` pointing to the bundle. Other tools use the normal system trust store and never see the proxy CA.

## Measured impact

| Category | Overhead | Notes |
|----------|----------|-------|
| git, curl, gh, glab | +5–55ms | CONNECT tunnel, no TLS interception |
| npm, brew, pip | Negligible | Cache-dominated; proxy adds <50ms per request |
| VPN, streaming, Jira | None observed | Transparent passthrough |
| kiro-cli request size | **−38–54%** | Based on mid-length conversations |

## Requirements

- macOS (tested on Sonoma/Apple Silicon)
- Python 3.8+
- mitmproxy (`brew install mitmproxy` — the installer handles this)

## Design

See [docs/design.md](docs/design.md) for the full architecture, security model, compression strategy, and test results.

## Development

```bash
# Clone the repo
git clone https://github.com/DrewGitsIt/headroom-kiro-proxy.git
cd headroom-kiro-proxy

# Run tests
python -m pytest tests/

# Run proxy manually (for development)
mitmdump -s src/proxy.py --listen-port 9090 --set confdir=~/.kiro-proxy --allow-hosts 'runtime\.us-east-1\.kiro\.dev'
```
