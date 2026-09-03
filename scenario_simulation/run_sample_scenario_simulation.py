"""Runnable entry point for STORY-008.

Three scenarios, following the same convention
root_cause/run_sample_root_cause_analysis.py established:

1. "real_data" - pulls the `inventory` table via the STORY-001
   data_integration orchestrator (same audited pull, same PostgresDataset
   mechanism root_cause's own real_data scenario uses), and runs a
   scenario against the first real row it finds - e.g.
   scripts/local_test_db.py's seeded SKU-GIZMO, which starts already
   below its safety stock. With no PostgreSQL credentials configured,
   this exercises the agent's clean "no inventory data" error path with
   real integration code, not synthetic data; with the local test
   Postgres running, it simulates against a real seeded position
   instead - either way, real integration code, not synthetic data. Its
   result is not asserted in demo_succeeded below (environment-
   dependent), same as root_cause's own real_data scenario.
2. "valid_demand_spike" - a hand-built baseline inventory position plus
   a 300% demand-change scenario, run through ScenarioSimulationAgent.
   Proves AC1 ("given a scenario input... it should provide impact
   assessments") and AC3 (an audit record with a timestamp and the input
   parameters, confirmed present after the run) deterministically,
   independent of environment.
3. "invalid_parameters" - the same baseline, but with a stock_change
   large enough to drive the projected stock negative. Proves AC2
   ("given invalid scenario parameters... it should notify the user of
   errors") deterministically, and confirms AC3 still holds for a
   rejected scenario (the audit trail must record failed simulations
   too, not only successful ones).

Usage:
    SUPPLYMIND_PG_HOST=... SUPPLYMIND_PG_DATABASE=... SUPPLYMIND_PG_USER=... \\
    SUPPLYMIND_PG_PASSWORD=... \\
        python -m scenario_simulation.run_sample_scenario_simulation
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import data_integration
import scenario_simulation
from agents.contracts import AgentQuery, AgentResponse
from agents.scenario_simulation_agent import ScenarioSimulationAgent
from data_integration.audit_trail import AuditStore
from data_integration.orchestrator import PostgresDataset, available_for_analysis, run_integration_with_audit
from scenario_simulation.audit_trail import ScenarioSimulationAuditStore

DATASETS = [PostgresDataset(name="inventory", query="SELECT * FROM inventory LIMIT 500")]

DEFAULT_INTEGRATION_AUDIT_LOG_PATH = Path(data_integration.__file__).resolve().parent / "audit_log.jsonl"
DEFAULT_SIMULATION_AUDIT_LOG_PATH = Path(scenario_simulation.__file__).resolve().parent / "simulation_audit_log.jsonl"

SYNTHETIC_BASELINE_ROW = {
    "sku": "SKU-1042",
    "current_stock": 100.0,
    "safety_stock": 20.0,
    "daily_demand_rate": 5.0,
    "lead_time_days": 10.0,
}


def _integration_audit_store() -> AuditStore:
    path = os.environ.get("SUPPLYMIND_AUDIT_LOG_PATH", str(DEFAULT_INTEGRATION_AUDIT_LOG_PATH))
    return AuditStore(path)


def _simulation_audit_store() -> ScenarioSimulationAuditStore:
    path = os.environ.get("SUPPLYMIND_SCENARIO_SIMULATION_AUDIT_LOG_PATH", str(DEFAULT_SIMULATION_AUDIT_LOG_PATH))
    return ScenarioSimulationAuditStore(path)


def _run_agent(
    agent: ScenarioSimulationAgent, scenario_name: str, baseline_row: dict, simulation_id: str, **deltas: Any
) -> AgentResponse:
    context = {"scenario_name": scenario_name, "baseline_row": baseline_row, "simulation_id": simulation_id, **deltas}
    return agent.run(AgentQuery(text=f"simulate: {scenario_name}", context=context))


def _summarize(
    scenario: str,
    simulation_id: str,
    baseline_row: dict | None,
    response: AgentResponse,
    audit_store: ScenarioSimulationAuditStore,
    **extra: Any,
) -> dict:
    audit_records = audit_store.records_for_simulation(simulation_id)
    return {
        "scenario": scenario,
        "simulation_id": simulation_id,
        "baseline_row": baseline_row,
        "status": response.status,
        "recommendation": response.recommendation,
        "confidence": response.confidence,
        "error": response.error,
        "audit_trail": [
            {
                "outcome": r.outcome,
                "timestamp": r.timestamp,
                "input_parameters": r.input_parameters,
                "risk_level_changed": r.risk_level_changed,
            }
            for r in audit_records
        ],
        **extra,
    }


def _real_data_scenario(agent: ScenarioSimulationAgent, audit_store: ScenarioSimulationAuditStore) -> dict:
    results = run_integration_with_audit(DATASETS, _integration_audit_store())
    analysis_ready = available_for_analysis(results)
    inventory_rows = analysis_ready.get("inventory", [])

    if not inventory_rows:
        response = agent.run(
            AgentQuery(text="simulate against real inventory", context={"scenario_name": "supplier lead time +50%"})
        )
        return _summarize("real_data", "real_data", None, response, audit_store, real_inventory_rows_fetched=0)

    # SKU-GIZMO (scripts/local_test_db.py's seed) already starts below its
    # safety stock when the local test Postgres is running - a modest
    # lead-time increase on a real row that's already critical is a more
    # informative demo than picking whichever row happens to sort first.
    baseline_row = next((r for r in inventory_rows if r.get("sku") == "SKU-GIZMO"), inventory_rows[0])
    lead_time_increase = float(baseline_row["lead_time_days"]) * 0.5
    response = _run_agent(
        agent, "supplier lead time +50%", baseline_row, "real_data", lead_time_change_days=lead_time_increase
    )
    return _summarize(
        "real_data", "real_data", baseline_row, response, audit_store, real_inventory_rows_fetched=len(inventory_rows)
    )


def _valid_demand_spike_scenario(agent: ScenarioSimulationAgent, audit_store: ScenarioSimulationAuditStore) -> dict:
    response = _run_agent(agent, "300% demand spike", SYNTHETIC_BASELINE_ROW, "valid_demand_spike", demand_change_pct=3.0)
    return _summarize("valid_demand_spike", "valid_demand_spike", SYNTHETIC_BASELINE_ROW, response, audit_store)


def _invalid_parameters_scenario(agent: ScenarioSimulationAgent, audit_store: ScenarioSimulationAuditStore) -> dict:
    response = _run_agent(agent, "stock shock", SYNTHETIC_BASELINE_ROW, "invalid_parameters", stock_change=-500.0)
    return _summarize("invalid_parameters", "invalid_parameters", SYNTHETIC_BASELINE_ROW, response, audit_store)


def main() -> int:
    audit_store = _simulation_audit_store()
    agent = ScenarioSimulationAgent(audit_store)

    scenarios = [
        _real_data_scenario(agent, audit_store),
        _valid_demand_spike_scenario(agent, audit_store),
        _invalid_parameters_scenario(agent, audit_store),
    ]
    print(json.dumps(scenarios, indent=2, default=str))

    spike = scenarios[1]
    invalid = scenarios[2]
    demo_succeeded = (
        spike["status"] == "ok"
        and spike["confidence"] is not None
        and len(spike["audit_trail"]) == 1
        and spike["audit_trail"][0]["outcome"] == "success"
        and spike["audit_trail"][0]["input_parameters"]["demand_change_pct"] == 3.0
        and invalid["status"] == "error"
        and "invalid projected position" in (invalid["error"] or "")
        and len(invalid["audit_trail"]) == 1
        and invalid["audit_trail"][0]["outcome"] == "failure"
        and invalid["audit_trail"][0]["input_parameters"]["stock_change"] == -500.0
    )
    return 0 if demo_succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
