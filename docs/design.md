# Kiro Compression Proxy — Design

**Status:** Proposed  
**Date:** 2026-07-09  
**Context:** Validated via mitmproxy spike against a real 285-turn kiro-cli session (1.96 MB per request on the wire).

## Problem

Kiro-cli sends the full conversation history on every request to `runtime.us-east-1.kiro.dev`. A 285-turn session sends **1.96 MB per request** (~500K tokens). At Opus pricing ($3/M input tokens), that's **$1.50 per turn** in a long session. Most of that payload is stale tool results and old screenshots the model no longer needs verbatim.

## Measured payload composition (285-turn session)

| Component | Size | % | Action |
|-----------|------|---|--------|
| Tool results in history | 514 KB | 25% | Compress (SmartCrusher-style) |
| Base64 images (screenshots) | 716 KB | 35% | Strip from old turns |
| Text content (prompts + responses) | 157 KB | 8% | Compress old turns |
| Tool definitions (current msg) | 115 KB | 6% | Pass through (required) |
| JSON structure / other | 552 KB | 27% | Shrinks proportionally |

## Architecture

```
kiro-cli
    │ HTTPS_PROXY=http://127.0.0.1:9090
    ▼
┌──────────────────────────────────────┐
│  Kiro Compression Proxy (:9090)      │
│  (HTTPS MITM, trusted CA)            │
│                                      │
│  1. Intercept POST to runtime.kiro.dev│
│  2. Deserialize ConversationState    │
│  3. Compress history[] in place      │
│  4. Forward to real runtime.kiro.dev │
│  5. Stream response back unchanged   │
└──────────────────────────────────────┘
    │
    ▼
runtime.us-east-1.kiro.dev (AWS Q Developer → Bedrock → Claude)
```

## Wire protocol (from capture)

**Endpoint:** `POST https://runtime.us-east-1.kiro.dev/`

**Request body (JSON):**
```json
{
  "conversationState": {
    "conversationId": "uuid",
    "history": [
      {
        "userInputMessage": {
          "content": "text...",
          "origin": "KIRO_CLI",
          "images": [{"format": "png", "source": {"bytes": "base64..."}}],
          "userInputMessageContext": {
            "toolResults": [{"toolUseId": "...", "content": [{"text": "..."}]}],
            "envState": {...}
          }
        }
      },
      {
        "assistantResponseMessage": {
          "content": "text..."
        }
      }
    ],
    "currentMessage": {
      "userInputMessage": {
        "content": "current prompt",
        "userInputMessageContext": {
          "tools": [...],       // 115KB, 35 tool definitions
          "toolResults": [...], // current turn's tool results
          "envState": {...}
        }
      }
    }
  },
  "profileArn": "arn:aws:codewhisperer:..."
}
```

**Response:** Streaming JSON (assistantResponseEvent chunks). **Pass through unchanged.**

## Compression strategy

### Tier 1: Image stripping (easy, biggest win)

**Rule:** Remove `images` arrays from history messages older than N turns (default: 4).

**Replacement:** Replace with a text annotation in the `content` field:
```
[screenshot from turn 50 removed — still available in session file if needed]
```

**Estimated savings:** ~600 KB per request on this session (35% of payload).

**Why it's safe:** The model references screenshots by what was discussed in the surrounding text. By turn 285, a screenshot from turn 50 adds no information the model can act on — the conversation has long since moved past whatever was in that image.

### Tier 2: Tool result compression (moderate, proven approach)

**Rule:** Compress `toolResults` in `userInputMessageContext` for history messages older than N turns (default: 4).

**Compression approaches (in order of complexity):**

1. **Truncation with summary header** (simplest):
   ```
   [tool result truncated — originally 45KB]
   File: src/main.rs (2,400 lines)
   Key sections: main(), Config struct, handle_request()
   ```

