from __future__ import annotations

from unittest.mock import patch

from agents.contracts import AgentQuery, validate_response
from agents.data_quality_monitoring_agent import DataQualityMonitoringAgent
from agents.orchestrator import Orchestrator
from agents.recommendation_agent import RecommendationAgent
from data_quality_monitoring.audit_trail import QualityAuditStore


def _row(**overrides) -> dict:
    defaults = dict(po_id="PO-1", expected_date="2025-01-01", actual_date="2025-01-01")
    defaults.update(overrides)
    return defaults


def _complete_rows() -> list[dict]:
    return [_row(po_id="PO-1"), _row(po_id="PO-2"), _row(po_id="PO-3")]


def _incomplete_rows() -> list[dict]:
    return [_row(po_id="PO-1")] + [{"po_id": f"PO-{i}"} for i in range(2, 10)]  # 1/9 complete


def test_run_returns_ok_response_with_a_full_score_for_clean_data(tmp_path):
    agent = DataQualityMonitoringAgent(QualityAuditStore(tmp_path / "audit.jsonl"))

    response = agent.run(AgentQuery(text="check data quality", context={"delivery_rows": _complete_rows()}))

    assert validate_response(response) is response
    assert response.status == "ok"
    assert response.findings == []
    assert "100/100" in response.recommendation
    assert "ALERT" not in response.recommendation
    assert response.confidence == 1.0


def test_run_alerts_in_the_recommendation_when_quality_is_poor(tmp_path):
    agent = DataQualityMonitoringAgent(QualityAuditStore(tmp_path / "audit.jsonl"))

    response = agent.run(AgentQuery(text="check data quality", context={"delivery_rows": _incomplete_rows()}))

    assert validate_response(response) is response
    assert response.status == "ok"
    assert "ALERT" in response.recommendation
    assert response.confidence < 1.0


def test_run_treats_zero_rows_as_an_ok_alert_not_an_error(tmp_path):
    # Unlike SupplierEvaluationAgent/ShipmentDelayAnalysisAgent, "no data
    # at all" is itself the finding this agent exists to surface.
    agent = DataQualityMonitoringAgent(QualityAuditStore(tmp_path / "audit.jsonl"))

    response = agent.run(AgentQuery(text="check data quality", context={}))

    assert validate_response(response) is response
    assert response.status == "ok"
    assert "no rows available" in response.recommendation
    assert response.confidence < 1.0


def test_run_persists_an_audit_record_and_is_idempotent_per_check_id(tmp_path):
    store = QualityAuditStore(tmp_path / "audit.jsonl")
    agent = DataQualityMonitoringAgent(store)

    agent.run(
        AgentQuery(
            text="check data quality", context={"delivery_rows": _complete_rows(), "check_id": "check-agent-1"}
        )
    )
    agent.run(
        AgentQuery(
            text="check data quality", context={"delivery_rows": _complete_rows(), "check_id": "check-agent-1"}
        )
    )

    records = store.records_for_check("check-agent-1")
    assert len(records) == 1
    assert records[0].dimension == "completeness"
    assert records[0].outcome == "success"


def test_run_returns_error_response_for_an_unexpected_crash(tmp_path):
    agent = DataQualityMonitoringAgent(QualityAuditStore(tmp_path / "audit.jsonl"))

    with patch(
        "data_quality_monitoring.evaluator.assess_data_quality",
        side_effect=RuntimeError("boom"),
    ):
        response = agent.run(AgentQuery(text="check data quality", context={"delivery_rows": _complete_rows()}))

    assert response.status == "error"
    assert "boom" in response.error


# --- integration: this agent's alert reaches RecommendationAgent (STORY-006) ---


def test_poor_quality_alert_flows_through_the_orchestrator_into_a_recommendation(tmp_path):
    store = QualityAuditStore(tmp_path / "audit.jsonl")
    stage1 = Orchestrator([DataQualityMonitoringAgent(store)])

    stage1_run = stage1.coordinate(
        AgentQuery(text="check data quality", context={"delivery_rows": _incomplete_rows()})
    )

    assert stage1_run.outcome == "success"
    agent_outputs = [r.response for r in stage1_run.results if r.response is not None]

    stage2 = Orchestrator([RecommendationAgent()])
    stage2_run = stage2.coordinate(
        AgentQuery(text="generate recommendations", context={"agent_outputs": agent_outputs})
    )

    assert stage2_run.outcome == "success"
    recommendation_response = stage2_run.results[0].response
    assert recommendation_response.status == "ok"
    assert "ALERT" in recommendation_response.recommendation
