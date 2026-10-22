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
    from session_timer import SessionTimer
    _COMPRESSION_AVAILABLE = True
except Exception as _exc:
    _import_err = _exc
    compress_kiro_request = None  # type: ignore[assignment]
    SessionTimer = None  # type: ignore[assignment, misc]

# Module-level session timer shared across all requests.
# Tracks per-conversationId last-seen timestamps so we only compress
# when the provider's prompt cache is cold.
_session_timer: SessionTimer | None = None
if _COMPRESSION_AVAILABLE and SessionTimer is not None:
    _session_timer = SessionTimer()


def is_compression_available() -> bool:
    """Whether the compression engine loaded successfully."""
    return _COMPRESSION_AVAILABLE


def get_import_error() -> Exception | None:
    """Return the compression import error, if any."""
    return _import_err


_PROXY_DIR = Path.home() / ".kiro-proxy"
_CA_CERT_PATH = _PROXY_DIR / "ca-cert.pem"
_CA_KEY_PATH = _PROXY_DIR / "ca-key.pem"
# Legacy single-host cert/key, kept as a last-resort fallback for installs
# that predate per-host cert generation (no ca-key.pem on disk).
_LEGACY_CERT_PATH = _PROXY_DIR / "cert.pem"
_LEGACY_KEY_PATH = _PROXY_DIR / "key.pem"

# In-memory cache of generated leaf certs, keyed by hostname. Avoids
# re-signing a new cert (and rebuilding an SSLContext) on every single
# connection to the same host — kiro's traffic hits the same 1-3 hosts
# repeatedly for the life of the process.
_leaf_cert_cache: dict[str, ssl.SSLContext] = {}
_ca_keypair: tuple[Any, Any] | None = None  # (ca_key, ca_cert), loaded once
_ca_load_failed = False


def _load_ca() -> tuple[Any, Any] | None:
    """Load (and cache) the local CA private key + certificate.

    Returns None if the CA files are missing or unreadable — caller should
    fall back to the legacy static cert or raw tunnel passthrough.
    """
    global _ca_keypair, _ca_load_failed
    if _ca_keypair is not None:
        return _ca_keypair
    if _ca_load_failed:
        return None

    if not _CA_CERT_PATH.exists() or not _CA_KEY_PATH.exists():
        return None

    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization

        ca_key = serialization.load_pem_private_key(_CA_KEY_PATH.read_bytes(), password=None)
        ca_cert = x509.load_pem_x509_certificate(_CA_CERT_PATH.read_bytes())
        _ca_keypair = (ca_key, ca_cert)
        return _ca_keypair
    except Exception as exc:
        logger.error("Failed to load CA cert/key: %s — falling back to static cert", exc)
        _ca_load_failed = True
        return None


def _generate_leaf_cert(host: str) -> tuple[bytes, bytes] | None:
    """Sign a fresh leaf cert for ``host`` using the local CA.

    Returns (cert_pem, key_pem), or None if the CA isn't available or
    signing failed for any reason.
    """
    ca = _load_ca()
    if ca is None:
        return None
    ca_key, ca_cert = ca

    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
        from datetime import datetime, timedelta, timezone

        leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
        now = datetime.now(timezone.utc)
        leaf_cert = (
            x509.CertificateBuilder()
            .subject_name(leaf_name)
            .issuer_name(ca_cert.subject)
            .public_key(leaf_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=825))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        cert_pem = leaf_cert.public_bytes(serialization.Encoding.PEM)
        key_pem = leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        return cert_pem, key_pem
    except Exception as exc:
        logger.error("Failed to generate leaf cert for %s: %s", host, exc)
        return None


def _legacy_static_context() -> ssl.SSLContext | None:
    """Load the old single-host cert.pem/key.pem, for installs without a CA key."""
    if not _LEGACY_CERT_PATH.exists() or not _LEGACY_KEY_PATH.exists():
        return None
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(_LEGACY_CERT_PATH), str(_LEGACY_KEY_PATH))
        # Our request parser only understands HTTP/1.1 framing. Without this,
        # a client that supports HTTP/2 may negotiate it via ALPN and send
        # binary frames we can't parse, which fails silently further down.
        ctx.set_alpn_protocols(["http/1.1"])
        return ctx
    except (ssl.SSLError, OSError) as exc:
        logger.error("Failed to load static TLS certs: %s", exc)
        return None


def is_tls_available() -> bool:
    """Whether some form of TLS interception (dynamic CA or static cert) works."""
    return _load_ca() is not None or (_LEGACY_CERT_PATH.exists() and _LEGACY_KEY_PATH.exists())


