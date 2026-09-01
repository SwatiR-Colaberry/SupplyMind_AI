from __future__ import annotations

from agents.contracts import AgentQuery, validate_response
from agents.orchestrator import Orchestrator
from agents.recommendation_agent import RecommendationAgent
from agents.supplier_evaluation_agent import SupplierEvaluationAgent
from supplier_evaluation.audit_trail import SupplierEvaluationAuditStore


def _delivery_row(**overrides) -> dict:
    defaults = dict(supplier="Acme", po_id="PO-1", expected_date="2025-01-01", actual_date="2025-01-01")
    defaults.update(overrides)
    return defaults


def _reliable_supplier_rows(supplier="Acme") -> list[dict]:
    return [
        _delivery_row(supplier=supplier, po_id="PO-1", expected_date="2025-01-01", actual_date="2025-01-01"),
        _delivery_row(supplier=supplier, po_id="PO-2", expected_date="2025-01-05", actual_date="2025-01-05"),
        _delivery_row(supplier=supplier, po_id="PO-3", expected_date="2025-01-10", actual_date="2025-01-10"),
    ]


def _unreliable_supplier_rows(supplier="SlowFreight") -> list[dict]:
    return [
        _delivery_row(supplier=supplier, po_id="PO-4", expected_date="2025-01-01", actual_date="2025-01-20"),
        _delivery_row(supplier=supplier, po_id="PO-5", expected_date="2025-01-05", actual_date="2025-01-25"),
        _delivery_row(supplier=supplier, po_id="PO-6", expected_date="2025-01-10", actual_date="2025-01-30"),
    ]


def test_run_returns_error_response_when_no_delivery_rows_are_provided(tmp_path):
    agent = SupplierEvaluationAgent(SupplierEvaluationAuditStore(tmp_path / "audit.jsonl"))

    response = agent.run(AgentQuery(text="evaluate suppliers", context={}))

    assert validate_response(response) is response
    assert response.status == "error"
    assert "no delivery data" in response.error


def test_run_returns_ok_response_with_a_supplier_finding_per_supplier(tmp_path):
    agent = SupplierEvaluationAgent(SupplierEvaluationAuditStore(tmp_path / "audit.jsonl"))
    rows = _reliable_supplier_rows("Acme") + _unreliable_supplier_rows("SlowFreight")

    response = agent.run(AgentQuery(text="evaluate suppliers", context={"delivery_rows": rows}))

    assert validate_response(response) is response
    assert response.status == "ok"
    by_subject = {f.subject: f for f in response.findings}
    assert by_subject["Acme"].subject_kind == "supplier"
    assert by_subject["Acme"].severity == "low"
    assert by_subject["SlowFreight"].severity == "critical"
    assert "Acme" in response.recommendation
    assert "SlowFreight" in response.recommendation


def test_run_lowers_confidence_when_a_supplier_has_too_little_data(tmp_path):
    agent = SupplierEvaluationAgent(SupplierEvaluationAuditStore(tmp_path / "audit.jsonl"))
    rows = _reliable_supplier_rows("Acme") + [_delivery_row(supplier="NewVendor", po_id="PO-9")]

    response = agent.run(AgentQuery(text="evaluate suppliers", context={"delivery_rows": rows}))

    assert response.status == "ok"
    assert response.confidence < 1.0


def test_run_returns_full_confidence_when_every_supplier_is_cleanly_scored(tmp_path):
    agent = SupplierEvaluationAgent(SupplierEvaluationAuditStore(tmp_path / "audit.jsonl"))

    response = agent.run(AgentQuery(text="evaluate suppliers", context={"delivery_rows": _reliable_supplier_rows()}))

    assert response.confidence == 1.0


def test_run_returns_error_response_when_every_row_is_unattributable(tmp_path):
    agent = SupplierEvaluationAgent(SupplierEvaluationAuditStore(tmp_path / "audit.jsonl"))
    rows = [{"po_id": "PO-1", "expected_date": "2025-01-01", "actual_date": "2025-01-01"}]  # no supplier

    response = agent.run(AgentQuery(text="evaluate suppliers", context={"delivery_rows": rows}))

    assert response.status == "error"
    assert "missing a supplier" in response.error


def test_run_persists_an_audit_record_per_supplier(tmp_path):
    store = SupplierEvaluationAuditStore(tmp_path / "audit.jsonl")
    agent = SupplierEvaluationAgent(store)

    response = agent.run(
        AgentQuery(
            text="evaluate suppliers",
            context={"delivery_rows": _reliable_supplier_rows("Acme"), "evaluation_id": "eval-agent-1"},
        )
    )

    assert response.status == "ok"
    records = store.records_for_evaluation("eval-agent-1")
    assert len(records) == 1
    assert records[0].supplier == "Acme"
    assert records[0].outcome == "success"


# --- integration: this agent's findings reach RecommendationAgent (STORY-006) ---


def test_supplier_findings_flow_through_the_orchestrator_into_a_recommendation(tmp_path):
    store = SupplierEvaluationAuditStore(tmp_path / "audit.jsonl")
    stage1 = Orchestrator([SupplierEvaluationAgent(store)])
    rows = _reliable_supplier_rows("Acme") + _unreliable_supplier_rows("SlowFreight")

    stage1_run = stage1.coordinate(AgentQuery(text="evaluate suppliers", context={"delivery_rows": rows}))

    assert stage1_run.outcome == "success"
    agent_outputs = [r.response for r in stage1_run.results if r.response is not None]

    stage2 = Orchestrator([RecommendationAgent()])
    stage2_run = stage2.coordinate(
        AgentQuery(text="generate recommendations", context={"agent_outputs": agent_outputs})
    )

    assert stage2_run.outcome == "success"
    recommendation_response = stage2_run.results[0].response
    assert recommendation_response.status == "ok"
    assert "SlowFreight" in recommendation_response.recommendation
