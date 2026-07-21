"""Unit tests for interceptor.py — HTTP request parsing and chunked body.

Uses asyncio.StreamReader with feed_data/feed_eof to simulate network input
without real sockets.
"""

import asyncio
import pytest
from interceptor import _read_http_request, _read_chunked_body


def _make_reader(data: bytes) -> asyncio.StreamReader:
    """Create a StreamReader pre-loaded with data, then EOF."""
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


class TestReadHttpRequest:
    """Tests for _read_http_request()."""

    @pytest.fixture(autouse=True)
    def _event_loop(self):
        """Ensure tests have clean event loop."""
        pass

    async def test_simple_get_no_body(self):
        """Parse a simple GET with no body."""
        raw = b"GET /v1/messages HTTP/1.1\r\nHost: example.com\r\n\r\n"
        reader = _make_reader(raw)
        result = await _read_http_request(reader)
        assert result is not None
        method, path, headers, body = result
        assert method == "GET"
        assert path == "/v1/messages"
        assert headers["host"] == "example.com"
        assert body == b""

    async def test_post_with_content_length(self):
        """Parse a POST with Content-Length body."""
        body_data = b'{"model": "claude-sonnet-4-20250514"}'
        raw = (
            b"POST /v1/messages HTTP/1.1\r\n"
            b"Host: api.example.com\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body_data)).encode() + b"\r\n"
            b"\r\n"
            + body_data
        )
        reader = _make_reader(raw)
        result = await _read_http_request(reader)
        assert result is not None
        method, path, headers, body = result
        assert method == "POST"
        assert path == "/v1/messages"
        assert body == body_data
        assert headers["content-type"] == "application/json"

    async def test_chunked_transfer_encoding(self):
        """Parse a request with Transfer-Encoding: chunked."""
        # Build chunked body: "Hello" (5 bytes) + "World" (5 bytes) + 0-terminator
        chunked_body = b"5\r\nHello\r\n5\r\nWorld\r\n0\r\n\r\n"
        raw = (
            b"POST /v1/messages HTTP/1.1\r\n"
            b"Host: api.example.com\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"\r\n"
            + chunked_body
        )
        reader = _make_reader(raw)
        result = await _read_http_request(reader)
        assert result is not None
        _, _, _, body = result
        assert body == b"HelloWorld"

    async def test_empty_request_returns_none(self):
        """Empty input (EOF immediately) returns None."""
        reader = _make_reader(b"")
        result = await _read_http_request(reader)
        assert result is None

    async def test_malformed_request_line(self):
        """Request line with only one part returns None."""
        reader = _make_reader(b"GARBAGE\r\n\r\n")
        result = await _read_http_request(reader)
        assert result is None

    async def test_missing_content_length_no_body(self):
        """POST with no Content-Length and no chunked → empty body."""
        raw = b"POST /upload HTTP/1.1\r\nHost: x\r\n\r\n"
        reader = _make_reader(raw)
        result = await _read_http_request(reader)
        assert result is not None
        _, _, _, body = result
        assert body == b""

    async def test_non_numeric_content_length(self):
        """Non-numeric Content-Length should raise ValueError (caught by caller)."""
        raw = b"POST /v1/messages HTTP/1.1\r\nContent-Length: abc\r\n\r\n"
        reader = _make_reader(raw)
        with pytest.raises(ValueError):
            await _read_http_request(reader)

    async def test_headers_case_insensitive_keys(self):
        """Header keys are lowercased."""
        raw = b"GET / HTTP/1.1\r\nX-Custom-Header: MyValue\r\nHOST: example.com\r\n\r\n"
        reader = _make_reader(raw)
        result = await _read_http_request(reader)
        assert result is not None
        _, _, headers, _ = result
        assert headers["x-custom-header"] == "MyValue"
        assert headers["host"] == "example.com"

    async def test_duplicate_headers_last_wins(self):
        """Duplicate headers: last value wins (dict behavior)."""
        raw = b"GET / HTTP/1.1\r\nX-Foo: first\r\nX-Foo: second\r\n\r\n"
        reader = _make_reader(raw)
        result = await _read_http_request(reader)
        _, _, headers, _ = result
        assert headers["x-foo"] == "second"

    async def test_large_body(self):
        """Large body is read completely with Content-Length."""
        body_data = b"x" * 100_000
        raw = (
            b"POST / HTTP/1.1\r\n"
            b"Content-Length: 100000\r\n"
            b"\r\n"
            + body_data
        )
        reader = _make_reader(raw)
        result = await _read_http_request(reader)
        _, _, _, body = result
        assert len(body) == 100_000
        assert body == body_data


class TestReadChunkedBody:
    """Tests for _read_chunked_body()."""

    async def test_single_chunk(self):
        """Single chunk + terminator."""
        data = b"a\r\n0123456789\r\n0\r\n\r\n"  # 0xa = 10 bytes
        reader = _make_reader(data)
        body = await _read_chunked_body(reader)
        assert body == b"0123456789"

    async def test_multiple_chunks(self):
        """Multiple chunks assembled in order."""
        data = b"3\r\nfoo\r\n3\r\nbar\r\n0\r\n\r\n"
        reader = _make_reader(data)
        body = await _read_chunked_body(reader)
        assert body == b"foobar"

    async def test_zero_byte_terminal(self):
        """Zero-length chunk terminates the body."""
        data = b"0\r\n\r\n"
        reader = _make_reader(data)
        body = await _read_chunked_body(reader)
        assert body == b""

    async def test_chunk_extensions_ignored(self):
        """Chunk extensions (;key=value) after size are stripped."""
        data = b"5;ext=val\r\nHello\r\n0\r\n\r\n"
        reader = _make_reader(data)
        body = await _read_chunked_body(reader)
        assert body == b"Hello"

    async def test_truncated_stream_returns_partial(self):
        """If stream ends mid-chunk, returns what was successfully read.

        When readexactly raises IncompleteReadError, the partial data from
        the *current* chunk is lost, but previously completed chunks are kept.
        """
        # First chunk: 3 bytes "abc" — succeeds
        # Second chunk: promises 10 bytes but EOF after 5 — fails
        reader = asyncio.StreamReader()
        reader.feed_data(b"3\r\nabc\r\na\r\n12345")
        reader.feed_eof()
        body = await _read_chunked_body(reader)
        # First chunk "abc" was completed, second chunk partial is lost
        assert body == b"abc"

    async def test_invalid_hex_size_returns_partial(self):
        """Non-hex chunk size triggers ValueError → returns partial."""
        data = b"5\r\nHello\r\nZZZ\r\n"
        reader = _make_reader(data)
        body = await _read_chunked_body(reader)
        # First chunk succeeded, second fails to parse
        assert body == b"Hello"

    async def test_hex_uppercase(self):
        """Hex sizes can be uppercase."""
        data = b"A\r\n0123456789\r\n0\r\n\r\n"
        reader = _make_reader(data)
        body = await _read_chunked_body(reader)
        assert body == b"0123456789"

    async def test_empty_stream(self):
        """Empty stream (EOF immediately) returns empty body."""
        reader = _make_reader(b"")
        body = await _read_chunked_body(reader)
        assert body == b""
