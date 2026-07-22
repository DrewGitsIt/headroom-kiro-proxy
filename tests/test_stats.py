"""Unit tests for stats.py — metrics tracking and aggregation.

Uses reset_stats() fixture to isolate test state between runs.
"""

import pytest
from stats import (
    _stats,
    _MAX_HISTORY,
    record_compression,
    record_request_timing,
    get_stats,
    reset_stats,
)


@pytest.fixture(autouse=True)
def clean_stats():
    """Reset stats before each test to prevent cross-test contamination."""
    reset_stats()
    yield
    reset_stats()


class TestRecordCompression:
    """Tests for record_compression()."""

    def test_basic_savings(self):
        """Records correct savings percentage."""
        record_compression(original_bytes=1000, compressed_bytes=600)
        assert _stats["last_original_kb"] == pytest.approx(1000 / 1024, abs=0.1)
        assert _stats["last_compressed_kb"] == pytest.approx(600 / 1024, abs=0.1)
        assert _stats["last_savings_pct"] == 40.0

    def test_zero_original_bytes(self):
        """Division by zero guard: 0 original bytes → 0% savings."""
        record_compression(original_bytes=0, compressed_bytes=0)
        assert _stats["last_savings_pct"] == 0.0

    def test_no_compression(self):
        """Compressed same size as original → 0% savings."""
        record_compression(original_bytes=500, compressed_bytes=500)
        assert _stats["last_savings_pct"] == 0.0

    def test_history_rolling_window(self):
        """savings_pct_history should cap at _MAX_HISTORY entries."""
        for i in range(_MAX_HISTORY + 20):
            record_compression(original_bytes=1000, compressed_bytes=500)
        assert len(_stats["savings_pct_history"]) == _MAX_HISTORY

    def test_history_keeps_most_recent(self):
        """Rolling window keeps the most recent values."""
        for i in range(_MAX_HISTORY + 5):
            # Vary savings so we can identify position
            record_compression(original_bytes=1000, compressed_bytes=1000 - i)
        # Last entry should be from the most recent call
        last_savings = _stats["savings_pct_history"][-1]
        expected = round((1 - (1000 - (_MAX_HISTORY + 4)) / 1000) * 100, 1)
        assert last_savings == expected


class TestRecordRequestTiming:
    """Tests for record_request_timing()."""

    def test_appends_to_history(self):
        """Records TTFB and total time."""
        record_request_timing(ttfb_ms=42.7, total_ms=150.3)
        assert _stats["ttfb_ms_history"] == [43]
        assert _stats["response_time_ms_history"] == [150]

    def test_rolling_window_cap(self):
        """History caps at _MAX_HISTORY entries."""
        for i in range(_MAX_HISTORY + 10):
            record_request_timing(ttfb_ms=float(i), total_ms=float(i * 2))
        assert len(_stats["ttfb_ms_history"]) == _MAX_HISTORY
        assert len(_stats["response_time_ms_history"]) == _MAX_HISTORY

    def test_rounding(self):
        """Values are rounded to integers."""
        record_request_timing(ttfb_ms=0.4, total_ms=0.6)
        assert _stats["ttfb_ms_history"] == [0]
        assert _stats["response_time_ms_history"] == [1]


