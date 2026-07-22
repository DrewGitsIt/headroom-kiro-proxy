"""Asyncio CONNECT proxy for kiro TLS interception.

Entry point for the kiro compression proxy. Handles:
- CONNECT requests (route to interceptor or raw tunnel)
- Local /health and /stats endpoints
- Background metrics reporting
- Server lifecycle (start, serve, shutdown)

Usage:
    python proxy.py --port 9090
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from interceptor import intercept_kiro, is_compression_available, get_import_error, make_server_ssl_context
from stats import _stats, get_stats, load_daily_totals, flush_daily_totals

logger = logging.getLogger("kiro_proxy.proxy")

KIRO_RUNTIME_HOST = "runtime.us-east-1.kiro.dev"


# --- Connection handling ---


async def _handle_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Main entry point for each incoming connection.

    Routes to:
    - /health, /stats → local request handler
    - CONNECT to kiro → TLS interception
    - CONNECT to anything else → raw tunnel passthrough
    """
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
        await intercept_kiro(reader, writer, target, _raw_tunnel)
    else:
        await _tunnel_passthrough(reader, writer, target)


async def _handle_local_request(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str
) -> None:
    """Serve /health and /stats on the proxy port."""
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


# --- Tunneling ---


async def _tunnel_passthrough(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, target: str
) -> None:
    """Non-kiro traffic: send 200 and open a raw byte-pipe tunnel."""
    _stats["tunnels_passthrough"] += 1
    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await writer.drain()
    await _raw_tunnel(reader, writer, target)


async def _raw_tunnel(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, target: str
) -> None:
    """Bidirectional byte-pipe between client and upstream.

    Cancels both directions when either side closes or errors.
    try/finally guarantees cleanup even if asyncio.wait itself raises.
    """
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

    t1 = asyncio.create_task(pipe(reader, upstream_writer))
    t2 = asyncio.create_task(pipe(upstream_reader, writer))
    try:
        done, pending = await asyncio.wait(
            {t1, t2}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        for t in (t1, t2):
            if not t.done():
                t.cancel()
        await asyncio.gather(t1, t2, return_exceptions=True)


# --- Server lifecycle ---


def _proxy_version() -> str:
    """Return the proxy version string, falling back to 'unknown'."""
    version_file = Path(__file__).parent.parent / "VERSION"
    if version_file.exists():
        return version_file.read_text().strip() or "unknown"
    return "unknown"


async def _daily_reporter() -> None:
    """Background task: upload metrics periodically.

    Uses reporter.start_periodic_reporter if available.
    All exceptions are caught; this coroutine must never crash the proxy.
    """
    try:
        from reporter import start_periodic_reporter
        await start_periodic_reporter(get_stats)
    except ImportError:
        logger.debug("reporter module not available, metrics disabled")
    except Exception as exc:
        logger.debug("reporter: unexpected error: %s", exc)


async def _periodic_flush() -> None:
    """Background task: flush stats to disk every 10 minutes.

    Ensures daily totals survive proxy restarts. Runs until cancelled.
    """
    while True:
        await asyncio.sleep(600)  # 10 minutes
        flush_daily_totals()


async def start_connect_proxy(port: int = 9090) -> asyncio.Server:
    """Start the CONNECT proxy server and log startup status."""
    server = await asyncio.start_server(
        _handle_connect, "127.0.0.1", port, reuse_address=True
    )
    addr = server.sockets[0].getsockname()
    logger.info("kiro-proxy listening on %s:%d (pid=%d)", addr[0], addr[1], os.getpid())

    if not is_compression_available():
        err = get_import_error()
        logger.warning("DEGRADED: compression unavailable (handler import failed: %s)", err)
        logger.warning("DEGRADED: proxy will pass traffic through without compression")
    ssl_ctx = make_server_ssl_context()
    if ssl_ctx is None:
        logger.warning("DEGRADED: TLS interception unavailable — all traffic will tunnel raw")
    else:
        logger.info("TLS interception ready (cert loaded)")
    if is_compression_available():
        logger.info("Compression engine ready")

    return server


def main() -> None:
    import argparse
    import signal

    parser = argparse.ArgumentParser(description="Kiro compression proxy")
    parser.add_argument("--port", type=int, default=9090)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Load prior daily totals (survives restarts within the same day)
    load_daily_totals()

    async def run() -> None:
        # Flush stats on SIGTERM (launchd stop, kiro-proxy restart)
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: _shutdown(loop))

        server = await start_connect_proxy(port=args.port)
        reporter_task = asyncio.create_task(_daily_reporter(), name="periodic_reporter")
        flush_task = asyncio.create_task(_periodic_flush(), name="periodic_flush")
        try:
            async with server:
                await server.serve_forever()
        finally:
            reporter_task.cancel()
            flush_task.cancel()
            try:
                await reporter_task
            except asyncio.CancelledError:
                pass
            try:
                await flush_task
            except asyncio.CancelledError:
                pass
            # Final flush on shutdown
            flush_daily_totals()
            logger.info("Shutdown complete, daily totals flushed")

    def _shutdown(loop: asyncio.AbstractEventLoop) -> None:
        """Signal handler: stop the event loop gracefully."""
        logger.info("Received shutdown signal, flushing stats...")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    asyncio.run(run())


if __name__ == "__main__":
    main()
