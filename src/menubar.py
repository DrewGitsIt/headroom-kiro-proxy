"""
Kiro Proxy — macOS menu bar applet.

Shows compression stats in the menu bar. Polls the proxy's /stats endpoint
every 10 seconds and updates the display.

Usage:
    python src/menubar.py

Requires: rumps (pip install rumps)
"""

import json
import os
import urllib.request
from datetime import datetime, timezone

import rumps

STATS_URL = "http://127.0.0.1:9090/stats"
HEALTH_URL = "http://127.0.0.1:9090/health"
POLL_INTERVAL = 10  # seconds
ICON_PATH = os.path.join(os.path.dirname(__file__), "..", "assets", "mushroom-16.png")

# Cost estimate: Claude Opus 4.6 input pricing ($5/MTok).
# Source: https://platform.claude.com/docs/en/about-claude/pricing
# Assumes ~4 chars/token (Anthropic's stated approximation for English text).
# JSON tokenizes less efficiently (~2.5-3 chars/token due to keys and brackets),
# so actual savings are likely 25-40% higher than reported here. We intentionally
# understate to stay conservative.
COST_PER_BYTE = 5.0 / 1_000_000 / 4


class KiroProxyApp(rumps.App):
    def __init__(self):
        super().__init__(
            "Kiro Proxy",
            icon=ICON_PATH if os.path.exists(ICON_PATH) else None,
            template=True,  # macOS handles dark/light mode
        )

        # Menu items — using callbacks to make them "clickable" (renders full brightness)
        self.status_item = rumps.MenuItem("Status: checking...", callback=self._noop)
        self.savings_item = rumps.MenuItem("Savings: --", callback=self._noop)
        self.requests_item = rumps.MenuItem("Requests: --", callback=self._noop)
        self.bytes_item = rumps.MenuItem("Bytes saved: --", callback=self._noop)
        self.cost_item = rumps.MenuItem("Est. saved: --", callback=self._noop)
        self.last_item = rumps.MenuItem("Last compressed: --", callback=self._noop)
        self.errors_item = rumps.MenuItem("Errors: --", callback=self._noop)

        self.menu = [
            self.status_item,
            None,  # separator
            self.savings_item,
            self.requests_item,
            self.bytes_item,
            self.cost_item,
            None,  # separator
            self.last_item,
            self.errors_item,
            None,  # separator
        ]

    @staticmethod
    def _noop(_):
        """No-op callback — makes menu items render at full brightness."""
        pass

    @rumps.timer(POLL_INTERVAL)
    def _poll(self, _):
        """Timer callback on main runloop — polls /stats and updates UI."""
        try:
            self._update_stats()
        except Exception:
            self._set_offline()

    def _update_stats(self):
        """Fetch stats and update menu items."""
        req = urllib.request.Request(STATS_URL, method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())

        savings_pct = data.get("savings_percent", 0)
        requests_total = data.get("requests_total", 0)
        requests_compressed = data.get("requests_compressed", 0)
        bytes_before = data.get("bytes_before", 0)
        bytes_after = data.get("bytes_after", 0)
        bytes_saved = bytes_before - bytes_after
        errors = data.get("errors_fallen_through", 0)
        last_at = data.get("last_request_at", "")

        # Update title (shown in menu bar next to icon)
        if requests_total > 0 and savings_pct > 0:
            self.title = f" {savings_pct:.0f}%"
        else:
            self.title = ""

        # Update menu items
        self.status_item.title = "Status: ● Running"
        self.savings_item.title = f"Savings: {savings_pct:.1f}%"
        self.requests_item.title = f"Requests: {requests_compressed}/{requests_total} compressed"
        self.bytes_item.title = f"Bytes saved: {self._human_bytes(bytes_saved)}"

        cost_saved = bytes_saved * COST_PER_BYTE
        self.cost_item.title = f"Est. saved: ~${cost_saved:.2f}"

        if last_at:
            self.last_item.title = f"Last compressed: {self._relative_time(last_at)}"
        else:
            self.last_item.title = "Last compressed: none yet"

        if errors > 0:
            self.errors_item.title = f"⚠️ {errors} error(s) (fell through)"
        else:
            self.errors_item.title = "✓ 0 errors"

    def _set_offline(self):
        """Update UI to show proxy is offline."""
        self.title = " ⚠️"
        self.status_item.title = "Status: ○ Offline"
        self.savings_item.title = "Savings: --"
        self.requests_item.title = "Requests: --"
        self.bytes_item.title = "Bytes saved: --"
        self.cost_item.title = "Est. saved: --"
        self.last_item.title = "Last compressed: --"
        self.errors_item.title = "Errors: --"

    @staticmethod
    def _human_bytes(n: int) -> str:
        """Format bytes as human-readable."""
        if n < 1024:
            return f"{n} B"
        elif n < 1024 * 1024:
            return f"{n / 1024:.1f} KB"
        else:
            return f"{n / (1024 * 1024):.1f} MB"

    @staticmethod
    def _relative_time(iso_str: str) -> str:
        """Convert ISO timestamp to relative time."""
        try:
            # Parse UTC timestamp correctly
            dt = datetime.strptime(iso_str, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
            now = datetime.now(timezone.utc)
            diff = (now - dt).total_seconds()

            if diff < 0:
                return "just now"
            elif diff < 60:
                return f"{int(diff)}s ago"
            elif diff < 3600:
                return f"{int(diff / 60)}m ago"
            elif diff < 86400:
                return f"{int(diff / 3600)}h ago"
            else:
                return f"{int(diff / 86400)}d ago"
        except (ValueError, OverflowError):
            return iso_str


if __name__ == "__main__":
    KiroProxyApp().run()
