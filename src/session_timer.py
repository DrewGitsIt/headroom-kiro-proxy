"""Per-session cache TTL tracker.

Tracks when each conversationId was last seen, enabling the proxy to
decide whether the provider's prompt cache is likely warm or cold.

Design rationale (from cache-investigation-2026-07-16.md):
- Bedrock prompt caching has a 5-minute TTL (extended on hit).
- If the cache is warm (last request < 5 min ago), we must NOT modify
  the prefix — any byte change invalidates the cache.
- If the cache is cold (≥ 5 min, or first request), we can compress
  freely since there's no cached prefix to break.
"""

from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger("kiro_proxy.session_timer")

# Default TTL matches Bedrock's default prompt cache expiry.
DEFAULT_TTL_SECONDS = int(os.environ.get("KIRO_PROXY_CACHE_TTL", "300"))


class SessionTimer:
    """Track last-seen timestamps per conversationId.

    Thread-safe for single-threaded asyncio use (no concurrent mutation).
    """

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self._ttl = ttl_seconds
        self._last_seen: dict[str, float] = {}

    @property
    def ttl(self) -> int:
        return self._ttl

    def is_cache_warm(self, conversation_id: str) -> bool:
        """True if < TTL seconds since last request for this session.

        A warm cache means the provider likely has a cached prefix for this
        conversation — we must pass through byte-faithfully.
        """
        last = self._last_seen.get(conversation_id)
        if last is None:
            return False
        return (time.monotonic() - last) < self._ttl

    def is_cache_cold(self, conversation_id: str) -> bool:
        """True if ≥ TTL seconds since last request, or never seen.

        A cold cache means there's no cached prefix to break — compress freely.
        """
        return not self.is_cache_warm(conversation_id)

    def touch(self, conversation_id: str) -> None:
        """Record that we just processed a request for this session."""
        self._last_seen[conversation_id] = time.monotonic()
        # Evict abandoned sessions to prevent unbounded memory growth.
        # A session idle for 10x TTL (50 min by default) is certainly dead.
        self._evict_stale()

    def _evict_stale(self) -> None:
        """Remove sessions idle for > 10x TTL."""
        cutoff = time.monotonic() - (self._ttl * 10)
        stale = [k for k, v in self._last_seen.items() if v < cutoff]
        for k in stale:
            del self._last_seen[k]
        if stale:
            logger.debug("Evicted %d stale session(s)", len(stale))

    def active_sessions(self) -> int:
        """Number of tracked sessions (for stats/debugging)."""
        return len(self._last_seen)

    def seconds_since_last(self, conversation_id: str) -> float | None:
        """Seconds since last request for a session, or None if never seen."""
        last = self._last_seen.get(conversation_id)
        if last is None:
            return None
        return time.monotonic() - last