2. **SmartCrusher-style structural compression** (port from headroom-ai):
   - JSON arrays → schema + sample row + count
   - File contents → imports + function signatures + key lines
   - Build/test output → errors only, strip passing tests

3. **Headroom-ai library directly** (if we solve the format translation):
   - Translate toolResults content → Anthropic message format
   - Run through headroom's compression pipeline
   - Translate back

**Estimated savings:** ~334 KB per request (19% of payload).

### Tier 3: Old assistant response compression (minor)

**Rule:** Compress assistant responses older than N turns that contain long code blocks or explanations the model has already acted on.

**Approach:** Truncate to first 500 chars + `[... N chars truncated]`.

**Estimated savings:** ~48 KB per request (2.4% of payload).

### Combined projection

| Tier | Savings | Effort | Per-request $ saved (Opus) |
|------|---------|--------|---------------------------|
| Images only | 600 KB (30%) | 1 day | $0.45 |
| Images + tool results | 934 KB (47%) | 3-5 days | $0.70 |
| All three | 982 KB (49%) | 1 week | $0.73 |

Over a 285-turn session: **$128–$208 saved** depending on tier.

## Implementation plan

### Phase 1: MITM proxy skeleton (1 day)

A Python script using `mitmproxy` as a library (programmatic mode):
- Intercept `POST https://runtime.us-east-1.kiro.dev/`
- Deserialize JSON body
- Apply compression to `conversationState.history`
- Re-serialize and forward
- Stream response unchanged
- Log before/after sizes

**Dependencies:** `mitmproxy` (already installed), Python 3.

**Setup:** System CA trust for mitmproxy cert + `HTTPS_PROXY` in shell profile.

### Phase 2: Image stripping (half day)

```python
def compress_history(history, protect_recent=4):
    for i, msg in enumerate(history[:-protect_recent]):
        if "userInputMessage" in msg:
            um = msg["userInputMessage"]
            if um.get("images"):
                turn_num = i // 2 + 1
                um["content"] += f"\n[screenshot from turn {turn_num} removed]"
                um["images"] = []
    return history
```

### Phase 3: Tool result compression (2-3 days)

```python
def compress_tool_results(msg, turn_index):
    ctx = msg.get("userInputMessage", {}).get("userInputMessageContext", {})
    results = ctx.get("toolResults", [])
    for result in results:
        for part in result.get("content", []):
            if "text" in part and len(part["text"]) > 500:
                original_len = len(part["text"])
                # Keep first 300 chars + structural summary
                summary = summarize_tool_result(part["text"])
                part["text"] = f"[compressed from {original_len} chars]\n{summary}"
    return msg
```

The `summarize_tool_result` function can start as simple truncation and graduate to headroom-ai's SmartCrusher if the format translation is worth it.

### Phase 4: Daemonize and polish (1 day)

- LaunchAgent plist for auto-start
- Shell profile export (`HTTPS_PROXY=...`)
- Stats logging (daily savings report)
- Graceful bypass when proxy is down (kiro falls through to direct)

## Open questions

1. **Does kiro validate request integrity?** If `runtime.kiro.dev` checks a hash/signature of the payload, modifying it in transit would break things. The mitmproxy spike worked (kiro got a 200 back), so this is likely fine — but need to verify with actual compressed payloads.

2. **Does removing old images degrade quality?** Needs A/B testing. Hypothesis: by turn 20, a screenshot from turn 5 contributes zero information. But this needs validation on real work.

3. **Token counting:** The proxy compresses chars, but the billing is in tokens. We should add tiktoken-based before/after measurement to track actual savings.

4. **mitmproxy library overhead:** Using mitmproxy programmatically adds TLS decrypt/re-encrypt latency. For a 2MB request, this is ~10-50ms — negligible vs the 5-6s model inference time.

5. ~~Cert trust management~~ **RESOLVED:** `SSL_CERT_FILE` provides per-process trust scoping. No system keychain modification needed. Narrower scope than any commercial proxy tool.

