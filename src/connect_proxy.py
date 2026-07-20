"""Asyncio CONNECT proxy for kiro TLS interception.

- For ``runtime.us-east-1.kiro.dev``: terminates TLS, compresses the
  request body via SmartCrusher, forwards upstream.
- For all other hosts: transparent byte-pipe tunnel (no interception).
- Serves /health and /stats on the proxy port for monitoring.

Usage:
    python connect_proxy.py --port 9090
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
import time
from pathlib import Path
from typing import Any

from handler import compress_kiro_request

logger = logging.getLogger("kiro_proxy.connect")

KIRO_RUNTIME_HOST = "runtime.us-east-1.kiro.dev"

# Stats tracking
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

_MAX_HISTORY = 50
_COST_PER_INPUT_TOKEN = 3.0 / 1_000_000
_CHARS_PER_TOKEN = 4


def _record_request_timing(ttfb_ms: float, total_ms: float) -> None:
    _stats["ttfb_ms_history"].append(round(ttfb_ms))
    if len(_stats["ttfb_ms_history"]) > _MAX_HISTORY:
        _stats["ttfb_ms_history"] = _stats["ttfb_ms_history"][-_MAX_HISTORY:]
    _stats["response_time_ms_history"].append(round(total_ms))
    if len(_stats["response_time_ms_history"]) > _MAX_HISTORY:
        _stats["response_time_ms_history"] = _stats["response_time_ms_history"][-_MAX_HISTORY:]


def _record_compression(original_bytes: int, compressed_bytes: int) -> None:
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


def get_stats() -> dict[str, Any]:
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


async def _handle_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")
    logger.debug("new connection from %s", peer)

    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=30.0)
    except (asyncio.TimeoutError, ConnectionError):
        writer.close()
        return

    if not request_line:
        writer.close()
        return

    line = request_line.decode("latin-1", errors="replace").strip()
    parts = line.split(" ", 2)
    if len(parts) < 2:
        writer.close()
        return

    method = parts[0].upper()

    if method in ("GET", "POST") and parts[1] in ("/health", "/stats"):
        await _handle_local_request(reader, writer, parts[1])
        return

    if method != "CONNECT":
        writer.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
        await writer.drain()
        writer.close()
        return

    target = parts[1]
    host = target.rsplit(":", 1)[0]

    # Read and discard headers
    while True:
        header_line = await reader.readline()
        if header_line in (b"\r\n", b"\n", b""):
            break

    if host.rstrip(".").lower() == KIRO_RUNTIME_HOST:
        await _intercept_kiro(reader, writer, target)
    else:
        await _tunnel_passthrough(reader, writer, target)


async def _handle_local_request(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str
) -> None:
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break

    if path == "/health":
        body = b"ok"
        content_type = "text/plain"
    else:
        body = json.dumps(get_stats(), indent=2).encode()
        content_type = "application/json"

    response = (
        f"HTTP/1.1 200 OK\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"\r\n"
    ).encode() + body
    writer.write(response)
    await writer.drain()
    writer.close()


async def _intercept_kiro(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, target: str
) -> None:
    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await writer.drain()

    server_ssl_ctx = _make_server_ssl_context()
    if server_ssl_ctx is None:
        logger.warning("No TLS cert available, tunneling raw")
        _stats["tunnels_passthrough"] += 1
        await _raw_tunnel(reader, writer, target)
        return

    logger.info("kiro: TLS handshake with client for %s", target)
    try:
        transport = writer.transport
        protocol = transport.get_protocol()
        loop = asyncio.get_event_loop()
        new_transport = await loop.start_tls(
            transport, protocol, server_ssl_ctx, server_side=True
        )
        tls_reader = reader
        tls_writer = asyncio.StreamWriter(new_transport, protocol, reader, loop)
        logger.info("kiro: TLS handshake complete")
    except (ssl.SSLError, OSError) as e:
        logger.error("kiro: TLS handshake FAILED: %s", e)
        try:
            writer.close()
        except Exception:
            pass
        return

    try:
        request_data = await _read_http_request(tls_reader)
    except (asyncio.TimeoutError, ConnectionError):
        tls_writer.close()
        return

    if request_data is None:
        tls_writer.close()
        return

    method, path, headers, body = request_data
    request_start = time.perf_counter()
    original_size = len(body)
    _stats["requests_total"] += 1
    _stats["bytes_request_original"] += original_size
    _stats["last_request_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    try:
        compressed_body, compression_stats = compress_kiro_request(body)
        if len(compressed_body) < len(body):
            _stats["requests_compressed"] += 1
            _stats["images_stripped"] += compression_stats.get("images_stripped", 0)
            _stats["tool_results_compressed"] += compression_stats.get("tool_results_compressed", 0)
            _stats["assistant_responses_truncated"] += compression_stats.get("assistant_responses_truncated", 0)
            savings_pct = (1 - len(compressed_body) / len(body)) * 100
            logger.info(
                "kiro: compressed %d → %d bytes (%.1f%% saved, "
                "%d images, %d tool results, %d assistants truncated)",
                len(body), len(compressed_body), savings_pct,
                compression_stats.get("images_stripped", 0),
                compression_stats.get("tool_results_compressed", 0),
                compression_stats.get("assistant_responses_truncated", 0),
            )
            body = compressed_body
        _record_compression(original_size, len(body))
        _stats["bytes_request_sent"] += len(body)
    except Exception as exc:
        logger.warning("Compression failed, forwarding unchanged: %s", exc)
        _stats["errors_fallen_through"] += 1
        _record_compression(original_size, original_size)
        _stats["bytes_request_sent"] += len(body)

    try:
        response_bytes = await _forward_and_stream(method, path, headers, body, target, tls_writer)
        total_ms = (time.perf_counter() - request_start) * 1000
        _stats["bytes_response_total"] += response_bytes
        _stats["last_response_size_kb"] = round(response_bytes / 1024, 1)
        _record_request_timing(_stats["last_ttfb_ms"], total_ms)
    except Exception as exc:
        logger.error("Upstream request failed: %s", exc)
        try:
            tls_writer.write(
                b"HTTP/1.1 502 Bad Gateway\r\n"
                b"Content-Type: text/plain\r\n\r\n"
                b"kiro-proxy: upstream connection failed"
            )
            await tls_writer.drain()
        except Exception:
            pass
    finally:
        try:
            tls_writer.close()
        except Exception:
            pass


async def _tunnel_passthrough(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, target: str
) -> None:
    _stats["tunnels_passthrough"] += 1
    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await writer.drain()
    await _raw_tunnel(reader, writer, target)


async def _raw_tunnel(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, target: str
) -> None:
    host, _, port_str = target.rpartition(":")
    port = int(port_str) if port_str else 443

    try:
        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=30.0
        )
    except (asyncio.TimeoutError, OSError) as exc:
        logger.debug("Cannot connect to %s: %s", target, exc)
        writer.close()
        return

    async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await src.read(65536)
                if not data:
                    break
                dst.write(data)
                await dst.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            try:
                dst.close()
            except Exception:
                pass

    await asyncio.gather(
        pipe(reader, upstream_writer),
        pipe(upstream_reader, writer),
        return_exceptions=True,
    )


async def _read_http_request(
    reader: asyncio.StreamReader,
) -> tuple[str, str, dict[str, str], bytes] | None:
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=30.0)
    except asyncio.TimeoutError:
        return None

    if not request_line:
        return None

    parts = request_line.decode("latin-1").strip().split(" ", 2)
    if len(parts) < 2:
        return None

    method, path = parts[0], parts[1]

    headers: dict[str, str] = {}
    content_length = 0
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        decoded = line.decode("latin-1").strip()
        if ":" in decoded:
            key, _, value = decoded.partition(":")
            headers[key.strip().lower()] = value.strip()
            if key.strip().lower() == "content-length":
                content_length = int(value.strip())

    body = b""
    if content_length > 0:
        try:
            body = await asyncio.wait_for(reader.readexactly(content_length), timeout=60.0)
        except asyncio.IncompleteReadError as e:
            body = e.partial

    return method, path, headers, body


async def _forward_and_stream(
    method: str, path: str, headers: dict[str, str], body: bytes,
    target: str, client_writer: asyncio.StreamWriter,
) -> int:
    host, _, port_str = target.rpartition(":")
    port = int(port_str) if port_str else 443

    upstream_ssl = ssl.create_default_context()
    upstream_reader, upstream_writer = await asyncio.wait_for(
        asyncio.open_connection(host, port, ssl=upstream_ssl), timeout=30.0
    )

    headers["content-length"] = str(len(body))
    for hop in ("proxy-connection", "proxy-authorization", "connection",
                "keep-alive", "transfer-encoding", "te", "upgrade"):
        headers.pop(hop, None)
    headers["host"] = host
    headers["connection"] = "close"

    request_line = f"{method} {path} HTTP/1.1\r\n"
    header_lines = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    raw_request = (request_line + header_lines + "\r\n").encode() + body

    upstream_writer.write(raw_request)
    await upstream_writer.drain()
    request_sent_at = time.perf_counter()

    total_response_bytes = 0
    try:
        response_headers = b""
        while b"\r\n\r\n" not in response_headers:
            chunk = await asyncio.wait_for(upstream_reader.read(65536), timeout=60.0)
            if not chunk:
                return total_response_bytes
            response_headers += chunk

        header_end = response_headers.index(b"\r\n\r\n") + 4
        header_bytes = response_headers[:header_end]
        body_start = response_headers[header_end:]

        client_writer.write(header_bytes)
        await client_writer.drain()
        total_response_bytes += len(header_bytes)

        ttfb_ms = (time.perf_counter() - request_sent_at) * 1000
        _stats["last_ttfb_ms"] = round(ttfb_ms)

        # Parse response framing
        header_text = header_bytes.decode("latin-1")
        content_length = -1
        for line in header_text.split("\r\n"):
            lower = line.lower()
            if lower.startswith("content-length:"):
                content_length = int(line.split(":", 1)[1].strip())

        if content_length >= 0:
            remaining = content_length - len(body_start)
            if body_start:
                client_writer.write(body_start)
                await client_writer.drain()
                total_response_bytes += len(body_start)
            while remaining > 0:
                chunk = await asyncio.wait_for(
                    upstream_reader.read(min(remaining, 65536)), timeout=300.0
                )
                if not chunk:
                    break
                client_writer.write(chunk)
                await client_writer.drain()
                remaining -= len(chunk)
                total_response_bytes += len(chunk)
        else:
            # Chunked or close-delimited: read until EOF
            if body_start:
                client_writer.write(body_start)
                await client_writer.drain()
                total_response_bytes += len(body_start)
            while True:
                chunk = await asyncio.wait_for(upstream_reader.read(65536), timeout=300.0)
                if not chunk:
                    break
                client_writer.write(chunk)
                await client_writer.drain()
                total_response_bytes += len(chunk)

    except asyncio.TimeoutError:
        logger.warning("Upstream response timed out (received %d bytes)", total_response_bytes)
    except (ConnectionError, OSError) as exc:
        logger.debug("Connection closed during streaming: %s", exc)
    finally:
        try:
            upstream_writer.close()
        except Exception:
            pass

    return total_response_bytes


def _make_server_ssl_context() -> ssl.SSLContext | None:
    cert_path = Path.home() / ".kiro-proxy" / "cert.pem"
    key_path = Path.home() / ".kiro-proxy" / "key.pem"

    if not cert_path.exists() or not key_path.exists():
        return None

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert_path), str(key_path))
    return ctx


async def start_connect_proxy(port: int = 9090) -> asyncio.Server:
    server = await asyncio.start_server(
        _handle_connect, "127.0.0.1", port, reuse_address=True
    )
    addr = server.sockets[0].getsockname()
    logger.info("kiro-proxy listening on %s:%d (pid=%d)", addr[0], addr[1], os.getpid())
    return server


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Kiro compression proxy")
    parser.add_argument("--port", type=int, default=9090)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    async def run() -> None:
        server = await start_connect_proxy(port=args.port)
        async with server:
            await server.serve_forever()

    asyncio.run(run())


if __name__ == "__main__":
    main()
