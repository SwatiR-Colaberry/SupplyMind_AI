from __future__ import annotations

import http.client
import json
import threading
import urllib.error
import urllib.request
from urllib.parse import urlparse

import pytest

from chat_interface.audit_trail import ChatAuditStore
from chat_interface.evaluator import ChatEvaluator
from chat_interface.serve_chat_ui import MAX_REQUEST_BODY_BYTES, ChatUIServer
from dashboard.metrics import DashboardMetric, DashboardSnapshot


def _snapshot() -> DashboardSnapshot:
    return DashboardSnapshot(
        dashboard_id="test-snap",
        generated_at="2026-09-07T00:00:00+00:00",
        metrics=[
            DashboardMetric(
                metric_id="stockout_risk_agent",
                label="Stockout Risk",
                status="ok",
                headline="2 SKUs at critical risk",
                source_agent="stockout_risk_agent",
                confidence=0.9,
            )
        ],
    )


@pytest.fixture
def server(tmp_path):
    store = ChatAuditStore(tmp_path / "audit.jsonl")
    evaluator = ChatEvaluator(store)

    httpd = ChatUIServer(("127.0.0.1", 0), evaluator, _snapshot(), "test data")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{httpd.server_port}"
    try:
        yield base_url, store
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def _post(base_url: str, path: str, payload: dict | None = None, raw_body: bytes | None = None):
    data = raw_body if raw_body is not None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base_url + path, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        resp = urllib.request.urlopen(req)
        return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _post_with_raw_content_length(base_url: str, path: str, content_length: object, body: bytes = b'{"query":"hi"}'):
    """POST with an arbitrary, possibly-invalid Content-Length header value.

    urllib.request always computes a correct Content-Length itself, so
    reaching a malformed/negative/oversized header (the exact inputs
    _read_request_body() must reject) requires talking to the socket at a
    lower level via http.client.
    """
    parsed = urlparse(base_url)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    try:
        conn.putrequest("POST", path)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(content_length))
        conn.endheaders()
        conn.send(body)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())
    finally:
        conn.close()


def test_get_root_returns_html_page(server):
    base_url, _ = server
    resp = urllib.request.urlopen(base_url + "/")
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("text/html")
    body = resp.read().decode("utf-8")
    assert "SupplyMind AI Chat" in body
    assert "test-snap" in body  # dashboard_id interpolated into the page


def test_get_unknown_path_returns_404(server):
    base_url, _ = server
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(base_url + "/nonexistent")
    assert exc_info.value.code == 404


def test_post_chat_valid_query_returns_answered_and_is_audited(server):
    base_url, store = server
    status, data = _post(base_url, "/api/chat", {"query": "what's our stockout risk?"})
    assert status == 200
    assert data["status"] == "answered"
    assert data["topic"] == "stockout_risk_agent"
    assert "critical risk" in data["answer"]
    assert store.has_recorded(data["interaction_id"])


def test_post_chat_unsupported_query_returns_unsupported_and_is_audited(server):
    base_url, store = server
    status, data = _post(base_url, "/api/chat", {"query": "what's the weather?"})
    assert status == 200
    assert data["status"] == "unsupported"
    assert data["topic"] is None
    assert store.has_recorded(data["interaction_id"])


def test_post_chat_missing_query_field_returns_400(server):
    base_url, _ = server
    status, data = _post(base_url, "/api/chat", {"nope": True})
    assert status == 400
    assert "invalid request" in data["error"]


def test_post_chat_malformed_json_returns_400(server):
    base_url, _ = server
    status, data = _post(base_url, "/api/chat", raw_body=b"{not valid json")
    assert status == 400


def test_post_chat_non_string_query_returns_400(server):
    base_url, _ = server
    status, data = _post(base_url, "/api/chat", {"query": 123})
    assert status == 400


def test_post_unknown_path_returns_404(server):
    base_url, _ = server
    status, data = _post(base_url, "/api/nope", {"query": "hi"})
    assert status == 404


def test_post_chat_non_numeric_content_length_returns_400_not_a_crash(server):
    # Regression test: a pre-commit review found this previously raised an
    # uncaught ValueError out of do_POST, dropping the connection with no
    # HTTP response at all instead of a clean 400.
    base_url, _ = server
    status, data = _post_with_raw_content_length(base_url, "/api/chat", "notanumber")
    assert status == 400
    assert "invalid request" in data["error"]


def test_post_chat_negative_content_length_returns_400_and_does_not_hang_the_server(server):
    # Regression test: a pre-commit review found self.rfile.read(-1)
    # blocks forever waiting for EOF on a live connection, wedging this
    # single-threaded server for every other client. The fixture's
    # 5-second connection timeout means this test would itself hang/fail
    # rather than silently pass if the bug were still present, and the
    # follow-up request proves the server is still serving other clients.
    base_url, _ = server
    status, data = _post_with_raw_content_length(base_url, "/api/chat", -1)
    assert status == 400
    assert "must not be negative" in data["error"]

    # The server must still be responsive to a completely different
    # request right after - this is the actual "did it wedge" proof.
    resp = urllib.request.urlopen(base_url + "/")
    assert resp.status == 200


def test_post_chat_oversized_content_length_returns_400(server):
    base_url, _ = server
    status, data = _post_with_raw_content_length(base_url, "/api/chat", MAX_REQUEST_BODY_BYTES + 1)
    assert status == 400
    assert "exceeds" in data["error"]
