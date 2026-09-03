"""Deterministic scenario simulation for supply chain what-if analysis (STORY-008 / REQ-014).

Pure computation - no I/O. Reuses inventory_risk/risk_model.py's
assess_stockout_risk() (STORY-004) as the underlying model rather than
inventing a new one: a scenario is a baseline InventoryPosition plus a
set of deltas (demand change %, lead time change, safety stock change,
stock change), and the "impact" the caller asked for is the difference
between assess_stockout_risk() run on the baseline and on the position
those deltas produce.

Per CLAUDE.md's core principle ("LLMs are probabilistic, production
systems must be deterministic"), this is arithmetic composed from an
already-audited deterministic model, not a call to an LLM or a
third-party simulation API.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from inventory_risk.risk_model import (
    InventoryPosition,
    RiskModelError,
    StockoutRiskAssessment,
    assess_stockout_risk,
)

_RISK_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


class ScenarioValidationError(ValueError):
    """Raised when a scenario's inputs can't produce a meaningful simulation.

    This is AC2's "invalid scenario parameters" failure path - checked
    before/around the simulation itself, so an invalid scenario is
    rejected outright rather than silently producing a nonsensical
    impact assessment (e.g. a projected negative stock level).
    """


@dataclass(frozen=True)
class ScenarioInput:
    """A baseline inventory position plus the deltas defining the what-if scenario.

    Deltas default to 0/0.0 (no change) so a caller can vary only the
    dimension they care about. demand_change_pct is a fraction (0.2 ==
    +20%), not a percentage point; -1.0 (demand halting entirely) is a
    valid, already-modeled scenario (see assess_stockout_risk's
    has_demand=False branch) - only a delta that would drive demand
    negative (< -1.0) is rejected.
    """

    scenario_name: str
    baseline: InventoryPosition
    demand_change_pct: float = 0.0
    lead_time_change_days: float = 0.0
    safety_stock_change: float = 0.0
    stock_change: float = 0.0


@dataclass(frozen=True)
class ScenarioImpactAssessment:
    scenario_name: str
    baseline: StockoutRiskAssessment
    projected: StockoutRiskAssessment
    risk_level_changed: bool
    days_of_supply_delta: float  # projected - baseline; math.nan when either side is math.inf (undefined)
    detail: str


def _apply_deltas(scenario: ScenarioInput) -> InventoryPosition:
    baseline = scenario.baseline
    return InventoryPosition(
        sku=baseline.sku,
        current_stock=baseline.current_stock + scenario.stock_change,
        safety_stock=baseline.safety_stock + scenario.safety_stock_change,
        daily_demand_rate=baseline.daily_demand_rate * (1.0 + scenario.demand_change_pct),
        lead_time_days=baseline.lead_time_days + scenario.lead_time_change_days,
    )


def simulate_scenario(scenario: ScenarioInput) -> ScenarioImpactAssessment:
    """Simulate one what-if scenario against its baseline inventory position.

    Handles (raises ScenarioValidationError for): a blank scenario_name,
    a demand_change_pct below -1.0 (would make demand negative), and any
    delta combination that produces a projected position
    assess_stockout_risk() itself rejects (negative stock, non-positive
    lead time) - surfaced here as a scenario-input problem rather than a
    generic model failure, since the baseline itself was valid.

    Does not handle: whether the baseline InventoryPosition itself is
    valid - that is assess_stockout_risk()'s own concern, and a caller
    building an invalid baseline gets RiskModelError evaluating it, same
    as any other caller of that function.
    """
    if not scenario.scenario_name or not scenario.scenario_name.strip():
        raise ScenarioValidationError("scenario_name must be non-empty")
    if scenario.demand_change_pct < -1.0:
        raise ScenarioValidationError(
            f"demand_change_pct must be >= -1.0 (demand cannot go negative), got {scenario.demand_change_pct}"
        )

    baseline_assessment = assess_stockout_risk(scenario.baseline)

    projected_position = _apply_deltas(scenario)
    try:
        projected_assessment = assess_stockout_risk(projected_position)
    except RiskModelError as exc:
        raise ScenarioValidationError(
            f"scenario {scenario.scenario_name!r} produces an invalid projected position: {exc}"
        ) from exc

    if math.isinf(baseline_assessment.days_of_supply) or math.isinf(projected_assessment.days_of_supply):
        days_of_supply_delta = math.nan
    else:
        days_of_supply_delta = projected_assessment.days_of_supply - baseline_assessment.days_of_supply

    risk_level_changed = baseline_assessment.risk_level != projected_assessment.risk_level
    if not risk_level_changed:
        direction = "leaves unchanged"
    elif _RISK_SEVERITY_RANK[projected_assessment.risk_level] > _RISK_SEVERITY_RANK[baseline_assessment.risk_level]:
        direction = "worsens"
    else:
        direction = "improves"
    detail = (
        f"scenario {direction} risk from {baseline_assessment.risk_level} to {projected_assessment.risk_level} "
        f"for {scenario.baseline.sku} ({projected_assessment.detail})"
    )

    return ScenarioImpactAssessment(
        scenario_name=scenario.scenario_name,
        baseline=baseline_assessment,
        projected=projected_assessment,
        risk_level_changed=risk_level_changed,
        days_of_supply_delta=days_of_supply_delta,
        detail=detail,
    )
