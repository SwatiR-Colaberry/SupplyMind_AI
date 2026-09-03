"""Runnable entry point for STORY-008.

Two scenarios, following the same synthetic-demo convention
root_cause/run_sample_root_cause_analysis.py uses for its
environment-independent cases (that file's "synthetic_demand_spike" /
"synthetic_insufficient_data" pair). Unlike STORY-007, scenario
simulation has no natural real/live dataset to pull from -
data_integration's customer_orders/delivery_records are historical
records, not hypothetical what-ifs - so both scenarios here are
synthetic by design, not a fallback for missing credentials:

1. "valid_demand_spike" - a baseline inventory position plus a 300%
   demand-change scenario, run through ScenarioSimulationAgent. Proves
   AC1 ("given a scenario input... it should provide impact
   assessments") and AC3 (an audit record with a timestamp and the input
   parameters, confirmed present after the run) in one pass.
2. "invalid_parameters" - the same baseline, but with a stock_change
   large enough to drive the projected stock negative. Proves AC2
   ("given invalid scenario parameters... it should notify the user of
   errors") deterministically, and confirms AC3 still holds for a
   rejected scenario (the audit trail must record failed simulations
   too, not only successful ones).

Usage:
    python -m scenario_simulation.run_sample_scenario_simulation
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import scenario_simulation
from agents.contracts import AgentQuery, AgentResponse
from agents.scenario_simulation_agent import ScenarioSimulationAgent
from scenario_simulation.audit_trail import ScenarioSimulationAuditStore

DEFAULT_AUDIT_LOG_PATH = Path(scenario_simulation.__file__).resolve().parent / "simulation_audit_log.jsonl"

BASELINE_ROW = {
    "sku": "SKU-1042",
    "current_stock": 100.0,
    "safety_stock": 20.0,
    "daily_demand_rate": 5.0,
    "lead_time_days": 10.0,
}


def _audit_store() -> ScenarioSimulationAuditStore:
    path = os.environ.get("SUPPLYMIND_SCENARIO_SIMULATION_AUDIT_LOG_PATH", str(DEFAULT_AUDIT_LOG_PATH))
    return ScenarioSimulationAuditStore(path)


def _run_agent(agent: ScenarioSimulationAgent, scenario_name: str, simulation_id: str, **deltas) -> AgentResponse:
    context = {
        "scenario_name": scenario_name,
        "baseline_row": BASELINE_ROW,
        "simulation_id": simulation_id,
        **deltas,
    }
    return agent.run(AgentQuery(text=f"simulate: {scenario_name}", context=context))


def _summarize(
    scenario: str, simulation_id: str, response: AgentResponse, audit_store: ScenarioSimulationAuditStore
) -> dict:
    audit_records = audit_store.records_for_simulation(simulation_id)
    return {
        "scenario": scenario,
        "simulation_id": simulation_id,
        "baseline_row": BASELINE_ROW,
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
    }


def _valid_demand_spike_scenario(agent: ScenarioSimulationAgent, audit_store: ScenarioSimulationAuditStore) -> dict:
    response = _run_agent(agent, "300% demand spike", "valid_demand_spike", demand_change_pct=3.0)
    return _summarize("valid_demand_spike", "valid_demand_spike", response, audit_store)


def _invalid_parameters_scenario(agent: ScenarioSimulationAgent, audit_store: ScenarioSimulationAuditStore) -> dict:
    response = _run_agent(agent, "stock shock", "invalid_parameters", stock_change=-500.0)
    return _summarize("invalid_parameters", "invalid_parameters", response, audit_store)


def main() -> int:
    audit_store = _audit_store()
    agent = ScenarioSimulationAgent(audit_store)

    scenarios = [
        _valid_demand_spike_scenario(agent, audit_store),
        _invalid_parameters_scenario(agent, audit_store),
    ]
    print(json.dumps(scenarios, indent=2, default=str))

    spike = scenarios[0]
    invalid = scenarios[1]
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
