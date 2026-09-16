from __future__ import annotations

import http.client
import json
import threading
import urllib.error
import urllib.request
from urllib.parse import urlparse

import pytest

from agents.contracts import AgentFinding
from chat_interface.audit_trail import ChatAuditStore
from chat_interface.evaluator import ChatEvaluator
import dashboard.live_refresh as live_refresh
from chat_interface.serve_chat_ui import MAX_REQUEST_BODY_BYTES, ChatUIServer, current_snapshot
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


def test_get_root_page_includes_the_shared_first_visit_onboarding_overlay(server):
    base_url, _ = server
    resp = urllib.request.urlopen(base_url + "/")
    body = resp.read().decode("utf-8")

    assert '<div class="onboarding-overlay" id="onboarding-overlay">' in body


def test_get_root_page_transcript_starts_with_a_designed_empty_state(server):
    # A design-review ask for empty states to match "the theme of the
    # app" - the transcript used to start as plain placeholder text; now
    # the same icon+title+text card local_apps/theme.py's
    # render_empty_state() builds everywhere else.
    base_url, _ = server
    resp = urllib.request.urlopen(base_url + "/")
    body = resp.read().decode("utf-8")

    assert '<div class="empty-state">' in body
    assert '<div class="empty-state-title">No questions yet</div>' in body


def test_get_root_page_header_uses_the_shared_background_treatment_and_icon(server):
    base_url, _ = server
    resp = urllib.request.urlopen(base_url + "/")
    body = resp.read().decode("utf-8")

    assert '<div class="page-header">' in body
    assert '<div class="page-header-icon page-header-icon-accent-2">' in body
    assert "<h1>SupplyMind AI Chat</h1>" in body
    assert "test-snap" in body  # dashboard_id still interpolated, now inside the header band


def test_get_root_page_shares_data_consoles_page_width(server):
    # Regression (2026-09-12): this page's .page was 900px while Data
    # Console and Live Data both use 1180px - since the nav bar sits at
    # .page's left edge, switching to this tab visibly shifted the whole
    # layout as the centered margin recalculated for the narrower width, a
    # user reported as "the page center is moving." Matched to 1180px.
    base_url, _ = server
    resp = urllib.request.urlopen(base_url + "/")
    body = resp.read().decode("utf-8")

    assert ".page { max-width: 1180px" in body


def test_get_root_page_offers_several_example_questions(server):
    # Added 2026-09-12 after a user asked to "add 3 or 4 more examples" -
    # clickable chips that fill the input and submit, not just the
    # input's own placeholder text.
    base_url, _ = server
    resp = urllib.request.urlopen(base_url + "/")
    body = resp.read().decode("utf-8")

    assert body.count('class="example-chip"') >= 4
    assert "input.value = chip.textContent;" in body
    assert "form.requestSubmit();" in body


def test_get_root_page_explains_confidence_with_a_tooltip(server):
    # Added 2026-09-12 after a user said "confidence" "does not explain
    # anything" on its own - a plain-language title tooltip on the label.
    base_url, _ = server
    resp = urllib.request.urlopen(base_url + "/")
    body = resp.read().decode("utf-8")

    assert 'class="term" data-tooltip="How sure the system is in this answer' in body


def test_get_root_page_no_longer_has_its_own_glossary_panel(server):
    # Regression (2026-09-13): this page's compact glossary panel (added
    # 2026-09-12, fifth report) moved to a dedicated Glossary screen after
    # a follow-up pointed out it was "only accessible in [the] what if
    # tab" and that the panel "has not been removed from other pages"
    # once that screen existed. The inline .term confidence tooltip and
    # render_answer_lines()'s own inline glossary-wrapping (see the tests
    # above/below) are unaffected - only the standalone panel is gone.
    base_url, _ = server
    resp = urllib.request.urlopen(base_url + "/")
    body = resp.read().decode("utf-8")

    assert "glossary-panel" not in body


def test_get_root_page_js_posts_to_the_standalone_api_path_by_default(server):
    # Regression (2026-09-11): render_page()'s api_path defaulted wrong
    # would mean this standalone server's own page can't reach its own
    # API either - a user asking a question got a plain "not found" from
    # a mismatched fetch target, which no test calling the API endpoint
    # directly (as every other test in this file does) could ever catch.
    base_url, _ = server
    resp = urllib.request.urlopen(base_url + "/")
    body = resp.read().decode("utf-8")

    assert "fetch('/api/chat'" in body


def test_get_root_nav_bar_links_to_the_other_2_local_apps(server):
    # Regression guard: this page previously had no nav bar at all, so
    # there was no way to get from the chat UI to Data Console or the
    # What-If Simulator without typing a URL by hand.
    import local_apps.urls as app_urls

    base_url, _ = server
    resp = urllib.request.urlopen(base_url + "/")
    text = resp.read().decode("utf-8")
    assert f'href="{app_urls.DATA_CONSOLE_URL}"' in text
    assert f'href="{app_urls.LIVE_DASHBOARD_URL}"' in text
    assert f'href="{app_urls.SCENARIO_SIMULATOR_URL}"' in text


def test_get_root_nav_bar_matches_the_other_local_apps(server):
    # Regression guard (2026-09-11, grew from 4 to 5 items 2026-09-13 with
    # the addition of Learn): every screen's nav bar now comes from the
    # same shared local_apps.theme.render_nav_bar(), so this page shows
    # the same top-level tabs, in the same order, as Data Console and the
    # What-If Simulator - not its own drifted subset.
    base_url, _ = server
    resp = urllib.request.urlopen(base_url + "/")
    text = resp.read().decode("utf-8")
    assert text.count('class="nav-item') == 5
    assert '<span class="nav-item nav-current">' in text
    assert "AI Assistant</span>" in text


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
    # This fixture's metric has no per-subject findings -> nothing to chart.
    assert data["charts"] == []