6. **CA bundle freshness:** If Apple adds/revokes system roots, the combined bundle goes stale. Proxy startup should regenerate it. Low urgency — root CA changes are rare.

## Alternative: No-MITM approach (spike-only, not for production)

The mitmproxy spike (Path B) validated the opportunity but has fatal UX flaws:
- If the proxy process dies, the kiro session is bricked with no recovery
- General-purpose MITM tooling buffers full request/response, adding failure modes
- No fail-through path

Path B is useful for **measurement only**. Production must use Path A.

A truly clean alternative would be a configurable endpoint in kiro itself (e.g. `KIRO_RUNTIME_URL` env var). This would eliminate the TLS interception requirement entirely. That's a kiro product feature request, not something we can build externally.

## Decision

Build a purpose-built compression proxy (Path A) with fail-through guarantee. The mitmproxy spike validated the opportunity but exposed a fatal UX flaw: if the proxy dies, the kiro session is bricked with no recovery. A production proxy must forward unchanged on any failure.

---

## Path A: Implementation Breakdown

### Component 1: MITM Proxy Core (Python, ~200 LOC)

A single-file Python HTTPS proxy that:
- Binds on `127.0.0.1:9090`
- Terminates TLS using a self-signed CA (same as mitmproxy's approach)
- Intercepts `POST https://runtime.us-east-1.kiro.dev/`
- All other traffic: tunnel unchanged (CONNECT passthrough)
- On the intercepted endpoint: read body → compress → forward → stream response back
- **Handles concurrent sessions:** async event loop (tokio-style via mitmproxy/asyncio) processes multiple requests in parallel; compression is CPU-bound but <100ms per request so no queuing under typical load (3-5 concurrent sessions)

**Fail-through contract:**
```python
async def handle_request(request):
    if not is_runtime_kiro_request(request):
        return await tunnel_unchanged(request)
    
    original_body = request.body
    try:
        compressed_body = compress_conversation(original_body)
    except Exception:
        compressed_body = original_body  # FAIL-THROUGH: forward unchanged
    
    return await forward_to_upstream(request, compressed_body)
```

No buffering of response — stream it byte-for-byte from upstream to client.

**Concurrency model:**
- mitmproxy's core is async (asyncio). Each connection is an independent coroutine.
- Multiple kiro sessions hitting the proxy simultaneously each get their own coroutine.
- Compression is stateless — no shared mutable state between requests. Each request carries its full history; the proxy compresses based on position alone.
- No per-conversation tracking needed. The `conversationId` in the payload is irrelevant to the proxy; it just shortens old turns.

**Port binding:**
- Primary port: 9090
- If bind fails: log error, retry every 5s (launchd keepalive handles the restart)
- Future: port fallback range like headroom (9090-9100), with the alias reading a port file

**Key library choice:** `mitmproxy` in programmatic mode (not CLI) gives us:
- TLS termination with auto-generated certs per-host
- Async connection handling (multiple concurrent sessions)
- Connection pooling to upstream
- Streaming response support
- Battle-tested HTTPS proxy implementation

Alternative: raw `asyncio` + `ssl` + `hyper` if we want zero-dep. More work but smaller footprint.

### Component 2: Compression Engine (~150 LOC)

```python
def compress_conversation(body: bytes) -> bytes:
    req = json.loads(body)
    
    if "conversationState" not in req:
        return body  # Not a chat request, pass through
    
    history = req["conversationState"]["history"]
    protect_recent = 4  # Last 4 turns are sacred
    
    compressed_history = []
    for i, msg in enumerate(history):
        if i >= len(history) - protect_recent:
            compressed_history.append(msg)  # Recent: untouched
        else:
            compressed_history.append(compress_turn(msg, i))
    
    req["conversationState"]["history"] = compressed_history
    return json.dumps(req).encode()
```

**Compression rules (applied to old turns only):**

| Content type | Rule | Expected savings |
|---|---|---|
| `images` array | Strip entirely, add text marker | ~600 KB/session |
| `toolResults` > 2KB | Truncate to 500 chars + summary header | ~334 KB/session |
| `assistantResponseMessage` > 5KB | Keep first 1000 chars + `[truncated]` | ~48 KB/session |
| `content` (user text) | Pass through unchanged | 0 |

### Component 3: CA Certificate Management (per-process, no system trust)

Kiro-cli uses `rustls-native-certs` which checks `SSL_CERT_FILE` before the
macOS system keychain. This means we can scope CA trust to the kiro-cli process
only — no system keychain modification, no admin password, no impact on browsers
or other tools.

**On install (one-time):**
1. Generate a root CA key + cert:
   ```bash
   mkdir -p ~/.kiro-proxy
   openssl req -x509 -newkey rsa:2048 -keyout ~/.kiro-proxy/ca.key \
     -out ~/.kiro-proxy/ca.pem -days 3650 -nodes \
     -subj "/CN=Kiro Compression Proxy CA"
   chmod 600 ~/.kiro-proxy/ca.key
   ```
2. Build a combined CA bundle (system roots + proxy CA):
   ```bash
   security export -t certs -f pemseq \
     -k /System/Library/Keychains/SystemRootCertificates.keychain \
     -o ~/.kiro-proxy/system-roots.pem
   cat ~/.kiro-proxy/system-roots.pem ~/.kiro-proxy/ca.pem \
     > ~/.kiro-proxy/ca-bundle.pem
   ```
3. The proxy uses `ca.key` + `ca.pem` to generate per-host certs at runtime.

**On uninstall:**
1. Remove the alias from shell profile.
2. `rm -rf ~/.kiro-proxy/`
3. Done. No keychain entries to clean up.

**Security model:**
- The CA is **only trusted by processes that have `SSL_CERT_FILE` set** — i.e., kiro-cli launched via the alias.
- Browsers, curl, git, other CLI tools: **completely unaffected**.
- Private key never leaves `~/.kiro-proxy/ca.key` (chmod 600).
- No admin privileges required for any step.
- Strictly narrower scope than Charles Proxy, Proxyman, or corporate TLS inspection (all of which require system keychain trust).

**Maintenance:** If Apple updates system root CAs, regenerate the bundle:
```bash
# Add to proxy startup script:
security export -t certs -f pemseq \
  -k /System/Library/Keychains/SystemRootCertificates.keychain \
  -o ~/.kiro-proxy/system-roots.pem
cat ~/.kiro-proxy/system-roots.pem ~/.kiro-proxy/ca.pem \
  > ~/.kiro-proxy/ca-bundle.pem
```

### Component 4: Process Management (launchd)

**Plist:** `~/Library/LaunchAgents/com.kiro-proxy.compression.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "...">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.kiro-proxy.compression</string>
    <key>ProgramArguments</key>
    <array>
        <string>/path/to/python3</string>
        <string>/path/to/kiro-proxy.py</string>
    </array>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>~/.kiro-proxy/logs/proxy.log</string>
    <key>StandardErrorPath</key>
    <string>~/.kiro-proxy/logs/proxy.err</string>
</dict>
</plist>
```

**KeepAlive: true** means launchd restarts it within ~1s if it crashes. Kiro's next request hits a live proxy.

### Component 5: Shell Integration

Add to `.zshrc` / `.zprofile`:
```bash
# >>> kiro-proxy >>>
alias kiro-cli='SSL_CERT_FILE=~/.kiro-proxy/ca-bundle.pem HTTPS_PROXY=http://127.0.0.1:9090 kiro-cli'
# <<< kiro-proxy <<<
```

This scopes both the proxy routing AND the CA trust to kiro-cli only.
No other process in the shell is affected. No system-wide proxy, no global
cert trust.

**Reversibility:** Remove the two comment-fenced lines. That's it.

### Component 6: Stats & Monitoring (~50 LOC)

Local stats endpoint at `http://127.0.0.1:9090/stats`:
```json
{
  "requests_total": 47,
  "requests_compressed": 43,
  "bytes_before": 82400000,
  "bytes_after": 45200000,
  "savings_percent": 45.1,
  "images_stripped": 312,
  "tool_results_compressed": 891,
  "last_request_at": "2026-07-09T14:30:00Z",
  "errors_fallen_through": 2
}
```

---

## Implementation Sequence

| Step | What | Effort | Deliverable |
|------|------|--------|-------------|
| 1 | Proxy skeleton: intercept + forward unchanged | 2 hrs | Traffic flows through proxy with zero behavior change |
| 2 | Image stripping on old turns | 2 hrs | 30% payload reduction, verify kiro still works |
| 3 | Tool result truncation | 4 hrs | Additional 19% reduction |
| 4 | CA cert generation + trust automation | 1 hr | One-command install |
| 5 | launchd plist + keepalive | 1 hr | Auto-start, crash recovery |
| 6 | Shell alias integration | 30 min | Scoped to kiro only |
| 7 | Stats endpoint + logging | 2 hrs | Visibility into savings |
| 8 | Validation: run a full work session | 2 hrs | Confirm no degradation |

**Total: ~2 days to a working system.**

---

## Risk Mitigation

| Risk | Mitigation |
|------|-----------|
| Proxy crash kills session | launchd KeepAlive restarts in <1s; kiro retries failed TCP |
| Compression breaks model quality | `protect_recent=4` keeps active context intact; only old turns compressed |
| Modified payload rejected by server | Fail-through: any exception → forward original bytes |
| CA private key on disk | `chmod 600`; excluded from backups; only affects processes with `SSL_CERT_FILE` set |
| CA trust leaks to other tools | Impossible — scoped via per-process `SSL_CERT_FILE` env var in alias |
| HTTPS_PROXY leaks to other tools | Impossible — scoped via per-process env var in alias |
| Upstream changes wire format | JSON parse failure → fail-through (forward unchanged) |
| Added latency | JSON parse + compress on 2MB ≈ 50-100ms; negligible vs 6s model inference |
| Apple updates system CAs | Proxy startup regenerates `ca-bundle.pem` from current system roots |

---

## DevEx: Multi-session, zero-friction

**Goal:** Every `kiro-cli` invocation on this machine routes through the proxy automatically. No per-session setup, no remembering env vars, no manual proxy management.

**How it works from the user's perspective:**
1. Run the install script once (generates CA, installs launchd plist, adds alias)
2. Open N terminal tabs, run `kiro-cli chat` in each
3. All sessions route through the compression proxy transparently
4. If the proxy crashes: launchd restarts it in <1s, next request succeeds
5. If the proxy is stopped: `kiro-cli` fails to connect to `127.0.0.1:9090`, gets TCP RST, then... **problem.**

**The remaining fail-open gap:**

Unlike headroom's intercept layer (which owns the port and can forward direct when the backend is down), our design has the proxy as the *only* path. If the proxy process is absent (not crashed-and-restarting, but deliberately stopped or uninstalled without removing the alias), kiro-cli gets a connection refused and fails.

**Solutions (pick one):**

| Approach | Behavior when proxy is down | Complexity |
|----------|---------------------------|-----------|
| **A. launchd KeepAlive (chosen)** | Proxy is always running. Crash → restart in <1s. Deliberate stop = user's intent. | Low |
| **B. Wrapper script instead of alias** | Script checks if proxy is up; if not, runs kiro-cli without HTTPS_PROXY/SSL_CERT_FILE | Medium |
| **C. Both** | Wrapper script as belt, launchd as suspenders | Medium |

**Recommended: C (both).** The wrapper costs 5 lines and eliminates the edge case:

```bash
#!/bin/bash
# ~/.kiro-proxy/kiro-wrapper.sh
if curl -s --max-time 0.2 http://127.0.0.1:9090/health > /dev/null 2>&1; then
  exec env SSL_CERT_FILE=~/.kiro-proxy/ca-bundle.pem \
           HTTPS_PROXY=http://127.0.0.1:9090 \
           /usr/local/bin/kiro-cli "$@"  # real binary path
else
  exec /usr/local/bin/kiro-cli "$@"  # direct, uncompressed
fi
```

Then the alias becomes:
```bash
alias kiro-cli='~/.kiro-proxy/kiro-wrapper.sh'
```

**Result:**
- Proxy up → compressed, saves tokens
- Proxy down → direct to AWS, uncompressed, session works fine
- User never notices either way unless they check stats

---

## Success Criteria

1. A 285-turn session that previously sent 1.96 MB/request sends ≤1.3 MB/request (35%+ reduction)
2. Zero session failures caused by the proxy over a 1-week trial
3. Model response quality is subjectively identical (no confusion about missing context)
4. Proxy adds <200ms latency to request path
5. `fail_through` counter in stats stays near zero (compression logic is stable)

## Validation: Adverse Effects Testing (2026-07-09)

Global `HTTPS_PROXY` with `--allow-hosts "runtime\.us-east-1\.kiro\.dev"` was tested
against all common network-dependent engineering workflows. The proxy CONNECT-tunnels
all non-kiro HTTPS traffic (transparent byte pipe, no TLS interception, no cert
involvement).

### Automated test results

| Tool | Operation | Proxy overhead | Status |
|------|-----------|---------------|--------|
| git | ls-remote github.com | +1ms | ✅ |
| git | ls-remote gitlab.com | +33ms | ✅ |
| git | fetch (existing repo) | +33ms | ✅ |
| curl | GitHub API | +7ms | ✅ |
| curl | GitLab API | +46ms | ✅ |
| curl | Jira API | +48ms | ✅ |
| curl | Google APIs | +17ms | ✅ |
| curl | Google OAuth | +55ms | ✅ |
| curl | Google Docs API | +55ms | ✅ |
| curl | VS Code Marketplace | +37ms | ✅ |
| gh CLI | api /user | +53ms | ✅ |
| glab CLI | api /user | +103ms | ✅ |
| npm | view (registry fetch) | Noise (cache-dominated) | ✅ |
| brew | update | +150ms | ✅ |
| pip | index versions | ~500ms first, then negligible | ✅ |
| SSH | git@github.com | Unaffected (not proxied) | ✅ |
| AWS CLI | sts get-caller-identity | No behavioral change | ✅ |

**Methodology:** Each operation run with and without proxy, multiple samples, cache
cleared between runs where applicable. "Overhead" is the median additional latency
attributable to the CONNECT tunnel through the proxy.

### Manual validation (user-confirmed)

| Activity | Duration | Issues |
|----------|----------|--------|
| VPN connection (corporate) | Normal | None |
| Jira ticket management (CRUD) | Normal | None |
| YouTube streaming | Normal | None |
| Spotify streaming | Normal | None |
| kiro-cli chat (terminal) | Normal + compressed | None |
| kiro-cli acp (tao-initiated) | Normal + compressed | None |
| Kiro IDE sessions | Normal + compressed | None |

### Conclusion

- **Zero failures** across all tested tools and services
- **Zero certificate errors** (non-kiro traffic never sees the proxy CA)
- **5-100ms typical overhead** per HTTPS connection (CONNECT tunnel cost)
- **No behavioral changes** to any tool or service
- **Streaming services unaffected** (they use system proxy settings / NSURLSession, not env vars; and even if routed through proxy, CONNECT tunnel is transparent)
- Initial npm/brew "slowdowns" were measurement artifacts from cache invalidation, not proxy overhead
