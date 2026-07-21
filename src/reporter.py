"""Daily metrics reporter for kiro-proxy.

Uploads a single JSON record per day to S3 using AWS SigV4 signing
(stdlib only — no boto3). All errors are caught and logged; this module
must never crash the proxy.

Layout of ~/.kiro-proxy/:
    aws_credentials   — INI: [default] aws_access_key_id / aws_secret_access_key
    install_id        — plain UUID4, generated on first run
    config            — optional INI-like; telemetry=false disables uploads
    last_reported_date — plain YYYY-MM-DD, written after a successful upload
    last_report.json  — copy of the last payload sent, for user inspection
"""

from __future__ import annotations

import configparser
import hashlib
import hmac
import json
import logging
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import uuid

logger = logging.getLogger("kiro_proxy.reporter")

# ── constants ────────────────────────────────────────────────────────────────

_BUCKET = "kiro-proxy-metrics-111452723372"
_REGION = "us-east-1"
_PROXY_DIR = Path.home() / ".kiro-proxy"

_CREDENTIALS_FILE = _PROXY_DIR / "aws_credentials"
_INSTALL_ID_FILE = _PROXY_DIR / "install_id"
_CONFIG_FILE = _PROXY_DIR / "config"
_LAST_DATE_FILE = _PROXY_DIR / "last_reported_date"
_LAST_REPORT_FILE = _PROXY_DIR / "last_report.json"

# ── credential / config helpers ──────────────────────────────────────────────


def _read_credentials() -> tuple[str, str] | None:
    """Return (access_key_id, secret_access_key) or None if unavailable."""
    if not _CREDENTIALS_FILE.exists():
        logger.debug("reporter: credentials file not found at %s", _CREDENTIALS_FILE)
        return None

    parser = configparser.ConfigParser()
    try:
        parser.read(_CREDENTIALS_FILE)
    except configparser.Error as exc:
        logger.debug("reporter: failed to parse credentials: %s", exc)
        return None

    section = "default"
    if not parser.has_section(section):
        # Try without section header — configparser needs one
        section = parser.sections()[0] if parser.sections() else None
    if section is None:
        logger.debug("reporter: no sections in credentials file")
        return None

    key_id = parser.get(section, "aws_access_key_id", fallback=None)
    secret = parser.get(section, "aws_secret_access_key", fallback=None)

    if not key_id or not secret:
        logger.debug("reporter: aws_access_key_id or aws_secret_access_key missing")
        return None

    assert key_id.strip(), "access key id must not be blank"
    assert secret.strip(), "secret access key must not be blank"
    return key_id.strip(), secret.strip()


def get_install_id() -> str:
    """Return persistent install ID, generating one on first call."""
    _PROXY_DIR.mkdir(parents=True, exist_ok=True)

    if _INSTALL_ID_FILE.exists():
        value = _INSTALL_ID_FILE.read_text().strip()
        if value:
            assert len(value) > 0, "install_id must not be empty"
            return value

    new_id = str(uuid.uuid4())
    _INSTALL_ID_FILE.write_text(new_id + "\n")
    logger.debug("reporter: generated new install_id %s", new_id)
    assert len(new_id) == 36, "UUID must be 36 chars"
    return new_id


def is_telemetry_enabled() -> bool:
    """Return False if the user has explicitly opted out via telemetry=false."""
    if not _CONFIG_FILE.exists():
        return True  # opt-in by default

    try:
        content = _CONFIG_FILE.read_text()
    except OSError as exc:
        logger.debug("reporter: cannot read config: %s", exc)
        return True  # fail open

    for line in content.splitlines():
        stripped = line.strip().lower()
        if stripped == "telemetry=false":
            return False

    return True


