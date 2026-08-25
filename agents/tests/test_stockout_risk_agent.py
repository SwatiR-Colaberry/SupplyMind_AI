from __future__ import annotations

import json
from decimal import Decimal

import pytest

import agents.stockout_risk_agent as agent_module
from agents.contracts import AgentQuery, validate_response
from agents.stockout_risk_agent import StockoutRiskAgent
from inventory_risk.risk_model import InventoryPosition, RiskModelError, assess_stockout_risk


def _row(**overrides) -> dict:
    defaults = dict(sku="SKU-1", current_stock=100.0, safety_stock=20.0, daily_demand_rate=5.0, lead_time_days=10.0)
    defaults.update(overrides)
    return defaults


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


def test_run_returns_a_valid_ok_response_for_clean_inventory_rows():
    agent = StockoutRiskAgent()
    rows = [_row(sku="SKU-1"), _row(sku="SKU-2", current_stock=5.0, safety_stock=20.0)]

    response = agent.run(AgentQuery(text="assess stockout risk", context={"inventory_rows": rows}))

    assert validate_response(response) is response
    assert response.status == "ok"
    assert response.agent_name == "stockout_risk_agent"
    assert 0.0 <= response.confidence <= 1.0
    assert "SKU-1" in response.recommendation
    assert "SKU-2" in response.recommendation


def test_run_confidence_is_the_mean_of_per_sku_confidences():
    agent = StockoutRiskAgent()
    rows = [_row(sku="SKU-1"), _row(sku="SKU-2", current_stock=5.0, safety_stock=20.0)]
    expected = [
        assess_stockout_risk(InventoryPosition(sku=r["sku"], current_stock=r["current_stock"], safety_stock=r["safety_stock"], daily_demand_rate=r["daily_demand_rate"], lead_time_days=r["lead_time_days"]))
        for r in rows
    ]
    expected_mean = sum(a.confidence for a in expected) / len(expected)

    response = agent.run(AgentQuery(text="assess", context={"inventory_rows": rows}))

    assert response.confidence == pytest.approx(expected_mean)


def test_run_orders_recommendation_with_most_severe_risk_first():
    agent = StockoutRiskAgent()
    rows = [
        _row(sku="SKU-LOW", current_stock=100.0, safety_stock=5.0, daily_demand_rate=1.0, lead_time_days=5.0),
        _row(sku="SKU-CRITICAL", current_stock=2.0, safety_stock=20.0),
    ]

    response = agent.run(AgentQuery(text="assess", context={"inventory_rows": rows}))

    assert response.status == "ok"
    assert response.recommendation.index("SKU-CRITICAL") < response.recommendation.index("SKU-LOW")


def test_run_returns_error_response_when_inventory_rows_missing():
    agent = StockoutRiskAgent()

    response = agent.run(AgentQuery(text="assess", context={}))

    assert validate_response(response) is response
    assert response.status == "error"
    assert "no inventory data provided" in response.error


def test_run_returns_error_response_when_inventory_rows_is_empty_list():
    agent = StockoutRiskAgent()

    response = agent.run(AgentQuery(text="assess", context={"inventory_rows": []}))

    assert response.status == "error"
    assert "no inventory data provided" in response.error


def test_run_flags_a_nan_row_for_review_instead_of_silently_predicting_low_risk():
    # Regression: a NaN current_stock previously passed both validation layers
    # undetected and came out the other end as a confidently wrong "low risk,
    # confidence 1.0" prediction - the worst possible outcome for a stockout
    # system. It must now be excluded and flagged for review instead.
    agent = StockoutRiskAgent()
    rows = [_row(sku="SKU-NAN", current_stock=float("nan"))]

    response = agent.run(AgentQuery(text="assess", context={"inventory_rows": rows}))

    assert response.status == "error"
    assert "SKU-NAN" in response.error
    assert "NaN" in response.error


def test_run_returns_error_response_when_every_row_is_corrupted():
    agent = StockoutRiskAgent()
    rows = [_row(sku="SKU-1", current_stock=-1.0), _row(sku="SKU-2", lead_time_days=0.0)]

    response = agent.run(AgentQuery(text="assess", context={"inventory_rows": rows}))

    assert validate_response(response) is response
    assert response.status == "error"
    assert "flagged for review" in response.error
    # The specific per-row reason must survive into the error, not just an aggregate count.
    assert "SKU-1" in response.error and "current_stock is negative" in response.error
    assert "SKU-2" in response.error and "lead_time_days must be positive" in response.error


def test_run_recommendation_includes_specific_reasons_for_partially_corrupted_rows():
    agent = StockoutRiskAgent()
    rows = [_row(sku="SKU-GOOD"), _row(sku="SKU-BAD", current_stock=-1.0)]

    response = agent.run(AgentQuery(text="assess", context={"inventory_rows": rows}))

    assert response.status == "ok"
    assert "Data quality notes" in response.recommendation
    assessed_section, quality_section = response.recommendation.split(" | Data quality notes: ", 1)
    # SKU-BAD must not appear in the risk-assessment portion - it was excluded, not silently assessed.
    assert "SKU-GOOD" in assessed_section
    assert "SKU-BAD" not in assessed_section
    # But its specific reason must be surfaced in the review notes, not just an aggregate count.
    assert "SKU-BAD" in quality_section
    assert "current_stock is negative" in quality_section


