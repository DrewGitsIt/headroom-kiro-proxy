"""Stats tracking for the kiro compression proxy.

Maintains rolling counters, timing histograms, and cost estimates.
Thread-safe access isn't needed because the proxy is single-threaded
(asyncio event loop).

Persistence (Option C): Reportable counters are flushed to a local JSON
file every ~10 minutes and on SIGTERM. On startup, prior values are loaded
so the daily total survives proxy restarts. The file is reset at midnight
(when the date changes).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("kiro_proxy.stats")

_MAX_HISTORY = 50

# Approximate cost estimate — assumes Sonnet input pricing ($3/MTok).
# This is a rough estimate for the stats display; actual cost depends on
# which model kiro-cli is using (Opus, Sonnet, Haiku, etc.).
_COST_PER_INPUT_TOKEN = 3.0 / 1_000_000
_CHARS_PER_TOKEN = 4

# Path to the daily totals persistence file
_DAILY_TOTALS_FILE = Path.home() / ".kiro-proxy" / "daily_totals.json"

# Keys that are cumulative counters (reportable, survive restarts)
_REPORTABLE_KEYS = (
    "requests_total",
    "requests_compressed",
    "tunnels_passthrough",
    "bytes_request_original",
    "bytes_request_sent",
    "bytes_response_total",
    "images_stripped",
    "tool_results_compressed",
    "assistant_responses_truncated",
    "errors_fallen_through",
)

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


# --- Daily totals persistence ---


def load_daily_totals() -> None:
    """Load prior daily totals from disk into _stats on startup.

    If the file's date matches today, add its counters to the in-memory
    stats (accumulate across restarts). If the date is stale (yesterday
    or older), ignore it — the new day starts fresh.
    """
    try:
        if not _DAILY_TOTALS_FILE.exists():
            return
        data = json.loads(_DAILY_TOTALS_FILE.read_text())
        file_date = data.get("date", "")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if file_date != today:
            logger.debug("Daily totals file is from %s (today is %s), ignoring", file_date, today)
            return
        for key in _REPORTABLE_KEYS:
            if key in data:
                _stats[key] += data[key]
        logger.info("Loaded daily totals from prior session (%d requests so far today)", _stats["requests_total"])
    except Exception as exc:
        logger.debug("Could not load daily totals: %s", exc)


def flush_daily_totals() -> None:
    """Write current reportable counters to disk.

    Called periodically (~10 min) and on SIGTERM. The file is a single
    JSON object — overwritten in place, not appended.
    """
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        data: dict[str, Any] = {"date": today}
        for key in _REPORTABLE_KEYS:
            data[key] = _stats[key]
        _DAILY_TOTALS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _DAILY_TOTALS_FILE.write_text(json.dumps(data))
        logger.debug("Flushed daily totals to %s", _DAILY_TOTALS_FILE)
    except Exception as exc:
        logger.debug("Could not flush daily totals: %s", exc)
