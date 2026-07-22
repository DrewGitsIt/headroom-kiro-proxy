"""Hourly metrics reporter for kiro-proxy.

Uploads compression stats as JSON to S3, partitioned by year/month for
Athena query efficiency. Uses the user's existing AWS credentials via
boto3's default credential chain (SSO, env vars, or credential file).

Layout in S3:
    s3://kiro-proxy-metrics-111452723372/metrics/year=YYYY/month=MM/{install_id}/{date}.json

Athena table:
    Database: kiro_proxy (in ai-platform-dev, account 111452723372)
    Table: daily_metrics
    Partitions: year (string), month (string)

This module must never crash the proxy. Every public entry point wraps
in try/except Exception.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("kiro_proxy.reporter")

# --- Configuration ---

_BUCKET = "kiro-proxy-metrics-111452723372"
_PREFIX = "metrics"
_REGION = "us-east-1"
_AWS_PROFILE = "ai-platform-dev"
_CONFIG_DIR = Path.home() / ".kiro-proxy"
_INSTALL_ID_FILE = _CONFIG_DIR / "install_id"
_CONFIG_FILE = _CONFIG_DIR / "config"
_LAST_REPORT_FILE = _CONFIG_DIR / "last_reported_date"
_LAST_ERROR_FILE = _CONFIG_DIR / "last_report_error"

# Report interval: ~60 minutes ± random jitter to avoid thundering herd
_REPORT_INTERVAL_BASE_SECONDS = 3600
_REPORT_JITTER_SECONDS = 600  # ±10 minutes


def is_telemetry_enabled() -> bool:
    """Check if telemetry is enabled in config."""
    try:
        if not _CONFIG_FILE.exists():
            return True  # Default: enabled
        content = _CONFIG_FILE.read_text()
        for line in content.splitlines():
            if line.strip().startswith("telemetry="):
                return line.strip().split("=", 1)[1].lower() in ("on", "true", "1")
        return True  # Default: enabled if not specified
    except Exception:
        return True


def get_install_id() -> str:
    """Get or create a stable install ID (UUID4)."""
    try:
        if _INSTALL_ID_FILE.exists():
            return _INSTALL_ID_FILE.read_text().strip()
        install_id = str(uuid.uuid4())
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _INSTALL_ID_FILE.write_text(install_id)
        return install_id
    except Exception as exc:
        logger.debug("Cannot read/create install_id: %s", exc)
        return "unknown"


def build_payload(stats: dict[str, Any], proxy_version: str = "0.4.0") -> dict[str, Any]:
    """Build the metrics payload from proxy stats.

    The schema matches the Glue table `kiro_proxy.daily_metrics`:
        install_id, proxy_version, report_date, requests_total,
        requests_compressed, bytes_saved, est_tokens_saved,
        est_cost_saved_usd, avg_savings_pct, images_stripped,
        tool_results_compressed, assistant_responses_truncated,
        errors_fallen_through
    """
    now = datetime.now(timezone.utc)
    install_id = get_install_id()

    # Read directly from get_stats() output keys
    requests_total = stats.get("requests_total", 0)
    requests_compressed = stats.get("requests_compressed", 0)
    bytes_saved = stats.get("bytes_saved", 0)
    est_tokens_saved = stats.get("est_tokens_saved", bytes_saved // 4)
    est_cost_saved_usd = stats.get("est_cost_saved_usd", 0.0)
    avg_savings_pct = stats.get("avg_savings_pct", 0.0)

    return {
        "install_id": install_id,
        "proxy_version": proxy_version,
        "report_date": now.strftime("%Y-%m-%d"),
        "requests_total": requests_total,
        "requests_compressed": requests_compressed,
        "bytes_saved": bytes_saved,
        "est_tokens_saved": est_tokens_saved,
        "est_cost_saved_usd": round(est_cost_saved_usd, 4),
        "avg_savings_pct": round(avg_savings_pct, 2),
        "images_stripped": stats.get("images_stripped", 0),
        "tool_results_compressed": stats.get("tool_results_compressed", 0),
        "assistant_responses_truncated": stats.get("assistant_responses_truncated", 0),
        "errors_fallen_through": stats.get("errors_fallen_through", 0),
    }


def _upload_to_s3(payload: dict[str, Any]) -> None:
    """Upload a metrics payload to S3 using boto3.

    Uses the user's existing AWS credential chain. If credentials are
    missing or expired, raises an exception (caught by the caller).
    """
    import boto3  # noqa: E402 — import here so missing boto3 doesn't crash module load

    now = datetime.now(timezone.utc)
    install_id = payload["install_id"]
    report_date = payload["report_date"]

    # S3 key with Hive-style partitions for Athena
    key = (
        f"{_PREFIX}/year={now.strftime('%Y')}/month={now.strftime('%m')}"
        f"/{install_id}/{report_date}.json"
    )

    session = boto3.Session(profile_name=_AWS_PROFILE, region_name=_REGION)
    s3 = session.client("s3")
    s3.put_object(
        Bucket=_BUCKET,
        Key=key,
        Body=json.dumps(payload).encode("utf-8"),
        ContentType="application/json",
    )
    logger.info("Reported metrics to s3://%s/%s", _BUCKET, key)


def report_now(stats: dict[str, Any]) -> bool:
    """Attempt a single metrics report. Returns True on success.

    On success, also drains any queued unreported days.
    On failure, queues today's payload for retry next time.
    """
    if not is_telemetry_enabled():
        return False

    try:
        payload = build_payload(stats)
        _upload_to_s3(payload)

        # Record success
        _LAST_REPORT_FILE.write_text(payload["report_date"])
        if _LAST_ERROR_FILE.exists():
            _LAST_ERROR_FILE.unlink()

        # Drain queued reports from prior failed days
        _drain_queue()

        # Remove today from queue (if it was queued from an earlier failed attempt)
        _dequeue(payload["report_date"])

        return True

    except ImportError:
        _record_error("boto3 not installed")
        logger.debug("boto3 not available, skipping metrics report")
        return False
    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        _record_error(error_msg)
        logger.debug("Metrics report failed: %s", error_msg)
        # Queue today's payload for retry
        try:
            payload = build_payload(stats)
            _enqueue(payload)
        except Exception:
            pass
        return False


# --- Report queue (unreported days) ---

_QUEUE_DIR = _CONFIG_DIR / "report_queue"
_MAX_QUEUED_DAYS = 30  # Drop data older than 30 days


def _enqueue(payload: dict[str, Any]) -> None:
    """Save a failed report to the queue for later retry."""
    try:
        _QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        queue_file = _QUEUE_DIR / f"{payload['report_date']}.json"
        queue_file.write_text(json.dumps(payload))
        # Prune queue if over cap
        files = sorted(_QUEUE_DIR.glob("*.json"))
        while len(files) > _MAX_QUEUED_DAYS:
            files[0].unlink()
            files = files[1:]
    except Exception:
        pass


def _dequeue(report_date: str) -> None:
    """Remove a date from the queue (successfully reported)."""
    try:
        queue_file = _QUEUE_DIR / f"{report_date}.json"
        if queue_file.exists():
            queue_file.unlink()
    except Exception:
        pass


def _drain_queue() -> None:
    """Upload all queued reports. Best-effort — failures stay queued."""
    if not _QUEUE_DIR.exists():
        return
    for queue_file in sorted(_QUEUE_DIR.glob("*.json")):
        try:
            payload = json.loads(queue_file.read_text())
            _upload_to_s3(payload)
            queue_file.unlink()
            logger.debug("Drained queued report for %s", payload.get("report_date", "?"))
        except Exception as exc:
            logger.debug("Queue drain failed for %s: %s", queue_file.name, exc)
            break  # Stop on first failure (credentials still bad)


def _record_error(msg: str) -> None:
    """Persist the last error reason for 'kiro-proxy telemetry status'."""
    try:
        _LAST_ERROR_FILE.write_text(msg)
    except Exception:
        pass


async def start_periodic_reporter(get_stats_fn) -> None:
    """Run the reporter loop — fires approximately once per hour.

    get_stats_fn: callable that returns the current stats dict.
    This coroutine runs forever (until cancelled) and never raises.
    """
    if not is_telemetry_enabled():
        logger.info("Telemetry disabled, reporter not starting")
        return

    # Initial delay: random 1-5 minutes so we don't report on startup
    initial_delay = random.uniform(60, 300)
    await asyncio.sleep(initial_delay)

    while True:
        try:
            stats = get_stats_fn()
            # Run S3 upload in executor to avoid blocking the event loop
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, report_now, stats)
        except Exception as exc:
            logger.debug("Reporter loop error: %s", exc)

        # Next report: base interval ± jitter
        interval = _REPORT_INTERVAL_BASE_SECONDS + random.uniform(
            -_REPORT_JITTER_SECONDS, _REPORT_JITTER_SECONDS
        )
        await asyncio.sleep(interval)


def get_telemetry_status() -> dict[str, Any]:
    """Return telemetry status for 'kiro-proxy telemetry status'."""
    status = {
        "enabled": is_telemetry_enabled(),
        "install_id": get_install_id(),
        "last_report_date": None,
        "last_error": None,
    }
    try:
        if _LAST_REPORT_FILE.exists():
            status["last_report_date"] = _LAST_REPORT_FILE.read_text().strip()
    except Exception:
        pass
    try:
        if _LAST_ERROR_FILE.exists():
            status["last_error"] = _LAST_ERROR_FILE.read_text().strip()
    except Exception:
        pass
    return status