def test_post_chat_answer_includes_a_list_rendered_glossary_tagged_answer_html(server):
    # Added 2026-09-12 after a user said the chat answer "is not in a
    # list" and unexplained jargon "does not explain anything" - answer_html
    # reuses dashboard/render.py's own condense/split/highlight/glossary
    # pipeline (render_answer_lines()) rather than a second implementation.
    base_url, _ = server
    status, data = _post(base_url, "/api/chat", {"query": "what's our stockout risk?"})

    assert status == 200
    assert "answer_html" in data
    assert '<ul class="tile-headline">' in data["answer_html"]
    assert "<li class=\"headline-line\">" in data["answer_html"]


def test_post_chat_answer_includes_a_rendered_chart_when_the_metric_has_findings(tmp_path):
    findings = [
        AgentFinding(subject="SKU-1", subject_kind="sku", severity="critical", detail="below safety stock"),
    ]
    snapshot = DashboardSnapshot(
        dashboard_id="test-snap-charts",
        generated_at="2026-09-10T00:00:00+00:00",
        metrics=[
            DashboardMetric(
                metric_id="stockout_risk_agent", label="Stockout Risk", status="ok",
                headline="1 SKU at risk", source_agent="stockout_risk_agent", confidence=0.9, findings=findings,
            )
        ],
    )
    store = ChatAuditStore(tmp_path / "audit.jsonl")
    httpd = ChatUIServer(("127.0.0.1", 0), ChatEvaluator(store), snapshot, "test data")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{httpd.server_port}"
    try:
        status, data = _post(base_url, "/api/chat", {"query": "what's our stockout risk?"})
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()

    assert status == 200
    assert len(data["charts"]) == 1
    chart = data["charts"][0]
    assert chart["title"]
    assert "<svg" in chart["svg"]
    assert "SKU-1" in chart["svg"]
    assert chart["omitted_count"] == 0


def test_post_chat_named_subject_query_returns_subject_and_no_topic(tmp_path):
    findings = [AgentFinding(subject="SKU-1", subject_kind="sku", severity="critical", detail="2 days of supply left")]
    snapshot = DashboardSnapshot(
        dashboard_id="test-snap-subject",
        generated_at="2026-09-11T00:00:00+00:00",
        metrics=[
            DashboardMetric(
                metric_id="stockout_risk_agent", label="Stockout Risk", status="ok",
                headline="1 SKU at risk", source_agent="stockout_risk_agent", confidence=0.9, findings=findings,
            )
        ],
    )
    store = ChatAuditStore(tmp_path / "audit.jsonl")
    httpd = ChatUIServer(("127.0.0.1", 0), ChatEvaluator(store), snapshot, "test data")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{httpd.server_port}"
    try:
        status, data = _post(base_url, "/api/chat", {"query": "why is SKU-1 at risk?"})
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()

    assert status == 200
    assert data["status"] == "answered"
    assert data["subject"] == "SKU-1"
    assert data["topic"] is None
    assert "2 days of supply left" in data["answer"]


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


def test_current_snapshot_falls_back_to_the_startup_snapshot_when_run_analysis_never_ran():
    # dashboard.live_refresh.latest_real_data_snapshot() is None until
    # "Run Analysis" succeeds at least once in this process - conftest.py's
    # autouse reset guarantees that starting state here.
    startup_snapshot = _snapshot()

    snapshot, data_source = current_snapshot(startup_snapshot, "synthetic demo data")

    assert snapshot is startup_snapshot
    assert data_source == "synthetic demo data"


def test_current_snapshot_prefers_the_latest_run_analysis_result(monkeypatch):
    # Regression (2026-09-11): before current_snapshot() existed, chat
    # always answered from whatever choose_snapshot() picked at server
    # startup - reconnecting a data source or clicking "Run Analysis"
    # never changed what chat answered from. A user asked directly
    # whether they'd need to "provide the data again" for chat; the
    # answer should be no, once Run Analysis has been run once.
    refreshed = _snapshot()
    monkeypatch.setattr(live_refresh, "_latest_real_data_snapshot", refreshed)

    snapshot, data_source = current_snapshot(_snapshot(), "synthetic demo data")

    assert snapshot is refreshed
    assert "Run Analysis" in data_source


def test_chat_page_and_api_pick_up_a_run_analysis_refresh_without_a_server_restart(server):
    # End-to-end proof through the actual running server (not just the
    # pure current_snapshot() function above): a snapshot that appears
    # via live_refresh *after* the server already started is used on the
    # very next request, both for the rendered page and for /api/chat.
    base_url, _ = server
    refreshed = DashboardSnapshot(
        dashboard_id="refreshed-snap",
        generated_at="2026-09-11T00:00:00+00:00",
        metrics=[
            DashboardMetric(
                metric_id="stockout_risk_agent",
                label="Stockout Risk",
                status="ok",
                headline="0 SKUs at risk - just refreshed",
                source_agent="stockout_risk_agent",
                confidence=0.99,
            )
        ],
    )
    live_refresh._latest_real_data_snapshot = refreshed
    try:
        resp = urllib.request.urlopen(base_url + "/")
        assert "refreshed-snap" in resp.read().decode("utf-8")

        status, data = _post(base_url, "/api/chat", {"query": "what's our stockout risk?"})
        assert status == 200
        assert "just refreshed" in data["answer"]
    finally:
        live_refresh._latest_real_data_snapshot = None
