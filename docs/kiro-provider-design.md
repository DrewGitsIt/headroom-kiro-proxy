# KiroProvider for Headroom — Design

**Status:** Draft  
**Date:** 2026-07-14  
**Goal:** Replace the custom kiro-proxy MITM with a first-class headroom provider, enabling `headroom wrap kiro`.

## Motivation

The current kiro-proxy has three structural problems that headroom solves by design:

1. **Cache busting:** Our kiro→anthropic→compress→anthropic→kiro translation changes the serialized bytes on every request, breaking Bedrock's prefix cache. Headroom's CacheAligner stabilizes prefixes — but only when it operates on the ACTUAL wire format, not a translation.

2. **Inflated savings:** We report headroom's token count on the Anthropic translation, not on kiro's wire format. The numbers are meaningless.

3. **Availability:** mitmproxy in the request path means hangs are unrecoverable. Headroom's proxy has timeouts, health monitoring, and graceful degradation built in.

## Architecture

Kiro is architecturally different from every agent headroom currently supports:

| Agent | API format | How headroom intercepts |
|-------|-----------|------------------------|
| Claude Code | Anthropic `/v1/messages` | Set `ANTHROPIC_BASE_URL` to proxy |
| Codex | OpenAI `/v1/chat/completions` | Set `OPENAI_BASE_URL` to proxy |
| Aider | OpenAI-compatible | Set base URL to proxy |
| **Kiro** | **Custom `POST /` to `runtime.us-east-1.kiro.dev`** | **`HTTPS_PROXY` + SSL cert** |

Kiro doesn't expose a configurable base URL. Traffic must be intercepted via `HTTPS_PROXY` + a trusted CA cert, just like the current proxy does. This is the same pattern headroom uses for Copilot's subscription mode.

## Components to Implement

### 1. Agent Provider: `headroom/providers/kiro/`

```
headroom/providers/kiro/
├── __init__.py         # Exports
├── install.py          # build_install_env() → env vars for kiro sessions
└── runtime.py          # Kiro version detection, runtime constants
```

#### `install.py`

```python
"""Kiro install-time helpers."""
from __future__ import annotations


KIRO_RUNTIME_HOST = "runtime.us-east-1.kiro.dev"


def build_install_env(*, port: int, backend: str) -> dict[str, str]:
    """Build the persistent install environment for Kiro.

    Kiro doesn't support a custom API base URL. We route traffic through
    headroom's proxy via HTTPS_PROXY + a CA bundle that includes headroom's
    generated certificate.
    """
    del backend  # Kiro always hits its own runtime endpoint
    return {
        "HTTPS_PROXY": f"http://127.0.0.1:{port}",
        "SSL_CERT_FILE": "~/.headroom/kiro-ca-bundle.pem",
    }
```

#### `runtime.py`

```python
"""Runtime helpers for Kiro-facing integrations."""
from __future__ import annotations

import re
import shutil

KIRO_RUNTIME_HOST = "runtime.us-east-1.kiro.dev"
KIRO_RUNTIME_URL = f"https://{KIRO_RUNTIME_HOST}/"

# Kiro's wire format uses conversationState.history with these message types
KIRO_USER_MESSAGE_KEY = "userInputMessage"
KIRO_ASSISTANT_MESSAGE_KEY = "assistantResponseMessage"

_KIRO_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def detect_kiro_version() -> tuple[int, int, int] | None:
    """Best-effort detection of installed kiro-cli version."""
    binary = shutil.which("kiro-cli") or shutil.which("kiro")
    if not binary:
        return None
    # TODO: kiro-cli --version parsing once we know the format
    return None


def proxy_requires_tls_intercept() -> bool:
    """Kiro always requires TLS interception (no configurable base URL)."""
    return True
```

### 2. Route Handler: `handle_kiro_runtime()`

