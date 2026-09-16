from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

import dashboard.live_refresh as live_refresh
import local_apps.urls as app_urls
from chat_interface.audit_trail import ChatAuditStore
from chat_interface.evaluator import ChatEvaluator
from dashboard.metrics import DashboardMetric, DashboardSnapshot
from local_apps.unified_server import UnifiedServer


def _chat_snapshot() -> DashboardSnapshot:
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
    store = ChatAuditStore(tmp_path / "chat_audit.jsonl")
    evaluator = ChatEvaluator(store)
    httpd = UnifiedServer(("127.0.0.1", 0), evaluator, _chat_snapshot(), "test data")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{httpd.server_port}"
    try:
        yield base_url
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def _get(base_url: str, path: str):
    try:
        resp = urllib.request.urlopen(base_url + path)
        return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _post(base_url: str, path: str, payload: dict):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base_url + path, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        resp = urllib.request.urlopen(req)
        return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


# --- Data Console still owns "/" and everything it always owned -------


def test_get_root_serves_data_console_unchanged(server):
    status, body = _get(server, "/")

    assert status == 200
    assert b"Connect Your Data" in body


def test_get_unknown_path_still_404s_through_data_consoles_own_routing(server):
    status, _ = _get(server, "/nonexistent")

    assert status == 404


# --- AI Assistant mounted under /chat/ ---------------------------------


def test_get_chat_serves_the_ai_assistant_page(server):
    status, body = _get(server, "/chat/")

    assert status == 200
    assert b"SupplyMind AI Chat" in body


def test_get_chat_page_js_posts_to_the_prefixed_chat_api_path(server):
    # Regression (2026-09-11): the merged server's own routing was
    # correctly prefixed from the start, but chat_ui.render_page()'s
    # embedded JS still hardcoded the un-prefixed "/api/chat" - a path
    # this merged server has no route for - so a real browser click on
    # "Ask" got a plain "not found" from this server's own generic 404,
    # even though every test that posted to "/chat/api/chat" directly
    # (as the other tests in this file do) passed. This test exercises
    # the actual served page, not the API path directly, so it would have
    # caught that gap.
    _, body = _get(server, "/chat/")

    assert b"fetch('/chat/api/chat'" in body


def test_get_chat_without_trailing_slash_also_serves_the_page(server):
    status, body = _get(server, "/chat")

    assert status == 200
    assert b"SupplyMind AI Chat" in body


def test_post_chat_api_answers_a_query(server):
    status, data = _post(server, "/chat/api/chat", {"query": "what's our stockout risk?"})

    assert status == 200
    assert data["status"] == "answered"
    assert "Stockout Risk" in data["answer"]


def test_post_chat_api_rejects_malformed_json(server):
    status, data = _post(server, "/chat/api/chat", {"query": 123})  # not a string

    assert status == 400
    assert "invalid request" in data["error"]


# --- What-If Simulator mounted under /simulate/ ------------------------


def test_get_simulate_serves_the_simulator_page(server):
    status, body = _get(server, "/simulate/")

    assert status == 200
    assert b"What-If Simulator" in body


def test_get_simulate_page_js_posts_to_the_prefixed_simulate_api_path(server):
    # Same regression class as test_get_chat_page_js_posts_to_the_prefixed_chat_api_path
    # above, for scenario_simulator.render_page() - this is the exact bug
    # a user hit clicking "Run Simulation" and getting "not found".
    _, body = _get(server, "/simulate/")

    assert b"fetch('/simulate/api/simulate'" in body


def test_post_simulate_api_runs_a_scenario(server):
    payload = {
        "scenario_name": "what-if",
        "baseline": {
            "sku": "SKU-1",
            "current_stock": 100.0,
            "safety_stock": 20.0,
            "daily_demand_rate": 5.0,
            "lead_time_days": 10.0,
        },
        "deltas": {},
    }

    status, data = _post(server, "/simulate/api/simulate", payload)

    assert status == 200
    assert "baseline" in data and "projected" in data


def test_post_simulate_api_rejects_a_missing_baseline(server):
    status, data = _post(server, "/simulate/api/simulate", {"scenario_name": "what-if"})

    assert status == 400
    assert "baseline is required" in data["error"]


# --- Learn mounted under /learn/ ----------------------------------------


def test_get_learn_serves_the_learn_page(server):
    status, body = _get(server, "/learn/")

    assert status == 200
    assert b"<h1>Glossary</h1>" in body


def test_get_learn_without_trailing_slash_also_serves_the_page(server):
    status, body = _get(server, "/learn")

    assert status == 200
    assert b"<h1>Glossary</h1>" in body


# --- nav bars agree with each other across all screens ------------------


def test_all_screens_show_the_same_five_item_nav_bar(server):
    # Grew from 4 to 5 items (2026-09-13) with the addition of the
    # /learn/-mounted screen, labeled "Glossary" (renamed from "Learn"
    # after a user asked for "something more meaningful").
    _, root_body = _get(server, "/")
    _, chat_body = _get(server, "/chat/")
    _, simulate_body = _get(server, "/simulate/")
    _, learn_body = _get(server, "/learn/")

    for body, current_label in (
        (root_body, "Data Console"),
        (chat_body, "AI Assistant"),
        (simulate_body, "What-If Simulator"),
        (learn_body, "Glossary"),
    ):
        text = body.decode("utf-8")
        assert text.count('class="nav-item') == 5
        assert '<span class="nav-item nav-current">' in text
        assert f"{current_label}</span>" in text


def test_chat_picks_up_a_run_analysis_refresh_without_a_server_restart(server):
    # Same regression class as chat_interface's own equivalent test - here
    # exercised through the merged server, which is what a real user
    # actually clicks through.
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
        _, chat_body = _get(server, "/chat/")
        assert b"refreshed-snap" in chat_body

        status, data = _post(server, "/chat/api/chat", {"query": "what's our stockout risk?"})
        assert status == 200
        assert "just refreshed" in data["answer"]
    finally:
        live_refresh._latest_real_data_snapshot = None


def test_chat_page_links_to_data_console_and_simulator_on_the_same_origin(server):
    # Nav links come from local_apps.urls's own configured DATA_CONSOLE_PORT,
    # not this fixture's own random test port (UnifiedServer is constructed
    # directly here, bypassing build_server()'s port selection) - the same
    # reason chat_interface/scenario_simulation's own nav-bar tests assert
    # against app_urls.* rather than the fixture's dynamic base_url.
    _, body = _get(server, "/chat/")
    text = body.decode("utf-8")

    assert f'href="{app_urls.DATA_CONSOLE_URL}"' in text
    assert f'href="{app_urls.SCENARIO_SIMULATOR_URL}"' in text
