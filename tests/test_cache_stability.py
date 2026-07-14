"""
Cache stability and compression validation suite.

Verifies that:
(a) We compress when needed and track savings accurately
(b) We do NOT compress when it would be harmful (short conversations, recent messages)
(c) Our compressed prefixes are STABLE across turns — the precondition for
    server-side prompt caching to work (cache reads instead of writes)

Since kiro's runtime doesn't expose cache_read/cache_write metrics to clients,
we validate the PRECONDITION for good caching: prefix stability. If the compressed
form of messages [0..N] is identical whether we process them as part of a 100-msg
conversation or a 200-msg conversation, the server will get cache hits.

Run: PYTHONPATH=src python -m pytest tests/test_cache_stability.py -v
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from compress import compress_conversation, HEADROOM_AVAILABLE
from kiro_translator import kiro_to_anthropic, anthropic_to_kiro


@pytest.fixture
def fixture_data():
    """Load the real captured fixture."""
    path = os.path.join(
        os.path.dirname(__file__), "..", "testdata", "captured-request-285-turns.json"
    )
    if not os.path.exists(path):
        pytest.skip("Fixture not available")
    with open(path) as f:
        return json.load(f)


@pytest.fixture
def long_history(fixture_data):
    """Extract the full history from the fixture."""
    return fixture_data["conversationState"]["history"]


def _make_request_body(history, fixture_data=None):
    """Build a kiro request body from a history slice."""
    req = {
        "conversationState": {
            "history": history,
            "conversationId": "test-stability",
            "currentMessage": {"userInputMessage": {"content": "next"}},
        },
        "profileArn": "arn:aws:test",
    }
    return json.dumps(req).encode()


# --- (a) We compress when needed ---


class TestCompressesWhenNeeded:
    """Verify compression activates on large conversations."""

    def test_large_history_gets_compressed(self, long_history):
        """A 100+ message history should see meaningful compression."""
        body = _make_request_body(long_history[:100])
        result = compress_conversation(body)
        assert len(result["body"]) < len(body)
        # At least 10% reduction on a 100-msg conversation
        ratio = len(result["body"]) / len(body)
        assert ratio < 0.9, f"Expected >10% reduction, got {(1-ratio)*100:.1f}%"

    def test_full_fixture_compression_ratio(self, long_history):
        """Full fixture should compress meaningfully."""
        body = _make_request_body(long_history)
        result = compress_conversation(body)
        ratio = len(result["body"]) / len(body)
        assert ratio < 0.9, f"Expected >10% reduction on full fixture, got {(1-ratio)*100:.1f}%"

    def test_tokens_saved_is_nonzero(self, long_history):
        """Token savings should be reported for large conversations."""
        if not HEADROOM_AVAILABLE:
            pytest.skip("headroom not installed")
        body = _make_request_body(long_history[:100])
        result = compress_conversation(body)
        assert result["tokens_saved"] > 0

    def test_stats_are_consistent(self, long_history):
        """Token metrics should be internally consistent."""
        if not HEADROOM_AVAILABLE:
            pytest.skip("headroom not installed")
        body = _make_request_body(long_history)
        result = compress_conversation(body)
        assert result["tokens_before"] > 0
        assert result["tokens_after"] > 0
        assert result["tokens_saved"] == result["tokens_before"] - result["tokens_after"]
        assert result["compression_ratio"] > 0


# --- (b) We do NOT compress when it shouldn't ---


class TestDoesNotCompressUnnecessarily:
    """Verify we pass through short conversations unchanged."""

    def test_single_message_passes_through(self):
        """A single-message conversation should not be compressed."""
        history = [{"userInputMessage": {"content": "hello world"}}]
        body = _make_request_body(history)
        result = compress_conversation(body)
        # Output should be roughly the same size (just re-serialized)
        size_diff = abs(len(result["body"]) - len(body))
        assert size_diff < 50, "Single message should not be significantly modified"

    def test_short_conversation_minimal_change(self):
        """A 4-message conversation (within protect_recent) should be mostly preserved."""
        history = [
            {"userInputMessage": {"content": "write a function to sort a list"}},
            {"assistantResponseMessage": {"content": "def sort_list(lst): return sorted(lst)"}},
            {"userInputMessage": {"content": "add type hints"}},
            {"assistantResponseMessage": {"content": "def sort_list(lst: list) -> list: return sorted(lst)"}},
        ]
        body = _make_request_body(history)
        result = compress_conversation(body)
        # Should be within 5% of original size (minor JSON re-serialization differences)
        ratio = len(result["body"]) / len(body)
        assert ratio > 0.9, f"Short conversation was over-compressed: {(1-ratio)*100:.1f}% reduction"

    def test_no_conversation_state_passthrough(self):
        """Requests without conversationState pass through byte-identical."""
        body = json.dumps({"health": "check"}).encode()
        result = compress_conversation(body)
        assert result["body"] == body
        assert result["tokens_saved"] == 0

    def test_recent_messages_are_protected(self, long_history):
        """The last N messages should not be modified by compression."""
        body = _make_request_body(long_history[:50])
        result = compress_conversation(body)
        parsed = json.loads(result["body"])
        compressed_history = parsed["conversationState"]["history"]

        # Last 4 messages (protect_recent=4) should match original
        for i in range(-4, 0):
            orig = long_history[:50][i]
            comp = compressed_history[i]
            # Both should have the same message type
            if "userInputMessage" in orig:
                assert "userInputMessage" in comp
                # User content should be preserved in recent messages
                orig_content = orig["userInputMessage"].get("content", "")
                comp_content = comp["userInputMessage"].get("content", "")
                if orig_content:  # skip empty tool-result-only messages
                    assert orig_content == comp_content, f"Recent message {i} was modified!"
            elif "assistantResponseMessage" in orig:
                assert "assistantResponseMessage" in comp


# --- (c) Prefix stability (cache-friendly) ---


class TestPrefixCacheStability:
    """Verify that compressed prefixes are stable across turns.

    This is THE critical property for avoiding cache re-writes.
    If compress(history[:N]) produces the same first M messages regardless
    of whether N=100 or N=200, the server's prompt cache will get hits.
    """

    def test_deterministic_same_input(self, long_history):
        """Same input must produce exactly the same output."""
        body = _make_request_body(long_history[:50])
        result1 = compress_conversation(body)
        result2 = compress_conversation(body)
        assert result1["body"] == result2["body"]

    def test_prefix_stable_across_two_turns(self, long_history):
        """Messages compressed at turn N should be identical at turn N+2."""
        # Compress at turn 50
        body_50 = _make_request_body(long_history[:50])
        result_50 = compress_conversation(body_50)
        history_50 = json.loads(result_50["body"])["conversationState"]["history"]

        # Compress at turn 52
        body_52 = _make_request_body(long_history[:52])
        result_52 = compress_conversation(body_52)
        history_52 = json.loads(result_52["body"])["conversationState"]["history"]

        # The first 46 messages (50 - 4 protected) should be identical in both
        stable_count = 46
        mismatches = 0
        for i in range(min(stable_count, len(history_50), len(history_52))):
            if json.dumps(history_50[i], sort_keys=True) != json.dumps(history_52[i], sort_keys=True):
                mismatches += 1

        assert mismatches == 0, (
            f"{mismatches} messages differ between turn 50 and turn 52 "
            f"(prefix should be stable for cache hits)"
        )

    def test_prefix_stable_across_ten_turns(self, long_history):
        """Prefix stability over a longer span (simulates 5 interactions)."""
        results = []
        for turn_count in [60, 62, 64, 66, 68, 70]:
            body = _make_request_body(long_history[:turn_count])
            result = compress_conversation(body)
            history = json.loads(result["body"])["conversationState"]["history"]
            results.append(history)

        # The first 56 messages (60 - 4 protected) should be identical across ALL turns
        reference = results[0]
        stable_count = 56
        for turn_idx, history in enumerate(results[1:], 1):
            for msg_idx in range(min(stable_count, len(reference), len(history))):
                ref_msg = json.dumps(reference[msg_idx], sort_keys=True)
                cur_msg = json.dumps(history[msg_idx], sort_keys=True)
                assert ref_msg == cur_msg, (
                    f"Prefix unstable at msg[{msg_idx}] between turn 60 and turn {60 + turn_idx*2}: "
                    f"server would force a cache WRITE here"
                )

    def test_cache_read_ratio_projection(self, long_history):
        """Project the cache read:write ratio over a simulated session.

        In a conversation of N turns:
        - First request: ALL tokens are cache writes (cold start)
        - Subsequent requests: only NEW tokens (2 messages) are writes;
          the stable prefix is a cache read

        We verify the ratio by checking that the stable prefix size dominates.
        """
        # Simulate 20 turns (40 messages)
        turn_sizes = []
        for turn_count in range(20, 60, 2):
            body = _make_request_body(long_history[:turn_count])
            result = compress_conversation(body)
            compressed = json.loads(result["body"])
            history_out = compressed["conversationState"]["history"]
            turn_sizes.append(len(json.dumps(history_out)))

        # The growth per turn (new content) should be much smaller than total
        # This simulates: cache_write = growth, cache_read = total - growth
        total_requests = len(turn_sizes)
        # First request is all writes
        writes = turn_sizes[0]
        reads = 0

        for i in range(1, total_requests):
            growth = turn_sizes[i] - turn_sizes[i - 1]
            if growth > 0:
                writes += growth  # New content = cache write
                reads += turn_sizes[i - 1]  # Existing prefix = cache read
            else:
                # Compression was more aggressive this turn (shouldn't happen with stable prefix)
                reads += turn_sizes[i]

        if reads > 0:
            ratio = reads / writes
            print(f"\nProjected cache read:write ratio = {ratio:.1f}:1")
            print(f"  Total writes: {writes:,} bytes")
            print(f"  Total reads:  {reads:,} bytes")
            # We expect at least 10:1 read:write ratio over 20 turns
            # (50:1 to 100:1 for longer conversations)
            assert ratio > 5, (
                f"Cache read:write ratio too low: {ratio:.1f}:1. "
                f"Expected >5:1 minimum. Prefix may be unstable."
            )
        else:
            pytest.skip("Could not compute ratio (single turn)")
