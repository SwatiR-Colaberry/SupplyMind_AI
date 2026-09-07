from __future__ import annotations

import pytest

from chat_interface.chat import ChatQueryError, answer_query
from dashboard.metrics import DashboardMetric, DashboardSnapshot


def _snapshot(metrics: list[DashboardMetric]) -> DashboardSnapshot:
    return DashboardSnapshot(dashboard_id="snap-1", generated_at="2026-09-07T00:00:00+00:00", metrics=metrics)


def test_answer_query_returns_ok_tile_headline_and_confidence() -> None:
    snapshot = _snapshot(
        [
            DashboardMetric(
                metric_id="stockout_risk_agent",
                label="Stockout Risk",
                status="ok",
                headline="2 SKUs at critical stockout risk",
                source_agent="stockout_risk_agent",
                confidence=0.9,
            )
        ]
    )

    response = answer_query("what's our stockout risk?", snapshot)

    assert response.status == "answered"
    assert response.topic == "stockout_risk_agent"
    assert response.source_agent == "stockout_risk_agent"
    assert response.confidence == 0.9
    assert "Stockout Risk" in response.answer
    assert "2 SKUs at critical stockout risk" in response.answer


def test_answer_query_reports_data_unavailable_for_errored_tile() -> None:
    snapshot = _snapshot(
        [
            DashboardMetric(
                metric_id="supplier_evaluation_agent",
                label="Supplier Risk",
                status="error",
                headline="no supply chain data provided",
                source_agent="supplier_evaluation_agent",
            )
        ]
    )

    response = answer_query("how are our suppliers doing?", snapshot)

    assert response.status == "data_unavailable"
    assert response.topic == "supplier_evaluation_agent"
    assert "could not be processed" in response.answer
    assert "no supply chain data provided" in response.answer


def test_answer_query_reports_data_unavailable_when_topic_has_no_tile() -> None:
    # Topic is recognized, but this snapshot's fleet never included that
    # agent - a legitimate question the current data just can't answer,
    # distinct from an unsupported *topic* entirely.
    snapshot = _snapshot([])

    response = answer_query("what's the demand forecast?", snapshot)

    assert response.status == "data_unavailable"
    assert response.topic == "demand_forecasting_agent"
    assert "don't have any" in response.answer


def test_answer_query_returns_unsupported_for_unrecognized_query() -> None:
    snapshot = _snapshot([])

    response = answer_query("what's the weather like today?", snapshot)

    assert response.status == "unsupported"
    assert response.topic is None
    assert response.answer.startswith("I don't have an answer for that yet.")
    # The unsupported message must actually name what IS supported.
    assert "supplier" in response.answer


def test_answer_query_returns_unsupported_for_blank_query() -> None:
    snapshot = _snapshot([])

    response = answer_query("   ", snapshot)

    assert response.status == "unsupported"


def test_answer_query_raises_for_non_snapshot_input() -> None:
    with pytest.raises(ChatQueryError):
        answer_query("what's our stockout risk?", {"not": "a snapshot"})
