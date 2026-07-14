"""
Compression engine for kiro conversation history.

Uses headroom's cache-aware compression pipeline via a format translation layer.
Kiro's custom wire format is translated to Anthropic format, compressed by
headroom (which understands prompt caching), then translated back.

Two modes:
- 'cache' (default): Prioritizes prefix stability for server-side prompt caching.
  Compressed output is deterministic — same prefix produces same compressed prefix.
- 'token': Maximum compression regardless of cache impact.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from kiro_translator import anthropic_to_kiro, kiro_to_anthropic

logger = logging.getLogger("kiro-proxy.compress")

# Try to import headroom; fall back to basic compression if unavailable
try:
    from headroom import compress as headroom_compress
    from headroom.compress import CompressConfig

    HEADROOM_AVAILABLE = True
    logger.info("headroom compression engine loaded")
except ImportError:
    HEADROOM_AVAILABLE = False
    logger.warning("headroom not available, using basic compression fallback")


# Fallback constants (used when headroom is not installed)
PROTECT_RECENT_MESSAGES = 8
TOOL_RESULT_MAX_CHARS = 500
ASSISTANT_RESPONSE_MAX_CHARS = 1000
ASSISTANT_RESPONSE_MIN_SIZE = 5000


def compress_conversation(body: bytes, mode: str = "cache") -> dict[str, Any]:
    """
    Compress a kiro-cli request body.

    Args:
        body: Raw request body bytes (JSON)
        mode: 'cache' (prefix-stable) or 'token' (max compression)

    Returns a dict with:
        body: compressed bytes
        images_stripped: count of images removed
        tool_results_compressed: count of tool results truncated
        assistant_responses_truncated: count of assistant responses truncated
        tokens_before: token count before compression (if headroom available)
        tokens_after: token count after compression (if headroom available)
        transforms_applied: list of transform names applied
    """
    req = json.loads(body)

    if "conversationState" not in req:
        return _empty_result(body)

    history = req["conversationState"].get("history", [])
    if not history:
        return _empty_result(body)

    if HEADROOM_AVAILABLE:
        return _compress_with_headroom(req, history, mode)
    else:
        return _compress_fallback(req, history)


def _compress_with_headroom(
    req: dict[str, Any], history: list[dict[str, Any]], mode: str
) -> dict[str, Any]:
    """Compress using headroom's cache-aware pipeline."""

    # Step 1: Translate kiro → Anthropic format
    anthropic_messages = kiro_to_anthropic(history)

    if not anthropic_messages:
        return _empty_result(json.dumps(req, separators=(",", ":")).encode())

    # Step 2: Run headroom compression
    config = CompressConfig(
        compress_user_messages=True,  # Tool results are in user messages
        protect_recent=4,  # Protect last 4 messages (2 turns)
        protect_analysis_context=True,
    )

    result = headroom_compress(
        anthropic_messages,
        model="claude-opus-4-6",
        model_limit=1_000_000,  # Opus 4.6 has 1M context
        config=config,
    )

    # Step 3: Translate compressed Anthropic → kiro format
    compressed_history = anthropic_to_kiro(result.messages)

    # Step 4: Rebuild the kiro request
    req["conversationState"]["history"] = compressed_history
    compressed_body = json.dumps(req, separators=(",", ":")).encode()

    # Count what changed
    images_stripped = _count_images_removed(history, compressed_history)
    tool_results_compressed = _count_tool_results_compressed(history, compressed_history)

    return {
        "body": compressed_body,
        "images_stripped": images_stripped,
        "tool_results_compressed": tool_results_compressed,
        "assistant_responses_truncated": 0,  # Headroom handles this internally
        "tokens_before": result.tokens_before,
        "tokens_after": result.tokens_after,
        "tokens_saved": result.tokens_saved,
        "compression_ratio": result.compression_ratio,
        "transforms_applied": result.transforms_applied,
    }


