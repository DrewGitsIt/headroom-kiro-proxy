"""
Compression engine for kiro conversation history.

Applies three tiers of compression to old turns in the conversation:
1. Image stripping — remove base64 screenshots from old turns
2. Tool result truncation — shorten large tool outputs to summaries
3. Assistant response truncation — shorten old verbose responses

Recent turns (last `protect_recent` messages) are never modified.
"""

import json
import logging
from typing import Any

logger = logging.getLogger("kiro-proxy.compress")

# Compression thresholds
PROTECT_RECENT_MESSAGES = 8  # Last 8 messages (4 user+assistant pairs) are sacred
TOOL_RESULT_MAX_CHARS = 500  # Truncate tool results longer than this
ASSISTANT_RESPONSE_MAX_CHARS = 1000  # Truncate assistant responses longer than this
ASSISTANT_RESPONSE_MIN_SIZE = 5000  # Only truncate if > this size


def compress_conversation(body: bytes) -> dict[str, Any]:
    """
    Compress a kiro-cli request body.

    Returns a dict with:
        body: compressed bytes
        images_stripped: count of images removed
        tool_results_compressed: count of tool results truncated
    """
    req = json.loads(body)

    if "conversationState" not in req:
        return {"body": body, "images_stripped": 0, "tool_results_compressed": 0}

    history = req["conversationState"].get("history", [])
    if not history:
        return {"body": body, "images_stripped": 0, "tool_results_compressed": 0}

    images_stripped = 0
    tool_results_compressed = 0
    assistant_responses_truncated = 0

    # Determine the protection boundary
    protect_start = max(0, len(history) - PROTECT_RECENT_MESSAGES)

    for i, msg in enumerate(history):
        if i >= protect_start:
            break  # Don't touch recent messages

        if "userInputMessage" in msg:
            um = msg["userInputMessage"]

            # Tier 1: Strip images
            images = um.get("images", [])
            if images:
                count = len(images)
                turn_num = i // 2 + 1
                # Add annotation to content
                annotation = f"\n[{count} screenshot(s) from turn {turn_num} removed — available in session file]"
                um["content"] = um.get("content", "") + annotation
                um["images"] = []
                images_stripped += count

            # Tier 2: Compress tool results
            ctx = um.get("userInputMessageContext", {})
            tool_results = ctx.get("toolResults", [])
            for result in tool_results:
                for part in result.get("content", []):
                    text = part.get("text", "")
                    if len(text) > TOOL_RESULT_MAX_CHARS:
                        original_len = len(text)
                        summary = _summarize_tool_result(text, TOOL_RESULT_MAX_CHARS)
                        part["text"] = summary
                        tool_results_compressed += 1

        elif "assistantResponseMessage" in msg:
            # Tier 3: Truncate old verbose assistant responses
            arm = msg["assistantResponseMessage"]
            content = arm.get("content", "")
            if len(content) > ASSISTANT_RESPONSE_MIN_SIZE:
                arm["content"] = (
                    content[:ASSISTANT_RESPONSE_MAX_CHARS]
                    + f"\n[... {len(content) - ASSISTANT_RESPONSE_MAX_CHARS:,} chars truncated]"
                )
                assistant_responses_truncated += 1

    req["conversationState"]["history"] = history
    compressed_body = json.dumps(req, separators=(",", ":")).encode()

    return {
        "body": compressed_body,
        "images_stripped": images_stripped,
        "tool_results_compressed": tool_results_compressed,
        "assistant_responses_truncated": assistant_responses_truncated,
    }


def _summarize_tool_result(text: str, max_chars: int) -> str:
    """
    Produce a summary of a tool result.

    Strategy:
    - Keep the first few lines (often contain the key info like file path, error message)
    - Add a size indicator so the model knows content was removed
    - For structured content (JSON, code), try to preserve the shape
    """
    lines = text.split("\n")
    original_len = len(text)
    original_lines = len(lines)

    # Heuristic: keep first N lines that fit within max_chars
    summary_lines = []
    char_count = 0
    for line in lines:
        if char_count + len(line) + 1 > max_chars - 80:  # leave room for the footer
            break
        summary_lines.append(line)
        char_count += len(line) + 1

    if not summary_lines:
        # Single very long line — just truncate
        summary_lines = [text[:max_chars - 80]]

    summary = "\n".join(summary_lines)
    footer = f"\n[... truncated from {original_len:,} chars / {original_lines} lines]"
    return summary + footer
