"""Kiro conversation compression — public API.

Wraps handler.compress_kiro_request() with token counting and a dict return
format suitable for stats reporting and testing.

Usage:
    from compress import compress_conversation

    result = compress_conversation(request_body_bytes)
    # result = {
    #     "body": b"...",            # compressed request body
    #     "images_stripped": 3,
    #     "tool_results_compressed": 7,
    #     "assistant_responses_truncated": 5,
    #     "cache_bypass": False,
    #     "tokens_before": 45000,
    #     "tokens_after": 22000,
    #     "tokens_saved": 23000,
    #     "compression_ratio": 0.49,
    # }
"""

from __future__ import annotations

import logging

from handler import compress_kiro_request
from session_timer import SessionTimer

logger = logging.getLogger("kiro_proxy.compress")

# Expose whether the full headroom pipeline (with token counting) is available.
try:
    from headroom import count_tokens_text
    HEADROOM_AVAILABLE = True
except ImportError:
    HEADROOM_AVAILABLE = False


def compress_conversation(
    body: bytes, *, session_timer: SessionTimer | None = None
) -> dict:
    """Compress a kiro request body and return stats.

    If session_timer is provided, respects cache warmth (passes through
    unchanged when cache is warm). Without a timer, always compresses
    (cold-cache behavior).

    Always returns a dict with at minimum {"body": ..., "images_stripped": 0,
    "tokens_saved": 0}. On any internal error, returns the original body
    unchanged (fail-through).
    """
    result = {
        "body": body,
        "images_stripped": 0,
        "tool_results_compressed": 0,
        "assistant_responses_truncated": 0,
        "cache_bypass": False,
        "tokens_before": 0,
        "tokens_after": 0,
        "tokens_saved": 0,
        "compression_ratio": 1.0,
    }

    try:
        compressed_body, stats = compress_kiro_request(
            body, session_timer=session_timer
        )
    except Exception as exc:
        logger.warning("compress_kiro_request failed, passing through: %s", exc)
        return result

    result["body"] = compressed_body
    result["images_stripped"] = stats.get("images_stripped", 0)
    result["tool_results_compressed"] = stats.get("tool_results_compressed", 0)
    result["assistant_responses_truncated"] = stats.get("assistant_responses_truncated", 0)
    result["cache_bypass"] = bool(stats.get("cache_bypass", 0))

    # If cache bypass, no compression happened — skip token counting
    if result["cache_bypass"]:
        return result

    # Token counting (only available with headroom-ai installed)
    if HEADROOM_AVAILABLE:
        try:
            tokens_before = _count_tokens(body)
            tokens_after = _count_tokens(compressed_body)
            result["tokens_before"] = tokens_before
            result["tokens_after"] = tokens_after
            result["tokens_saved"] = tokens_before - tokens_after
            if tokens_before > 0:
                result["compression_ratio"] = tokens_after / tokens_before
        except Exception as exc:
            logger.debug("Token counting failed: %s", exc)
            # Fall back to byte-based estimate
            result["tokens_before"] = len(body) // 4
            result["tokens_after"] = len(compressed_body) // 4
            result["tokens_saved"] = result["tokens_before"] - result["tokens_after"]
            if result["tokens_before"] > 0:
                result["compression_ratio"] = result["tokens_after"] / result["tokens_before"]
    else:
        # Byte-based token estimate (~4 chars per token)
        result["tokens_before"] = len(body) // 4
        result["tokens_after"] = len(compressed_body) // 4
        result["tokens_saved"] = result["tokens_before"] - result["tokens_after"]
        if result["tokens_before"] > 0:
            result["compression_ratio"] = result["tokens_after"] / result["tokens_before"]

    return result


def _count_tokens(body: bytes) -> int:
    """Count tokens in a request body using headroom's tokenizer."""
    text = body.decode("utf-8", errors="replace")
    return count_tokens_text(text)
