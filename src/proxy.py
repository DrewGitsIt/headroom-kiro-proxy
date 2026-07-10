"""
Kiro Compression Proxy — mitmproxy addon.

Intercepts POST requests to runtime.us-east-1.kiro.dev, compresses
the conversation history to reduce token costs, and forwards upstream.
All other traffic is tunneled unchanged.

Usage:
    mitmdump -s src/proxy.py --listen-port 9090 --set confdir=~/.kiro-proxy

The addon also serves /health and /stats on the proxy port itself.
"""

import json
import logging
import time
from dataclasses import dataclass, field

from mitmproxy import http, ctx

from compress import compress_conversation

logger = logging.getLogger("kiro-proxy")

RUNTIME_HOST = "runtime.us-east-1.kiro.dev"


@dataclass
class Stats:
    requests_total: int = 0
    requests_compressed: int = 0
    bytes_before: int = 0
    bytes_after: int = 0
    images_stripped: int = 0
    tool_results_compressed: int = 0
    errors_fallen_through: int = 0
    last_request_at: str = ""
    started_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))


class KiroCompressionProxy:
    """mitmproxy addon that compresses kiro-cli conversation history."""

    def __init__(self):
        self.stats = Stats()

    def request(self, flow: http.HTTPFlow) -> None:
        """Intercept requests to runtime.kiro.dev and compress history."""

        # Handle local control plane requests (health/stats)
        if flow.request.pretty_host in ("127.0.0.1", "localhost"):
            self._handle_local_request(flow)
            return

        # Only intercept runtime.kiro.dev POST requests
        if flow.request.pretty_host != RUNTIME_HOST:
            return
        if flow.request.method != "POST":
            return

        self.stats.requests_total += 1
        self.stats.last_request_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        original_body = flow.request.get_content()
        if not original_body:
            return

        original_size = len(original_body)
        self.stats.bytes_before += original_size

        try:
            result = compress_conversation(original_body)
            compressed_body = result["body"]
            compressed_size = len(compressed_body)

            self.stats.bytes_after += compressed_size
            self.stats.images_stripped += result.get("images_stripped", 0)
            self.stats.tool_results_compressed += result.get("tool_results_compressed", 0)

            if compressed_size < original_size:
                self.stats.requests_compressed += 1
                savings_pct = (1 - compressed_size / original_size) * 100
                logger.info(
                    f"Compressed: {original_size:,} → {compressed_size:,} bytes "
                    f"({savings_pct:.1f}% saved, "
                    f"{result.get('images_stripped', 0)} images stripped, "
                    f"{result.get('tool_results_compressed', 0)} tool results compressed)"
                )
                flow.request.set_content(compressed_body)
            else:
                self.stats.bytes_after += (original_size - compressed_size)  # correct the stat
                logger.debug("No compression benefit, forwarding unchanged")

        except Exception as e:
            # FAIL-THROUGH: forward original bytes on any error
            self.stats.errors_fallen_through += 1
            self.stats.bytes_after += original_size
            logger.warning(f"Compression failed, forwarding unchanged: {e}")

    def _handle_local_request(self, flow: http.HTTPFlow) -> None:
        """Serve /health and /stats endpoints for the wrapper script."""
        path = flow.request.path

        if path == "/health":
            flow.response = http.Response.make(
                200,
                b"ok",
                {"Content-Type": "text/plain"},
            )
        elif path == "/stats":
            stats_dict = {
                "requests_total": self.stats.requests_total,
                "requests_compressed": self.stats.requests_compressed,
                "bytes_before": self.stats.bytes_before,
                "bytes_after": self.stats.bytes_after,
                "savings_percent": round(
                    (1 - self.stats.bytes_after / self.stats.bytes_before) * 100, 1
                ) if self.stats.bytes_before > 0 else 0.0,
                "images_stripped": self.stats.images_stripped,
                "tool_results_compressed": self.stats.tool_results_compressed,
                "errors_fallen_through": self.stats.errors_fallen_through,
                "last_request_at": self.stats.last_request_at,
                "started_at": self.stats.started_at,
            }
            flow.response = http.Response.make(
                200,
                json.dumps(stats_dict, indent=2).encode(),
                {"Content-Type": "application/json"},
            )
        else:
            # Not a control plane request, let it pass through
            return

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        """Stream responses without buffering."""
        if flow.request.pretty_host == RUNTIME_HOST:
            flow.response.stream = True


addons = [KiroCompressionProxy()]
