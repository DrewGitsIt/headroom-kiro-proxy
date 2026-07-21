"""Unit tests for handler.py internal helpers.

Tests _truncate_with_summary edge cases, _compress_user_message idempotency,
and _compress_assistant_message threshold behavior.
"""

import pytest
from handler import (
    _truncate_with_summary,
    _compress_user_message,
    _compress_assistant_message,
    ASSISTANT_TRUNCATE_THRESHOLD,
    ASSISTANT_TRUNCATE_KEEP,
)


class TestTruncateWithSummary:
    """Test _truncate_with_summary edge cases."""

    def test_short_text_returns_none(self):
        """Text within max_chars should return None (no truncation needed)."""
        result = _truncate_with_summary("short text", max_chars=500)
        assert result is None

    def test_exact_max_chars_returns_none(self):
        """Text exactly at max_chars should return None."""
        text = "x" * 500
        result = _truncate_with_summary(text, max_chars=500)
        assert result is None

    def test_normal_truncation(self):
        """Text longer than max_chars should be truncated with footer."""
        text = "line one\nline two\nline three\n" * 100
        result = _truncate_with_summary(text, max_chars=200)
        assert result is not None
        assert "[... truncated from" in result
        assert "chars /" in result
        assert "lines]" in result
        # Result should be shorter than original
        assert len(result) < len(text)

    def test_max_chars_below_80_footer_budget(self):
        """When max_chars < 80, kept_lines will be empty — falls back to slice.

        This tests the edge case where the footer budget (80 chars) exceeds
        max_chars, making max_chars - 80 negative. The code should still
        produce output without crashing.
        """
        text = "A" * 200  # Longer than max_chars, triggers truncation
        result = _truncate_with_summary(text, max_chars=50)
        assert result is not None
        # Should contain the footer even in degenerate case
        assert "[... truncated from" in result

    def test_all_lines_longer_than_budget(self):
        """When every line exceeds the char budget, falls back to first-line slice."""
        # Each line is 200 chars — way above any reasonable budget
        text = ("x" * 200 + "\n") * 10
        result = _truncate_with_summary(text, max_chars=150)
        assert result is not None
        assert "[... truncated from" in result
        # Should still contain some content (the slice fallback)
        content_before_footer = result.split("\n[... truncated")[0]
        assert len(content_before_footer) > 0

    def test_single_very_long_line(self):
        """A single line longer than max_chars gets sliced."""
        text = "x" * 10000
        result = _truncate_with_summary(text, max_chars=300)
        assert result is not None
        assert len(result) < len(text)
        assert "[... truncated from 10,000 chars / 1 lines]" in result

    def test_footer_contains_original_stats(self):
        """Footer reports the original char count and line count."""
        text = "line\n" * 1000  # 5000 chars, 1000 lines
        result = _truncate_with_summary(text, max_chars=200)
        assert "5,000 chars" in result
        # Lines include trailing empty from final \n
        assert "lines]" in result

    def test_max_chars_zero(self):
        """max_chars=0 should not crash (degenerate input)."""
        text = "some text that is definitely longer than zero"
        # With max_chars=0, text (46 chars) > 0 so truncation triggers.
        # max_chars - 80 = -80, so all lines fail the budget check.
        # Fallback: text[:0 - 80] = text[:-80] which is empty for short text
        # or text[:negative] for longer. Either way, should not crash.
        result = _truncate_with_summary(text, max_chars=0)
        # Don't assert specific output, just that it doesn't crash
        assert result is not None or result is None  # trivially true = no crash


class TestCompressUserMessageIdempotency:
    """Test that _compress_user_message is idempotent for image annotation."""

    def test_annotation_not_duplicated_on_second_pass(self):
        """Processing the same message twice should not stack annotations."""
        msg = {
            "content": "Please look at this",
            "images": [{"data": "base64..."}, {"data": "base64..."}],
        }
        stats = {"images_stripped": 0, "tool_results_compressed": 0}

        # First pass — adds annotation, clears images
        _compress_user_message(msg, turn_index=0, stats=stats)
        assert "[2 screenshot(s) from turn 1 removed]" in msg["content"]
        assert msg["images"] == []
        assert stats["images_stripped"] == 2
        first_pass_content = msg["content"]

        # Simulate retry: put images back (would happen if body was re-parsed)
        msg["images"] = [{"data": "base64..."}]
        _compress_user_message(msg, turn_index=0, stats=stats)
        # Content should NOT have a second annotation
        assert msg["content"] == first_pass_content
        # Images still cleared
        assert msg["images"] == []
        # Count should not increase (annotation guard prevents double-count)
        assert stats["images_stripped"] == 2

    def test_annotation_added_only_when_images_present(self):
        """No annotation if images list is empty."""
        msg = {"content": "no images here", "images": []}
        stats = {"images_stripped": 0, "tool_results_compressed": 0}
        _compress_user_message(msg, turn_index=0, stats=stats)
        assert "screenshot" not in msg["content"]
        assert stats["images_stripped"] == 0

    def test_turn_number_in_annotation(self):
        """Annotation includes correct turn number from index."""
        msg = {"content": "look", "images": [{"data": "x"}]}
        stats = {"images_stripped": 0, "tool_results_compressed": 0}
        _compress_user_message(msg, turn_index=6, stats=stats)
        # turn_index 6 → turn_num = 6 // 2 + 1 = 4
        assert "turn 4" in msg["content"]


class TestCompressAssistantMessage:
    """Test _compress_assistant_message truncation behavior."""

    def test_short_message_not_truncated(self):
        """Messages below threshold are untouched."""
        arm = {"content": "short response"}
        stats = {"assistant_responses_truncated": 0}
        _compress_assistant_message(arm, stats=stats)
        assert arm["content"] == "short response"
        assert stats["assistant_responses_truncated"] == 0

    def test_at_threshold_not_truncated(self):
        """Message exactly at threshold is NOT truncated."""
        arm = {"content": "x" * ASSISTANT_TRUNCATE_THRESHOLD}
        stats = {"assistant_responses_truncated": 0}
        _compress_assistant_message(arm, stats=stats)
        assert len(arm["content"]) == ASSISTANT_TRUNCATE_THRESHOLD
        assert stats["assistant_responses_truncated"] == 0

    def test_above_threshold_truncated(self):
        """Message above threshold gets truncated."""
        original = "y" * (ASSISTANT_TRUNCATE_THRESHOLD + 5000)
        arm = {"content": original}
        stats = {"assistant_responses_truncated": 0}
        _compress_assistant_message(arm, stats=stats)
        assert len(arm["content"]) < len(original)
        assert arm["content"].startswith("y" * ASSISTANT_TRUNCATE_KEEP)
        assert "chars truncated]" in arm["content"]
        assert stats["assistant_responses_truncated"] == 1

    def test_non_string_content_ignored(self):
        """Non-string content field should be silently skipped."""
        arm = {"content": 12345}
        stats = {"assistant_responses_truncated": 0}
        _compress_assistant_message(arm, stats=stats)
        assert arm["content"] == 12345

    def test_missing_content_ignored(self):
        """Missing content key should not crash."""
        arm = {"role": "assistant"}
        stats = {"assistant_responses_truncated": 0}
        _compress_assistant_message(arm, stats=stats)
        assert "content" not in arm
