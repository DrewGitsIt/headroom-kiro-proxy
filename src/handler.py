"""Kiro request compression handler.

Compresses kiro's ``conversationState.history`` in-place, operating
directly on kiro's custom wire format. Uses headroom's SmartCrusher
(Rust-backed via PyO3) when available, falls back to structural truncation.

Cache-safety model (2026-07-22):
  - If a SessionTimer is provided and the cache is WARM (< 5min since last
    request for this conversationId), the request passes through byte-for-byte
    with zero modifications — preserving the provider's prompt cache prefix.
  - If the cache is COLD (≥ 5min or first request), we compress freely since
    there's no cached prefix to invalidate.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from session_timer import SessionTimer

logger = logging.getLogger("kiro_proxy.handler")

# Number of recent history entries to protect from compression.
# NOTE: "entries" are individual history items, NOT logical turns. In kiro's
# wire format a single logical turn may span 2-4 entries (user → assistant
# with tool_use → user with tool_result → assistant reply). So 8 entries
# protects roughly 2-4 logical turns depending on tool use density.
# Override via KIRO_PROXY_PROTECT_ENTRIES env var.
PROTECT_RECENT_ENTRIES = int(os.environ.get("KIRO_PROXY_PROTECT_ENTRIES", "8"))

# Minimum tool result size (chars) before compression is attempted.
MIN_COMPRESS_CHARS = 800

# Maximum chars to keep from a truncated assistant response.
ASSISTANT_TRUNCATE_KEEP = 1000

# Minimum assistant response size before truncation kicks in.
ASSISTANT_TRUNCATE_THRESHOLD = 5000


def compress_kiro_request(
    body: bytes, *, session_timer: SessionTimer | None = None
) -> tuple[bytes, dict[str, int]]:
    """Compress a kiro runtime request body in-place.

    If session_timer is provided, checks whether the prompt cache is likely
    warm for this conversation. If warm, returns the body unchanged to
    preserve cache hits. If cold (or no timer), compresses freely.

    Returns the (possibly compressed) body and a stats dict. On any parse
    error, returns the original body unchanged with zero stats (fail-through).
    """
    stats: dict[str, int] = {
        "images_stripped": 0,
        "tool_results_compressed": 0,
        "assistant_responses_truncated": 0,
        "cache_bypass": 0,
    }

    try:
        req = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.debug("body is not valid JSON, passing through")
        return body, stats

    if not isinstance(req, dict):
        return body, stats

    if "conversationState" not in req:
        return body, stats

    conversation_state = req["conversationState"]
    conversation_id = conversation_state.get("conversationId")

    # --- Cache-safety gate ---
    # If timer says cache is warm, pass through byte-faithfully.
    if session_timer is not None and conversation_id:
        if session_timer.is_cache_warm(conversation_id):
            logger.info(
                "kiro: cache WARM for %s (%.0fs since last) — passing through unchanged",
                conversation_id[:8],
                session_timer.seconds_since_last(conversation_id) or 0,
            )
            stats["cache_bypass"] = 1
            # Record that we saw this session (keeps the timer alive)
            session_timer.touch(conversation_id)
            return body, stats
        else:
            elapsed = session_timer.seconds_since_last(conversation_id)
            if elapsed is not None:
                logger.info(
                    "kiro: cache COLD for %s (%.0fs since last) — compressing",
                    conversation_id[:8], elapsed,
                )
            else:
                logger.info(
                    "kiro: first request for %s — compressing",
                    conversation_id[:8],
                )

    history = conversation_state.get("history")
    if not history or not isinstance(history, list):
        # Touch timer even for empty history (session is active)
        if session_timer and conversation_id:
            session_timer.touch(conversation_id)
        return body, stats

    # Messages beyond this index are "recent" and must not be touched.
    protect_start = max(0, len(history) - PROTECT_RECENT_ENTRIES)

    for i in range(protect_start):
        msg = history[i]
        if not isinstance(msg, dict):
            continue

        if "userInputMessage" in msg:
            _compress_user_message(msg["userInputMessage"], turn_index=i, stats=stats)
        elif "assistantResponseMessage" in msg:
            _compress_assistant_message(msg["assistantResponseMessage"], stats=stats)

    # Deterministic serialization (byte-stable prefix for cache hits)
    compressed_body = json.dumps(req, sort_keys=True, separators=(",", ":")).encode()

    # Record this request so subsequent requests within the TTL pass through.
    if session_timer and conversation_id:
        session_timer.touch(conversation_id)

    return compressed_body, stats


def _compress_user_message(
    um: dict[str, Any],
    *,
    turn_index: int,
    stats: dict[str, int],
) -> None:
    """Compress a kiro userInputMessage in-place."""
    # --- Strip old images ---
    # Guard: skip if already annotated (idempotency — prevents annotation
    # stacking on retry or multiple compression passes).
    images = um.get("images")
    existing_content = um.get("content") or ""
    if images and isinstance(images, list) and len(images) > 0:
        if "screenshot(s) from turn" not in existing_content:
            count = len(images)
            turn_num = turn_index // 2 + 1
            annotation = f"\n[{count} screenshot(s) from turn {turn_num} removed]"
            um["content"] = existing_content + annotation
            stats["images_stripped"] += count
        um["images"] = []

    # --- Compress tool results ---
    ctx = um.get("userInputMessageContext")
    if not ctx or not isinstance(ctx, dict):
        return

    tool_results = ctx.get("toolResults")
    if not tool_results or not isinstance(tool_results, list):
        return

    for tr in tool_results:
        if not isinstance(tr, dict):
            continue
        content_parts = tr.get("content")
        if not content_parts or not isinstance(content_parts, list):
            continue
        for part in content_parts:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if not isinstance(text, str):
                continue
            if len(text) < MIN_COMPRESS_CHARS:
                continue

            compressed = _smart_crush_text(text)
            if compressed is not None and len(compressed) < len(text) * 0.8:
                part["text"] = compressed
                stats["tool_results_compressed"] += 1


def _compress_assistant_message(
    arm: dict[str, Any],
    *,
    stats: dict[str, int],
) -> None:
    """Truncate old verbose assistant responses."""
    content = arm.get("content")
    if not isinstance(content, str):
        return
    if len(content) <= ASSISTANT_TRUNCATE_THRESHOLD:
        return

    truncated_len = len(content) - ASSISTANT_TRUNCATE_KEEP
    arm["content"] = (
        content[:ASSISTANT_TRUNCATE_KEEP]
        + f"\n[... {truncated_len:,} chars truncated]"
    )
    stats["assistant_responses_truncated"] += 1


def _smart_crush_text(text: str) -> str | None:
    """Apply headroom's SmartCrusher to a tool result text.

    Returns compressed text, or None if not beneficial. Falls back to
    structural truncation if SmartCrusher can't handle the input.
    """
    try:
        from headroom.transforms.smart_crusher import smart_crush_tool_output

        crushed, was_modified, info = smart_crush_tool_output(text)
        if was_modified:
            logger.debug("SmartCrusher: %s", info)
            return crushed
    except ImportError:
        logger.debug("SmartCrusher not available (headroom-ai not installed)")
    except Exception as exc:
        logger.debug("SmartCrusher failed, falling back: %s", exc)

    return _truncate_with_summary(text)


def _truncate_with_summary(text: str, max_chars: int = 500) -> str | None:
    """Truncate text keeping the beginning + a size annotation."""
    if len(text) <= max_chars:
        return None

    lines = text.split("\n")
    kept_lines: list[str] = []
    char_count = 0
    for line in lines:
        if char_count + len(line) + 1 > max_chars - 80:
            break
        kept_lines.append(line)
        char_count += len(line) + 1

    if not kept_lines:
        kept_lines = [text[:max_chars - 80]]

    footer = f"\n[... truncated from {len(text):,} chars / {len(lines)} lines]"
    return "\n".join(kept_lines) + footer
