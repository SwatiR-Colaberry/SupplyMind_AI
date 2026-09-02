from __future__ import annotations

from agents.contracts import AgentQuery, validate_response
from agents.orchestrator import Orchestrator
from agents.root_cause_agent import RootCauseAnalysisAgent
from root_cause.audit_trail import RootCauseAuditStore


def _order(order_date: str, quantity: float) -> dict:
    return {"order_date": order_date, "quantity": quantity}


def _spike_demand_history() -> list[dict]:
    return [
        _order("2025-01-15", 100),
        _order("2025-02-15", 105),
        _order("2025-03-15", 95),
        _order("2025-04-15", 500),
    ]


def _delivery_row(**overrides) -> dict:
    defaults = dict(supplier="Acme", po_id="PO-1", expected_date="2025-01-01", actual_date="2025-01-01")
    defaults.update(overrides)
    return defaults


def test_run_returns_error_response_when_subject_is_missing(tmp_path):
    agent = RootCauseAnalysisAgent(RootCauseAuditStore(tmp_path / "audit.jsonl"))

    response = agent.run(AgentQuery(text="why is this SKU at risk?", context={"subject_kind": "sku"}))

    assert validate_response(response) is response
    assert response.status == "error"
    assert "subject" in response.error


def test_run_returns_error_response_when_subject_kind_is_invalid(tmp_path):
    agent = RootCauseAnalysisAgent(RootCauseAuditStore(tmp_path / "audit.jsonl"))

    response = agent.run(AgentQuery(text="?", context={"subject": "SKU-1", "subject_kind": "widget"}))

    assert response.status == "error"


def test_run_returns_error_response_with_limitations_when_no_signal_data_is_available(tmp_path):
    agent = RootCauseAnalysisAgent(RootCauseAuditStore(tmp_path / "audit.jsonl"))

    response = agent.run(
        AgentQuery(text="why is SKU-1 at risk?", context={"subject": "SKU-1", "subject_kind": "sku"})
    )

    assert validate_response(response) is response
    assert response.status == "error"
    assert "no anomaly or reliability data" in response.error


def test_run_derives_a_demand_spike_root_cause_from_raw_demand_history(tmp_path):
    agent = RootCauseAnalysisAgent(RootCauseAuditStore(tmp_path / "audit.jsonl"))

    response = agent.run(
        AgentQuery(
            text="why is SKU-1 at risk?",
            context={
                "subject": "SKU-1",
                "subject_kind": "sku",
                "as_of_period": "2025-04",
                "demand_history": _spike_demand_history(),
            },
        )
    )

    assert validate_response(response) is response
    assert response.status == "ok"
    assert "demand_spike" in response.recommendation
    assert response.confidence == 0.85


def test_run_derives_a_supplier_delay_root_cause_from_raw_delivery_rows(tmp_path):
    agent = RootCauseAnalysisAgent(RootCauseAuditStore(tmp_path / "audit.jsonl"))
    rows = [_delivery_row(supplier="Acme", po_id="PO-9", expected_date="2025-01-01", actual_date="2025-01-10")]

    response = agent.run(
        AgentQuery(
            text="why is SKU-1 at risk?",
            context={"subject": "SKU-1", "subject_kind": "sku", "supplier": "Acme", "delivery_rows": rows},
        )
    )

    assert response.status == "ok"
    assert "supplier_delay" in response.recommendation


def test_run_returns_ok_with_zero_confidence_when_data_exists_but_nothing_correlates(tmp_path):
    agent = RootCauseAnalysisAgent(RootCauseAuditStore(tmp_path / "audit.jsonl"))
    # A demand spike in a period that doesn't match the issue's own period.
    history = _spike_demand_history()

    response = agent.run(
        AgentQuery(
            text="why is SKU-1 at risk?",
            context={
                "subject": "SKU-1",
                "subject_kind": "sku",
                "as_of_period": "2025-09",
                "demand_history": history,
            },
        )
    )

    assert response.status == "ok"
    assert response.confidence == 0.0
    assert "no correlated cause" in response.recommendation


def test_run_persists_an_audit_record_with_timestamp_and_confidence(tmp_path):
    store = RootCauseAuditStore(tmp_path / "audit.jsonl")
    agent = RootCauseAnalysisAgent(store)

    response = agent.run(
        AgentQuery(
            text="why is SKU-1 at risk?",
            context={
                "subject": "SKU-1",
                "subject_kind": "sku",
                "as_of_period": "2025-04",
                "demand_history": _spike_demand_history(),
                "analysis_id": "run-agent-1",
            },
        )
    )

    assert response.status == "ok"
    records = store.records_for_analysis("run-agent-1")
    assert len(records) == 1
    assert records[0].confidence == 0.85
    assert records[0].timestamp


def test_root_cause_analysis_flows_through_the_orchestrator(tmp_path):
    store = RootCauseAuditStore(tmp_path / "audit.jsonl")
    orchestrator = Orchestrator([RootCauseAnalysisAgent(store)])

    run = orchestrator.coordinate(
        AgentQuery(
            text="why is SKU-1 at risk?",
            context={
                "subject": "SKU-1",
                "subject_kind": "sku",
                "as_of_period": "2025-04",
                "demand_history": _spike_demand_history(),
            },
        )
    )

    assert run.outcome == "success"
    assert run.results[0].response.status == "ok"
