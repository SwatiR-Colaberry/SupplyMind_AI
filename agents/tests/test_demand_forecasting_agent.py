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


def _per_sku_demand_rows(months: int = 6) -> list[dict]:
    rows = []
    for m in range(1, months + 1):
        rows.append({"sku": "SKU-1", "order_date": date(2025, m, 5), "quantity": 100 + m})
        rows.append({"sku": "SKU-2", "order_date": date(2025, m, 10), "quantity": 10})
    return rows


def test_run_findings_stays_empty_when_no_row_carries_a_sku_field():
    # Backward-compat: a caller/dataset that never maps "sku" onto
    # customer_orders gets exactly today's aggregate-only behavior, not a
    # degraded or error response.
    agent = DemandForecastingAgent()

    response = agent.run(AgentQuery(text="forecast", context={"demand_history": _demand_rows()}))

    assert response.status == "ok"
    assert response.findings == []


def test_run_adds_one_finding_per_forecastable_sku():
    agent = DemandForecastingAgent()

    response = agent.run(AgentQuery(text="forecast", context={"demand_history": _per_sku_demand_rows()}))

    assert response.status == "ok"
    subjects = {f.subject for f in response.findings}
    assert subjects == {"SKU-1", "SKU-2"}
    assert all(f.subject_kind == "sku" for f in response.findings)


def test_run_skips_a_sku_with_too_little_history_without_failing_the_others():
    rows = _per_sku_demand_rows(months=6)
    rows.append({"sku": "SKU-3", "order_date": date(2025, 1, 1), "quantity": 5})  # only 1 month - unforecastable
    agent = DemandForecastingAgent()

    response = agent.run(AgentQuery(text="forecast", context={"demand_history": rows}))

    assert response.status == "ok"
    subjects = {f.subject for f in response.findings}
    assert subjects == {"SKU-1", "SKU-2"}


def test_run_per_sku_finding_metric_value_is_the_next_period_forecast():
    agent = DemandForecastingAgent()

    response = agent.run(AgentQuery(text="forecast", context={"demand_history": _per_sku_demand_rows()}))

    sku1 = next(f for f in response.findings if f.subject == "SKU-1")
    assert sku1.metric_value is not None
    assert sku1.metric_value > 0


def test_run_flags_a_sku_with_a_recent_demand_spike_as_high_severity():
    rows = _per_sku_demand_rows(months=6)
    rows.append({"sku": "SKU-2", "order_date": date(2025, 6, 20), "quantity": 500})  # spike in the latest month
    agent = DemandForecastingAgent()

    response = agent.run(AgentQuery(text="forecast", context={"demand_history": rows}))

    sku2 = next(f for f in response.findings if f.subject == "SKU-2")
    assert sku2.severity in ("high", "critical")
    assert "anomaly" in sku2.detail


def test_run_honors_a_custom_sku_field_name():
    rows = [{"product_id": "SKU-1", "order_date": date(2025, m, 5), "quantity": 100} for m in range(1, 7)]
    agent = DemandForecastingAgent()

    response = agent.run(
        AgentQuery(text="forecast", context={"demand_history": rows, "sku_field": "product_id"})
    )

    assert response.status == "ok"
    assert {f.subject for f in response.findings} == {"SKU-1"}


def _per_category_demand_rows(months: int = 6) -> list[dict]:
    rows = []
    for m in range(1, months + 1):
        rows.append({"category": "Electronics", "order_date": date(2025, m, 5), "quantity": 100 + m})
        rows.append({"category": "Apparel", "order_date": date(2025, m, 10), "quantity": 10})
    return rows


def _per_region_demand_rows(months: int = 6) -> list[dict]:
    rows = []
    for m in range(1, months + 1):
        rows.append({"region": "West", "order_date": date(2025, m, 5), "quantity": 100 + m})
        rows.append({"region": "East", "order_date": date(2025, m, 10), "quantity": 10})
    return rows


def test_run_adds_one_finding_per_forecastable_category():
    agent = DemandForecastingAgent()

    response = agent.run(AgentQuery(text="forecast", context={"demand_history": _per_category_demand_rows()}))

    assert response.status == "ok"
    category_findings = [f for f in response.findings if f.subject_kind == "category"]
    assert {f.subject for f in category_findings} == {"Electronics", "Apparel"}


def test_run_adds_one_finding_per_forecastable_region():
    agent = DemandForecastingAgent()

    response = agent.run(AgentQuery(text="forecast", context={"demand_history": _per_region_demand_rows()}))

    assert response.status == "ok"
    region_findings = [f for f in response.findings if f.subject_kind == "region"]
    assert {f.subject for f in region_findings} == {"West", "East"}


def test_run_honors_custom_category_and_region_field_names():
    rows = [
        {"product_category": "Electronics", "sales_region": "West", "order_date": date(2025, m, 5), "quantity": 100}
        for m in range(1, 7)
    ]
    agent = DemandForecastingAgent()

    response = agent.run(
        AgentQuery(
            text="forecast",
            context={"demand_history": rows, "category_field": "product_category", "region_field": "sales_region"},
        )
    )

    assert response.status == "ok"
    assert {f.subject for f in response.findings if f.subject_kind == "category"} == {"Electronics"}
    assert {f.subject for f in response.findings if f.subject_kind == "region"} == {"West"}


def test_run_combines_sku_category_and_region_findings_without_interfering():
    rows = [
        {"sku": "SKU-1", "category": "Electronics", "region": "West", "order_date": date(2025, m, 5), "quantity": 100}
        for m in range(1, 7)
    ]
    agent = DemandForecastingAgent()

    response = agent.run(AgentQuery(text="forecast", context={"demand_history": rows}))

    assert response.status == "ok"
    kinds = {f.subject_kind for f in response.findings}
    assert kinds == {"sku", "category", "region"}
    assert {f.subject for f in response.findings} == {"SKU-1", "Electronics", "West"}


def test_run_findings_stays_empty_when_no_row_carries_category_or_region_fields():
    agent = DemandForecastingAgent()

    response = agent.run(AgentQuery(text="forecast", context={"demand_history": _demand_rows()}))

    assert response.status == "ok"
    assert not any(f.subject_kind in ("category", "region") for f in response.findings)


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
