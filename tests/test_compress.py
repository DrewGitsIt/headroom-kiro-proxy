"""Tests for the compression engine."""

import json
import os
import sys

import pytest

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from compress import compress_conversation, PROTECT_RECENT_MESSAGES


FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "testdata", "captured-request-285-turns.json"
)


@pytest.fixture
def fixture_body():
    """Load the real captured request fixture."""
    with open(FIXTURE_PATH, "rb") as f:
        return f.read()


@pytest.fixture
def fixture_json():
    """Load the fixture as parsed JSON."""
    with open(FIXTURE_PATH) as f:
        return json.load(f)


class TestCompressConversation:
    """Test the main compress_conversation function."""

    def test_returns_valid_json(self, fixture_body):
        result = compress_conversation(fixture_body)
        parsed = json.loads(result["body"])
        assert "conversationState" in parsed

    def test_preserves_history_length(self, fixture_body, fixture_json):
        result = compress_conversation(fixture_body)
        parsed = json.loads(result["body"])
        original_len = len(fixture_json["conversationState"]["history"])
        compressed_len = len(parsed["conversationState"]["history"])
        assert compressed_len == original_len

    def test_preserves_current_message(self, fixture_body, fixture_json):
        result = compress_conversation(fixture_body)
        parsed = json.loads(result["body"])
        # currentMessage should be completely untouched
        assert "currentMessage" in parsed["conversationState"]
        original_cm = fixture_json["conversationState"]["currentMessage"]
        compressed_cm = parsed["conversationState"]["currentMessage"]
        assert original_cm == compressed_cm

    def test_preserves_profile_arn(self, fixture_body, fixture_json):
        result = compress_conversation(fixture_body)
        parsed = json.loads(result["body"])
        assert parsed.get("profileArn") == fixture_json.get("profileArn")

    def test_achieves_meaningful_compression(self, fixture_body):
        result = compress_conversation(fixture_body)
        original_size = len(fixture_body)
        compressed_size = len(result["body"])
        savings = 1 - compressed_size / original_size
        # Should achieve at least 40% compression on the real fixture
        assert savings > 0.40, f"Only achieved {savings*100:.1f}% compression"

    def test_strips_images_from_old_turns(self, fixture_body):
        result = compress_conversation(fixture_body)
        assert result["images_stripped"] > 0

    def test_compresses_tool_results(self, fixture_body):
        result = compress_conversation(fixture_body)
        assert result["tool_results_compressed"] > 0

    def test_protects_recent_messages(self, fixture_body, fixture_json):
        result = compress_conversation(fixture_body)
        parsed = json.loads(result["body"])
        history = parsed["conversationState"]["history"]
        original_history = fixture_json["conversationState"]["history"]

        # Last PROTECT_RECENT_MESSAGES messages should be identical
        for i in range(1, PROTECT_RECENT_MESSAGES + 1):
            assert history[-i] == original_history[-i], (
                f"Message at position -{i} was modified (should be protected)"
            )

    def test_passthrough_on_non_conversation(self):
        """Non-conversation payloads should pass through unchanged."""
        body = json.dumps({"something": "else"}).encode()
        result = compress_conversation(body)
        assert result["body"] == body
        assert result["images_stripped"] == 0
        assert result["tool_results_compressed"] == 0

    def test_passthrough_on_empty_history(self):
        """Empty history should pass through unchanged."""
        body = json.dumps({
            "conversationState": {"history": [], "currentMessage": {}}
        }).encode()
        result = compress_conversation(body)
        assert result["body"] == body

    def test_image_annotations_added(self, fixture_body):
        """Stripped images should leave text annotations."""
        result = compress_conversation(fixture_body)
        parsed = json.loads(result["body"])
        history = parsed["conversationState"]["history"]

        # Find a message that had images (we know they exist from analysis)
        found_annotation = False
        for msg in history:
            if "userInputMessage" in msg:
                content = msg["userInputMessage"].get("content", "")
                if "screenshot" in content and "removed" in content:
                    found_annotation = True
                    # Verify the images array is empty
                    assert msg["userInputMessage"].get("images", []) == []
                    break

        assert found_annotation, "No image removal annotation found"

    def test_tool_result_truncation_preserves_structure(self, fixture_body):
        """Truncated tool results should still have the expected structure."""
        result = compress_conversation(fixture_body)
        parsed = json.loads(result["body"])
        history = parsed["conversationState"]["history"]

        for msg in history:
            if "userInputMessage" in msg:
                ctx = msg["userInputMessage"].get("userInputMessageContext", {})
                for tr in ctx.get("toolResults", []):
                    # Every tool result should still have these fields
                    assert "toolUseId" in tr
                    assert "content" in tr
                    for part in tr["content"]:
                        # Parts can be {"text": ...} or {"json": ...}
                        assert "text" in part or "json" in part


class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_invalid_json_raises(self):
        """Invalid JSON should raise, letting proxy catch and fail-through."""
        with pytest.raises(json.JSONDecodeError):
            compress_conversation(b"not json at all")

    def test_very_short_history(self):
        """History shorter than protect_recent should be fully protected."""
        body = json.dumps({
            "conversationState": {
                "history": [
                    {"userInputMessage": {"content": "hi", "images": [{"source": {"bytes": "abc"}}]}}
                ],
                "currentMessage": {},
            }
        }).encode()
        result = compress_conversation(body)
        parsed = json.loads(result["body"])
        # Single message is within protect_recent, so images should be preserved
        assert parsed["conversationState"]["history"][0]["userInputMessage"]["images"] == [
            {"source": {"bytes": "abc"}}
        ]
        assert result["images_stripped"] == 0
