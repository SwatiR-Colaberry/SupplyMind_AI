from __future__ import annotations

import pytest

from agents.contracts import AgentFinding
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
    # No per-subject findings on this metric -> nothing to chart.
    assert response.chart_specs == []


def test_answer_query_attaches_a_chart_for_a_topic_with_per_subject_findings() -> None:
    findings = [
        AgentFinding(subject="SKU-1", subject_kind="sku", severity="critical", detail="below safety stock"),
        AgentFinding(subject="SKU-2", subject_kind="sku", severity="high", detail="below safety stock"),
    ]
    snapshot = _snapshot(
        [
            DashboardMetric(
                metric_id="stockout_risk_agent",
                label="Stockout Risk",
                status="ok",
                headline="2 SKUs at risk",
                source_agent="stockout_risk_agent",
                confidence=0.9,
                findings=findings,
            )
        ]
    )

    response = answer_query("what's our stockout risk?", snapshot)

    assert len(response.chart_specs) == 1
    chart = response.chart_specs[0]
    assert chart.chart_id == "stockout_risk_agent-sku"
    assert [bar.label for bar in chart.bars] == ["SKU-1", "SKU-2"]


def test_answer_query_attaches_no_chart_for_unsupported_or_data_unavailable() -> None:
    findings = [AgentFinding(subject="SKU-1", subject_kind="sku", severity="critical", detail="x")]
    snapshot = _snapshot(
        [
            DashboardMetric(
                metric_id="stockout_risk_agent", label="Stockout Risk", status="error",
                headline="no data", source_agent="stockout_risk_agent", findings=findings,
            )
        ]
    )

    unsupported = answer_query("what's the weather?", snapshot)
    data_unavailable = answer_query("what's our stockout risk?", snapshot)

    assert unsupported.chart_specs == []
    assert data_unavailable.chart_specs == []


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


def test_answer_query_answers_about_a_named_subject_across_every_metric() -> None:
    # "why is SKU-1 at risk?" should pull SKU-1's finding from every metric
    # that has one - here, both the stockout-risk assessment and a
    # root-cause causal-chain step - not just whichever topic's keyword
    # ("risk") happens to match first.
    snapshot = _snapshot(
        [
            DashboardMetric(
                metric_id="stockout_risk_agent",
                label="Stockout Risk",
                status="ok",
                headline="1 SKU at critical risk",
                source_agent="stockout_risk_agent",
                confidence=0.9,
                findings=[
                    AgentFinding(subject="SKU-1", subject_kind="sku", severity="critical", detail="2 days of supply left")
                ],
            ),
            DashboardMetric(
                metric_id="risk_detection_agent",
                label="Supply Chain Risk",
                status="ok",
                headline="root cause found",
                source_agent="risk_detection_agent",
                confidence=0.8,
                findings=[
                    AgentFinding(subject="SKU-1", subject_kind="sku", severity="high", detail="Demand up (2026-08) -> Stockout risk up (SKU-1)")
                ],
            ),
        ]
    )

    response = answer_query("why is SKU-1 at risk?", snapshot)

    assert response.status == "answered"
    assert response.subject == "SKU-1"
    assert response.topic is None
    assert "Stockout Risk: 2 days of supply left" in response.answer
    assert "Supply Chain Risk: Demand up (2026-08) -> Stockout risk up (SKU-1)" in response.answer


def test_answer_query_named_subject_takes_priority_over_a_topic_keyword_match() -> None:
    # The query also contains "risk", which alone would route to
    # risk_detection_agent - the more specific named-subject answer must
    # win instead.
    snapshot = _snapshot(
        [
            DashboardMetric(
                metric_id="stockout_risk_agent",
                label="Stockout Risk",
                status="ok",
                headline="1 SKU at risk",
                source_agent="stockout_risk_agent",
                findings=[AgentFinding(subject="SKU-1", subject_kind="sku", severity="critical", detail="critical detail")],
            )
        ]
    )

    response = answer_query("what's the risk on SKU-1?", snapshot)

    assert response.subject == "SKU-1"
    assert response.topic is None


def test_answer_query_falls_back_to_topic_routing_when_no_subject_is_named() -> None:
    snapshot = _snapshot(
        [
            DashboardMetric(
                metric_id="stockout_risk_agent",
                label="Stockout Risk",
                status="ok",
                headline="1 SKU at risk",
                source_agent="stockout_risk_agent",
                findings=[AgentFinding(subject="SKU-1", subject_kind="sku", severity="critical", detail="critical detail")],
            )
        ]
    )

    response = answer_query("what's our stockout risk?", snapshot)

    assert response.subject is None
    assert response.topic == "stockout_risk_agent"