def _compress_fallback(
    req: dict[str, Any], history: list[dict[str, Any]]
) -> dict[str, Any]:
    """Basic compression fallback when headroom is not installed.

    WARNING: This does NOT preserve prompt caching. Use headroom for production.
    """
    images_stripped = 0
    tool_results_compressed = 0
    assistant_responses_truncated = 0

    protect_start = max(0, len(history) - PROTECT_RECENT_MESSAGES)

    for i, msg in enumerate(history):
        if i >= protect_start:
            break

        if "userInputMessage" in msg:
            um = msg["userInputMessage"]

            # Strip images
            images = um.get("images", [])
            if images:
                count = len(images)
                turn_num = i // 2 + 1
                annotation = f"\n[{count} screenshot(s) from turn {turn_num} removed]"
                um["content"] = um.get("content", "") + annotation
                um["images"] = []
                images_stripped += count

            # Truncate tool results
            ctx = um.get("userInputMessageContext", {})
            tool_results = ctx.get("toolResults", [])
            for result in tool_results:
                for part in result.get("content", []):
                    text = part.get("text", "")
                    if len(text) > TOOL_RESULT_MAX_CHARS:
                        lines = text.split("\n")
                        summary_lines = []
                        char_count = 0
                        for line in lines:
                            if char_count + len(line) + 1 > TOOL_RESULT_MAX_CHARS - 80:
                                break
                            summary_lines.append(line)
                            char_count += len(line) + 1
                        if not summary_lines:
                            summary_lines = [text[: TOOL_RESULT_MAX_CHARS - 80]]
                        footer = f"\n[... truncated from {len(text):,} chars / {len(lines)} lines]"
                        part["text"] = "\n".join(summary_lines) + footer
                        tool_results_compressed += 1

        elif "assistantResponseMessage" in msg:
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
        "tokens_before": 0,
        "tokens_after": 0,
        "tokens_saved": 0,
        "compression_ratio": 0.0,
        "transforms_applied": ["fallback"],
    }


def _empty_result(body: bytes) -> dict[str, Any]:
    """Return a no-op result for requests that don't need compression."""
    return {
        "body": body,
        "images_stripped": 0,
        "tool_results_compressed": 0,
        "assistant_responses_truncated": 0,
        "tokens_before": 0,
        "tokens_after": 0,
        "tokens_saved": 0,
        "compression_ratio": 0.0,
        "transforms_applied": [],
    }


def _count_images_removed(
    original: list[dict[str, Any]], compressed: list[dict[str, Any]]
) -> int:
    """Count images present in original but not in compressed."""
    original_count = sum(
        len(msg.get("userInputMessage", {}).get("images", []))
        for msg in original
    )
    compressed_count = sum(
        len(msg.get("userInputMessage", {}).get("images", []))
        for msg in compressed
    )
    return max(0, original_count - compressed_count)


def _count_tool_results_compressed(
    original: list[dict[str, Any]], compressed: list[dict[str, Any]]
) -> int:
    """Estimate tool results that were compressed (length reduction)."""
    # This is an approximation — we count tool results in original that
    # are shorter in the compressed version
    count = 0
    for orig_msg, comp_msg in zip(original, compressed):
        if "userInputMessage" not in orig_msg:
            continue
        orig_ctx = orig_msg["userInputMessage"].get("userInputMessageContext", {})
        comp_ctx = comp_msg.get("userInputMessage", {}).get("userInputMessageContext", {})
        orig_results = orig_ctx.get("toolResults", [])
        comp_results = comp_ctx.get("toolResults", [])
        for orig_tr, comp_tr in zip(orig_results, comp_results):
            orig_text = "".join(p.get("text", "") for p in orig_tr.get("content", []))
            comp_text = "".join(p.get("text", "") for p in comp_tr.get("content", []))
            if len(comp_text) < len(orig_text) * 0.8:  # >20% reduction
                count += 1
    return count
