from __future__ import annotations

from agents.contracts import AgentQuery, validate_response
from agents.orchestrator import Orchestrator
from agents.recommendation_agent import RecommendationAgent
from agents.shipment_delay_analysis_agent import ShipmentDelayAnalysisAgent
from shipment_delay_analysis.audit_trail import ShipmentDelayAuditStore


def _delivery_row(**overrides) -> dict:
    defaults = dict(supplier="Acme", po_id="PO-1", expected_date="2025-01-01", actual_date="2025-01-01")
    defaults.update(overrides)
    return defaults


def _on_time_rows(supplier="Acme") -> list[dict]:
    return [
        _delivery_row(supplier=supplier, po_id="PO-1", expected_date="2025-01-01", actual_date="2025-01-01"),
        _delivery_row(supplier=supplier, po_id="PO-2", expected_date="2025-01-05", actual_date="2025-01-05"),
    ]


def _delayed_rows(supplier="SlowFreight") -> list[dict]:
    return [
        _delivery_row(supplier=supplier, po_id="PO-4", expected_date="2025-01-01", actual_date="2025-01-20"),
        _delivery_row(supplier=supplier, po_id="PO-5", expected_date="2025-01-05", actual_date="2025-01-25"),
    ]


def test_run_returns_error_response_when_no_delivery_rows_are_provided(tmp_path):
    agent = ShipmentDelayAnalysisAgent(ShipmentDelayAuditStore(tmp_path / "audit.jsonl"))

    response = agent.run(AgentQuery(text="analyze shipment delays", context={}))

    assert validate_response(response) is response
    assert response.status == "error"
    assert "no delivery data" in response.error


def test_run_returns_ok_response_with_no_findings_when_every_delivery_is_on_time(tmp_path):
    agent = ShipmentDelayAnalysisAgent(ShipmentDelayAuditStore(tmp_path / "audit.jsonl"))

    response = agent.run(AgentQuery(text="analyze shipment delays", context={"delivery_rows": _on_time_rows()}))

    assert validate_response(response) is response
    assert response.status == "ok"
    assert response.findings == []
    assert "no delays found" in response.recommendation


def test_run_returns_ok_response_with_a_finding_per_delayed_po(tmp_path):
    agent = ShipmentDelayAnalysisAgent(ShipmentDelayAuditStore(tmp_path / "audit.jsonl"))
    rows = _on_time_rows("Acme") + _delayed_rows("SlowFreight")

    response = agent.run(AgentQuery(text="analyze shipment delays", context={"delivery_rows": rows}))

    assert validate_response(response) is response
    assert response.status == "ok"
    by_subject = {f.subject: f for f in response.findings}
    assert set(by_subject) == {"PO-4", "PO-5"}
    assert by_subject["PO-4"].subject_kind == "po"
    assert by_subject["PO-4"].severity == "critical"
    assert "PO-4" in response.recommendation
    assert "$" in response.recommendation


def test_run_uses_a_custom_cost_per_day_late(tmp_path):
    agent = ShipmentDelayAnalysisAgent(ShipmentDelayAuditStore(tmp_path / "audit.jsonl"))
    rows = [_delivery_row(po_id="PO-1", expected_date="2025-01-01", actual_date="2025-01-06")]  # 5 days late

    response = agent.run(
        AgentQuery(text="analyze shipment delays", context={"delivery_rows": rows, "cost_per_day_late": 200.0})
    )

    assert "$1,000.00" in response.recommendation  # 5 days * $200/day


def test_run_lowers_confidence_when_delivery_rows_have_data_quality_issues(tmp_path):
    agent = ShipmentDelayAnalysisAgent(ShipmentDelayAuditStore(tmp_path / "audit.jsonl"))
    rows = _delayed_rows("SlowFreight") + [
        {"supplier": "Acme", "po_id": "PO-9", "expected_date": "2025-01-01"}  # missing actual_date
    ]

    response = agent.run(AgentQuery(text="analyze shipment delays", context={"delivery_rows": rows}))

    assert response.status == "ok"
    assert response.confidence < 1.0


def test_run_returns_full_confidence_when_every_row_is_clean(tmp_path):
    agent = ShipmentDelayAnalysisAgent(ShipmentDelayAuditStore(tmp_path / "audit.jsonl"))

    response = agent.run(AgentQuery(text="analyze shipment delays", context={"delivery_rows": _on_time_rows()}))

    assert response.confidence == 1.0


def test_run_persists_an_audit_record_per_delayed_po(tmp_path):
    store = ShipmentDelayAuditStore(tmp_path / "audit.jsonl")
    agent = ShipmentDelayAnalysisAgent(store)

    response = agent.run(
        AgentQuery(
            text="analyze shipment delays",
            context={"delivery_rows": _delayed_rows("SlowFreight"), "analysis_id": "analysis-agent-1"},
        )
    )

    assert response.status == "ok"
    records = store.records_for_analysis("analysis-agent-1")
    assert len(records) == 2
    assert {r.po_id for r in records} == {"PO-4", "PO-5"}
    assert all(r.outcome == "success" for r in records)


def test_run_returns_error_response_for_an_invalid_parameter(tmp_path):
    agent = ShipmentDelayAnalysisAgent(ShipmentDelayAuditStore(tmp_path / "audit.jsonl"))

    response = agent.run(
        AgentQuery(
            text="analyze shipment delays",
            context={"delivery_rows": _delayed_rows(), "cost_per_day_late": -1.0},
        )
    )

    assert response.status == "error"
    assert "cost_per_day_late" in response.error


# --- integration: this agent's findings reach RecommendationAgent (STORY-006) ---


def test_delay_findings_flow_through_the_orchestrator_into_a_recommendation(tmp_path):
    store = ShipmentDelayAuditStore(tmp_path / "audit.jsonl")
    stage1 = Orchestrator([ShipmentDelayAnalysisAgent(store)])
    rows = _on_time_rows("Acme") + _delayed_rows("SlowFreight")

    stage1_run = stage1.coordinate(AgentQuery(text="analyze shipment delays", context={"delivery_rows": rows}))

    assert stage1_run.outcome == "success"
    agent_outputs = [r.response for r in stage1_run.results if r.response is not None]

    stage2 = Orchestrator([RecommendationAgent()])
    stage2_run = stage2.coordinate(
        AgentQuery(text="generate recommendations", context={"agent_outputs": agent_outputs})
    )

    assert stage2_run.outcome == "success"
    recommendation_response = stage2_run.results[0].response
    assert recommendation_response.status == "ok"
    assert "PO-4" in recommendation_response.recommendation
