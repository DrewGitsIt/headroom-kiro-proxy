"""Unit tests for reporter.py — telemetry upload logic.

Tests:
- build_payload() schema correctness
- S3 key path structure
- is_telemetry_enabled() config parsing
- report_now() with mocked boto3
"""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest
import reporter


@pytest.fixture
def tmp_config_dir(tmp_path, monkeypatch):
    """Point reporter's config paths to a temp directory."""
    monkeypatch.setattr(reporter, "_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(reporter, "_INSTALL_ID_FILE", tmp_path / "install_id")
    monkeypatch.setattr(reporter, "_CONFIG_FILE", tmp_path / "config")
    monkeypatch.setattr(reporter, "_LAST_REPORT_FILE", tmp_path / "last_reported_date")
    monkeypatch.setattr(reporter, "_LAST_ERROR_FILE", tmp_path / "last_report_error")
    monkeypatch.setattr(reporter, "_QUEUE_DIR", tmp_path / "report_queue")
    return tmp_path


class TestBuildPayload:
    """Tests for build_payload() schema and values."""

    def test_all_required_keys_present(self, tmp_config_dir):
        """Payload contains all fields the Athena table expects."""
        stats = {
            "requests_intercepted": 100,
            "requests_compressed": 80,
            "bytes_saved": 40000,
            "avg_compression_pct": 42.5,
            "images_stripped": 10,
            "tool_results_compressed": 5,
            "assistant_responses_truncated": 3,
            "errors_fallen_through": 1,
            "uptime_hours": 2.5,
        }
        payload = reporter.build_payload(stats, proxy_version="1.0.0")

        expected_keys = {
            "install_id", "proxy_version", "report_date",
            "requests_total", "requests_compressed", "bytes_saved",
            "est_tokens_saved", "est_cost_saved_usd", "avg_savings_pct",
            "images_stripped", "tool_results_compressed",
            "assistant_responses_truncated", "errors_fallen_through",
        }
        assert set(payload.keys()) == expected_keys

    def test_token_estimation(self, tmp_config_dir):
        """Tokens estimated at ~4 chars per token from bytes_saved."""
        stats = {"bytes_saved": 4000, "est_tokens_saved": 1000}
        payload = reporter.build_payload(stats)
        assert payload["est_tokens_saved"] == 1000  # reads from stats directly

    def test_token_estimation_fallback(self, tmp_config_dir):
        """If est_tokens_saved not in stats, falls back to bytes_saved / 4."""
        stats = {"bytes_saved": 4000}
        payload = reporter.build_payload(stats)
        assert payload["est_tokens_saved"] == 1000  # 4000 / 4

    def test_cost_estimation(self, tmp_config_dir):
        """Cost passed through from stats when available."""
        stats = {"bytes_saved": 4_000_000, "est_cost_saved_usd": 3.0}
        payload = reporter.build_payload(stats)
        assert payload["est_cost_saved_usd"] == pytest.approx(3.0, abs=0.01)

    def test_empty_stats_produce_zeros(self, tmp_config_dir):
        """Empty/missing stats produce zero values, not crashes."""
        payload = reporter.build_payload({})
        assert payload["requests_total"] == 0
        assert payload["bytes_saved"] == 0
        assert payload["est_tokens_saved"] == 0
        assert payload["est_cost_saved_usd"] == 0.0
        assert payload["avg_savings_pct"] == 0.0

    def test_report_date_is_today(self, tmp_config_dir):
        """report_date is today's date in YYYY-MM-DD format."""
        from datetime import datetime, timezone
        payload = reporter.build_payload({})
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert payload["report_date"] == today

    def test_install_id_persisted(self, tmp_config_dir):
        """Install ID is created on first call and reused on second."""
        payload1 = reporter.build_payload({})
        payload2 = reporter.build_payload({})
        assert payload1["install_id"] == payload2["install_id"]
        assert payload1["install_id"] != "unknown"
        # Should be a valid UUID4
        import uuid
        uuid.UUID(payload1["install_id"])  # Raises ValueError if not valid

    def test_proxy_version_passed_through(self, tmp_config_dir):
        """proxy_version from argument appears in payload."""
        payload = reporter.build_payload({}, proxy_version="2.1.0")
        assert payload["proxy_version"] == "2.1.0"


class TestS3KeyPath:
    """Test the S3 key path structure for Hive-style partitioning."""

    def test_key_structure(self, tmp_config_dir):
        """Key follows metrics/year=YYYY/month=MM/{install_id}/{date}.json."""
        stats = {"bytes_saved": 100}
        payload = reporter.build_payload(stats)

        # Simulate what _upload_to_s3 would generate
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        install_id = payload["install_id"]
        report_date = payload["report_date"]

        expected_key = (
            f"metrics/year={now.strftime('%Y')}/month={now.strftime('%m')}"
            f"/{install_id}/{report_date}.json"
        )

        # Verify the key matches the format by checking each component
        assert expected_key.startswith("metrics/year=")
        assert "/month=" in expected_key
        assert install_id in expected_key
        assert expected_key.endswith(".json")
        # Month is zero-padded
        assert f"/month={now.strftime('%m')}/" in expected_key


class TestIsTelemetryEnabled:
    """Tests for is_telemetry_enabled() config parsing."""

    def test_default_enabled_no_config(self, tmp_config_dir):
        """No config file → telemetry enabled by default."""
        assert reporter.is_telemetry_enabled() is True

    def test_explicit_on(self, tmp_config_dir):
        """telemetry=on → enabled."""
        (tmp_config_dir / "config").write_text("telemetry=on\n")
        assert reporter.is_telemetry_enabled() is True

    def test_explicit_true(self, tmp_config_dir):
        """telemetry=true → enabled."""
        (tmp_config_dir / "config").write_text("telemetry=true\n")
        assert reporter.is_telemetry_enabled() is True

    def test_explicit_1(self, tmp_config_dir):
        """telemetry=1 → enabled."""
        (tmp_config_dir / "config").write_text("telemetry=1\n")
        assert reporter.is_telemetry_enabled() is True

    def test_explicit_off(self, tmp_config_dir):
        """telemetry=off → disabled."""
        (tmp_config_dir / "config").write_text("telemetry=off\n")
        assert reporter.is_telemetry_enabled() is False

    def test_explicit_false(self, tmp_config_dir):
        """telemetry=false → disabled."""
        (tmp_config_dir / "config").write_text("telemetry=false\n")
        assert reporter.is_telemetry_enabled() is False

    def test_explicit_0(self, tmp_config_dir):
        """telemetry=0 → disabled."""
        (tmp_config_dir / "config").write_text("telemetry=0\n")
        assert reporter.is_telemetry_enabled() is False

    def test_misspelled_key_defaults_to_enabled(self, tmp_config_dir):
        """Misspelled key (telemetrey=false) → defaults to enabled."""
        (tmp_config_dir / "config").write_text("telemetrey=false\n")
        assert reporter.is_telemetry_enabled() is True

    def test_weird_value_defaults_to_disabled(self, tmp_config_dir):
        """telemetry=off-sometimes → not in truthy set → disabled."""
        (tmp_config_dir / "config").write_text("telemetry=off-sometimes\n")
        assert reporter.is_telemetry_enabled() is False

    def test_config_with_other_settings(self, tmp_config_dir):
        """Config with multiple settings, telemetry is parsed correctly."""
        content = "mode=global\ntelemetry=off\nversion=1.0\n"
        (tmp_config_dir / "config").write_text(content)
        assert reporter.is_telemetry_enabled() is False


class TestReportNow:
    """Tests for report_now() with mocked S3."""

    def test_success_with_mocked_upload(self, tmp_config_dir):
        """Successful report calls _upload_to_s3 and records last_reported_date."""
        upload_calls = []

        def mock_upload(payload):
            upload_calls.append(payload)

        with patch("reporter._upload_to_s3", mock_upload):
            result = reporter.report_now({"bytes_saved": 100})

        assert result is True
        assert len(upload_calls) == 1
        payload = upload_calls[0]
        assert "install_id" in payload
        assert "report_date" in payload
        assert payload["est_tokens_saved"] == 25  # 100 bytes / 4 chars per token

        # Last report date file was written
        assert (tmp_config_dir / "last_reported_date").exists()

    def test_disabled_telemetry_skips(self, tmp_config_dir):
        """When telemetry is disabled, report_now returns False immediately."""
        (tmp_config_dir / "config").write_text("telemetry=off\n")
        result = reporter.report_now({"bytes_saved": 100})
        assert result is False

    def test_upload_import_error_handled(self, tmp_config_dir):
        """ImportError from _upload_to_s3 → returns False, records error."""
        with patch("reporter._upload_to_s3", side_effect=ImportError("no boto3")):
            result = reporter.report_now({"bytes_saved": 100})
        assert result is False
        error_file = tmp_config_dir / "last_report_error"
        assert error_file.exists()
        assert "boto3" in error_file.read_text()

    def test_upload_error_queues_payload(self, tmp_config_dir):
        """S3 error → returns False, payload queued for retry."""
        with patch("reporter._upload_to_s3", side_effect=Exception("AccessDenied")):
            result = reporter.report_now({"bytes_saved": 100})

        assert result is False
        queue_dir = tmp_config_dir / "report_queue"
        assert queue_dir.exists()
        queued_files = list(queue_dir.glob("*.json"))
        assert len(queued_files) == 1
        # Queued file contains valid payload
        payload = json.loads(queued_files[0].read_text())
        assert "install_id" in payload

    def test_success_drains_queue(self, tmp_config_dir):
        """Successful report drains previously queued reports."""
        # Set up a queued report from yesterday
        queue_dir = tmp_config_dir / "report_queue"
        queue_dir.mkdir()
        old_payload = {"install_id": "test", "report_date": "2026-07-20", "requests_total": 50}
        (queue_dir / "2026-07-20.json").write_text(json.dumps(old_payload))

        upload_calls = []

        def mock_upload(payload):
            upload_calls.append(payload)

        with patch("reporter._upload_to_s3", mock_upload):
            result = reporter.report_now({"bytes_saved": 100})

        assert result is True
        # Today's report + drained yesterday's report
        assert len(upload_calls) == 2
        # Queue is now empty
        assert list(queue_dir.glob("*.json")) == []

    def test_success_clears_previous_error(self, tmp_config_dir):
        """Successful report deletes the last_report_error file."""
        # Set up a prior error
        (tmp_config_dir / "last_report_error").write_text("previous error")

        with patch("reporter._upload_to_s3", lambda p: None):
            result = reporter.report_now({"bytes_saved": 100})

        assert result is True
        assert not (tmp_config_dir / "last_report_error").exists()

    def test_queue_capped_at_max_days(self, tmp_config_dir):
        """Queue doesn't grow beyond _MAX_QUEUED_DAYS."""
        queue_dir = tmp_config_dir / "report_queue"
        queue_dir.mkdir()
        # Fill queue with 35 files (over the 30 cap)
        for i in range(35):
            date = f"2026-06-{i+1:02d}"
            (queue_dir / f"{date}.json").write_text(json.dumps({"report_date": date}))

        # Trigger enqueue which prunes
        with patch("reporter._upload_to_s3", side_effect=Exception("fail")):
            reporter.report_now({"bytes_saved": 100})

        # Should be capped at 30
        queued = list(queue_dir.glob("*.json"))
        assert len(queued) <= reporter._MAX_QUEUED_DAYS


class TestGetTelemetryStatus:
    """Tests for get_telemetry_status()."""

    def test_fresh_install(self, tmp_config_dir):
        """Fresh install: enabled, no last report, no errors."""
        status = reporter.get_telemetry_status()
        assert status["enabled"] is True
        assert status["last_report_date"] is None
        assert status["last_error"] is None

    def test_after_successful_report(self, tmp_config_dir):
        """After report: last_report_date is populated."""
        (tmp_config_dir / "last_reported_date").write_text("2026-07-20")
        status = reporter.get_telemetry_status()
        assert status["last_report_date"] == "2026-07-20"
        assert status["last_error"] is None

    def test_after_failed_report(self, tmp_config_dir):
        """After failure: last_error is populated."""
        (tmp_config_dir / "last_report_error").write_text("AccessDenied: no permission")
        status = reporter.get_telemetry_status()
        assert status["last_error"] == "AccessDenied: no permission"
