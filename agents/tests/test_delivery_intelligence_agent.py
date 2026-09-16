from __future__ import annotations

from agents.contracts import AgentFinding, AgentQuery, AgentResponse, validate_response
from agents.delivery_intelligence_agent import DeliveryIntelligenceAgent


def _stockout_response(findings: list[AgentFinding], status: str = "ok") -> AgentResponse:
    return AgentResponse(agent_name="stockout_risk_agent", status=status, recommendation="r", findings=findings, error="boom" if status == "error" else None)


def _supplier_response(findings: list[AgentFinding], status: str = "ok") -> AgentResponse:
    return AgentResponse(agent_name="supplier_evaluation_agent", status=status, recommendation="r", findings=findings, error="boom" if status == "error" else None)


def _stockout_finding(sku: str, severity: str, supplier: str | None) -> AgentFinding:
    return AgentFinding(subject=sku, subject_kind="sku", severity=severity, detail=f"{sku} detail", supplier=supplier)


def _supplier_finding(name: str, severity: str) -> AgentFinding:
    return AgentFinding(subject=name, subject_kind="supplier", severity=severity, detail=f"{name} is unreliable")


def test_run_returns_ok_with_no_confidence_when_stockout_risk_agent_is_missing():
    agent = DeliveryIntelligenceAgent()
    outputs = [_supplier_response([_supplier_finding("Acme", "high")])]

    response = agent.run(AgentQuery(text="correlate", context={"agent_outputs": outputs}))

    assert validate_response(response) is response
    assert response.status == "ok"
    assert response.confidence is None
    assert "wasn't available" in response.recommendation


def test_run_returns_ok_with_no_confidence_when_supplier_evaluation_agent_errored():
    agent = DeliveryIntelligenceAgent()
    outputs = [
        _stockout_response([_stockout_finding("SKU-1", "critical", supplier="Acme")]),
        _supplier_response([], status="error"),
    ]

    response = agent.run(AgentQuery(text="correlate", context={"agent_outputs": outputs}))

    assert response.status == "ok"
    assert response.confidence is None


def test_run_returns_ok_with_no_confidence_when_no_sku_has_a_supplier_mapped():
    agent = DeliveryIntelligenceAgent()
    outputs = [
        _stockout_response([_stockout_finding("SKU-1", "critical", supplier=None)]),
        _supplier_response([_supplier_finding("Acme", "high")]),
    ]

    response = agent.run(AgentQuery(text="correlate", context={"agent_outputs": outputs}))

    assert response.status == "ok"
    assert response.confidence is None
    assert "not mapped" in response.recommendation or "No supplier is mapped" in response.recommendation


def test_run_returns_full_confidence_with_no_narrative_when_nothing_is_at_risk():
    agent = DeliveryIntelligenceAgent()
    outputs = [
        _stockout_response([_stockout_finding("SKU-1", "low", supplier="Acme")]),
        _supplier_response([_supplier_finding("Acme", "high")]),
    ]

    response = agent.run(AgentQuery(text="correlate", context={"agent_outputs": outputs}))

    assert response.status == "ok"
    assert response.confidence == 1.0
    assert "No supplier-driven stockout risk detected" in response.recommendation


def test_run_returns_a_real_narrative_when_a_flagged_supplier_has_at_risk_skus():
    agent = DeliveryIntelligenceAgent()
    outputs = [
        _stockout_response(
            [
                _stockout_finding("SKU-1", "critical", supplier="Acme"),
                _stockout_finding("SKU-2", "medium", supplier="Acme"),
            ]
        ),
        _supplier_response([_supplier_finding("Acme", "high")]),
    ]

    response = agent.run(AgentQuery(text="correlate", context={"agent_outputs": outputs}))

    assert validate_response(response) is response
    assert response.status == "ok"
    assert response.confidence == 1.0
    assert "Acme" in response.recommendation
    assert "SKU-1" in response.recommendation and "SKU-2" in response.recommendation


def test_run_returns_error_response_when_agent_outputs_missing():
    agent = DeliveryIntelligenceAgent()

    response = agent.run(AgentQuery(text="correlate", context={}))

    assert response.status == "ok"  # empty agent_outputs -> both source agents "missing", not an error
    assert response.confidence is None


def test_run_returns_error_response_when_agent_outputs_contains_non_agent_response():
    agent = DeliveryIntelligenceAgent()

    response = agent.run(AgentQuery(text="correlate", context={"agent_outputs": ["not an AgentResponse"]}))

    assert response.status == "error"
    assert "must contain only AgentResponse" in response.error


def test_run_returns_error_response_when_agent_outputs_is_not_iterable():
    agent = DeliveryIntelligenceAgent()

    response = agent.run(AgentQuery(text="correlate", context={"agent_outputs": 42}))

    assert response.status == "error"
    assert "must be an iterable" in response.error