This is the novel piece. Headroom's proxy needs a new handler that:
1. Catches `POST` requests where `Host: runtime.us-east-1.kiro.dev`
2. Parses kiro's `conversationState.history` JSON
3. Applies SmartCrusher to tool results **in-place** (no format translation)
4. Strips old images **in-place**
5. Re-serializes with **deterministic JSON** for prefix stability
6. Forwards to the real `runtime.us-east-1.kiro.dev`
7. Streams response back unchanged

```python
"""Kiro runtime request handler for headroom proxy."""
from __future__ import annotations

import json
from typing import Any

from headroom.transforms.smart_crusher import smart_crush


PROTECT_RECENT_TURNS = 4  # Don't touch the last N message pairs


async def handle_kiro_runtime(request_body: bytes, headers: dict) -> bytes:
    """Compress kiro conversation history in-place.

    Operates directly on kiro's wire format — no translation to/from
    Anthropic messages. This preserves byte-stability for Bedrock's
    prefix cache.
    """
    req = json.loads(request_body)

    if "conversationState" not in req:
        return request_body

    history = req["conversationState"].get("history", [])
    if not history:
        return request_body

    protect_start = max(0, len(history) - PROTECT_RECENT_TURNS * 2)

    for i, msg in enumerate(history):
        if i >= protect_start:
            break  # Recent messages are sacred

        if "userInputMessage" in msg:
            _compress_user_message(msg["userInputMessage"], turn_index=i)
        elif "assistantResponseMessage" in msg:
            _compress_assistant_message(msg["assistantResponseMessage"])

    # Deterministic serialization — critical for prefix cache stability
    return json.dumps(req, sort_keys=True, separators=(",", ":")).encode()


def _compress_user_message(um: dict[str, Any], *, turn_index: int) -> None:
    """Compress a kiro userInputMessage in-place."""
    # Strip old images
    images = um.get("images", [])
    if images:
        count = len(images)
        turn_num = turn_index // 2 + 1
        um["content"] = um.get("content", "") + (
            f"\n[{count} screenshot(s) from turn {turn_num} removed]"
        )
        um["images"] = []

    # Compress tool results
    ctx = um.get("userInputMessageContext", {})
    tool_results = ctx.get("toolResults", [])
    for tr in tool_results:
        for part in tr.get("content", []):
            text = part.get("text", "")
            if len(text) > 800:  # Only compress substantial tool output
                compressed = smart_crush(text)
                if compressed is not None and len(compressed) < len(text) * 0.8:
                    part["text"] = compressed


def _compress_assistant_message(arm: dict[str, Any]) -> None:
    """Compress old assistant responses (truncate verbose ones)."""
    content = arm.get("content", "")
    if len(content) > 5000:
        arm["content"] = content[:1000] + f"\n[... {len(content) - 1000:,} chars truncated]"
```

### 3. Proxy Route Registration

In `route_specs.py`, add:

```python
KIRO_HANDLER_ROUTES: tuple[ProviderHandlerRoute, ...] = (
    ProviderHandlerRoute("POST", "/", "handle_kiro_runtime"),
)
```

But this is tricky — kiro POSTs to `/` on `runtime.us-east-1.kiro.dev`. Headroom's proxy typically works by path-based routing (e.g., `/v1/messages` → Anthropic handler). Kiro uses host-based routing with a bare `/` path.

**This requires a new routing mechanism in headroom's proxy:**

```python
# In proxy server routing logic:
if request.headers.get("host") == "runtime.us-east-1.kiro.dev":
    return await handle_kiro_runtime(request.body, request.headers)
```

### 4. TLS Interception

Unlike Claude/Codex (which point at `http://127.0.0.1:8787`), kiro needs the proxy to MITM `runtime.us-east-1.kiro.dev`. This means:

1. Headroom proxy generates a CA cert (it may already do this for Copilot subscription mode)
2. `SSL_CERT_FILE` points to a bundle with system roots + headroom's CA
3. Headroom proxy terminates TLS for `runtime.us-east-1.kiro.dev` and re-encrypts upstream

This is the most architecturally novel piece — headroom's proxy is currently a **forward proxy** (clients set base URLs to point at it). Kiro requires it to act as a **MITM proxy** for a specific host.

### 5. Install Registry Registration

