# Kiro Prompt Caching Investigation & Headroom Compression Strategy

**Date:** 2026-07-16  
**Authors:** Drew Kidwell, with engineering assistance  
**Status:** Active — findings inform proxy compression strategy

---

## 1. Problem Statement

### Kiro does not use Bedrock prompt caching for input tokens

Kiro's documentation states: *"Kiro applies provider-level efficiencies, such as token-efficient tool use and prompt caching when available, to cut underlying token spend."*

Our CloudWatch investigation shows this claim does not match observed behavior. After controlled testing across all kiro-supported models, we found **zero evidence of Bedrock prompt caching being used** by kiro's runtime.

### Evidence

| Model | InputTokenCount (test window) | CacheWriteInputTokenCount | CacheReadInputTokenCount |
|-------|------------------------------|---------------------------|--------------------------|
| claude-haiku-4.5 | 20,346 ✓ (our test visible) | 0 | 0 |
| claude-sonnet-4.6 | 48,931,851 (account-wide) | 0 | 0 |
| claude-opus-4.6 | present | 0 (last 3 days) | 0 (last 3 days) |
| claude-opus-4.8 | present | 10.7M (Jul 14-15) | present |

The only model showing cache activity is **opus-4.8**, which is NOT in kiro's model list. That traffic comes from other users on the account calling Bedrock directly (likely via Claude Code with `CLAUDE_CODE_USE_BEDROCK=1`).

### Verification methodology

We confirmed `InputTokenCount` and `CacheWriteInputTokenCount` come from the **same CloudWatch instrumentation layer**. In a 5-minute window (14:35-14:40 UTC-4 on 2026-07-16), our kiro test sent 20,346 input tokens that appeared in `InputTokenCount` but produced zero `CacheWriteInputTokenCount` datapoints. If kiro's runtime were sending `cache_control` markers to Bedrock, cache writes would appear in the same bucket.

### Possible explanations

