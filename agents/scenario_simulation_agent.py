"""Scenario simulation agent (STORY-008 / REQ-014).

Wraps scenario_simulation/ (ScenarioEvaluator, itself wrapping the pure
simulate_scenario() computation with a durable, timestamped audit trail)
as an Agent so it plugs into the existing Orchestrator (STORY-002)
without any changes to orchestration logic - same shape as
RootCauseAnalysisAgent (STORY-007) and StockoutRiskAgent (STORY-004).

Reuses inventory_risk/data_quality.py's assess_inventory_data_quality()
(STORY-004) to validate the raw baseline row before it becomes an
InventoryPosition - this is the "data processing errors" failure path,
distinct from "invalid scenario inputs" (bad deltas), which
scenario_simulation.simulation.ScenarioValidationError already covers.

Like RootCauseAnalysisAgent, this agent is stateful: it owns a
ScenarioSimulationAuditStore so every simulation run through the
Orchestrator gets the same audited trust-spine guarantee
run_sample_scenario_simulation.py's direct ScenarioEvaluator calls get -
STORY-008's own AC3 applies to every entry point, not just a standalone
demo script.

Query contract (via AgentQuery.context):
    "scenario_name": str - required. A human-readable label for the
        what-if being run (e.g. "20% demand spike", "supplier lead time
        +15d").
    "baseline_row": dict - required. One raw inventory row, same shape
        StockoutRiskAgent expects (sku, current_stock, safety_stock,
        daily_demand_rate, lead_time_days) - see
        inventory_risk/data_quality.py's REQUIRED_FIELDS.
    "demand_change_pct" / "lead_time_change_days" / "safety_stock_change"
        / "stock_change": float, optional - the scenario's deltas;
        default to 0.0 (no change) - see ScenarioInput.
    "simulation_id": str, optional - passed through to
        ScenarioEvaluator.run() for idempotency; defaults to a fresh
        UUID per call if omitted.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import scenario_simulation
from agents.contracts import AgentFinding, AgentQuery, AgentResponse
from agents.logging_setup import get_logger
from inventory_risk.data_quality import REQUIRED_FIELDS, assess_inventory_data_quality
from inventory_risk.risk_model import InventoryPosition
from scenario_simulation.audit_trail import ScenarioSimulationAuditStore
from scenario_simulation.evaluator import ScenarioEvaluator, ScenarioSimulationRun
from scenario_simulation.simulation import ScenarioInput

logger = get_logger()

DEFAULT_AUDIT_LOG_PATH = Path(scenario_simulation.__file__).resolve().parent / "simulation_audit_log.jsonl"

_DELTA_FIELDS = ("demand_change_pct", "lead_time_change_days", "safety_stock_change", "stock_change")


def _default_audit_store() -> ScenarioSimulationAuditStore:
    path = os.environ.get("SUPPLYMIND_SCENARIO_SIMULATION_AUDIT_LOG_PATH", str(DEFAULT_AUDIT_LOG_PATH))
    return ScenarioSimulationAuditStore(path)


class ScenarioSimulationAgent:
    name = "scenario_simulation_agent"

    def __init__(self, audit_store: ScenarioSimulationAuditStore | None = None) -> None:
        self._evaluator = ScenarioEvaluator(audit_store or _default_audit_store())

    def run(self, query: AgentQuery) -> AgentResponse:
        """Produce a scenario-impact AgentResponse for the scenario named in query.context.

        Handles (returns status="error" for, rather than raising - a
        raised exception here would surface in the Orchestrator as
        "agent_communication_failed", the wrong classification for a
        request/data problem the caller can act on): a missing
        scenario_name or baseline_row ("data processing errors" - a
        caller bug, not a simulation outcome); a baseline_row the
        inventory data-quality check flags (missing/invalid fields -
        "data processing errors"); the ScenarioEvaluator reporting
        "invalid_input" (AC2's "notify the user of errors" path - the
        deltas themselves don't make sense against this baseline); and
        "crashed" (the "simulation model failure" / "simulation API
        failure" failure path - the evaluator already converts any
        unexpected exception into a reported outcome rather than
        raising).

        Any other, truly unexpected exception (a bug in this method
        itself) is left to propagate - the Orchestrator already has a
        dedicated, tested path for an agent raising
        (agent_communication_failed, isolated per-agent), so this agent
        does not duplicate that handling.
        """
        context = query.context
        scenario_name = context.get("scenario_name")
        baseline_row = context.get("baseline_row")
        if not isinstance(scenario_name, str) or not scenario_name.strip():
            return self._error_response(
                "scenario_name is required and must be a non-empty string", error_class="ScenarioSimulationRequestError"
            )
        if not isinstance(baseline_row, dict) or not baseline_row:
            return self._error_response(
                "baseline_row is required and must be a non-empty object", error_class="ScenarioSimulationRequestError"
            )

        quality = assess_inventory_data_quality([baseline_row])
        if not quality.clean_rows:
            reasons = "; ".join(quality.flagged_rows[0].reasons) if quality.flagged_rows else "invalid baseline_row"
            return self._error_response(
                f"baseline_row flagged for review: {reasons}", error_class="InventoryDataQualityError"
            )
        clean_row = quality.clean_rows[0]
        baseline = InventoryPosition(**{f: clean_row[f] for f in REQUIRED_FIELDS})

        deltas = self._validate_deltas(context)
        if isinstance(deltas, str):
            return self._error_response(deltas, error_class="ScenarioSimulationRequestError")

        scenario = ScenarioInput(scenario_name=scenario_name, baseline=baseline, **deltas)

        run = self._evaluator.run(scenario, simulation_id=context.get("simulation_id"))

        if run.outcome == "invalid_input":
            return self._error_response(run.limitation or "invalid scenario parameters", error_class="ScenarioValidationError")
        if run.outcome == "crashed":
            return self._error_response(run.crash_error or "scenario simulation failed", error_class="ScenarioSimulationError")

        logger.info(
            "scenario_simulation_agent_completed",
            extra={
                "event": "scenario_simulation_agent_completed",
                "outcome": "success",
                "correlation_id": run.simulation_id,
                "context": {
                    "scenario_name": scenario.scenario_name,
                    "sku": baseline.sku,
                    "risk_level_changed": run.impact.risk_level_changed,
                },
            },
        )
        impact = run.impact
        assert impact is not None  # only reached when run.outcome == "success"
        return AgentResponse(
            agent_name=self.name,
            status="ok",
            recommendation=self._format_recommendation(run),
            confidence=impact.projected.confidence,
            findings=[
                AgentFinding(
                    subject=baseline.sku,
                    subject_kind="sku",
                    severity=impact.projected.risk_level,
                    detail=impact.detail,
                )
            ],
        )

    @staticmethod
    def _validate_deltas(context: dict[str, Any]) -> dict[str, float] | str:
        """Validate the four optional delta fields, defaulting an omitted or explicit-null one to 0.0.

        Returns the validated {field: float} mapping, or an error message
        string if any supplied delta isn't a real number - e.g. a JSON
        API caller sending "demand_change_pct": "high" or a stray list.
        A caller-supplied None (a field present but null, as opposed to
        omitted) is treated the same as "not supplied", not rejected -
        common for optional fields serialized from a JSON API.
        """
        deltas: dict[str, float] = {}
        for field_name in _DELTA_FIELDS:
            value = context.get(field_name)
            if value is None:
                value = 0.0
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return f"{field_name} must be a number, got {value!r}"
            deltas[field_name] = float(value)
        return deltas

    def _error_response(self, message: str, error_class: str) -> AgentResponse:
        logger.warning(
            "scenario_simulation_agent_failed",
            extra={
                "event": "scenario_simulation_agent_failed",
                "outcome": "failure",
                "error_class": error_class,
                "context": {"detail": message},
            },
        )
        return AgentResponse(agent_name=self.name, status="error", error=message)

    @staticmethod
    def _format_recommendation(run: ScenarioSimulationRun) -> str:
        impact = run.impact
        assert impact is not None  # only called when run.outcome == "success"
        return (
            f"Scenario '{impact.scenario_name}' impact assessment: {impact.detail} "
            f"(baseline confidence {impact.baseline.confidence:.2f}, projected confidence {impact.projected.confidence:.2f})"
        )
