from __future__ import annotations

import agents.recommendation_agent as agent_module
from agents.contracts import AgentFinding, AgentQuery, AgentResponse, validate_response
from agents.recommendation_agent import RecommendationAgent


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


def test_run_returns_error_when_agent_outputs_missing():
    agent = RecommendationAgent()

    response = agent.run(AgentQuery(text="recommend", context={}))

    assert validate_response(response) is response
    assert response.status == "error"
    assert "no agent outputs provided" in response.error


def test_run_returns_error_when_agent_outputs_is_empty_list():
    agent = RecommendationAgent()

    response = agent.run(AgentQuery(text="recommend", context={"agent_outputs": []}))

    assert response.status == "error"
    assert "no agent outputs provided" in response.error


def test_run_returns_error_when_every_agent_output_failed():
    agent = RecommendationAgent()
    outputs = [_error("demand_forecasting_agent", "no historical demand data provided")]

    response = agent.run(AgentQuery(text="recommend", context={"agent_outputs": outputs}))

    assert response.status == "error"
    assert "no successful agent output" in response.error


def test_run_returns_error_when_agent_outputs_contains_a_malformed_entry():
    agent = RecommendationAgent()
    outputs = [{"agent_name": "not_a_real_agent_response"}]

    response = agent.run(AgentQuery(text="recommend", context={"agent_outputs": outputs}))

    assert validate_response(response) is response
    assert response.status == "error"
    assert "AgentResponse" in response.error


def test_run_returns_ok_combining_two_successful_agents():
    agent = RecommendationAgent()
    outputs = [
        _ok("demand_forecasting_agent", "Demand forecast: steady", 0.8),
        _ok("stockout_risk_agent", "Stockout risk: SKU-1 low", 0.75),
    ]

    response = agent.run(AgentQuery(text="recommend", context={"agent_outputs": outputs}))

    assert validate_response(response) is response
    assert response.status == "ok"
    assert "demand_forecasting_agent" in response.recommendation
    assert "stockout_risk_agent" in response.recommendation
    assert "Demand forecast: steady" in response.recommendation
    assert response.confidence == 0.775  # mean of 0.8 and 0.75, no conflicts to penalize


def test_run_notes_excluded_agents_in_the_recommendation():
    agent = RecommendationAgent()
    outputs = [
        _ok("stockout_risk_agent", "Stockout risk: SKU-1 low", 0.9),
        _error("demand_forecasting_agent", "no historical demand data provided"),
    ]

    response = agent.run(AgentQuery(text="recommend", context={"agent_outputs": outputs}))

    assert response.status == "ok"
    assert "Excluded (agent error): demand_forecasting_agent" in response.recommendation


def test_run_highlights_a_precise_subject_conflict():
    agent = RecommendationAgent()
    outputs = [
        _ok(
            "stockout_risk_agent",
            "Stockout risk: SKU-1 low",
            0.9,
            findings=[AgentFinding(subject="SKU-1", subject_kind="sku", severity="low", detail="ample stock")],
        ),
        _ok(
            "risk_detection_agent",
            "Supply chain risk score 70/100 (high)",
            0.85,
            findings=[
                AgentFinding(subject="SKU-1", subject_kind="sku", severity="critical", detail="2.1 days of supply")
            ],
        ),
    ]

    response = agent.run(AgentQuery(text="recommend", context={"agent_outputs": outputs}))

    assert response.status == "ok"
    assert "CONFLICTS DETECTED" in response.recommendation
    assert "SKU-1" in response.recommendation
    assert "critical" in response.recommendation and "low" in response.recommendation
    # A detected conflict must lower confidence below the plain mean (0.875).
    assert response.confidence < 0.875


def test_run_highlights_a_confidence_conflict_with_no_shared_subject():
    agent = RecommendationAgent()
    outputs = [
        _ok("demand_forecasting_agent", "Demand forecast: steady", 0.95),
        _ok("stockout_risk_agent", "Stockout risk: SKU-1 low", 0.30),
    ]

    response = agent.run(AgentQuery(text="recommend", context={"agent_outputs": outputs}))

    assert response.status == "ok"
    assert "CONFLICTS DETECTED" in response.recommendation
    assert "confidence spread" in response.recommendation


def test_run_logs_a_recommendation_for_every_successful_agent_with_supporting_data(monkeypatch):
    recorder = _RecordingLogger()
    monkeypatch.setattr(agent_module, "logger", recorder)
    agent = RecommendationAgent()
    outputs = [
        _ok("demand_forecasting_agent", "Demand forecast: steady", 0.8),
        _ok("stockout_risk_agent", "Stockout risk: SKU-1 low", 0.75),
    ]

    agent.run(AgentQuery(text="recommend", context={"agent_outputs": outputs}))

    logged = [c for c in recorder.info_calls if c["event"] == "recommendation_generated"]
    assert {c["context"]["source_agent"] for c in logged} == {"demand_forecasting_agent", "stockout_risk_agent"}
    for call in logged:
        assert call["context"]["recommendation"]
        assert call["context"]["confidence"] is not None


def test_run_logs_subject_and_confidence_conflicts(monkeypatch):
    recorder = _RecordingLogger()
    monkeypatch.setattr(agent_module, "logger", recorder)
    agent = RecommendationAgent()
    outputs = [
        _ok(
            "stockout_risk_agent",
            "Stockout risk: SKU-1 low",
            0.9,
            findings=[AgentFinding(subject="SKU-1", subject_kind="sku", severity="low", detail="ample stock")],
        ),
        _ok(
            "risk_detection_agent",
            "Supply chain risk score 70/100 (high)",
            0.1,
            findings=[
                AgentFinding(subject="SKU-1", subject_kind="sku", severity="critical", detail="2.1 days of supply")
            ],
        ),
    ]

    agent.run(AgentQuery(text="recommend", context={"agent_outputs": outputs}))

    subject_conflicts = [c for c in recorder.warning_calls if c["event"] == "recommendation_conflict_detected"]
    assert len(subject_conflicts) == 1
    assert subject_conflicts[0]["context"]["subject"] == "SKU-1"

    confidence_conflicts = [
        c for c in recorder.warning_calls if c["event"] == "recommendation_confidence_conflict_detected"
    ]
    assert len(confidence_conflicts) == 1
    assert set(confidence_conflicts[0]["context"]["agent_names"]) == {"stockout_risk_agent", "risk_detection_agent"}


def test_run_logs_a_warning_when_generation_fails(monkeypatch):
    recorder = _RecordingLogger()
    monkeypatch.setattr(agent_module, "logger", recorder)
    agent = RecommendationAgent()

    agent.run(AgentQuery(text="recommend", context={}))

    failed = [c for c in recorder.warning_calls if c["event"] == "recommendation_generation_failed"]
    assert len(failed) == 1
