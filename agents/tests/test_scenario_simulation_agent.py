from __future__ import annotations

from agents.contracts import AgentQuery, validate_response
from agents.orchestrator import Orchestrator
from agents.scenario_simulation_agent import ScenarioSimulationAgent
from scenario_simulation.audit_trail import ScenarioSimulationAuditStore


def _baseline_row(**overrides) -> dict:
    defaults = dict(sku="SKU-1", current_stock=100.0, safety_stock=20.0, daily_demand_rate=5.0, lead_time_days=10.0)
    defaults.update(overrides)
    return defaults


def test_run_returns_error_response_when_scenario_name_is_missing(tmp_path):
    agent = ScenarioSimulationAgent(ScenarioSimulationAuditStore(tmp_path / "audit.jsonl"))

    response = agent.run(AgentQuery(text="simulate", context={"baseline_row": _baseline_row()}))

    assert validate_response(response) is response
    assert response.status == "error"
    assert "scenario_name" in response.error


def test_run_returns_error_response_when_baseline_row_is_missing(tmp_path):
    agent = ScenarioSimulationAgent(ScenarioSimulationAuditStore(tmp_path / "audit.jsonl"))

    response = agent.run(AgentQuery(text="simulate", context={"scenario_name": "demand spike"}))

    assert response.status == "error"
    assert "baseline_row" in response.error


def test_run_returns_error_response_when_baseline_row_fails_data_quality_check(tmp_path):
    agent = ScenarioSimulationAgent(ScenarioSimulationAuditStore(tmp_path / "audit.jsonl"))

    response = agent.run(
        AgentQuery(
            text="simulate",
            context={"scenario_name": "demand spike", "baseline_row": _baseline_row(current_stock=-5.0)},
        )
    )

    assert validate_response(response) is response
    assert response.status == "error"
    assert "flagged for review" in response.error


def test_run_returns_error_response_for_invalid_scenario_parameters(tmp_path):
    agent = ScenarioSimulationAgent(ScenarioSimulationAuditStore(tmp_path / "audit.jsonl"))

    response = agent.run(
        AgentQuery(
            text="simulate",
            context={"scenario_name": "stock shock", "baseline_row": _baseline_row(), "stock_change": -500.0},
        )
    )

    assert validate_response(response) is response
    assert response.status == "error"
    assert "invalid projected position" in response.error


def test_run_produces_an_impact_assessment_for_a_demand_spike_scenario(tmp_path):
    agent = ScenarioSimulationAgent(ScenarioSimulationAuditStore(tmp_path / "audit.jsonl"))

    response = agent.run(
        AgentQuery(
            text="simulate",
            context={
                "scenario_name": "300% demand spike",
                "baseline_row": _baseline_row(),
                "demand_change_pct": 3.0,
            },
        )
    )

    assert validate_response(response) is response
    assert response.status == "ok"
    assert "worsens" in response.recommendation
    assert response.findings[0].subject == "SKU-1"
    assert response.findings[0].severity == "high"


def test_run_persists_an_audit_record_with_timestamp_and_input_parameters(tmp_path):
    store = ScenarioSimulationAuditStore(tmp_path / "audit.jsonl")
    agent = ScenarioSimulationAgent(store)

    response = agent.run(
        AgentQuery(
            text="simulate",
            context={
                "scenario_name": "300% demand spike",
                "baseline_row": _baseline_row(),
                "demand_change_pct": 3.0,
                "simulation_id": "run-agent-1",
            },
        )
    )

    assert response.status == "ok"
    records = store.records_for_simulation("run-agent-1")
    assert len(records) == 1
    assert records[0].timestamp
    assert records[0].input_parameters["demand_change_pct"] == 3.0


# --- regression coverage for the pre-commit code review fixes ---


def test_run_returns_error_response_when_baseline_row_is_not_an_object(tmp_path):
    # Regression: a non-dict baseline_row (e.g. a caller shape mistake -
    # int, list, string) used to reach assess_inventory_data_quality()
    # unchecked and crash with an uncaught TypeError instead of the clean
    # status="error" response this agent's docstring promises.
    agent = ScenarioSimulationAgent(ScenarioSimulationAuditStore(tmp_path / "audit.jsonl"))

    response = agent.run(AgentQuery(text="simulate", context={"scenario_name": "x", "baseline_row": 42}))

    assert validate_response(response) is response
    assert response.status == "error"
    assert "baseline_row" in response.error


def test_run_returns_error_response_when_scenario_name_is_not_a_string(tmp_path):
    # Regression: a non-string scenario_name (e.g. an int) passed the old
    # `not scenario_name or not str(scenario_name).strip()` guard (which
    # coerced to str only for the check, not the value actually used),
    # then crashed inside simulate_scenario()'s own .strip() call.
    agent = ScenarioSimulationAgent(ScenarioSimulationAuditStore(tmp_path / "audit.jsonl"))

    response = agent.run(AgentQuery(text="simulate", context={"scenario_name": 5, "baseline_row": _baseline_row()}))

    assert validate_response(response) is response
    assert response.status == "error"
    assert "scenario_name" in response.error


def test_run_returns_error_response_when_a_delta_field_is_explicit_null(tmp_path):
    # Regression: context.get(field, 0.0) only applies its default when
    # the key is absent - a JSON API caller sending an explicit null
    # (e.g. {"demand_change_pct": None}) passed a real None through to
    # ScenarioInput, which then crashed inside simulate_scenario()'s
    # `scenario.demand_change_pct < -1.0` comparison and got
    # misclassified as a "crashed" simulation failure instead of being
    # treated as "not supplied" (the documented default).
    agent = ScenarioSimulationAgent(ScenarioSimulationAuditStore(tmp_path / "audit.jsonl"))

    response = agent.run(
        AgentQuery(
            text="simulate",
            context={"scenario_name": "x", "baseline_row": _baseline_row(), "demand_change_pct": None},
        )
    )

    assert validate_response(response) is response
    assert response.status == "ok"
    assert response.recommendation is not None
    assert "leaves unchanged" in response.recommendation


def test_run_returns_error_response_when_a_delta_field_is_not_numeric(tmp_path):
    agent = ScenarioSimulationAgent(ScenarioSimulationAuditStore(tmp_path / "audit.jsonl"))

    response = agent.run(
        AgentQuery(
            text="simulate",
            context={"scenario_name": "x", "baseline_row": _baseline_row(), "demand_change_pct": "high"},
        )
    )

    assert validate_response(response) is response
    assert response.status == "error"
    assert "demand_change_pct" in response.error


def test_scenario_simulation_flows_through_the_orchestrator(tmp_path):
    store = ScenarioSimulationAuditStore(tmp_path / "audit.jsonl")
    orchestrator = Orchestrator([ScenarioSimulationAgent(store)])

    run = orchestrator.coordinate(
        AgentQuery(
            text="simulate a 300% demand spike",
            context={
                "scenario_name": "300% demand spike",
                "baseline_row": _baseline_row(),
                "demand_change_pct": 3.0,
            },
        )
    )

    assert run.outcome == "success"
    assert run.results[0].response.status == "ok"