In `install_registry.py`:

```python
from headroom.providers.kiro.install import build_install_env as _build_kiro_install_env

_ENV_BUILDERS["kiro"] = _build_kiro_install_env
```

### 6. Proxy Targets Registration

In `proxy_targets.py`:

```python
LEGACY_API_TARGET_ATTRS["kiro"] = "KIRO_RUNTIME_URL"
```

## Key Design Decision: Host-Based Routing

Headroom currently routes by **URL path** because all supported agents point their traffic at `http://127.0.0.1:8787/v1/...`. Kiro can't do that — it hardcodes `runtime.us-east-1.kiro.dev`.

**Options:**

### A. Add HTTPS_PROXY support to headroom's proxy (TLS MITM mode)

The proxy listens for `CONNECT` requests and selectively intercepts hosts it knows about (like kiro's runtime), passing everything else through as a CONNECT tunnel.

**Pros:** Clean separation. Kiro traffic gets compression; everything else is transparent.
**Cons:** Significant proxy architecture change. Currently headroom is a forward HTTP proxy, not a CONNECT-capable MITM.

### B. Use headroom proxy as an HTTPS_PROXY with a new `--intercept-host` flag

```bash
headroom proxy --port 8787 --intercept-host runtime.us-east-1.kiro.dev
```

Only the specified host gets TLS-terminated and compressed. All other CONNECT tunnels pass through unchanged.

**Pros:** Explicit, configurable, safe. Generalizable to future agents that hardcode endpoints.
**Cons:** Still requires MITM infra in headroom.

### C. Leverage headroom's existing `--backend bedrock` mode differently

Since kiro → runtime.kiro.dev → Bedrock → Claude, and headroom already has `--backend bedrock`, could we just... use Bedrock directly? No — kiro's runtime adds its own orchestration layer (tool management, session state). We can't bypass it.

**Recommended: B** — it's the most principled and generalizable. Other tools (VS Code extensions, IDE plugins) will have the same "can't change the base URL" problem.

## Prefix Stability Strategy

The critical insight: Bedrock uses prefix caching for Claude. If we compress old turns deterministically, the prefix of a 100-turn request will be byte-identical to the prefix of a 102-turn request (modulo the 2 new messages at the end).

**Rules for prefix stability:**

1. **Deterministic JSON serialization:** `json.dumps(obj, sort_keys=True, separators=(",",":"))`
2. **Position-based compression:** Whether a message gets compressed depends ONLY on its position relative to the end (is it in the "protected" zone?), not on absolute turn number.
3. **Idempotent compression:** Compressing an already-compressed message produces the same output.
4. **No metadata injection:** Don't add timestamps, request IDs, or anything non-deterministic to compressed content.

## Token Counting

For honest savings reporting, we need to count tokens on the KIRO format, not a translation:

```python
from headroom.tokenizers import count_tokens

tokens_before = count_tokens(original_body.decode(), model="claude-opus-4-6")
tokens_after = count_tokens(compressed_body.decode(), model="claude-opus-4-6")
```

This counts the actual bytes Bedrock will see, not a phantom Anthropic translation.

## Implementation Roadmap

| Phase | Deliverable | Effort |
|-------|------------|--------|
| 0 | File feature request issue on headroom repo | 30 min |
| 1 | Prototype `headroom/providers/kiro/` + handler locally | 1-2 days |
| 2 | Add CONNECT/TLS-intercept capability to headroom proxy | 2-3 days |
| 3 | Tests: prefix stability, compression ratio, fail-through | 1 day |
| 4 | Real behavior proof: run against captured 285-turn request | 1 day |
| 5 | PR submission + review cycle | 1-2 weeks |

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| Headroom maintainers reject the approach | Medium | High | File issue first, get buy-in before coding |
| MITM support is too invasive for headroom | Medium | High | Could be a plugin/extension instead of core |
| Kiro changes wire format | Low | Medium | Fail-through (forward unchanged on parse error) |
| TLS intercept adds latency | Low | Low | Same overhead as current proxy (~10-50ms) |
| Copilot subscription mode already has MITM infra | High (good!) | Positive | We can piggyback on existing TLS interception |

## Open Questions for Headroom Maintainers

1. Does headroom already have CONNECT/TLS-intercept support for Copilot subscription mode?
2. Would a `--intercept-host` flag be accepted, or should this be a plugin?
3. Is host-based routing (vs path-based) acceptable in the proxy core?
4. What's the preferred approach for agents with non-standard API formats?
5. Should the kiro format handler live in `headroom/providers/kiro/` or `headroom/proxy/handlers/`?

## Prior Art: Copilot Subscription Mode

From the README: "This lets Headroom intercept OpenAI-compatible Copilot CLI requests and apply the same proxy compression pipeline before forwarding to GitHub Copilot's hosted API."

This strongly suggests headroom already has some form of HTTPS interception. The Copilot subscription flow does token exchange and proxies to a hosted API — similar to what we need for kiro, except kiro's format isn't OpenAI-compatible.

## What We Built

All code lives in `~/repo/headroom/headroom/providers/kiro/`:

| File | Lines | Purpose |
|------|-------|---------|
| `__init__.py` | 12 | Exports |
| `install.py` | 49 | `build_install_env()` → HTTPS_PROXY + SSL_CERT_FILE |
| `runtime.py` | 67 | Host detection, version parsing, constants |
| `handler.py` | 214 | `compress_kiro_request()` — in-place compression |
| `connect_proxy.py` | 422 | Minimal CONNECT proxy with selective TLS interception |

Tests in `~/repo/headroom/tests/test_kiro_handler.py` (298 lines, 18 tests).

### Validated Results

- **49.7% payload reduction** on real 285-turn fixture (1.88 MB → 947 KB)
- **92/92 prefix messages byte-identical** across turns (Bedrock cache will hit)
- **Zero format translation** (operates directly on kiro wire format)
- **Fail-through on all error paths** (returns original bytes unchanged)
- **Deterministic JSON serialization** (sort_keys=True, separators)

### Architecture

```
headroom wrap kiro
    │
    ├── starts headroom proxy on :8787 (FastAPI, existing)
    ├── starts kiro CONNECT proxy on :9090 (connect_proxy.py, new)
    ├── launches kiro-cli with:
    │     HTTPS_PROXY=http://127.0.0.1:9090
    │     SSL_CERT_FILE=~/.headroom/kiro-ca-bundle.pem
    │
    ▼
kiro-cli → CONNECT runtime.us-east-1.kiro.dev:443
    │
    ├── connect_proxy.py intercepts (host match)
    │     1. Terminates TLS (presents generated cert)
    │     2. Reads HTTP POST body
    │     3. compress_kiro_request(body) → compressed body
    │     4. Forwards to real runtime.us-east-1.kiro.dev
    │     5. Streams response back to client
    │
    └── Other hosts → transparent CONNECT tunnel (no interception)
```

## What This Replaces

Once `headroom wrap kiro` works, the entire kiro-proxy repo becomes unnecessary:
- No more mitmproxy dependency (replaced by 422-line connect_proxy.py)
- No more kiro_translator.py (format translation eliminated)
- No more custom LaunchAgent/plist management
- No more custom CA cert generation scripts
- No more inflated stats

Users get:
- `headroom wrap kiro` → everything works
- `headroom unwrap kiro` → clean removal
- `headroom dashboard` → real-time savings (honest numbers)
- `headroom doctor` → health verification
- Fail-through on proxy hang (asyncio timeouts)
- Cross-agent memory (if using claude + kiro)

## Remaining Work

1. TLS cert generation on first `headroom wrap kiro` (generate CA + host cert for `runtime.us-east-1.kiro.dev`, build CA bundle)
2. Wire `connect_proxy.py` into headroom's `wrap` CLI command (start it alongside the main proxy)
3. Register kiro in `install_registry.py`
4. End-to-end test against a live kiro session
5. Streaming response support (current `_forward_to_upstream` buffers; needs chunked/streaming for long model responses)