1. Kiro uses an internal caching layer (not Bedrock's prompt caching) — e.g., a server-side KV cache managed by the Q Developer runtime
2. "When available" in their docs means the feature is not yet enabled for all models/plans
3. Kiro caches at the orchestration layer before requests reach Bedrock (wouldn't emit Bedrock metrics)
4. Prompt caching is only used in certain conditions we haven't triggered (e.g., specific payload sizes, specific conversation structures)

### Implications for our proxy

- **Prefix stability** (deterministic JSON serialization) provides no Bedrock cache benefit today because Bedrock caching isn't being requested
- **Payload compression** (stripping old images, truncating tool results) provides direct value by reducing raw input token count regardless of caching
- **If kiro enables Bedrock caching in the future**, our deterministic serialization will be ready — but we should not design around an assumption it's active

---

## 2. Test Methodology

### Setup

- **Account:** ai-platform-dev (111452723372)
- **Region:** us-east-1
- **Profile:** SSO with AdministratorAccess
- **CloudWatch namespace:** AWS/Bedrock
- **Proxy:** headroom kiro connect proxy on 127.0.0.1:9090

### Protocol

For each model under test:

1. **Baseline:** Query CloudWatch `CacheWriteInputTokenCount` and `CacheReadInputTokenCount` for the model (last 10 minutes, 1-minute resolution)
2. **Message 1 (large):** Send ~20,000 tokens of filler text through `kiro-cli chat --model <model>`. If caching is active, this should produce a cache WRITE.
3. **Message 2 (small follow-up):** Send a short question in the same session. Kiro resends the full conversation history (including the 20K token message 1) as prefix. If caching is active, the prefix should produce a cache READ.
4. **Wait:** 90 seconds for CloudWatch propagation
5. **Measure:** Query CloudWatch again (last 5 minutes) and compute delta

### Results

Script: `~/repo/headroom/tests/run_cache_test.py`

```
$ python tests/run_cache_test.py --model claude-haiku-4.5
  ✗ NO CACHING — 0 cache writes, 0 cache reads, 20,552 input tokens

$ python tests/run_cache_test.py --model claude-sonnet-4.6
  ✗ NO CACHING — 0 cache writes, 0 cache reads, 48,931,851 input tokens (account-wide noise)
```

### Control verification

To confirm the metrics are from the same source:
- Queried `InputTokenCount` with 5-minute resolution for haiku during our test
- Our 20K token payload appeared clearly at 14:35 (20,346 tokens)
- In the same 5-minute bucket: CacheWrite = 0
- Conclusion: same instrumentation, different behavior — kiro simply doesn't request caching

---

## 3. Technical Approach: Headroom as Kiro Compression Provider

### Architecture

```
kiro-cli
  │ HTTPS_PROXY=http://127.0.0.1:9090
  │ SSL_CERT_FILE=~/.headroom/kiro-ca-bundle.pem
  ▼
┌──────────────────────────────────────────────────┐
│  Kiro Connect Proxy (:9090)                       │
│  (asyncio CONNECT proxy, TLS intercept)           │
│                                                   │
│  1. Accept CONNECT runtime.us-east-1.kiro.dev:443│
│  2. TLS handshake (present generated cert)        │
│  3. Read HTTP POST body                           │
│  4. compress_kiro_request(body)                   │
│     - Strip old images (> PROTECT_RECENT_ENTRIES) │
│     - SmartCrusher on old tool results            │
│     - Truncate old assistant responses            │
│     - Deterministic JSON serialization            │
│  5. Forward to real runtime.us-east-1.kiro.dev    │
│  6. Stream response back (chunked, Connection:    │
│     close forces clean EOF)                       │
│                                                   │
│  Non-kiro traffic: transparent CONNECT tunnel     │
└──────────────────────────────────────────────────┘
  │
  ▼
runtime.us-east-1.kiro.dev → Bedrock → Claude
```

### Key design decisions

1. **No format translation.** Previous approach translated kiro's wire format to Anthropic messages and back. This is eliminated — we operate directly on kiro's `conversationState.history` structure.

2. **Deterministic JSON serialization.** `json.dumps(req, sort_keys=True, separators=(",", ":"))` ensures the same prefix produces identical bytes across requests. If Bedrock caching is enabled in the future, this is the precondition for cache hits.

3. **Connection: close to upstream.** Kiro's runtime responds with `Transfer-Encoding: chunked` + `Connection: keep-alive`. Without forcing close, our proxy hangs waiting for EOF that never comes on a keep-alive connection.

4. **Position-based compression.** Messages are compressed based on their position relative to the end of history (outside `PROTECT_RECENT_ENTRIES`), not based on absolute turn number. This ensures compression is deterministic across growing conversations.

5. **Fail-through everywhere.** Any exception → forward original bytes unchanged. The proxy never blocks kiro.

### Validated results (285-turn captured fixture)

- 49.7% payload reduction (1.88 MB → 947 KB)
- 92/92 prefix messages byte-identical across turns (prefix stability verified)
- 18/18 unit tests passing

### Lifecycle

```bash
headroom kiro install    # One-time: certs, launchd, shell env, applet
headroom kiro status     # Health check + stats
headroom kiro stop       # Disable (applet toggle does same)
headroom kiro start      # Re-enable
headroom kiro uninstall  # Clean removal
```

### Current limitations

- Conversations under 8 messages are passed through uncompressed (too short for meaningful savings)
- SmartCrusher falls back to structural truncation when headroom's compression module isn't available
- Stats are per-proxy-lifetime (reset on restart, not persisted)

---

## 4. Adaptive Compression Strategy: Compress Outside Cache TTL

### Concept

Bedrock's prompt cache has a **5-minute TTL** (extended on hit). If a kiro session is idle for ≥5 minutes, the cache has expired — meaning the next request will be a full cache miss regardless. At that point, aggressive compression is "free" from a cache-busting perspective because there's no cache to bust.

### Proposed behavior

```
                     ← 5 min TTL →
Request N ──────────────────────────────── Request N+1

If gap < 5 min:
  → Lightweight compression only (strip images, truncate)
  → Preserve prefix byte-stability (for potential future cache hits)

If gap ≥ 5 min:
  → Full compression: SmartCrusher + neural token-level pruning
  → No need to preserve prefix stability (cache is cold anyway)
  → Maximum token reduction before the full-price cache write
```

### Why this matters

When the cache is cold, the next request pays full input token price on the ENTIRE conversation history. This is the worst case. Aggressively compressing here:
- Reduces the full-price token bill by 50-70%
- The cache write that follows (if kiro ever enables caching) is on the already-compressed content — cheaper write
- Subsequent cache reads (within the next 5 minutes) benefit from reading fewer tokens

### Implementation

```python
# In _intercept_kiro:

import time

_last_request_time: float = 0.0
CACHE_TTL_SECONDS = 300  # 5 minutes (Bedrock's published TTL)

def _select_compression_mode() -> str:
    """Decide compression aggressiveness based on cache state."""
    global _last_request_time
    now = time.time()
    gap = now - _last_request_time
    _last_request_time = now

    if _last_request_time == 0 or gap >= CACHE_TTL_SECONDS:
        return "aggressive"  # Cache is cold — compress maximally
    else:
        return "conservative"  # Cache may be warm — preserve prefix
```

**Conservative mode (gap < 5 min):**
- Strip old images (position-based, deterministic)
- Truncate tool results over threshold (deterministic)
- Deterministic JSON serialization
- Do NOT apply SmartCrusher or neural pruning (would change prefix bytes)

**Aggressive mode (gap ≥ 5 min):**
- All conservative transforms PLUS:
- SmartCrusher on all tool results (semantic compression, non-deterministic)
- Neural token-level pruning via Kompress-v2-base (if headroom ML extra installed)
- Old assistant response summarization
- Maximum compression regardless of prefix stability

### Metrics to track

- `compression_mode_conservative` / `compression_mode_aggressive` counts
- `tokens_saved_conservative` / `tokens_saved_aggressive` (should show aggressive >> conservative)
- Time-since-last-request histogram (understand typical session patterns)
- If kiro enables caching later: correlate mode with CacheRead metrics

### Risks

1. **TTL changes.** If Bedrock changes from 5 min, our threshold is wrong. Mitigate: make it configurable (`HEADROOM_CACHE_TTL_SECONDS` env var).
2. **Multiple sessions.** Each kiro session gets its own conversation. The proxy tracks time per-connection, not per-session. Since each request is a new connection through the proxy, we'd need to track by `conversationId` from the request body.
3. **Aggressive compression changes semantics.** If the model previously saw full content and now sees compressed content, it might behave differently. Mitigate: only compress messages outside the protected window (model's active working set).

### Timeline

- Phase 1 (now): SmartCrusher on every request (validate compression + proxy stability)
- Phase 2: Measure LLMLingua-2 incremental savings on top of SmartCrusher
  - Install `llmlingua` with mBERT variant in the headroom venv
  - Run SmartCrusher → LLMLingua-2 in sequence on captured kiro payloads
  - Measure: additional token savings, latency cost, RAM impact
  - **Decision gate:** Only proceed to integrate if LLMLingua-2 adds >15% savings beyond SmartCrusher on real kiro payloads. If marginal, skip it.
- Phase 3: Add 5-minute TTL gate (conservative vs aggressive mode selection)
- Phase 4: If Phase 2 passes the decision gate, integrate LLMLingua-2 as background pre-compression during idle periods (amortize latency to zero)

---

## Appendix: CloudWatch Queries

```bash
# Check cache activity for any model
aws cloudwatch get-metric-statistics \
  --namespace AWS/Bedrock \
  --metric-name CacheWriteInputTokenCount \
  --dimensions Name=ModelId,Value=us.anthropic.claude-haiku-4-5-20251001-v1:0 \
  --start-time 2026-07-16T00:00:00Z \
  --end-time 2026-07-16T23:59:00Z \
  --period 3600 --statistics Sum \
  --profile ai-platform-dev --region us-east-1

# List all models with cache metrics
aws cloudwatch list-metrics \
  --namespace AWS/Bedrock \
  --metric-name CacheReadInputTokenCount \
  --profile ai-platform-dev --region us-east-1
```
