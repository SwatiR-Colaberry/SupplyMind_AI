from datetime import date

import pytest

import agents.demand_forecasting_agent as agent_module
from agents.contracts import AgentQuery, validate_response
from agents.demand_forecasting_agent import DemandForecastingAgent


def _demand_rows(months: int = 12) -> list[dict]:
    return [
        {"order_date": date(2025, m, 15), "quantity": 50 + m}
        for m in range(1, months + 1)
    ]


def test_run_returns_a_valid_ok_response_for_sufficient_history():
    agent = DemandForecastingAgent()

    response = agent.run(AgentQuery(text="forecast widget demand", context={"demand_history": _demand_rows()}))

    assert validate_response(response) is response
    assert response.status == "ok"
    assert response.agent_name == "demand_forecasting_agent"
    assert 0.0 <= response.confidence <= 1.0
    assert "2026-01" in response.recommendation


def test_run_uses_default_periods_ahead_of_three():
    agent = DemandForecastingAgent()

    response = agent.run(AgentQuery(text="forecast", context={"demand_history": _demand_rows()}))

    points_text = response.recommendation.split("): ", 1)[1].split(" | ")[0]
    assert len(points_text.split("; ")) == 3


def test_run_returns_error_response_when_demand_history_missing():
    agent = DemandForecastingAgent()

    response = agent.run(AgentQuery(text="forecast", context={}))

    assert validate_response(response) is response
    assert response.status == "error"
    assert "no historical demand data" in response.error


def test_run_returns_error_response_when_demand_history_is_empty_list():
    agent = DemandForecastingAgent()

    response = agent.run(AgentQuery(text="forecast", context={"demand_history": []}))

    assert response.status == "error"
    assert "no historical demand data" in response.error


def test_run_returns_error_response_on_aggregation_failure():
    agent = DemandForecastingAgent()
    rows = [{"order_date": date(2025, 1, 1), "quantity": "not-a-number"}]

    response = agent.run(AgentQuery(text="forecast", context={"demand_history": rows}))

    assert validate_response(response) is response
    assert response.status == "error"
    assert "data quality issue" in response.error


def test_run_returns_error_response_on_insufficient_history_to_train_a_trend():
    agent = DemandForecastingAgent()
    rows = [{"order_date": date(2025, 1, 1), "quantity": 10}]

    response = agent.run(AgentQuery(text="forecast", context={"demand_history": rows}))

    assert response.status == "error"
    assert "at least 2 historical points" in response.error


def test_run_returns_error_response_on_bad_periods_ahead_parameter():
    agent = DemandForecastingAgent()

    response = agent.run(
        AgentQuery(text="forecast", context={"demand_history": _demand_rows(), "periods_ahead": 0})
    )

    assert response.status == "error"
    assert "periods_ahead must be positive" in response.error


def test_run_honors_custom_date_and_quantity_field_names():
    agent = DemandForecastingAgent()
    rows = [{"txn_date": date(2025, m, 15), "units": 50 + m} for m in range(1, 13)]

    response = agent.run(
        AgentQuery(
            text="forecast",
            context={"demand_history": rows, "date_field": "txn_date", "quantity_field": "units"},
        )
    )

    assert response.status == "ok"


def test_run_recommendation_includes_data_quality_warnings_for_sparse_history():
    agent = DemandForecastingAgent()
    rows = _demand_rows(months=2)

    response = agent.run(AgentQuery(text="forecast", context={"demand_history": rows}))

    assert response.status == "ok"
    assert "Data quality notes" in response.recommendation


def test_run_flags_model_drift_when_previous_forecast_diverges_from_actuals():
    agent = DemandForecastingAgent()
    rows = _demand_rows(months=6)  # actual demand for 2025-01..2025-06
    # A previous forecast for those same months that badly undershot reality.
    previous_forecast_points = [
        {"period": f"2025-{m:02d}", "forecast_quantity": 10.0} for m in range(1, 7)
    ]

    response = agent.run(
        AgentQuery(
            text="forecast",
            context={"demand_history": rows, "previous_forecast_points": previous_forecast_points},
        )
    )

    assert response.status == "ok"
    assert "Model drift warning" in response.recommendation


def test_run_does_not_flag_drift_when_previous_forecast_was_close_to_actuals():
    agent = DemandForecastingAgent()
    rows = _demand_rows(months=6)
    previous_forecast_points = [
        {"period": f"2025-{m:02d}", "forecast_quantity": 50.0 + m} for m in range(1, 7)
    ]

    response = agent.run(
        AgentQuery(
            text="forecast",
            context={"demand_history": rows, "previous_forecast_points": previous_forecast_points},
        )
    )

    assert response.status == "ok"
    assert "Model drift warning" not in response.recommendation


def test_run_skips_drift_check_when_no_previous_forecast_supplied():
    agent = DemandForecastingAgent()

    response = agent.run(AgentQuery(text="forecast", context={"demand_history": _demand_rows()}))

    assert response.status == "ok"
    assert "Model drift warning" not in response.recommendation


def test_run_returns_error_response_for_malformed_previous_forecast_points():
    # Regression: previously a missing "forecast_quantity" key raised an
    # uncaught KeyError instead of a handled error response - since there
    # is no persistence layer, a caller replaying stale/partial state here
    # is a real, expected scenario, not a hypothetical one.
    agent = DemandForecastingAgent()

    response = agent.run(
        AgentQuery(
            text="forecast",
            context={
                "demand_history": _demand_rows(),
                "previous_forecast_points": [{"period": "2025-01"}],  # missing forecast_quantity
            },
        )
    )

    assert validate_response(response) is response
    assert response.status == "error"
    assert "invalid previous_forecast_points entry" in response.error


def test_run_lets_an_unexpected_forecasting_error_propagate(monkeypatch):
    # The "forecasting API failure" path: a genuinely unexpected exception
    # (not one of the typed error paths this agent handles) is left to
    # propagate rather than swallowed - the Orchestrator already has a
    # dedicated, tested path (agent_communication_failed) for an agent
    # raising, and misclassifying this as a data/parameter error would
    # hide it from that path.
    def _boom(*args, **kwargs):
        raise RuntimeError("upstream forecasting service unavailable")

    monkeypatch.setattr(agent_module, "forecast_demand", _boom)
    agent = DemandForecastingAgent()

    with pytest.raises(RuntimeError, match="upstream forecasting service unavailable"):
        agent.run(AgentQuery(text="forecast", context={"demand_history": _demand_rows()}))
