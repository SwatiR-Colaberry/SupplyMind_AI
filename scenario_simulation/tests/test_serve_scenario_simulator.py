from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer

import pytest

from scenario_simulation.serve_scenario_simulator import ScenarioSimulatorHandler


@pytest.fixture
def server():
    httpd = HTTPServer(("127.0.0.1", 0), ScenarioSimulatorHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{httpd.server_port}"
    try:
        yield base_url
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def _get(base_url: str, path: str = "/"):
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


_BASELINE = {
    "sku": "SKU-1",
    "current_stock": 100.0,
    "safety_stock": 20.0,
    "daily_demand_rate": 5.0,
    "lead_time_days": 10.0,
}


def test_get_root_serves_the_html_page(server):
    status, body = _get(server, "/")

    assert status == 200
    assert b"What-If Simulator" in body
    assert b"sim-form" in body


def test_get_root_page_includes_the_shared_first_visit_onboarding_overlay(server):
    status, body = _get(server, "/")
    text = body.decode("utf-8")

    assert status == 200
    assert '<div class="onboarding-overlay" id="onboarding-overlay">' in text


def test_get_root_page_result_panel_starts_with_a_designed_empty_state(server):
    # A design-review ask for empty states to match "the theme of the
    # app" - the result panel used to start as plain placeholder text;
    # now the same icon+title+text card local_apps/theme.py's
    # render_empty_state() builds everywhere else.
    status, body = _get(server, "/")
    text = body.decode("utf-8")

    assert status == 200
    assert '<div class="empty-state">' in text
    assert '<div class="empty-state-title">Nothing simulated yet</div>' in text


def test_get_root_page_puts_optional_fields_behind_an_advanced_disclosure(server):
    # A design-review ask to reduce clutter: "Incoming stock," "Unit
    # price," and "Demand std dev" are genuinely optional (they only add
    # extra output, never gate whether the simulation runs) - moved into
    # a collapsed <details> so the 4 required fields stand out on load.
    status, body = _get(server, "/")
    text = body.decode("utf-8")

    assert status == 200
    assert '<details class="advanced-options">' in text
    assert "<summary>Advanced options</summary>" in text
    advanced_start = text.index('<details class="advanced-options">')
    advanced_section = text[advanced_start : text.index("</details>", advanced_start)]
    assert 'id="incoming_stock"' in advanced_section
    assert 'id="unit_price"' in advanced_section
    assert 'id="demand_std_dev"' in advanced_section
    # The 4 required fields stay outside the disclosure, visible on load.
    assert 'id="current_stock"' not in advanced_section
    assert 'id="safety_stock"' not in advanced_section
    assert 'id="daily_demand_rate"' not in advanced_section
    assert 'id="lead_time_days"' not in advanced_section


def test_get_root_page_header_uses_the_shared_background_treatment_and_icon(server):
    status, body = _get(server, "/")

    assert status == 200
    assert b'<div class="page-header">' in body
    assert b'<div class="page-header-icon page-header-icon-success">' in body
    assert b"<h1>What-If Simulator</h1>" in body


def test_get_root_page_shares_data_consoles_page_width(server):
    # Regression (2026-09-12): this page's .page was 900px while Data
    # Console and Live Data both use 1180px - since the nav bar sits at
    # .page's left edge, a user reported switching to this (the last) tab
    # as "the page center is moving," which is exactly this width
    # mismatch making the centered margin recalculate. Matched to 1180px.
    status, body = _get(server, "/")

    assert status == 200
    assert b".page { max-width: 1180px" in body


def test_get_root_page_baseline_and_projected_columns_get_distinct_subtle_colors(server):
    # A user asked for the compare-column header to be bold and for the 2
    # boxes to have a subtle, distinct color each.
    status, body = _get(server, "/")

    assert status == 200
    assert b"font-weight: 750" in body
    assert b".compare-baseline { background: var(--neutral-tint); }" in body
    assert b".compare-projected { background: var(--brand-tint); }" in body
    assert b"renderAssessment('Baseline', data.baseline, 'compare-baseline')" in body
    assert b"renderAssessment('Projected', data.projected, 'compare-projected')" in body


def test_get_root_page_no_longer_has_its_own_glossary_panel(server):
    # Regression (2026-09-13): this page's compact glossary panel (added
    # 2026-09-12) moved to a dedicated Glossary screen after a follow-up
    # pointed out it was "only accessible in [the] what if tab" and that
    # the panel "has not been removed from other pages" once that screen
    # existed. The inline .term hover tooltips (see the Safety
    # stock/Lead time/Stockout probability/Risk score/Revenue at risk
    # tests elsewhere in this file) are unaffected - only the standalone
    # panel is gone.
    status, body = _get(server, "/")

    assert status == 200
    assert b"glossary-panel" not in body


def test_get_root_page_draws_a_risk_score_meter_instead_of_just_the_number(server):
    # A design-review ask to show charts over raw numbers wherever the
    # data supports it - the risk score is a 0-100 number, a natural fit
    # for a fill meter (same visual language as learn/visuals.py's
    # render_meter_svg) shown alongside the existing risk-level badge.
    status, body = _get(server, "/")
    text = body.decode("utf-8")

    assert status == 200
    assert "function riskMeterSvg(score, level)" in text
    assert "html += riskMeterSvg(a.risk_score, a.risk_level);" in text


def test_get_root_page_has_a_reset_button_that_clears_the_form_and_results(server):
    # Added 2026-09-12 (fourth report) after a user asked for a way to
    # "remove everything and it will help in entering new data" - a Reset
    # button next to Run Simulation that restores the form's own default
    # values (native <button type="reset">) and also clears the result
    # panel back to its starting placeholder, which the native reset event
    # doesn't know about on its own.
    status, body = _get(server, "/")
    text = body.decode("utf-8")

    assert status == 200
    assert '<button type="reset" id="reset-btn" class="btn-secondary">Reset</button>' in text
    assert "form.addEventListener('reset'" in text
    # Restores the exact server-rendered initial markup (captured once at
    # load, see initialResultHtml) rather than a hand-copied duplicate
    # that could drift from it once that initial state gained its own
    # icon+title+text empty-state treatment.
    assert "const initialResultHtml = resultPanel.innerHTML;" in text
    assert "resultPanel.innerHTML = initialResultHtml;" in text


def test_get_root_page_explains_safety_stock_and_lead_time_on_the_form_itself(server):
    # Added 2026-09-12 (fifth report): a user's screenshot showed the
    # baseline form (before running any simulation) with no tooltips at
    # all - the glossary/tooltip work so far only covered the *result*
    # panel, which doesn't exist until a simulation runs. "Safety stock"
    # and "Lead time (days)" are jargon the same way "risk score" is, and
    # appear as form labels here, not just result text.
    status, body = _get(server, "/")
    text = body.decode("utf-8")

    assert status == 200
    assert '<span class="term" data-tooltip="The extra buffer of inventory' in text
    assert '<span class="term" data-tooltip="How many days it takes for a reordered shipment to arrive.">Lead time (days)</span>' in text


def test_get_root_page_renders_the_result_panel_as_a_list(server):
    # Regression (2026-09-12): the result panel used to build separate
    # <div class="stat-line"> lines - a user said it "is not in a list."
    status, body = _get(server, "/")

    assert status == 200
    assert b"<ul class=\"stat-list\">" in body
    assert b"<li class=\"stat-line\">" in body


def test_get_root_page_explains_risk_score_with_a_tooltip(server):
    # Added 2026-09-12 after a user said "risk score" "does not explain
    # anything" on its own - a plain-language title tooltip on the label.
    status, body = _get(server, "/")

    assert status == 200
    assert b'class="term" data-tooltip="A single 0-100 score' in body


def test_get_root_page_js_posts_to_the_standalone_api_path_by_default(server):
    # Regression (2026-09-11): render_page()'s api_path defaulted wrong
    # would mean this standalone server's own page can't reach its own
    # API either - a user hitting "Run Simulation" got a plain "not
    # found" in the results panel from a mismatched fetch target, which
    # no test calling the API endpoint directly (as every other test in
    # this file does) could ever catch.
    status, body = _get(server, "/")

    assert status == 200
    assert b"fetch('/api/simulate'" in body


def test_get_root_nav_bar_links_to_the_other_2_local_apps(server):
    # Regression guard for local_apps/urls.py wiring: the nav bar must
    # link to real, well-formed URLs for Data Console and AI Assistant,
    # not a broken/empty href.
    import local_apps.urls as app_urls

    status, body = _get(server, "/")
    assert status == 200
    text = body.decode("utf-8")
    assert f'href="{app_urls.DATA_CONSOLE_URL}"' in text
    assert f'href="{app_urls.LIVE_DASHBOARD_URL}"' in text
    assert f'href="{app_urls.CHAT_UI_URL}"' in text


def test_get_root_nav_bar_matches_the_other_local_apps(server):
    # Regression guard (2026-09-11, grew from 4 to 5 items 2026-09-13 with
    # the addition of Learn): every screen's nav bar now comes from the
    # same shared local_apps.theme.render_nav_bar(), so this page shows
    # the same top-level tabs Data Console and the chat UI show - not its
    # own drifted subset.
    status, body = _get(server, "/")
    assert status == 200
    text = body.decode("utf-8")
    assert text.count('class="nav-item') == 5
    assert '<span class="nav-item nav-current">' in text
    assert "What-If Simulator</span>" in text


def test_get_unknown_path_is_404(server):
    status, _ = _get(server, "/nope")

    assert status == 404


def test_post_simulate_with_no_deltas_leaves_risk_unchanged(server):
    status, data = _post(server, "/api/simulate", {"scenario_name": "no-op", "baseline": _BASELINE})

    assert status == 200
    assert data["risk_level_changed"] is False
    assert data["direction"] == "unchanged"
    assert data["baseline"]["risk_level"] == data["projected"]["risk_level"]


def test_post_simulate_with_a_demand_spike_worsens_risk(server):
    payload = {"scenario_name": "spike", "baseline": _BASELINE, "deltas": {"demand_change_pct": 3.0}}

    status, data = _post(server, "/api/simulate", payload)

    assert status == 200
    assert data["risk_level_changed"] is True
    assert data["direction"] == "worsens"
    assert data["projected"]["risk_level"] == "critical"
    assert "Recommendation:" in data["recommendation_line"]


def test_post_simulate_with_unit_price_reports_revenue_at_risk(server):
    baseline = {**_BASELINE, "lead_time_days": 25.0, "unit_price": 40.0}  # forces a projected shortage

    status, data = _post(server, "/api/simulate", {"scenario_name": "priced", "baseline": baseline})

    assert status == 200
    assert data["projected"]["revenue_at_risk"] == pytest.approx(1000.0)


def test_post_simulate_missing_required_baseline_field_is_400(server):
    incomplete = {k: v for k, v in _BASELINE.items() if k != "lead_time_days"}

    status, data = _post(server, "/api/simulate", {"scenario_name": "x", "baseline": incomplete})

    assert status == 400
    assert "lead_time_days" in data["error"]


def test_post_simulate_missing_baseline_entirely_is_400(server):
    status, data = _post(server, "/api/simulate", {"scenario_name": "x"})

    assert status == 400
    assert "baseline" in data["error"]


def test_post_simulate_invalid_json_is_400(server):
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", int(server.rsplit(":", 1)[1]))
    conn.request("POST", "/api/simulate", body=b"not json", headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()

    assert resp.status == 400
    assert "invalid request" in data["error"]


def test_post_simulate_a_demand_change_below_negative_one_is_400(server):
    payload = {"scenario_name": "impossible", "baseline": _BASELINE, "deltas": {"demand_change_pct": -1.5}}

    status, data = _post(server, "/api/simulate", payload)

    assert status == 400


def test_post_unknown_path_is_404(server):
    status, data = _post(server, "/api/nope", {})

    assert status == 404
