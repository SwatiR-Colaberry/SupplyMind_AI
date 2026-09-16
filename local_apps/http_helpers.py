"""Shared stdlib http.server request/response helpers for the local SupplyMind AI dev servers.

Before this module existed, chat_interface/serve_chat_ui.py and
scenario_simulation/serve_scenario_simulator.py each hand-copied the
exact same two helpers (_read_request_body/_send_json) - real, growing
duplication now that local_apps/unified_server.py needs the same two
helpers for a 3rd handler. Dependency-free (stdlib only) for the same
"missing third module" cycle-avoidance reason local_apps/urls.py and
local_apps/theme.py already state in their own docstrings.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler


def read_request_body(handler: BaseHTTPRequestHandler, max_bytes: int) -> bytes:
    """Read and validate `handler`'s request body per its Content-Length header, or raise ValueError.

    Raises for: a non-numeric header (int() would otherwise raise an
    uncaught ValueError deeper in the stdlib); a negative length
    (handler.rfile.read() on a live socket would otherwise block forever
    waiting for EOF that never comes, wedging a single-threaded server
    for every other client); a length beyond `max_bytes` (no legitimate
    request to any of these local dev servers is anywhere near that
    large).
    """
    raw_length = handler.headers.get("Content-Length")
    if raw_length is None:
        return b""
    try:
        length = int(raw_length)
    except ValueError as exc:
        raise ValueError(f"Content-Length {raw_length!r} is not a valid integer") from exc
    if length < 0:
        raise ValueError(f"Content-Length {length} must not be negative")
    if length > max_bytes:
        raise ValueError(f"Content-Length {length} exceeds the {max_bytes}-byte limit")
    return handler.rfile.read(length) if length else b""


def send_json(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def send_html(handler: BaseHTTPRequestHandler, status: int, html: str) -> None:
    body = html.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