class TestGetStats:
    """Tests for get_stats() aggregation."""

    def test_empty_stats_no_crash(self):
        """get_stats() on fresh/empty state should not crash or divide by zero."""
        result = get_stats()
        assert result["requests_total"] == 0
        assert result["bytes_saved"] == 0
        assert result["cumulative_savings_pct"] == 0.0
        assert result["avg_savings_pct"] == 0.0
        assert result["avg_ttfb_ms"] == 0
        assert result["avg_response_ms"] == 0
        assert result["est_tokens_saved"] == 0
        assert result["est_cost_saved_usd"] == 0

    def test_cumulative_savings(self):
        """Cumulative savings calculated from total bytes."""
        _stats["bytes_request_original"] = 10000
        _stats["bytes_request_sent"] = 6000
        result = get_stats()
        assert result["bytes_saved"] == 4000
        assert result["cumulative_savings_pct"] == 40.0

    def test_average_savings(self):
        """Average savings from history."""
        record_compression(1000, 600)  # 40%
        record_compression(1000, 400)  # 60%
        result = get_stats()
        assert result["avg_savings_pct"] == 50.0

    def test_average_ttfb(self):
        """Average TTFB from history."""
        record_request_timing(100.0, 200.0)
        record_request_timing(200.0, 400.0)
        result = get_stats()
        assert result["avg_ttfb_ms"] == 150
        assert result["avg_response_ms"] == 300

    def test_cost_estimate(self):
        """Token and cost estimates are derived from bytes saved."""
        _stats["bytes_request_original"] = 40000
        _stats["bytes_request_sent"] = 20000
        result = get_stats()
        # 20000 bytes saved / 4 chars per token = 5000 tokens
        assert result["est_tokens_saved"] == 5000
        # 5000 tokens * $3/1M tokens = $0.015
        assert result["est_cost_saved_usd"] == pytest.approx(0.02, abs=0.01)

    def test_started_at_is_set(self):
        """started_at should be a non-empty timestamp string."""
        result = get_stats()
        assert result["started_at"] != ""
        assert "T" in result["started_at"]  # ISO format


class TestResetStats:
    """Tests for reset_stats() itself."""

    def test_clears_counters(self):
        """All counters reset to zero."""
        _stats["requests_total"] = 42
        _stats["bytes_request_original"] = 99999
        record_compression(1000, 500)
        record_request_timing(50.0, 100.0)
        reset_stats()
        assert _stats["requests_total"] == 0
        assert _stats["bytes_request_original"] == 0
        assert _stats["ttfb_ms_history"] == []
        assert _stats["savings_pct_history"] == []


class TestDailyTotalsPersistence:
    """Tests for flush_daily_totals() and load_daily_totals()."""

    @pytest.fixture(autouse=True)
    def _use_tmp_file(self, tmp_path, monkeypatch):
        """Point the daily totals file to a temp location."""
        import stats as stats_module
        self.totals_file = tmp_path / "daily_totals.json"
        monkeypatch.setattr(stats_module, "_DAILY_TOTALS_FILE", self.totals_file)

    def test_flush_creates_file(self):
        """flush_daily_totals writes the file with today's date."""
        from stats import flush_daily_totals
        from datetime import datetime, timezone
        _stats["requests_total"] = 42
        _stats["bytes_request_original"] = 10000
        _stats["bytes_request_sent"] = 6000
        flush_daily_totals()
        assert self.totals_file.exists()
        import json
        data = json.loads(self.totals_file.read_text())
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert data["date"] == today
        assert data["requests_total"] == 42
        assert data["bytes_request_original"] == 10000

    def test_load_adds_to_stats(self):
        """load_daily_totals adds file values to current stats."""
        from stats import flush_daily_totals, load_daily_totals
        _stats["requests_total"] = 50
        _stats["bytes_request_original"] = 5000
        flush_daily_totals()
        # Simulate restart: reset stats, then load
        reset_stats()
        assert _stats["requests_total"] == 0
        load_daily_totals()
        assert _stats["requests_total"] == 50
        assert _stats["bytes_request_original"] == 5000

    def test_load_ignores_stale_date(self):
        """File from yesterday is ignored."""
        import json
        data = {"date": "2020-01-01", "requests_total": 999}
        self.totals_file.write_text(json.dumps(data))
        from stats import load_daily_totals
        _stats["requests_total"] = 0
        load_daily_totals()
        assert _stats["requests_total"] == 0  # Not loaded

    def test_load_missing_file_no_crash(self):
        """Missing file doesn't crash."""
        from stats import load_daily_totals
        load_daily_totals()  # Should not raise

    def test_flush_then_load_accumulates(self):
        """Flush with 10, restart with 5 in memory, load → 15."""
        from stats import flush_daily_totals, load_daily_totals
        _stats["requests_total"] = 10
        flush_daily_totals()
        # Simulate restart: new session has 5 requests already
        reset_stats()
        _stats["requests_total"] = 5
        load_daily_totals()
        assert _stats["requests_total"] == 15
