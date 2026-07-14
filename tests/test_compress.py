"""
Tests for the kiro compression proxy.

Tests the translator (kiro ↔ anthropic format) and the compression
pipeline (headroom integration).
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from compress import compress_conversation
from kiro_translator import anthropic_to_kiro, kiro_to_anthropic


# --- Translator Tests ---


class TestKiroToAnthropic:
    """Test kiro → anthropic format translation."""

    def test_simple_text_messages(self):
        """Basic user/assistant text messages translate correctly."""
        history = [
            {"userInputMessage": {"content": "hello"}},
            {"assistantResponseMessage": {"content": "hi there"}},
        ]
        result = kiro_to_anthropic(history)
        assert len(result) == 2
        assert result[0]["role"] == "user"
        assert result[0]["content"] == [{"type": "text", "text": "hello"}]
        assert result[1]["role"] == "assistant"
        assert result[1]["content"] == [{"type": "text", "text": "hi there"}]

    def test_tool_use_messages(self):
        """Assistant tool_use blocks translate correctly."""
        history = [
            {
                "assistantResponseMessage": {
                    "content": "",
                    "toolUses": [
                        {
                            "toolUseId": "tool_1",
                            "name": "read",
                            "input": {"path": "/tmp/file.txt"},
                        }
                    ],
                }
            },
        ]
        result = kiro_to_anthropic(history)
        assert len(result) == 1
        assert result[0]["role"] == "assistant"
        assert result[0]["content"][0]["type"] == "tool_use"
        assert result[0]["content"][0]["id"] == "tool_1"
        assert result[0]["content"][0]["name"] == "read"
        assert result[0]["content"][0]["input"] == {"path": "/tmp/file.txt"}

    def test_tool_result_messages(self):
        """User messages with tool results translate correctly."""
        history = [
            {
                "userInputMessage": {
                    "content": "",
                    "userInputMessageContext": {
                        "toolResults": [
                            {
                                "toolUseId": "tool_1",
                                "content": [{"text": "file contents here"}],
                                "status": "success",
                            }
                        ]
                    },
                }
            },
        ]
        result = kiro_to_anthropic(history)
        assert len(result) == 1
        assert result[0]["role"] == "user"
        assert result[0]["content"][0]["type"] == "tool_result"
        assert result[0]["content"][0]["tool_use_id"] == "tool_1"
        assert result[0]["content"][0]["is_error"] is False

    def test_images_data_format(self):
        """Images with data field translate correctly."""
        history = [
            {
                "userInputMessage": {
                    "content": "look at this",
                    "images": [{"data": "base64data", "format": "image/png"}],
                }
            },
        ]
        result = kiro_to_anthropic(history)
        assert len(result) == 1
        blocks = result[0]["content"]
        image_blocks = [b for b in blocks if b["type"] == "image"]
        assert len(image_blocks) == 1
        assert image_blocks[0]["source"]["data"] == "base64data"

    def test_images_source_bytes_format(self):
        """Images with source.bytes field translate correctly."""
        history = [
            {
                "userInputMessage": {
                    "content": "screenshot",
                    "images": [{"source": {"bytes": "b64data"}}],
                }
            },
        ]
        result = kiro_to_anthropic(history)
        blocks = result[0]["content"]
        image_blocks = [b for b in blocks if b["type"] == "image"]
        assert len(image_blocks) == 1
        assert image_blocks[0]["source"]["data"] == "b64data"


class TestAnthropicToKiro:
    """Test anthropic → kiro format translation."""

    def test_simple_roundtrip(self):
        """Messages survive a kiro → anthropic → kiro round trip."""
        original = [
            {"userInputMessage": {"content": "hello"}},
            {"assistantResponseMessage": {"content": "hi"}},
        ]
        anthropic = kiro_to_anthropic(original)
        result = anthropic_to_kiro(anthropic)
        assert len(result) == 2
        assert result[0]["userInputMessage"]["content"] == "hello"
        assert result[1]["assistantResponseMessage"]["content"] == "hi"

    def test_tool_use_roundtrip(self):
        """Tool uses survive round trip."""
        original = [
            {
                "assistantResponseMessage": {
                    "content": "reading file",
                    "toolUses": [
                        {"toolUseId": "t1", "name": "read", "input": {"path": "/x"}}
                    ],
                }
            },
        ]
        anthropic = kiro_to_anthropic(original)
        result = anthropic_to_kiro(anthropic)
        arm = result[0]["assistantResponseMessage"]
        assert arm["content"] == "reading file"
        assert len(arm["toolUses"]) == 1
        assert arm["toolUses"][0]["toolUseId"] == "t1"
        assert arm["toolUses"][0]["name"] == "read"

    def test_tool_result_roundtrip(self):
        """Tool results survive round trip."""
        original = [
            {
                "userInputMessage": {
                    "content": "",
                    "userInputMessageContext": {
                        "toolResults": [
                            {
                                "toolUseId": "t1",
                                "content": [{"text": "result data"}],
                                "status": "success",
                            }
                        ]
                    },
                }
            },
        ]
        anthropic = kiro_to_anthropic(original)
        result = anthropic_to_kiro(anthropic)
        ctx = result[0]["userInputMessage"]["userInputMessageContext"]
        assert len(ctx["toolResults"]) == 1
        assert ctx["toolResults"][0]["toolUseId"] == "t1"
        assert ctx["toolResults"][0]["content"][0]["text"] == "result data"


# --- Compression Tests ---


class TestCompression:
    """Test the full compression pipeline."""

    def test_no_conversation_state(self):
        """Requests without conversationState pass through unchanged."""
        body = json.dumps({"other": "data"}).encode()
        result = compress_conversation(body)
        assert result["body"] == body
        assert result["images_stripped"] == 0

    def test_empty_history(self):
        """Empty history passes through unchanged."""
        body = json.dumps({
            "conversationState": {"history": [], "currentMessage": {}}
        }).encode()
        result = compress_conversation(body)
        assert result["body"] == body

    def test_short_history_protected(self):
        """History shorter than protect_recent is fully protected."""
        body = json.dumps({
            "conversationState": {
                "history": [
                    {"userInputMessage": {"content": "hello world " * 100}},
                    {"assistantResponseMessage": {"content": "response " * 100}},
                ],
                "currentMessage": {},
            }
        }).encode()
        result = compress_conversation(body)
        # Short history should not be compressed much (if at all)
        parsed = json.loads(result["body"])
        history = parsed["conversationState"]["history"]
        assert len(history) == 2
        # Content should be preserved (within protect_recent window)
        assert "hello world" in history[0]["userInputMessage"]["content"]

    def test_compression_reduces_size(self):
        """Large history with tool results gets compressed."""
        # Build a history with large tool results
        history = []
        for i in range(20):
            history.append({"userInputMessage": {"content": f"do thing {i}"}})
            history.append({
                "assistantResponseMessage": {
                    "content": "",
                    "toolUses": [
                        {"toolUseId": f"t{i}", "name": "shell", "input": {"command": "ls"}}
                    ],
                }
            })
            history.append({
                "userInputMessage": {
                    "content": "",
                    "userInputMessageContext": {
                        "toolResults": [
                            {
                                "toolUseId": f"t{i}",
                                "content": [{"text": f"line {j}\n" * 200 for j in range(3)}],
                                "status": "success",
                            }
                        ]
                    },
                }
            })
            history.append({"assistantResponseMessage": {"content": f"done with {i}. " * 50}})

        body = json.dumps({
            "conversationState": {"history": history, "currentMessage": {}}
        }).encode()

        result = compress_conversation(body)
        assert len(result["body"]) < len(body)
        assert result["tokens_saved"] >= 0  # headroom reports token savings

    def test_output_is_valid_json(self):
        """Compressed output is always valid JSON."""
        body = json.dumps({
            "conversationState": {
                "history": [
                    {"userInputMessage": {"content": "test " * 500}},
                    {"assistantResponseMessage": {"content": "response " * 500}},
                ],
                "currentMessage": {},
            }
        }).encode()
        result = compress_conversation(body)
        # Should not raise
        parsed = json.loads(result["body"])
        assert "conversationState" in parsed

    def test_preserves_non_history_fields(self):
        """Fields outside history are preserved unchanged."""
        body = json.dumps({
            "conversationState": {
                "history": [{"userInputMessage": {"content": "hi"}}],
                "conversationId": "abc-123",
                "currentMessage": {"userInputMessage": {"content": "now"}},
            },
            "profileArn": "arn:aws:test",
            "additionalModelRequestFields": {"output_config": {"effort": "high"}},
        }).encode()
        result = compress_conversation(body)
        parsed = json.loads(result["body"])
        assert parsed["profileArn"] == "arn:aws:test"
        assert parsed["additionalModelRequestFields"] == {"output_config": {"effort": "high"}}
        assert parsed["conversationState"]["conversationId"] == "abc-123"


class TestCompressionWithFixture:
    """Test with real captured data (if available)."""

    @pytest.fixture
    def fixture_body(self):
        fixture_path = os.path.join(
            os.path.dirname(__file__), "..", "testdata", "captured-request-285-turns.json"
        )
        if not os.path.exists(fixture_path):
            pytest.skip("Fixture file not available")
        with open(fixture_path, "rb") as f:
            return f.read()

    def test_fixture_compresses(self, fixture_body):
        """Real fixture compresses successfully."""
        result = compress_conversation(fixture_body)
        assert len(result["body"]) < len(fixture_body)
        assert result["tokens_saved"] > 0

    def test_fixture_produces_valid_kiro_format(self, fixture_body):
        """Compressed fixture is still valid kiro wire format."""
        result = compress_conversation(fixture_body)
        parsed = json.loads(result["body"])
        assert "conversationState" in parsed
        history = parsed["conversationState"]["history"]
        # Every message should be either user or assistant
        for msg in history:
            assert ("userInputMessage" in msg) or ("assistantResponseMessage" in msg)

    def test_fixture_deterministic(self, fixture_body):
        """Same input produces same output (cache stability)."""
        result1 = compress_conversation(fixture_body)
        result2 = compress_conversation(fixture_body)
        assert result1["body"] == result2["body"]
