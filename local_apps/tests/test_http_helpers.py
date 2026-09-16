from __future__ import annotations

import io
import json

import pytest

from local_apps.http_helpers import read_request_body, send_html, send_json


class _FakeHandler:
    """Stand-in for a BaseHTTPRequestHandler - just the attributes/methods
    read_request_body/send_json/send_html actually touch."""

    def __init__(self, headers: dict[str, str], body: bytes = b"") -> None:
        self.headers = headers
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.responses: list[tuple[int, list[tuple[str, str]]]] = []
        self._sent_headers: list[tuple[str, str]] = []

    def send_response(self, status: int) -> None:
        self._current_status = status

    def send_header(self, name: str, value: str) -> None:
        self._sent_headers.append((name, value))

    def end_headers(self) -> None:
        self.responses.append((self._current_status, list(self._sent_headers)))
        self._sent_headers = []


def test_read_request_body_returns_empty_bytes_when_no_content_length():
    handler = _FakeHandler(headers={})

    assert read_request_body(handler, max_bytes=100) == b""


def test_read_request_body_reads_exactly_content_length_bytes():
    handler = _FakeHandler(headers={"Content-Length": "5"}, body=b"hello world")

    assert read_request_body(handler, max_bytes=100) == b"hello"


def test_read_request_body_rejects_a_non_numeric_content_length():
    handler = _FakeHandler(headers={"Content-Length": "not-a-number"})

    with pytest.raises(ValueError, match="not a valid integer"):
        read_request_body(handler, max_bytes=100)


def test_read_request_body_rejects_a_negative_content_length():
    handler = _FakeHandler(headers={"Content-Length": "-1"})

    with pytest.raises(ValueError, match="must not be negative"):
        read_request_body(handler, max_bytes=100)


def test_read_request_body_rejects_a_content_length_beyond_the_cap():
    handler = _FakeHandler(headers={"Content-Length": "1000"})

    with pytest.raises(ValueError, match="exceeds the 100-byte limit"):
        read_request_body(handler, max_bytes=100)


def test_send_json_writes_the_status_headers_and_body():
    handler = _FakeHandler(headers={})

    send_json(handler, 200, {"answer": 42})

    assert handler.responses == [(200, [("Content-Type", "application/json"), ("Content-Length", "14")])]
    assert json.loads(handler.wfile.getvalue()) == {"answer": 42}


def test_send_html_writes_the_status_headers_and_body():
    handler = _FakeHandler(headers={})

    send_html(handler, 200, "<html>hi</html>")

    assert handler.responses == [(200, [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", "15")])]
    assert handler.wfile.getvalue() == b"<html>hi</html>"
