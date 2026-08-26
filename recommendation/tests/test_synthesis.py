from __future__ import annotations

import pytest

from agents.contracts import AgentFinding, AgentResponse
from recommendation.synthesis import RecommendationError, synthesize_recommendations


def _ok(agent_name: str, recommendation: str, confidence: float, findings: list[AgentFinding] | None = None):
    return AgentResponse(
        agent_name=agent_name,
        status="ok",
        recommendation=recommendation,
        confidence=confidence,
        findings=findings or [],
    )


def _error(agent_name: str, error: str):
    return AgentResponse(agent_name=agent_name, status="error", error=error)


def test_synthesize_raises_when_no_agent_outputs_provided():
    with pytest.raises(RecommendationError, match="no agent outputs provided"):
        synthesize_recommendations([])


def test_synthesize_raises_when_every_agent_output_failed():
    outputs = [_error("demand_forecasting_agent", "no historical demand data provided")]

    with pytest.raises(RecommendationError, match="no successful agent output"):
        synthesize_recommendations(outputs)


def test_synthesize_returns_one_recommendation_per_successful_agent():
    outputs = [
        _ok("demand_forecasting_agent", "Demand forecast: steady", 0.8),
        _ok("stockout_risk_agent", "Stockout risk: SKU-1 low", 0.75),
    ]

    result = synthesize_recommendations(outputs)

    assert [r.agent_name for r in result.recommendations] == ["demand_forecasting_agent", "stockout_risk_agent"]
    assert result.excluded_agents == []
    assert not result.has_conflicts


def test_synthesize_excludes_failed_agents_without_failing_the_whole_run():
    outputs = [
        _ok("stockout_risk_agent", "Stockout risk: SKU-1 low", 0.9),
        _error("demand_forecasting_agent", "no historical demand data provided"),
    ]

    result = synthesize_recommendations(outputs)

    assert [r.agent_name for r in result.recommendations] == ["stockout_risk_agent"]
    assert result.excluded_agents == ["demand_forecasting_agent"]


def test_synthesize_detects_a_precise_subject_conflict_between_two_agents():
    outputs = [
        _ok(
            "stockout_risk_agent",
            "Stockout risk: SKU-1 low",
            0.9,
            findings=[AgentFinding(subject="SKU-1", subject_kind="sku", severity="low", detail="ample stock")],
        ),
        _ok(
            "risk_detection_agent",
            "Supply chain risk score 70/100 (high): stockout risk for SKU-1",
            0.85,
            findings=[
                AgentFinding(subject="SKU-1", subject_kind="sku", severity="critical", detail="2.1 days of supply")
            ],
        ),
    ]

    result = synthesize_recommendations(outputs)

    assert len(result.subject_conflicts) == 1
    conflict = result.subject_conflicts[0]
    assert conflict.subject == "SKU-1"
    assert conflict.subject_kind == "sku"
    # Most severe entry first.
    assert conflict.entries[0] == ("risk_detection_agent", "critical", "2.1 days of supply")
    assert conflict.entries[1] == ("stockout_risk_agent", "low", "ample stock")
    assert "SKU-1" in conflict.description
    assert result.has_conflicts


def test_synthesize_does_not_flag_a_subject_reported_by_only_one_agent():
    outputs = [
        _ok(
            "stockout_risk_agent",
            "Stockout risk: SKU-1 low",
            0.9,
            findings=[AgentFinding(subject="SKU-1", subject_kind="sku", severity="low", detail="ample stock")],
        ),
    ]

    result = synthesize_recommendations(outputs)

    assert result.subject_conflicts == []


def test_synthesize_does_not_flag_a_subject_where_agents_agree_on_severity():
    finding = AgentFinding(subject="SKU-1", subject_kind="sku", severity="critical", detail="2.1 days of supply")
    outputs = [
        _ok("stockout_risk_agent", "Stockout risk: SKU-1 critical", 0.9, findings=[finding]),
        _ok("risk_detection_agent", "Risk score high", 0.85, findings=[finding]),
    ]

    result = synthesize_recommendations(outputs)

    assert result.subject_conflicts == []


def test_synthesize_detects_a_confidence_conflict_with_no_shared_subjects():
    outputs = [
        _ok("demand_forecasting_agent", "Demand forecast: steady", 0.95),
        _ok("stockout_risk_agent", "Stockout risk: SKU-1 low", 0.30),
    ]

    result = synthesize_recommendations(outputs)

    assert result.subject_conflicts == []
    assert result.confidence_conflict is not None
    assert result.confidence_conflict.spread == pytest.approx(0.65)
    assert set(result.confidence_conflict.agent_names) == {"demand_forecasting_agent", "stockout_risk_agent"}
    assert result.has_conflicts


def test_synthesize_does_not_flag_a_small_confidence_spread():
    outputs = [
        _ok("demand_forecasting_agent", "Demand forecast: steady", 0.85),
        _ok("stockout_risk_agent", "Stockout risk: SKU-1 low", 0.80),
    ]

    result = synthesize_recommendations(outputs)

    assert result.confidence_conflict is None
    assert not result.has_conflicts


def test_synthesize_confidence_conflict_ignores_agents_with_no_confidence():
    outputs = [
        _ok("demand_forecasting_agent", "Demand forecast: steady", None),
        _ok("stockout_risk_agent", "Stockout risk: SKU-1 low", 0.30),
    ]

    result = synthesize_recommendations(outputs)

    assert result.confidence_conflict is None