def make_server_ssl_context(host: str | None = None) -> ssl.SSLContext | None:
    """Build an SSLContext presenting a cert valid for ``host``.

    When the local CA key is available, signs and caches a fresh leaf cert
    for the exact requested hostname, so any kiro region/host works without
    reinstalling or editing a SAN list by hand. Falls back to the legacy
    static cert.pem (one fixed hostname) if no CA key is present, and to
    None (raw tunnel passthrough) if neither is usable.
    """
    if host:
        cached = _leaf_cert_cache.get(host)
        if cached is not None:
            return cached

        leaf = _generate_leaf_cert(host)
        if leaf is not None:
            cert_pem, key_pem = leaf
            try:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                # Force HTTP/1.1: our parser can't handle HTTP/2 binary framing,
                # and without this the client may silently negotiate h2.
                ctx.set_alpn_protocols(["http/1.1"])
                import tempfile

                with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as cert_f, \
                        tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as key_f:
                    cert_f.write(cert_pem)
                    key_f.write(key_pem)
                    cert_tmp_path, key_tmp_path = cert_f.name, key_f.name
                try:
                    ctx.load_cert_chain(cert_tmp_path, key_tmp_path)
                finally:
                    import os as _os
                    _os.unlink(cert_tmp_path)
                    _os.unlink(key_tmp_path)
                _leaf_cert_cache[host] = ctx
                logger.info("kiro: signed fresh TLS cert for %s via local CA", host)
                return ctx
            except (ssl.SSLError, OSError) as exc:
                logger.error("Failed to load generated leaf cert for %s: %s", host, exc)

    # Fall back to the legacy fixed-host cert (pre-CA installs).
    return _legacy_static_context()


async def intercept_kiro(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, target: str,
    raw_tunnel_fn,
) -> None:
    """Intercept a kiro CONNECT request: TLS-terminate, compress, forward.

    raw_tunnel_fn: fallback function to call if TLS setup fails.
    """
    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await writer.drain()

    host = target.rpartition(":")[0] or target
    server_ssl_ctx = make_server_ssl_context(host)
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
    except (asyncio.TimeoutError, ConnectionError) as exc:
        logger.warning("Reading request from %s failed: %s", target, exc)
        tls_writer.close()
        return

    if request_data is None:
        logger.warning(
            "No parseable HTTP/1.1 request on %s — connection dropped without compression",
            target,
        )
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
        compressed_body, compression_stats = compress_kiro_request(
            body, session_timer=_session_timer
        )
        if compression_stats.get("cache_bypass"):
            # Cache is warm — body passed through unchanged.
            _stats["requests_cache_bypass"] += 1
            record_compression(original_size, original_size)
            _stats["bytes_request_sent"] += len(body)
        else:
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
        logger.warning("Timed out waiting for first request line")
        return None

    if not request_line:
        logger.debug("Client closed connection before sending a request")
        return None

    parts = request_line.decode("latin-1").strip().split(" ", 2)
    if len(parts) < 2:
        logger.warning(
            "Could not parse request line as HTTP/1.1 (got %d bytes, first 32: %r); "
            "client may have negotiated an unsupported protocol (e.g. HTTP/2)",
            len(request_line), request_line[:32],
        )
        return None

    method, path = parts[0], parts[1]

    headers: dict[str, str] = {}
    content_length = 0
    is_chunked = False
    # Total timeout for header block: a well-behaved client sends all headers
    # within a few seconds. 30s covers slow connections without pinning forever.
    header_deadline = asyncio.get_event_loop().time() + 30.0
    while True:
        remaining = header_deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            logger.warning("Header read timed out (30s deadline)")
            return None
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=remaining)
        except asyncio.TimeoutError:
            logger.warning("Header read timed out waiting for next line")
            return None
        if line in (b"\r\n", b"\n", b""):
            break
        decoded = line.decode("latin-1").strip()
        if ":" in decoded:
            key, _, value = decoded.partition(":")
            headers[key.strip().lower()] = value.strip()
            if key.strip().lower() == "content-length":
                try:
                    content_length = int(value.strip())
                except ValueError:
                    logger.warning("Non-numeric Content-Length: %r, treating as 0", value.strip())
                    content_length = 0
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
    # Force HTTP/1.1 to the real kiro server too: our response-streaming
    # code below parses HTTP/1.1 framing (Content-Length / chunked), not h2.
    upstream_ssl.set_alpn_protocols(["http/1.1"])
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