def should_report_today() -> bool:
    """Return True if we have not already reported today (UTC date)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if not _LAST_DATE_FILE.exists():
        return True

    try:
        last = _LAST_DATE_FILE.read_text().strip()
    except OSError as exc:
        logger.debug("reporter: cannot read last_reported_date: %s", exc)
        return True  # fail open

    assert isinstance(last, str), "last_reported_date must be a string"
    return last != today


# ── payload builder ──────────────────────────────────────────────────────────


def build_payload(stats: dict[str, Any], version: str) -> dict[str, Any]:
    """Build the JSON payload from the live /stats dict."""
    assert isinstance(stats, dict), "stats must be a dict"
    assert isinstance(version, str), "version must be a string"

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    install_id = get_install_id()

    # Uptime: parse started_at from ISO8601 UTC string
    session_uptime_hours = 0.0
    started_at = stats.get("started_at", "")
    if started_at:
        try:
            fmt = "%Y-%m-%dT%H:%M:%SZ"
            start_dt = datetime.strptime(started_at, fmt).replace(tzinfo=timezone.utc)
            delta = datetime.now(timezone.utc) - start_dt
            session_uptime_hours = round(delta.total_seconds() / 3600, 2)
        except ValueError:
            logger.debug("reporter: could not parse started_at: %s", started_at)

    assert len(install_id) > 0, "install_id must not be empty"
    assert session_uptime_hours >= 0.0, "uptime must not be negative"

    return {
        "install_id": install_id,
        "proxy_version": version,
        "report_date": today,
        "requests_total": int(stats.get("requests_total", 0)),
        "requests_compressed": int(stats.get("requests_compressed", 0)),
        "bytes_saved": int(stats.get("bytes_saved", 0)),
        "est_tokens_saved": int(stats.get("est_tokens_saved", 0)),
        "est_cost_saved_usd": float(stats.get("est_cost_saved_usd", 0.0)),
        "avg_savings_pct": float(stats.get("avg_savings_pct", 0.0)),
        "images_stripped": int(stats.get("images_stripped", 0)),
        "tool_results_compressed": int(stats.get("tool_results_compressed", 0)),
        "assistant_responses_truncated": int(stats.get("assistant_responses_truncated", 0)),
        "errors_fallen_through": int(stats.get("errors_fallen_through", 0)),
        "session_uptime_hours": session_uptime_hours,
    }


# ── SigV4 signing ─────────────────────────────────────────────────────────────


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _sigv4_signing_key(secret_key: str, date_str: str, region: str, service: str) -> bytes:
    """Derive the SigV4 signing key for a given date/region/service."""
    k_date = _hmac_sha256(("AWS4" + secret_key).encode("utf-8"), date_str)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    k_signing = _hmac_sha256(k_service, "aws4_request")

    assert len(k_signing) == 32, "signing key must be 32 bytes"
    return k_signing


def _sigv4_headers(
    method: str,
    host: str,
    path: str,
    payload: bytes,
    access_key_id: str,
    secret_access_key: str,
    region: str,
    service: str,
) -> dict[str, str]:
    """Return a dict of headers that include Authorization (SigV4)."""
    assert method == method.upper(), "HTTP method must be uppercase"
    assert host, "host must not be empty"
    assert path.startswith("/"), "path must start with /"

    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_str = now.strftime("%Y%m%d")

    payload_hash = _sha256_hex(payload)

    # Canonical headers — must be sorted, lowercase keys, trimmed values.
    canonical_headers = f"host:{host}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amz_date}\n"
    signed_headers = "host;x-amz-content-sha256;x-amz-date"

    canonical_request = "\n".join([
        method,
        path,
        "",  # query string
        canonical_headers,
        signed_headers,
        payload_hash,
    ])

    credential_scope = f"{date_str}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        credential_scope,
        _sha256_hex(canonical_request.encode("utf-8")),
    ])

    signing_key = _sigv4_signing_key(secret_access_key, date_str, region, service)
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = (
        f"AWS4-HMAC-SHA256 Credential={access_key_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )

    assert len(signature) == 64, "HMAC-SHA256 signature must be 64 hex chars"

    return {
        "Host": host,
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
        "Authorization": authorization,
        "Content-Type": "application/json",
        "Content-Length": str(len(payload)),
    }


# ── S3 upload ────────────────────────────────────────────────────────────────


def upload_to_s3(payload: dict[str, Any]) -> None:
    """PUT payload JSON to S3 under metrics/{install_id}/{YYYY-MM-DD}.json.

    Raises on network/auth errors so the caller can decide how to handle them.
    """
    creds = _read_credentials()
    if creds is None:
        raise RuntimeError("No AWS credentials available")

    access_key_id, secret_access_key = creds
    install_id = payload["install_id"]
    report_date = payload["report_date"]

    assert install_id, "install_id must not be empty in payload"
    assert report_date, "report_date must not be empty in payload"

    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    object_key = f"metrics/{install_id}/{report_date}.json"
    path = f"/{object_key}"
    host = f"{_BUCKET}.s3.{_REGION}.amazonaws.com"
    url = f"https://{host}{path}"

    headers = _sigv4_headers(
        method="PUT",
        host=host,
        path=path,
        payload=body,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        region=_REGION,
        service="s3",
    )

    req = urllib.request.Request(url, data=body, method="PUT", headers=headers)

    logger.debug("reporter: PUT s3://%s%s (%d bytes)", _BUCKET, path, len(body))

    with urllib.request.urlopen(req, timeout=15) as resp:
        status = resp.getcode()
        assert status in (200, 204), f"unexpected S3 response status: {status}"
        logger.info("reporter: uploaded metrics to s3://%s%s (HTTP %d)", _BUCKET, path, status)


# ── last-report local copy ───────────────────────────────────────────────────


def _write_local_copy(payload: dict[str, Any]) -> None:
    """Write a human-readable copy to ~/.kiro-proxy/last_report.json."""
    try:
        _PROXY_DIR.mkdir(parents=True, exist_ok=True)
        _LAST_REPORT_FILE.write_text(json.dumps(payload, indent=2) + "\n")
        logger.debug("reporter: wrote local copy to %s", _LAST_REPORT_FILE)
    except OSError as exc:
        logger.debug("reporter: could not write last_report.json: %s", exc)


def _mark_reported_today() -> None:
    """Persist today's UTC date so we don't double-report."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        _PROXY_DIR.mkdir(parents=True, exist_ok=True)
        _LAST_DATE_FILE.write_text(today + "\n")
    except OSError as exc:
        logger.debug("reporter: could not write last_reported_date: %s", exc)


# ── orchestrator ─────────────────────────────────────────────────────────────


def maybe_report(stats: dict[str, Any], version: str) -> None:
    """Upload today's metrics if telemetry is on and we haven't reported yet.

    Never raises — all errors are caught and logged at debug level.
    This function is safe to call from an asyncio task.
    """
    try:
        if not is_telemetry_enabled():
            logger.debug("reporter: telemetry disabled, skipping")
            return

        if not should_report_today():
            logger.debug("reporter: already reported today, skipping")
            return

        payload = build_payload(stats, version)
        assert isinstance(payload, dict), "build_payload must return a dict"

        _write_local_copy(payload)
        upload_to_s3(payload)
        _mark_reported_today()

        logger.info(
            "reporter: daily report sent — %d requests, %d bytes saved, "
            "~%d tokens, ~$%.2f",
            payload["requests_total"],
            payload["bytes_saved"],
            payload["est_tokens_saved"],
            payload["est_cost_saved_usd"],
        )

    except Exception as exc:  # noqa: BLE001
        logger.debug("reporter: upload failed (non-fatal): %s", exc)
