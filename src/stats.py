"""Stats tracking for the kiro compression proxy.

Maintains rolling counters, timing histograms, and cost estimates.
Thread-safe access isn't needed because the proxy is single-threaded
(asyncio event loop).
"""

from __future__ import annotations

import time
from typing import Any

_MAX_HISTORY = 50

# Approximate cost estimate — assumes Sonnet input pricing ($3/MTok).
# This is a rough estimate for the stats display; actual cost depends on
# which model kiro-cli is using (Opus, Sonnet, Haiku, etc.).
_COST_PER_INPUT_TOKEN = 3.0 / 1_000_000
_CHARS_PER_TOKEN = 4

# Global stats dict — mutated by the proxy and read by /stats endpoint
_stats: dict[str, Any] = {
    "requests_total": 0,
    "requests_compressed": 0,
    "tunnels_passthrough": 0,
    "bytes_request_original": 0,
    "bytes_request_sent": 0,
    "bytes_response_total": 0,
    "images_stripped": 0,
    "tool_results_compressed": 0,
    "assistant_responses_truncated": 0,
    "errors_fallen_through": 0,
    "last_request_at": "",
    "last_ttfb_ms": 0,
    "last_original_kb": 0.0,
    "last_compressed_kb": 0.0,
    "last_savings_pct": 0.0,
    "last_response_size_kb": 0.0,
    "ttfb_ms_history": [],
    "response_time_ms_history": [],
    "savings_pct_history": [],
}
_started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def record_request_timing(ttfb_ms: float, total_ms: float) -> None:
    """Record TTFB and total response time into rolling history."""
    _stats["ttfb_ms_history"].append(round(ttfb_ms))
    if len(_stats["ttfb_ms_history"]) > _MAX_HISTORY:
        _stats["ttfb_ms_history"] = _stats["ttfb_ms_history"][-_MAX_HISTORY:]
    _stats["response_time_ms_history"].append(round(total_ms))
    if len(_stats["response_time_ms_history"]) > _MAX_HISTORY:
        _stats["response_time_ms_history"] = _stats["response_time_ms_history"][-_MAX_HISTORY:]


def record_compression(original_bytes: int, compressed_bytes: int) -> None:
    """Record per-request compression stats."""
    if original_bytes > 0:
        savings_pct = round((1 - compressed_bytes / original_bytes) * 100, 1)
    else:
        savings_pct = 0.0
    _stats["last_original_kb"] = round(original_bytes / 1024, 1)
    _stats["last_compressed_kb"] = round(compressed_bytes / 1024, 1)
    _stats["last_savings_pct"] = savings_pct
    _stats["savings_pct_history"].append(savings_pct)
    if len(_stats["savings_pct_history"]) > _MAX_HISTORY:
        _stats["savings_pct_history"] = _stats["savings_pct_history"][-_MAX_HISTORY:]


def reset_stats() -> None:
    """Reset all stats to initial values. Used by test fixtures."""
    _stats.update({
        "requests_total": 0,
        "requests_compressed": 0,
        "tunnels_passthrough": 0,
        "bytes_request_original": 0,
        "bytes_request_sent": 0,
        "bytes_response_total": 0,
        "images_stripped": 0,
        "tool_results_compressed": 0,
        "assistant_responses_truncated": 0,
        "errors_fallen_through": 0,
        "last_request_at": "",
        "last_ttfb_ms": 0,
        "last_original_kb": 0.0,
        "last_compressed_kb": 0.0,
        "last_savings_pct": 0.0,
        "last_response_size_kb": 0.0,
        "ttfb_ms_history": [],
        "response_time_ms_history": [],
        "savings_pct_history": [],
    })


def get_stats() -> dict[str, Any]:
    """Return a snapshot of all proxy stats for /stats and reporter."""
    bytes_orig = _stats["bytes_request_original"]
    bytes_sent = _stats["bytes_request_sent"]
    bytes_saved = bytes_orig - bytes_sent

    cumulative_savings_pct = round(
        (1 - bytes_sent / bytes_orig) * 100, 1
    ) if bytes_orig > 0 else 0.0

    savings_history = _stats["savings_pct_history"]
    avg_savings_pct = (
        round(sum(savings_history) / len(savings_history), 1)
        if savings_history else 0.0
    )

    tokens_saved_estimate = round(bytes_saved / _CHARS_PER_TOKEN)
    cost_saved_estimate = round(tokens_saved_estimate * _COST_PER_INPUT_TOKEN, 2)

    ttfb_history = _stats["ttfb_ms_history"]
    avg_ttfb_ms = round(sum(ttfb_history) / len(ttfb_history)) if ttfb_history else 0
    response_history = _stats["response_time_ms_history"]
    avg_response_ms = (
        round(sum(response_history) / len(response_history))
        if response_history else 0
    )

    return {
        "requests_total": _stats["requests_total"],
        "requests_compressed": _stats["requests_compressed"],
        "tunnels_passthrough": _stats["tunnels_passthrough"],
        "bytes_saved": bytes_saved,
        "cumulative_savings_pct": cumulative_savings_pct,
        "last_original_kb": _stats["last_original_kb"],
        "last_compressed_kb": _stats["last_compressed_kb"],
        "last_savings_pct": _stats["last_savings_pct"],
        "last_response_size_kb": _stats["last_response_size_kb"],
        "last_request_at": _stats["last_request_at"],
        "last_ttfb_ms": _stats["last_ttfb_ms"],
        "avg_savings_pct": avg_savings_pct,
        "avg_ttfb_ms": avg_ttfb_ms,
        "avg_response_ms": avg_response_ms,
        "images_stripped": _stats["images_stripped"],
        "tool_results_compressed": _stats["tool_results_compressed"],
        "assistant_responses_truncated": _stats["assistant_responses_truncated"],
        "errors_fallen_through": _stats["errors_fallen_through"],
        "est_tokens_saved": tokens_saved_estimate,
        "est_cost_saved_usd": cost_saved_estimate,
        "started_at": _started_at,
    }
