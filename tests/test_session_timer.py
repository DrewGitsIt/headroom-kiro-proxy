"""
Tests for cache-safe compression: session timer and warm/cold bypass.

Verifies that:
(a) SessionTimer correctly tracks per-conversation TTL
(b) When cache is warm (< 5min), requests pass through byte-for-byte unchanged
(c) When cache is cold (≥ 5min or first request), compression happens normally
(d) The timer touch() is called after both paths, keeping the session alive
"""

import json
import time
from unittest.mock import patch

import pytest

from session_timer import SessionTimer
from handler import compress_kiro_request


# --- Helpers ---


def _make_request_body(history, conversation_id="test-conv-123"):
    """Build a minimal kiro request body with the given history."""
    req = {
        "conversationState": {
            "conversationId": conversation_id,
            "history": history,
        }
    }
    return json.dumps(req).encode()


def _make_compressible_history(num_turns=20):
    """Create a history that will definitely be compressed (images + long tool results)."""
    history = []
    for i in range(num_turns):
        # User message with an image (will be stripped outside protect window)
        history.append({
            "userInputMessage": {
                "content": f"Turn {i}: please analyze this",
                "images": [{"format": "png", "source": {"bytes": "A" * 5000}}],
            }
        })
        # Assistant with a long tool result (will be crushed)
        history.append({
            "assistantResponseMessage": {
                "content": "Here's what I found:\n" + ("x" * 2000),
                "toolUses": [
                    {
                        "toolUseId": f"tool_{i}",
                        "name": "read",
                        "input": {"path": f"/file{i}.txt"},
                    }
                ],
            }
        })
        # Tool result (large, compressible)
        history.append({
            "toolResultMessage": {
                "toolUseId": f"tool_{i}",
                "content": "x" * 10000,
                "status": "success",
            }
        })
    return history


# --- SessionTimer Unit Tests ---


class TestSessionTimer:
    """Unit tests for the SessionTimer class."""

    def test_new_session_is_cold(self):
        """A conversation we've never seen is cold."""
        timer = SessionTimer(ttl_seconds=300)
        assert timer.is_cache_cold("unknown-id")
        assert not timer.is_cache_warm("unknown-id")

    def test_recently_touched_is_warm(self):
        """A conversation touched just now is warm."""
        timer = SessionTimer(ttl_seconds=300)
        timer.touch("conv-1")
        assert timer.is_cache_warm("conv-1")
        assert not timer.is_cache_cold("conv-1")

    def test_expired_session_is_cold(self):
        """A conversation touched > TTL seconds ago is cold."""
        timer = SessionTimer(ttl_seconds=1)
        timer.touch("conv-1")
        # Simulate time passing
        with patch("session_timer.time.monotonic", return_value=time.monotonic() + 2):
            assert timer.is_cache_cold("conv-1")
            assert not timer.is_cache_warm("conv-1")

    def test_different_conversations_tracked_independently(self):
        """Each conversationId has its own timer."""
        timer = SessionTimer(ttl_seconds=300)
        timer.touch("conv-A")
        assert timer.is_cache_warm("conv-A")
        assert timer.is_cache_cold("conv-B")

    def test_touch_resets_timer(self):
        """Touching a session resets its TTL countdown."""
        timer = SessionTimer(ttl_seconds=2)
        timer.touch("conv-1")

        # After 1.5s it's still warm
        t1 = time.monotonic() + 1.5
        with patch("session_timer.time.monotonic", return_value=t1):
            assert timer.is_cache_warm("conv-1")

            # Touch again at 1.5s
            timer.touch("conv-1")

        # At 3s from original (1.5s from second touch), still warm
        t2 = t1 + 1.5
        with patch("session_timer.time.monotonic", return_value=t2):
            assert timer.is_cache_warm("conv-1")

    def test_seconds_since_last(self):
        """seconds_since_last returns elapsed time or None."""
        timer = SessionTimer(ttl_seconds=300)
        assert timer.seconds_since_last("conv-1") is None

        timer.touch("conv-1")
        elapsed = timer.seconds_since_last("conv-1")
        assert elapsed is not None
        assert elapsed < 1.0  # Just touched

    def test_active_sessions_count(self):
        """active_sessions returns the number of tracked conversations."""
        timer = SessionTimer(ttl_seconds=300)
        assert timer.active_sessions() == 0
        timer.touch("conv-1")
        assert timer.active_sessions() == 1
        timer.touch("conv-2")
        assert timer.active_sessions() == 2

    def test_stale_sessions_evicted(self):
        """Sessions idle for > 10x TTL are evicted on next touch."""
        timer = SessionTimer(ttl_seconds=1)
        timer.touch("stale-conv")

        # 11 seconds later (> 10x TTL=1), touching a new conv evicts stale
        future = time.monotonic() + 11
        with patch("session_timer.time.monotonic", return_value=future):
            timer.touch("new-conv")
            assert timer.active_sessions() == 1  # Only new-conv remains


