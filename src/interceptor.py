"""TLS interceptor for kiro traffic.

Handles:
- TLS termination with the local CA cert
- HTTP request parsing (Content-Length and chunked TE)
- Request body compression via handler.compress_kiro_request()
- Upstream TLS connection and response streaming
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import time
from pathlib import Path
from typing import Any

from stats import _stats, record_compression, record_request_timing

logger = logging.getLogger("kiro_proxy.interceptor")

# --- Fail-safe imports ---
# The proxy MUST start and serve CONNECT tunnels even if every optional
# component fails to load. Compression is nice-to-have; connectivity is
# non-negotiable.

_COMPRESSION_AVAILABLE = False
_import_err: Exception | None = None
try:
    from handler import compress_kiro_request
    _COMPRESSION_AVAILABLE = True
except Exception as _exc:
    _import_err = _exc
    compress_kiro_request = None  # type: ignore[assignment]


def is_compression_available() -> bool:
    """Whether the compression engine loaded successfully."""
    return _COMPRESSION_AVAILABLE


def get_import_error() -> Exception | None:
    """Return the compression import error, if any."""
    return _import_err


def make_server_ssl_context() -> ssl.SSLContext | None:
    """Load the local CA cert/key for TLS interception.

    Returns None if certs are missing — caller should fall back to
    raw tunnel passthrough.
    """
    cert_path = Path.home() / ".kiro-proxy" / "cert.pem"
    key_path = Path.home() / ".kiro-proxy" / "key.pem"

    if not cert_path.exists() or not key_path.exists():
        logger.warning("TLS certs not found at %s — running in passthrough mode", cert_path.parent)
        return None

    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert_path), str(key_path))
        return ctx
    except (ssl.SSLError, OSError) as exc:
        logger.error("Failed to load TLS certs: %s — running in passthrough mode", exc)
        return None


async def intercept_kiro(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, target: str,
    raw_tunnel_fn,
) -> None:
    """Intercept a kiro CONNECT request: TLS-terminate, compress, forward.

    raw_tunnel_fn: fallback function to call if TLS setup fails.
    """
    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await writer.drain()

    server_ssl_ctx = make_server_ssl_context()
    if server_ssl_ctx is None:
        logger.warning("No TLS cert available, tunneling raw")
        _stats["tunnels_passthrough"] += 1
        await raw_tunnel_fn(reader, writer, target)
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
        if not _COMPRESSION_AVAILABLE:
            raise RuntimeError("compression unavailable")
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
        record_compression(original_size, len(body))
        _stats["bytes_request_sent"] += len(body)
    except Exception as exc:
        logger.warning("Compression failed, forwarding unchanged: %s", exc)
        _stats["errors_fallen_through"] += 1
        record_compression(original_size, original_size)
        _stats["bytes_request_sent"] += len(body)

    try:
        response_bytes = await _forward_and_stream(method, path, headers, body, target, tls_writer)
        total_ms = (time.perf_counter() - request_start) * 1000
        _stats["bytes_response_total"] += response_bytes
        _stats["last_response_size_kb"] = round(response_bytes / 1024, 1)
        record_request_timing(_stats["last_ttfb_ms"], total_ms)
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


# --- HTTP request parsing ---


async def _read_http_request(
    reader: asyncio.StreamReader,
) -> tuple[str, str, dict[str, str], bytes] | None:
    """Parse an HTTP/1.1 request (request line + headers + body).

    Handles both Content-Length and Transfer-Encoding: chunked bodies.
    Returns (method, path, headers, body) or None on timeout/error.
    """
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
    is_chunked = False
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
            elif key.strip().lower() == "transfer-encoding":
                is_chunked = "chunked" in value.strip().lower()

    body = b""
    if is_chunked:
        # Read chunked body into a flat buffer.
        body = await _read_chunked_body(reader)
    elif content_length > 0:
        try:
            body = await asyncio.wait_for(reader.readexactly(content_length), timeout=60.0)
        except asyncio.IncompleteReadError as e:
            body = e.partial

    return method, path, headers, body


async def _read_chunked_body(reader: asyncio.StreamReader) -> bytes:
    """Read an HTTP/1.1 chunked transfer-encoded body into a flat buffer.

    Returns the decoded body (chunks reassembled, chunk framing stripped).
    On any parse error or timeout, returns whatever was read so far.
    """
    buf = bytearray()
    try:
        while True:
            # Each chunk: <hex-size>\r\n<data>\r\n
            size_line = await asyncio.wait_for(reader.readline(), timeout=60.0)
            if not size_line:
                break
            # Chunk size may have extensions after a semicolon
            size_str = size_line.decode("latin-1").strip().split(";")[0]
            chunk_size = int(size_str, 16)
            if chunk_size == 0:
                # Terminal chunk — read trailing \r\n
                await reader.readline()
                break
            chunk_data = await asyncio.wait_for(
                reader.readexactly(chunk_size), timeout=60.0
            )
            buf.extend(chunk_data)
            # Read the trailing \r\n after chunk data
            await reader.readline()
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError):
        logger.warning("chunked: incomplete read, using %d bytes received", len(buf))

    return bytes(buf)


# --- Upstream forwarding ---


async def _forward_and_stream(
    method: str, path: str, headers: dict[str, str], body: bytes,
    target: str, client_writer: asyncio.StreamWriter,
) -> int:
    """Forward request upstream and stream the response back to the client.

    Opens a TLS connection to the real kiro server, sends the (possibly
    compressed) request, and streams the response back to the client.
    Returns total bytes of response streamed.

    HTTP/1.1 framing logic:
    - Reads response headers to find Content-Length or detect chunked/EOF-delimited
    - If Content-Length: reads exactly that many body bytes
    - Otherwise: reads until upstream closes the connection (chunked or close-delimited)
    - First byte of response body after headers marks TTFB (time to first byte)
    """
    host, _, port_str = target.rpartition(":")
    port = int(port_str) if port_str else 443

    upstream_ssl = ssl.create_default_context()
    upstream_reader, upstream_writer = await asyncio.wait_for(
        asyncio.open_connection(host, port, ssl=upstream_ssl), timeout=30.0
    )

    # Rewrite headers for upstream: set Content-Length from body, remove hop-by-hop
    headers["content-length"] = str(len(body))
    for hop in ("proxy-connection", "proxy-authorization", "connection",
                "keep-alive", "transfer-encoding", "te", "upgrade"):
        headers.pop(hop, None)
    headers["host"] = host
    headers["connection"] = "close"

    # Build and send the raw HTTP request
    request_line = f"{method} {path} HTTP/1.1\r\n"
    header_lines = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    raw_request = (request_line + header_lines + "\r\n").encode() + body

    upstream_writer.write(raw_request)
    await upstream_writer.drain()
    request_sent_at = time.perf_counter()

    # Stream response back to client
    total_response_bytes = 0
    try:
        # --- Phase 1: Read response headers ---
        # HTTP/1.1 headers end with a blank line (\r\n\r\n). We may receive
        # them across multiple read() calls, so accumulate until we see the
        # terminator. The read may also overshoot and include the beginning
        # of the response body (body_start).
        response_headers = b""
        while b"\r\n\r\n" not in response_headers:
            chunk = await asyncio.wait_for(upstream_reader.read(65536), timeout=60.0)
            if not chunk:
                return total_response_bytes
            response_headers += chunk

        # --- Phase 2: Split headers from body ---
        # Everything before (and including) \r\n\r\n is the header block.
        # Anything after is the start of the response body that was read
        # in the same socket read — we must forward it to the client.
        header_end = response_headers.index(b"\r\n\r\n") + 4
        header_bytes = response_headers[:header_end]
        body_start = response_headers[header_end:]

        # Forward headers to client immediately (enables streaming)
        client_writer.write(header_bytes)
        await client_writer.drain()
        total_response_bytes += len(header_bytes)

        # Record time-to-first-byte (headers arriving = first byte of response)
        ttfb_ms = (time.perf_counter() - request_sent_at) * 1000
        _stats["last_ttfb_ms"] = round(ttfb_ms)

        # --- Phase 3: Determine body framing ---
        # HTTP/1.1 has three ways to delimit the response body:
        # 1. Content-Length header: exact byte count known upfront
        # 2. Transfer-Encoding: chunked: size prefixed per chunk (we don't
        #    decode these — just pass the raw chunked frames through)
        # 3. Connection: close: read until the socket closes (EOF-delimited)
        #
        # We handle (1) explicitly and treat (2) and (3) the same way:
        # stream until upstream closes the connection.
        header_text = header_bytes.decode("latin-1")
        content_length = -1
        for line in header_text.split("\r\n"):
            lower = line.lower()
            if lower.startswith("content-length:"):
                content_length = int(line.split(":", 1)[1].strip())

        if content_length >= 0:
            # --- Case 1: Known body size ---
            # Subtract any bytes we already read as part of the header read
            # (body_start) from the remaining count.
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
            # --- Case 2/3: Chunked or close-delimited ---
            # Stream raw bytes until upstream closes. For chunked responses,
            # the chunk framing passes through unchanged — the client (kiro-cli)
            # handles de-chunking. This avoids needing to parse chunk boundaries
            # on the response path.
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