def test_run_isolates_a_per_sku_prediction_error_without_discarding_other_skus(monkeypatch):
    real_assess = agent_module.assess_stockout_risk

    def _flaky_assess(position):
        if position.sku == "SKU-BAD":
            raise RiskModelError("simulated model prediction error")
        return real_assess(position)

    monkeypatch.setattr(agent_module, "assess_stockout_risk", _flaky_assess)
    agent = StockoutRiskAgent()
    rows = [_row(sku="SKU-BAD"), _row(sku="SKU-GOOD")]

    response = agent.run(AgentQuery(text="assess", context={"inventory_rows": rows}))

    assert response.status == "ok"
    assert "SKU-GOOD" in response.recommendation
    assert "Flagged for review (prediction error)" in response.recommendation
    assert "SKU-BAD" in response.recommendation


def test_run_returns_error_response_when_every_clean_row_fails_prediction(monkeypatch):
    def _always_fails(position):
        raise RiskModelError("simulated model prediction error")

    monkeypatch.setattr(agent_module, "assess_stockout_risk", _always_fails)
    agent = StockoutRiskAgent()
    rows = [_row(sku="SKU-1")]

    response = agent.run(AgentQuery(text="assess", context={"inventory_rows": rows}))

    assert validate_response(response) is response
    assert response.status == "error"
    assert "SKU-1" in response.error


def test_run_lets_an_unexpected_prediction_error_propagate(monkeypatch):
    # The "prediction API failure" path: a genuinely unexpected exception is
    # left to propagate rather than swallowed - the Orchestrator already has
    # a dedicated, tested path (agent_communication_failed) for an agent
    # raising, isolated per-agent so it can't take down a sibling agent.
    def _boom(*args, **kwargs):
        raise RuntimeError("upstream prediction service unavailable")

    monkeypatch.setattr(agent_module, "assess_stockout_risk", _boom)
    agent = StockoutRiskAgent()

    with pytest.raises(RuntimeError, match="upstream prediction service unavailable"):
        agent.run(AgentQuery(text="assess", context={"inventory_rows": [_row()]}))


def test_run_normalizes_infinite_days_of_supply_for_json_safe_logging(monkeypatch):
    # Regression guard: days_of_supply is math.inf for a zero-demand SKU.
    # json.dumps(..., allow_nan=True) (the default) would silently emit the
    # non-standard `Infinity` token instead of raising, so this asserts the
    # normalized value directly and independently confirms strict-JSON safety.
    recorder = _RecordingLogger()
    monkeypatch.setattr(agent_module, "logger", recorder)
    agent = StockoutRiskAgent()
    rows = [_row(sku="SKU-1", daily_demand_rate=0.0)]

    agent.run(AgentQuery(text="assess", context={"inventory_rows": rows}))

    predicted = [c for c in recorder.info_calls if c["event"] == "stockout_risk_predicted"]
    assert len(predicted) == 1
    assert predicted[0]["context"]["days_of_supply"] is None
    json.dumps(predicted[0]["context"], allow_nan=False)


def test_run_assesses_rows_with_decimal_values_from_a_real_database(monkeypatch):
    # Regression: a real PostgreSQL NUMERIC column comes back from psycopg2
    # as decimal.Decimal, not float. Before the inventory_risk/data_quality.py
    # fix, every row like this was incorrectly flagged "not numeric" and
    # excluded, so the agent returned an error ("no SKU could be assessed")
    # against perfectly valid real-world data - this reproduces that
    # end-to-end, not just at the data_quality layer.
    agent = StockoutRiskAgent()
    rows = [
        _row(
            sku="SKU-DECIMAL",
            current_stock=Decimal("3.0"),
            safety_stock=Decimal("30.0"),
            daily_demand_rate=Decimal("6.0"),
            lead_time_days=Decimal("12.0"),
        )
    ]

    response = agent.run(AgentQuery(text="assess", context={"inventory_rows": rows}))

    assert validate_response(response) is response
    assert response.status == "ok"
    assert "SKU-DECIMAL" in response.recommendation
    assert "critical" in response.recommendation


def test_run_logs_a_prediction_for_every_successfully_assessed_sku(monkeypatch):
    recorder = _RecordingLogger()
    monkeypatch.setattr(agent_module, "logger", recorder)
    agent = StockoutRiskAgent()
    rows = [_row(sku="SKU-1"), _row(sku="SKU-2", current_stock=5.0, safety_stock=20.0)]

    agent.run(AgentQuery(text="assess", context={"inventory_rows": rows}))

    predicted = [c for c in recorder.info_calls if c["event"] == "stockout_risk_predicted"]
    assert {c["context"]["sku"] for c in predicted} == {"SKU-1", "SKU-2"}
    for call in predicted:
        assert "confidence" in call["context"]
        assert 0.0 <= call["context"]["confidence"] <= 1.0
