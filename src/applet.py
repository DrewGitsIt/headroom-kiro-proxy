"""Kiro Proxy — macOS menu bar applet (mushroom icon).

Polls the proxy's /stats endpoint every 10 seconds and displays
compression stats in the menu bar.

Requires: rumps (pip install rumps)
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import rumps

PROXY_DIR = Path.home() / ".kiro-proxy"
STATS_URL = "http://127.0.0.1:9090/stats"
HEALTH_URL = "http://127.0.0.1:9090/health"
POLL_INTERVAL = 10
PLIST_LABEL = "com.kiro-proxy.compression"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"
LOG_PATH = PROXY_DIR / "logs" / "proxy.err"

# Look for icon
_ICON_PATH = PROXY_DIR / "assets" / "mushroom-16.png"
ICON_PATH: str | None = str(_ICON_PATH) if _ICON_PATH.exists() else None


class KiroProxyApp(rumps.App):
    def __init__(self) -> None:
        super().__init__(
            "Kiro Proxy",
            icon=ICON_PATH,
            template=True,
        )
        self._enabled = True

        self.status_item = rumps.MenuItem("Status: checking...", callback=self._noop)
        self.savings_item = rumps.MenuItem("Savings: --", callback=self._noop)
        self.requests_item = rumps.MenuItem("Requests: --", callback=self._noop)
        self.bytes_item = rumps.MenuItem("Bytes saved: --", callback=self._noop)
        self.images_item = rumps.MenuItem("Images stripped: --", callback=self._noop)
        self.last_item = rumps.MenuItem("Last compressed: --", callback=self._noop)
        self.errors_item = rumps.MenuItem("Errors: --", callback=self._noop)
        self.toggle_item = rumps.MenuItem("Disable Proxy", callback=self._toggle_proxy)
        self.logs_item = rumps.MenuItem("Open Log…", callback=self._open_logs)

        self.menu = [
            self.status_item,
            None,
            self.savings_item,
            self.requests_item,
            self.bytes_item,
            self.images_item,
            None,
            self.last_item,
            self.errors_item,
            None,
            self.toggle_item,
            self.logs_item,
        ]

    @staticmethod
    def _noop(_: rumps.MenuItem) -> None:
        pass

    @rumps.timer(POLL_INTERVAL)
    def _poll(self, _: rumps.Timer) -> None:
        try:
            self._update_stats()
        except Exception:
            self._set_offline()

    def _update_stats(self) -> None:
        req = urllib.request.Request(STATS_URL, method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())

        requests_total = data.get("requests_total", 0)
        requests_compressed = data.get("requests_compressed", 0)
        avg_savings_pct = data.get("avg_savings_pct", 0)
        images_stripped = data.get("images_stripped", 0)
        tool_results = data.get("tool_results_compressed", 0)
        assistant_trunc = data.get("assistant_responses_truncated", 0)
        errors = data.get("errors_fallen_through", 0)
        last_at = data.get("last_request_at", "")
        last_ttfb = data.get("last_ttfb_ms", 0)
        avg_ttfb = data.get("avg_ttfb_ms", 0)
        last_original_kb = data.get("last_original_kb", 0)
        last_compressed_kb = data.get("last_compressed_kb", 0)
        last_savings_pct = data.get("last_savings_pct", 0)
        est_tokens_saved = data.get("est_tokens_saved", 0)
        est_cost_saved = data.get("est_cost_saved_usd", 0)

        if requests_compressed > 0:
            self.title = f" ↓{avg_savings_pct:.0f}%"
        else:
            self.title = ""

        self.status_item.title = "Status: ● Running"
        self._enabled = True
        self.toggle_item.title = "Disable Proxy"

        if requests_total > 0:
            self.savings_item.title = (
                f"Last: {last_original_kb:.0f}KB → {last_compressed_kb:.0f}KB "
                f"({last_savings_pct:.0f}% saved)"
            )
            self.requests_item.title = (
                f"Avg savings: {avg_savings_pct:.0f}% | "
                f"TTFB: {last_ttfb}ms (avg {avg_ttfb}ms)"
            )
            self.bytes_item.title = (
                f"Compressed: {images_stripped} imgs, "
                f"{tool_results} tools, {assistant_trunc} assistants"
            )
            self.images_item.title = (
                f"Est. saved: ~{_human_tokens(est_tokens_saved)} tokens "
                f"(~${est_cost_saved:.2f})"
            )
        else:
            self.savings_item.title = "Waiting for first request..."
            self.requests_item.title = "Requests: 0"
            self.bytes_item.title = "Compressed: --"
            self.images_item.title = "Est. saved: --"

        if last_at:
            self.last_item.title = (
                f"{requests_compressed}/{requests_total} requests compressed | "
                f"last: {_relative_time(last_at)}"
            )
        else:
            self.last_item.title = "Last request: none yet"

        if errors > 0:
            self.errors_item.title = f"⚠️ {errors} error(s)"
        else:
            self.errors_item.title = "✓ 0 errors"

    def _set_offline(self) -> None:
        self.title = ""
        self.status_item.title = "Status: ○ Stopped"
        self.savings_item.title = "Savings: --"
        self.requests_item.title = "Requests: --"
        self.bytes_item.title = "Bytes saved: --"
        self.images_item.title = "Images stripped: --"
        self.last_item.title = "Last compressed: --"
        self.errors_item.title = "Errors: --"
        self.toggle_item.title = "Enable Proxy" if not self._enabled else "Start Proxy"

    def _toggle_proxy(self, sender: rumps.MenuItem) -> None:
        uid = os.getuid()
        if self._enabled:
            subprocess.run(
                ["launchctl", "bootout", f"gui/{uid}", str(PLIST_PATH)],
                capture_output=True,
            )
            self._enabled = False
            sender.title = "Enable Proxy"
            self._set_offline()
            rumps.notification("Kiro Proxy", "Disabled", "Compression stopped.")
        else:
            result = subprocess.run(
                ["launchctl", "bootstrap", f"gui/{uid}", str(PLIST_PATH)],
                capture_output=True, text=True,
            )
            if result.returncode == 0 or "already bootstrapped" in result.stderr.lower():
                self._enabled = True
                sender.title = "Disable Proxy"
                rumps.notification("Kiro Proxy", "Enabled", "Compression active.")
            else:
                rumps.notification("Kiro Proxy", "Failed", result.stderr.strip()[:100])

    def _open_logs(self, _: rumps.MenuItem) -> None:
        if LOG_PATH.exists():
            subprocess.run(["open", "-a", "Console", str(LOG_PATH)])
        else:
            rumps.notification("Kiro Proxy", "No log file", f"Expected: {LOG_PATH}")


def _human_tokens(n: int) -> str:
    if n < 1000:
        return f"{n}"
    elif n < 1_000_000:
        return f"{n / 1000:.1f}K"
    else:
        return f"{n / 1_000_000:.1f}M"


def _relative_time(iso_str: str) -> str:
    try:
        dt = datetime.strptime(iso_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        diff = (datetime.now(timezone.utc) - dt).total_seconds()
        if diff < 60:
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
