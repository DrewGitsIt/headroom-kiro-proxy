# kiro-proxy — Cut kiro-cli token costs by ~50%

A local compression proxy that strips old screenshots, crushes verbose tool results, and truncates stale assistant turns from your kiro-cli conversation history — before it reaches the LLM. Works with kiro-cli chat, kiro-cli acp, and Kiro IDE.

## Install

```bash
curl -sSL --noproxy '*' https://raw.githubusercontent.com/DrewGitsIt/headroom-kiro-proxy/main/scripts/install.sh | bash
```

Then open a new terminal (or `source ~/.zshrc`). That's it — all kiro sessions are now compressed.

The installer will:
1. Test that kiro-cli works (baseline)
2. Set up a Python venv with the compression engine
3. Start the proxy and menu bar applet
4. Verify kiro-cli works through the proxy (end-to-end)

> **Trust model:** This downloads and executes scripts from this repo over HTTPS. Review the [install script](scripts/install.sh) before running if you prefer.

## What it does

- Runs a local HTTPS proxy on `127.0.0.1:9090`
- Only intercepts traffic to `runtime.us-east-1.kiro.dev` (everything else passes through unchanged)
- Strips base64 images from old turns (~35% savings per image)
- Compresses tool results via SmartCrusher (Rust, <50ms, ~30-55% savings)
- Truncates old assistant responses
- Protects recent messages (last 4 turns are never touched)
- Menu bar applet shows live stats (mushroom icon)
- Auto-starts on login, auto-restarts on crash

## What it does NOT do

- Modify your system keychain
- Install system-wide proxy settings (only env vars in your shell profile)
- Affect git, curl, npm, brew, AWS CLI, or any other tool
- Touch response data (responses stream back unchanged)

## Lifecycle

```bash
# Check status and stats
kiro-proxy status

# Tail proxy logs
kiro-proxy logs

# Temporarily disable (kiro-cli works normally, just uncompressed)
kiro-proxy disable

# Re-enable
kiro-proxy enable

# Restart (after config changes)
kiro-proxy restart

# Update to latest compression logic
kiro-proxy update

# Uninstall (clean removal, restores everything)
kiro-proxy uninstall
```

## How it works

```
kiro-cli (with HTTPS_PROXY + SSL_CERT_FILE)
  → asyncio CONNECT proxy on 127.0.0.1:9090
    → kiro traffic: TLS intercept → compress request → forward upstream
    → all other traffic: transparent CONNECT tunnel (no TLS interception)
```

The proxy CA is only trusted by processes that have `SSL_CERT_FILE` pointing to the bundle. Other tools use the normal system trust store and never see the proxy CA.

## Measured impact

| Category | Overhead | Notes |
|----------|----------|-------|
| git, curl, gh, glab | +5–55ms | CONNECT tunnel, no TLS interception |
| npm, brew, pip | Negligible | Cache-dominated; proxy adds <50ms per request |
| VPN, streaming, Jira | None observed | Transparent passthrough |
| kiro-cli request size | **−40–55%** | Based on mid-length conversations |

## Requirements

- macOS (tested on Sonoma/Apple Silicon and Intel)
- Python 3.10+
- kiro-cli (must be working before install)

## Design

See [docs/design.md](docs/design.md) for the full architecture, security model, compression strategy, and test results.

## Development

```bash
# Clone the repo
git clone https://github.com/DrewGitsIt/headroom-kiro-proxy.git
cd headroom-kiro-proxy

# Run the proxy manually
python3 -m venv .venv && .venv/bin/pip install headroom-ai
.venv/bin/python src/connect_proxy.py --port 9090 --debug

# Run tests
python -m pytest tests/
```