# --- Handler Cache Bypass Tests ---


class TestHandlerCacheBypass:
    """Test that handler.compress_kiro_request respects session timer."""

    def test_first_request_compresses(self):
        """First request for a conversation (cold cache) is compressed."""
        timer = SessionTimer(ttl_seconds=300)
        history = _make_compressible_history(20)
        body = _make_request_body(history, "first-conv")

        compressed_body, stats = compress_kiro_request(body, session_timer=timer)

        # Should have compressed
        assert len(compressed_body) < len(body)
        assert stats["cache_bypass"] == 0
        assert stats["images_stripped"] > 0

    def test_second_request_bypasses(self):
        """Second request within TTL (warm cache) passes through unchanged."""
        timer = SessionTimer(ttl_seconds=300)
        history = _make_compressible_history(20)
        body = _make_request_body(history, "warm-conv")

        # First request — compresses and touches timer
        compress_kiro_request(body, session_timer=timer)

        # Second request — should pass through unchanged
        result_body, stats = compress_kiro_request(body, session_timer=timer)
        assert result_body == body  # Byte-for-byte identical
        assert stats["cache_bypass"] == 1
        assert stats["images_stripped"] == 0
        assert stats["tool_results_compressed"] == 0

    def test_request_after_ttl_compresses(self):
        """Request after TTL expires (cold cache) is compressed again."""
        timer = SessionTimer(ttl_seconds=1)
        history = _make_compressible_history(20)
        body = _make_request_body(history, "cold-conv")

        # First request — compresses
        compress_kiro_request(body, session_timer=timer)

        # Simulate TTL expiring
        future = time.monotonic() + 2
        with patch("session_timer.time.monotonic", return_value=future):
            compressed_body, stats = compress_kiro_request(body, session_timer=timer)

        assert len(compressed_body) < len(body)
        assert stats["cache_bypass"] == 0

    def test_no_timer_always_compresses(self):
        """Without a session timer, compression always happens (backward compat)."""
        history = _make_compressible_history(20)
        body = _make_request_body(history, "no-timer-conv")

        compressed_body, stats = compress_kiro_request(body, session_timer=None)

        assert len(compressed_body) < len(body)
        assert stats["cache_bypass"] == 0

    def test_different_conversations_independent(self):
        """Different conversationIds don't share cache warmth."""
        timer = SessionTimer(ttl_seconds=300)
        history = _make_compressible_history(20)

        body_a = _make_request_body(history, "conv-A")
        body_b = _make_request_body(history, "conv-B")

        # Touch conv-A
        compress_kiro_request(body_a, session_timer=timer)

        # conv-A is warm (bypass), conv-B is cold (compress)
        _, stats_a = compress_kiro_request(body_a, session_timer=timer)
        compressed_b, stats_b = compress_kiro_request(body_b, session_timer=timer)

        assert stats_a["cache_bypass"] == 1
        assert stats_b["cache_bypass"] == 0
        assert len(compressed_b) < len(body_b)

    def test_bypass_returns_exact_original_bytes(self):
        """Cache bypass returns the EXACT input bytes, not a re-serialization."""
        timer = SessionTimer(ttl_seconds=300)
        # Use a body with intentional formatting (spaces, key order)
        raw = json.dumps(
            {"conversationState": {"conversationId": "exact-test", "history": []}},
            indent=2,
        ).encode()

        # First call (cold) — note: empty history won't compress, just touches
        compress_kiro_request(raw, session_timer=timer)

        # Second call (warm) — must return exact bytes
        result, stats = compress_kiro_request(raw, session_timer=timer)
        assert result == raw
        assert stats["cache_bypass"] == 1

    def test_short_history_still_touches_timer(self):
        """Even conversations with short history (no compression needed)
        should register with the timer."""
        timer = SessionTimer(ttl_seconds=300)
        history = [{"userInputMessage": {"content": "hi"}}]
        body = _make_request_body(history, "short-conv")

        # First request — no compression (too short), but should touch timer
        compress_kiro_request(body, session_timer=timer)

        assert timer.is_cache_warm("short-conv")
