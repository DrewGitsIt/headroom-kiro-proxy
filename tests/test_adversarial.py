"""Negative and adversarial input tests.

Verifies that the compression pipeline handles malformed, unexpected, and
adversarial inputs gracefully — returning the original body unchanged
(fail-through) rather than crashing.
"""

import json
import pytest
from handler import compress_kiro_request


class TestMalformedJsonBody:
    """compress_kiro_request with non-JSON or broken JSON input."""

    def test_empty_bytes(self):
        """Empty body passes through unchanged."""
        body = b""
        result, stats = compress_kiro_request(body)
        assert result == body
        assert stats["images_stripped"] == 0

    def test_truncated_json(self):
        """Incomplete JSON (cut mid-object) passes through."""
        body = b'{"conversationState": {"history": [{"userInputMessage":'
        result, stats = compress_kiro_request(body)
        assert result == body

    def test_partial_utf8(self):
        """Body with incomplete UTF-8 sequence passes through."""
        # \xc3 is the start of a 2-byte UTF-8 sequence, missing second byte
        body = b'{"conversationState": "\xc3'
        result, stats = compress_kiro_request(body)
        assert result == body

    def test_binary_body_not_json(self):
        """Binary (non-JSON) body like protobuf passes through."""
        body = bytes(range(256))  # All byte values 0x00-0xFF
        result, stats = compress_kiro_request(body)
        assert result == body

    def test_null_bytes_in_body(self):
        """Body containing null bytes passes through."""
        body = b'\x00\x00\x00{"not": "json"}\x00'
        result, stats = compress_kiro_request(body)
        assert result == body

    def test_extremely_nested_json(self):
        """Deeply nested JSON (potential stack overflow) passes through or compresses."""
        # 100 levels deep — within Python's recursion limit, should just work
        inner = '{"a": ' * 100 + '"val"' + '}' * 100
        body = json.dumps({"conversationState": {"history": []}, "nested": json.loads(inner)}).encode()
        result, stats = compress_kiro_request(body)
        # Should succeed (no history to compress) and return valid JSON
        assert json.loads(result) is not None


class TestUnexpectedMessageTypes:
    """History entries with wrong types or missing expected keys."""

    def test_history_entry_is_string(self):
        """History entry that's a string instead of dict is skipped."""
        body = json.dumps({
            "conversationState": {
                "history": [
                    "this is not a dict",
                    {"userInputMessage": {"content": "real message"}},
                ] * 6  # >8 entries so some are in compression range
            }
        }).encode()
        result, stats = compress_kiro_request(body)
        # Should not crash — strings are skipped by the isinstance check
        assert json.loads(result) is not None

    def test_history_entry_is_integer(self):
        """History entry that's an integer is skipped."""
        body = json.dumps({
            "conversationState": {
                "history": [42, None, True, {"userInputMessage": {"content": "x"}}] * 4
            }
        }).encode()
        result, stats = compress_kiro_request(body)
        assert json.loads(result) is not None

    def test_history_entry_is_none(self):
        """None entries in history are skipped."""
        body = json.dumps({
            "conversationState": {
                "history": [None] * 20
            }
        }).encode()
        result, stats = compress_kiro_request(body)
        assert json.loads(result) is not None

    def test_user_message_content_is_not_string(self):
        """userInputMessage.content that's a list (not string) doesn't crash."""
        body = json.dumps({
            "conversationState": {
                "history": [
                    {"userInputMessage": {"content": ["array", "not", "string"], "images": []}},
                ] * 12
            }
        }).encode()
        result, stats = compress_kiro_request(body)
        assert json.loads(result) is not None

    def test_assistant_message_content_is_dict(self):
        """assistantResponseMessage.content that's a dict doesn't crash."""
        body = json.dumps({
            "conversationState": {
                "history": [
                    {"assistantResponseMessage": {"content": {"nested": "object"}}},
                ] * 12
            }
        }).encode()
        result, stats = compress_kiro_request(body)
        assert json.loads(result) is not None

    def test_both_user_and_assistant_keys(self):
        """Message with both userInputMessage AND assistantResponseMessage."""
        body = json.dumps({
            "conversationState": {
                "history": [
                    {
                        "userInputMessage": {"content": "hello"},
                        "assistantResponseMessage": {"content": "world"},
                    },
                ] * 12
            }
        }).encode()
        result, stats = compress_kiro_request(body)
        # Should process the userInputMessage (first if branch wins)
        assert json.loads(result) is not None

    def test_empty_history_list(self):
        """Empty history list → no compression, just re-serialize."""
        body = json.dumps({"conversationState": {"history": []}}).encode()
        result, stats = compress_kiro_request(body)
        parsed = json.loads(result)
        assert parsed["conversationState"]["history"] == []

    def test_history_is_not_a_list(self):
        """history that's a dict instead of list → passthrough."""
        body = json.dumps({"conversationState": {"history": {"0": "msg"}}}).encode()
        result, stats = compress_kiro_request(body)
        # Falls through because history is not a list
        assert result == body


class TestExtremeMessageSizes:
    """Test behavior with very large inputs."""

    def test_large_single_message(self):
        """A single 1MB message in history is handled without OOM."""
        large_content = "x" * 1_000_000
        body = json.dumps({
            "conversationState": {
                "history": [
                    {"assistantResponseMessage": {"content": large_content}},
                ] * 2 + [
                    {"userInputMessage": {"content": "recent"}},
                ] * 8  # 8 protected entries
            }
        }).encode()
        result, stats = compress_kiro_request(body)
        parsed = json.loads(result)
        # The large assistant message (index 0-1) should be truncated
        assert stats["assistant_responses_truncated"] >= 1
        # Result should be significantly smaller than input
        assert len(result) < len(body)

    def test_many_small_messages(self):
        """1000 small messages — tests iteration performance."""
        body = json.dumps({
            "conversationState": {
                "history": [
                    {"userInputMessage": {"content": f"msg {i}", "images": []}}
                    if i % 2 == 0 else
                    {"assistantResponseMessage": {"content": f"reply {i}"}}
                    for i in range(1000)
                ]
            }
        }).encode()
        result, stats = compress_kiro_request(body)
        parsed = json.loads(result)
        # Should process without hanging — 1000 entries, last 8 protected
        assert len(parsed["conversationState"]["history"]) == 1000


class TestPassthrough:
    """Verify complete passthrough for non-kiro payloads."""

    def test_valid_json_no_conversation_state(self):
        """Valid JSON without conversationState → unchanged."""
        body = json.dumps({"model": "claude-sonnet-4-20250514", "messages": []}).encode()
        result, stats = compress_kiro_request(body)
        assert result == body

    def test_json_array_body(self):
        """Top-level JSON array → passthrough."""
        body = json.dumps([1, 2, 3]).encode()
        result, stats = compress_kiro_request(body)
        assert result == body

    def test_json_number_body(self):
        """Top-level JSON number → passthrough (no conversationState key)."""
        body = b"42"
        result, stats = compress_kiro_request(body)
        assert result == body
