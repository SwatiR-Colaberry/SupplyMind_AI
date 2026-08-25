from __future__ import annotations

import json

import pytest

import agents.risk_detection_agent as agent_module
from agents.contracts import AgentQuery, validate_response
from agents.risk_detection_agent import RiskDetectionAgent


class _RecordingLogger:
    """Stand-in for agents.logging_setup's logger, so tests can inspect exactly
    what would have been logged without depending on stdout/handler wiring."""

    def __init__(self) -> None:
        self.info_calls: list[dict] = []
        self.warning_calls: list[dict] = []

    def info(self, _msg, extra=None):
        self.info_calls.append(extra)

    def warning(self, _msg, extra=None):
        self.warning_calls.append(extra)


def _demand_rows_with_spike() -> list[dict]:
    flat = [{"order_date": f"2025-{m:02d}-01", "quantity": 100} for m in range(1, 7)]
    return flat + [{"order_date": "2025-07-01", "quantity": 900}]


def _delivery_row(**overrides) -> dict:
    defaults = dict(po_id="PO-1", expected_date="2025-01-01", actual_date="2025-01-15")
    defaults.update(overrides)
    return defaults


def _inventory_row(**overrides) -> dict:
    defaults = dict(sku="SKU-1", current_stock=2.0, safety_stock=20.0, daily_demand_rate=5.0, lead_time_days=10.0)
    defaults.update(overrides)
    return defaults


def test_run_returns_error_response_when_no_data_sources_are_provided():
    agent = RiskDetectionAgent()

    response = agent.run(AgentQuery(text="detect risk", context={}))

    assert validate_response(response) is response
    assert response.status == "error"
    assert "no supply chain data" in response.error


def test_run_returns_ok_response_combining_all_three_signals():
    agent = RiskDetectionAgent()
    context = {
        "demand_history": _demand_rows_with_spike(),
        "delivery_rows": [_delivery_row()],
        "inventory_rows": [_inventory_row()],
    }

    response = agent.run(AgentQuery(text="detect risk", context=context))

    assert validate_response(response) is response
    assert response.status == "ok"
    assert response.confidence == 1.0
    assert "2025-07" in response.recommendation
    assert "PO-1" in response.recommendation
    assert "SKU-1" in response.recommendation
    assert "critical" in response.recommendation


def test_run_works_with_only_one_data_source_provided():
    agent = RiskDetectionAgent()

    response = agent.run(AgentQuery(text="detect risk", context={"delivery_rows": [_delivery_row()]}))

    assert response.status == "ok"
    assert "PO-1" in response.recommendation


def test_run_degrades_gracefully_when_demand_history_is_too_short_for_detection():
    agent = RiskDetectionAgent()
    context = {
        "demand_history": [
            {"order_date": "2025-01-01", "quantity": 100},
            {"order_date": "2025-02-01", "quantity": 105},
        ],
        "delivery_rows": [_delivery_row()],
    }

    response = agent.run(AgentQuery(text="detect risk", context=context))

    assert response.status == "ok"
    assert "demand spike detection skipped" in response.recommendation
    assert response.confidence < 1.0


def test_run_flags_malformed_delivery_and_inventory_rows_without_failing_the_whole_run():
    agent = RiskDetectionAgent()
    context = {
        "delivery_rows": [_delivery_row(po_id="PO-BAD", actual_date="not-a-date"), _delivery_row(po_id="PO-OK")],
        "inventory_rows": [_inventory_row(sku="SKU-BAD", current_stock=-1.0)],
    }

    response = agent.run(AgentQuery(text="detect risk", context=context))

    assert response.status == "ok"
    assert "PO-OK" in response.recommendation
    assert "1 delivery row(s) flagged for review" in response.recommendation
    assert "1 inventory row(s) flagged for review" in response.recommendation
    assert response.confidence < 1.0


def test_run_returns_low_severity_score_when_nothing_anomalous_is_found():
    agent = RiskDetectionAgent()
    context = {"inventory_rows": [_inventory_row(sku="SKU-HEALTHY", current_stock=1000.0, safety_stock=10.0)]}

    response = agent.run(AgentQuery(text="detect risk", context=context))

    assert response.status == "ok"
    assert "low" in response.recommendation
    assert response.confidence == 1.0


def test_run_lets_an_unexpected_error_propagate(monkeypatch):
    # The "notification system failure" / infrastructure failure path: a
    # genuinely unexpected exception is left to propagate rather than
    # swallowed - the Orchestrator already has a dedicated, tested path
    # (agent_communication_failed) for an agent raising, isolated per-agent.
    def _boom(*args, **kwargs):
        raise RuntimeError("upstream risk scoring service unavailable")

    monkeypatch.setattr(agent_module, "compute_risk_score", _boom)
    agent = RiskDetectionAgent()

    with pytest.raises(RuntimeError, match="upstream risk scoring service unavailable"):
        agent.run(AgentQuery(text="detect risk", context={"delivery_rows": [_delivery_row()]}))


def test_run_logs_every_detected_anomaly_with_severity(monkeypatch):
    recorder = _RecordingLogger()
    monkeypatch.setattr(agent_module, "logger", recorder)
    agent = RiskDetectionAgent()
    context = {"demand_history": _demand_rows_with_spike(), "delivery_rows": [_delivery_row()]}

    agent.run(AgentQuery(text="detect risk", context=context))

    demand_events = [c for c in recorder.info_calls if c["event"] == "demand_anomaly_detected"]
    delay_events = [c for c in recorder.info_calls if c["event"] == "supplier_delay_detected"]
    score_events = [c for c in recorder.info_calls if c["event"] == "supply_chain_risk_score_computed"]

    assert len(demand_events) == 1
    assert demand_events[0]["context"]["period"] == "2025-07"
    assert demand_events[0]["context"]["severity"] == "critical"
    assert len(delay_events) == 1
    assert delay_events[0]["context"]["po_id"] == "PO-1"
    assert len(score_events) == 1
    assert "score" in score_events[0]["context"]
    assert "severity" in score_events[0]["context"]

    # JSON-safety regression guard, same as StockoutRiskAgent's: an infinite
    # z-score (a deviation from a perfectly flat baseline) must not leak
    # json.dumps' non-standard `Infinity` token into a log line.
    json.dumps(demand_events[0]["context"], allow_nan=False)


def test_run_logs_a_warning_and_returns_error_when_no_data_is_provided(monkeypatch):
    recorder = _RecordingLogger()
    monkeypatch.setattr(agent_module, "logger", recorder)
    agent = RiskDetectionAgent()

    response = agent.run(AgentQuery(text="detect risk", context={}))

    assert response.status == "error"
    failures = [c for c in recorder.warning_calls if c["event"] == "risk_detection_failed"]
    assert len(failures) == 1
    assert failures[0]["outcome"] == "failure"
