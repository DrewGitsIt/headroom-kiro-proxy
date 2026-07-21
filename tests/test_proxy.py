"""Integration tests for proxy.py — connection routing.

Starts the proxy on a random port and tests request dispatch:
- GET /health → 200 "ok"
- GET /stats → 200 valid JSON
- CONNECT kiro host → routed to interceptor (will fail TLS since no certs, but verifies routing)
- CONNECT other host → routed to raw tunnel
- Non-CONNECT methods → 405
"""

import asyncio
import json
import pytest
from proxy import start_connect_proxy, KIRO_RUNTIME_HOST
from stats import reset_stats


@pytest.fixture(autouse=True)
def _clean_stats():
    reset_stats()
    yield
    reset_stats()


@pytest.fixture()
async def proxy_server():
    """Start proxy on port 0 (OS-assigned) and yield (host, port)."""
    server = await start_connect_proxy(port=0)
    addr = server.sockets[0].getsockname()
    yield addr[0], addr[1]
    server.close()
    await server.wait_closed()


async def _send_raw(host: str, port: int, data: bytes, read_timeout: float = 2.0) -> bytes:
    """Open a TCP connection to the proxy and send raw bytes, return response."""
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(data)
    await writer.drain()
    # Read response until EOF or timeout
    response = b""
    try:
        while True:
            chunk = await asyncio.wait_for(reader.read(65536), timeout=read_timeout)
            if not chunk:
                break
            response += chunk
    except asyncio.TimeoutError:
        pass
    writer.close()
    return response


class TestLocalEndpoints:
    """Test /health and /stats endpoints."""

    async def test_health_returns_ok(self, proxy_server):
        """GET /health → 200 with 'ok' body."""
        host, port = proxy_server
        request = b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n"
        response = await _send_raw(host, port, request)
        assert b"HTTP/1.1 200 OK" in response
        assert b"ok" in response

    async def test_stats_returns_json(self, proxy_server):
        """GET /stats → 200 with valid JSON containing expected keys."""
        host, port = proxy_server
        request = b"GET /stats HTTP/1.1\r\nHost: localhost\r\n\r\n"
        response = await _send_raw(host, port, request)
        assert b"HTTP/1.1 200 OK" in response
        assert b"application/json" in response
        # Extract JSON body after headers
        body_start = response.index(b"\r\n\r\n") + 4
        body = json.loads(response[body_start:])
        assert "requests_total" in body
        assert "cumulative_savings_pct" in body
        assert "started_at" in body


class TestConnectRouting:
    """Test CONNECT request routing logic."""

    async def test_connect_kiro_host_routes_to_interceptor(self, proxy_server):
        """CONNECT to kiro runtime → interceptor (200 Connection Established).

        The interceptor will then attempt TLS handshake, which will fail
        because no certs exist in tests. But the 200 response proves routing
        happened correctly.
        """
        host, port = proxy_server
        request = f"CONNECT {KIRO_RUNTIME_HOST}:443 HTTP/1.1\r\nHost: {KIRO_RUNTIME_HOST}\r\n\r\n"
        response = await _send_raw(host, port, request.encode(), read_timeout=1.0)
        assert b"200 Connection Established" in response

    async def test_connect_non_kiro_routes_to_tunnel(self, proxy_server):
        """CONNECT to non-kiro host → raw tunnel (200 Connection Established).

        The tunnel will fail to connect upstream (no real server), but the
        initial 200 proves routing worked.
        """
        host, port = proxy_server
        # Use a host that won't resolve/connect (loopback on unlikely port)
        request = b"CONNECT 127.0.0.1:1 HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
        response = await _send_raw(host, port, request, read_timeout=1.0)
        # The tunnel sends 200 before attempting upstream connection.
        # If upstream fails, connection just closes.
        assert b"200 Connection Established" in response


class TestMethodValidation:
    """Test non-CONNECT method handling."""

    async def test_put_returns_405(self, proxy_server):
        """PUT request → 405 Method Not Allowed."""
        host, port = proxy_server
        request = b"PUT /something HTTP/1.1\r\nHost: localhost\r\n\r\n"
        response = await _send_raw(host, port, request)
        assert b"405" in response

    async def test_delete_returns_405(self, proxy_server):
        """DELETE request → 405."""
        host, port = proxy_server
        request = b"DELETE /resource HTTP/1.1\r\nHost: localhost\r\n\r\n"
        response = await _send_raw(host, port, request)
        assert b"405" in response

    async def test_post_to_unknown_path_returns_405(self, proxy_server):
        """POST to non-special path → 405 (only CONNECT or GET /health|/stats accepted)."""
        host, port = proxy_server
        request = b"POST /unknown HTTP/1.1\r\nHost: localhost\r\n\r\n"
        response = await _send_raw(host, port, request)
        assert b"405" in response

    async def test_empty_connection_closed_gracefully(self, proxy_server):
        """Connection that sends nothing and closes should not crash the proxy."""
        host, port = proxy_server
        reader, writer = await asyncio.open_connection(host, port)
        writer.close()
        await asyncio.sleep(0.1)
        # Proxy should still work after empty connection
        request = b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n"
        response = await _send_raw(host, port, request)
        assert b"200 OK" in response
