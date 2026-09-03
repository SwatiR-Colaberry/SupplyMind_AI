"""Orchestrates scenario simulation with a durable audit trail (STORY-008 / REQ-014).

Ties simulate_scenario() (simulation.py, pure computation) to
ScenarioSimulationAuditStore (audit_trail.py, persistence), the same way
root_cause/evaluator.py (STORY-007) ties analyze_root_cause() to
RootCauseAuditStore: the computation stays I/O-free and independently
testable, while this module is the one place that decides what gets
logged and durably recorded, and when.

Every call to run() gets exactly one audit record, whatever the outcome -
"audit trail not recorded for simulations" must never happen, mirroring
every other trust-spine implementation in this repo (STORY-007/011/012/013).

Three outcomes:
- "success": simulate_scenario() produced a ScenarioImpactAssessment.
- "invalid_input": simulate_scenario() raised ScenarioValidationError -
  AC2's "notify the user of errors" path for invalid scenario
  parameters - an expected, handled outcome, not a crash.
- "crashed": any other exception - the "simulation model failure" /
  "simulation API failure" / "data processing errors" failure paths this
  module does not attempt to interpret, only to audit and report back
  rather than let propagate uncaught.

If the audit store itself can't be written to (disk full, permissions),
that failure is never silently swallowed but also never masks a more
useful simulation-side failure that happened first - see _fail_run()'s
handling, identical in spirit to root_cause/evaluator.py's own.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

from scenario_simulation.audit_trail import ScenarioSimulationAuditStore, ScenarioSimulationAuditWriteError
from scenario_simulation.logging_setup import get_logger
from scenario_simulation.simulation import (
    ScenarioImpactAssessment,
    ScenarioInput,
    ScenarioValidationError,
    simulate_scenario,
)

logger = get_logger()

SimulationRunOutcome = Literal["success", "invalid_input", "crashed"]


def _input_parameters(scenario: ScenarioInput) -> dict[str, Any]:
    return {
        "demand_change_pct": scenario.demand_change_pct,
        "lead_time_change_days": scenario.lead_time_change_days,
        "safety_stock_change": scenario.safety_stock_change,
        "stock_change": scenario.stock_change,
    }


@dataclass
class ScenarioSimulationRun:
    """The full outcome of one ScenarioEvaluator.run() call."""

    simulation_id: str
    scenario: ScenarioInput
    impact: ScenarioImpactAssessment | None = None
    limitation: str | None = None  # set only when outcome == "invalid_input"
    crash_error: str | None = None

    @property
    def outcome(self) -> SimulationRunOutcome:
        if self.crash_error is not None:
            return "crashed"
        if self.limitation is not None:
            return "invalid_input"
        return "success"


class ScenarioEvaluator:
    """Runs simulate_scenario() for one scenario and audits the result, whatever it is."""

    def __init__(self, audit_store: ScenarioSimulationAuditStore) -> None:
        self._audit_store = audit_store

    def run(self, scenario: ScenarioInput, *, simulation_id: str | None = None) -> ScenarioSimulationRun:
        simulation_id = simulation_id or str(uuid.uuid4())
        sku = scenario.baseline.sku
        logger.info(
            "scenario_simulation_started",
            extra={
                "event": "scenario_simulation_started",
                "correlation_id": simulation_id,
                "context": {"scenario_name": scenario.scenario_name, "sku": sku},
            },
        )

        try:
            impact = simulate_scenario(scenario)
        except ScenarioValidationError as exc:
            return self._invalid_input(simulation_id, scenario, exc)
        except Exception as exc:  # noqa: BLE001 - deliberate: see module docstring
            return self._fail_run(simulation_id, scenario, exc)

        write_error = self._try_record(
            simulation_id=simulation_id,
            scenario_name=scenario.scenario_name,
            sku=sku,
            outcome="success",
            input_parameters=_input_parameters(scenario),
            risk_level_changed=impact.risk_level_changed,
            days_of_supply_delta=impact.days_of_supply_delta,
            detail=impact.detail,
        )
        if write_error is not None:
            return ScenarioSimulationRun(simulation_id=simulation_id, scenario=scenario, crash_error=str(write_error))

        logger.info(
            "scenario_simulation_completed",
            extra={
                "event": "scenario_simulation_completed",
                "outcome": "success",
                "correlation_id": simulation_id,
                "context": {
                    "scenario_name": scenario.scenario_name,
                    "sku": sku,
                    "risk_level_changed": impact.risk_level_changed,
                },
            },
        )
        return ScenarioSimulationRun(simulation_id=simulation_id, scenario=scenario, impact=impact)

    def _invalid_input(
        self, simulation_id: str, scenario: ScenarioInput, exc: ScenarioValidationError
    ) -> ScenarioSimulationRun:
        limitation = str(exc)
        logger.warning(
            "scenario_simulation_invalid_input",
            extra={
                "event": "scenario_simulation_invalid_input",
                "outcome": "failure",
                "error_class": exc.__class__.__name__,
                "correlation_id": simulation_id,
                "context": {"scenario_name": scenario.scenario_name, "sku": scenario.baseline.sku},
            },
        )
        write_error = self._try_record(
            simulation_id=simulation_id,
            scenario_name=scenario.scenario_name,
            sku=scenario.baseline.sku,
            outcome="failure",
            input_parameters=_input_parameters(scenario),
            detail=limitation,
        )
        if write_error is not None:
            return ScenarioSimulationRun(simulation_id=simulation_id, scenario=scenario, crash_error=str(write_error))
        return ScenarioSimulationRun(simulation_id=simulation_id, scenario=scenario, limitation=limitation)

    def _try_record(self, **kwargs: Any) -> ScenarioSimulationAuditWriteError | None:
        try:
            self._audit_store.record(**kwargs)
            return None
        except ScenarioSimulationAuditWriteError as exc:
            logger.error(
                "scenario_simulation_audit_write_failed",
                extra={
                    "event": "scenario_simulation_audit_write_failed",
                    "outcome": "failure",
                    "error_class": exc.__class__.__name__,
                    "correlation_id": kwargs.get("simulation_id"),
                    "context": {"scenario_name": kwargs.get("scenario_name"), "sku": kwargs.get("sku")},
                },
            )
            return exc

    def _fail_run(self, simulation_id: str, scenario: ScenarioInput, exc: Exception) -> ScenarioSimulationRun:
        logger.error(
            "scenario_simulation_failed",
            extra={
                "event": "scenario_simulation_failed",
                "outcome": "failure",
                "error_class": exc.__class__.__name__,
                "correlation_id": simulation_id,
                "context": {"scenario_name": scenario.scenario_name, "sku": scenario.baseline.sku},
            },
        )
        # If the audit write itself also fails here, _try_record already
        # logs that separately - the *original* exc is still the more
        # useful signal to hand back to the caller, so it is never
        # replaced or masked by a secondary audit-store exception. Same
        # reasoning as root_cause/evaluator.py's _fail_run().
        self._try_record(
            simulation_id=simulation_id,
            scenario_name=scenario.scenario_name,
            sku=scenario.baseline.sku,
            outcome="failure",
            input_parameters=_input_parameters(scenario),
            detail=f"{exc.__class__.__name__}: {exc}",
        )
        return ScenarioSimulationRun(simulation_id=simulation_id, scenario=scenario, crash_error=str(exc))
