"""Deterministic stockout-risk model (STORY-004 / REQ-006, REQ-011).

Pure computation - no I/O. Given one inventory position (current stock,
safety stock, a daily demand rate, and a replenishment lead time),
predicts a stockout risk level and a confidence in that classification.

Per CLAUDE.md's core principle ("LLMs are probabilistic, production
systems must be deterministic"), this is a plain arithmetic model over
supply-chain fundamentals (days of supply vs. lead time vs. safety
stock), not a call to an LLM or a third-party forecasting API.

Risk levels, in increasing severity:
    "low"      - stock covers at least MEDIUM_COVERAGE_RATIO lead times
    "medium"   - stock covers at least one lead time but less than
                 MEDIUM_COVERAGE_RATIO
    "high"     - stock will be exhausted before replenishment arrives
                 (fewer than one lead time of stock remaining), but the
                 safety-stock floor hasn't been breached yet
    "critical" - current stock is already at or below the safety-stock
                 floor

daily_demand_rate is caller-supplied: either a turnover-derived average
or a forecasted-demand rate (e.g. from STORY-003's forecast_demand(),
divided into a daily figure by the caller). This module doesn't care
which source it came from - REQ-011's "current and forecasted demand"
distinction lives in the caller/agent layer, not here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

RiskLevel = Literal["low", "medium", "high", "critical"]

MEDIUM_COVERAGE_RATIO = 1.5  # days_of_supply / lead_time_days threshold between "medium" and "low"


@dataclass(frozen=True)
class InventoryPosition:
    sku: str
    current_stock: float
    safety_stock: float
    daily_demand_rate: float
    lead_time_days: float


@dataclass(frozen=True)
class StockoutRiskAssessment:
    sku: str
    days_of_supply: float  # math.inf when daily_demand_rate is 0 (no consumption, no depletion)
    risk_level: RiskLevel
    confidence: float  # 0..1, distance from the nearest risk-level boundary, normalized by lead time
    detail: str


class RiskModelError(ValueError):
    """Raised when an InventoryPosition's values can't support a risk assessment.

    Not the same failure path as "corrupted data"
    (inventory_risk/data_quality.py's job, checked by the caller before
    this runs) - this is a narrower guard against values that make the
    arithmetic itself meaningless (negative stock, a non-positive lead
    time), the "incorrect risk thresholds" / "model prediction errors"
    failure paths.
    """


def _validate(position: InventoryPosition) -> None:
    # NaN fails every `< 0`/`<= 0` comparison below, so it must be
    # rejected explicitly first - otherwise it silently falls through as
    # "valid" and poisons every downstream comparison in
    # assess_stockout_risk with NaN. inventory_risk/data_quality.py
    # already rejects NaN rows before they reach this module in the
    # normal agent flow; this is the same guard applied here too, for a
    # caller that builds an InventoryPosition directly.
    for name, value in (
        ("current_stock", position.current_stock),
        ("safety_stock", position.safety_stock),
        ("daily_demand_rate", position.daily_demand_rate),
        ("lead_time_days", position.lead_time_days),
    ):
        if isinstance(value, float) and math.isnan(value):
            raise RiskModelError(f"{name} must be a real number, got NaN")
    if position.current_stock < 0:
        raise RiskModelError(f"current_stock must be >= 0, got {position.current_stock}")
    if position.safety_stock < 0:
        raise RiskModelError(f"safety_stock must be >= 0, got {position.safety_stock}")
    if position.daily_demand_rate < 0:
        raise RiskModelError(f"daily_demand_rate must be >= 0, got {position.daily_demand_rate}")
    if position.lead_time_days <= 0:
        raise RiskModelError(f"lead_time_days must be positive, got {position.lead_time_days}")


def assess_stockout_risk(position: InventoryPosition) -> StockoutRiskAssessment:
    """Classify one inventory position's stockout risk.

    Handles (raises RiskModelError for): negative stock/demand values, a
    non-positive lead time - values that make "days of supply" or "lead
    time coverage" undefined rather than just low-confidence.

    Does not handle: whether the input values are themselves
    trustworthy (stale snapshot, missing field defaulted to 0) - that is
    inventory_risk/data_quality.py's job, run by the caller before this.
    """
    _validate(position)

    has_demand = position.daily_demand_rate > 0
    days_of_supply = position.current_stock / position.daily_demand_rate if has_demand else math.inf
    coverage_ratio = days_of_supply / position.lead_time_days if has_demand else math.inf
    medium_threshold_days = position.lead_time_days * MEDIUM_COVERAGE_RATIO

    # boundary_distance_days is the distance (in days-of-supply terms) to the
    # *nearest* edge of the current risk zone, so confidence reflects how deep
    # into the zone a position sits rather than only how close it is to one
    # particular edge. `normalizer` scales that distance to 0..1 using each
    # zone's own width, since the zones aren't the same width: "medium" is
    # only MEDIUM_COVERAGE_RATIO - 1 (0.5) lead times wide, so normalizing it
    # by the full lead time like the open-ended zones would cap its
    # confidence at ~0.25 even dead-center in the zone.
    if position.current_stock <= position.safety_stock:
        risk_level: RiskLevel = "critical"
        boundary_distance_days = position.safety_stock - position.current_stock
        normalizer = position.lead_time_days
    elif coverage_ratio < 1.0:
        risk_level = "high"
        # "high" is bounded on both sides: the critical floor below, and the
        # medium threshold above. distance_to_critical converts the
        # stock-vs-safety_stock gap into the same days-of-supply units as
        # distance_to_medium so the two are comparable.
        distance_to_critical = (position.current_stock - position.safety_stock) / position.daily_demand_rate
        distance_to_medium = position.lead_time_days - days_of_supply
        boundary_distance_days = min(distance_to_critical, distance_to_medium)
        normalizer = position.lead_time_days
    elif coverage_ratio < MEDIUM_COVERAGE_RATIO:
        risk_level = "medium"
        boundary_distance_days = min(days_of_supply - position.lead_time_days, medium_threshold_days - days_of_supply)
        normalizer = (medium_threshold_days - position.lead_time_days) / 2
    else:
        risk_level = "low"
        boundary_distance_days = days_of_supply - medium_threshold_days
        normalizer = position.lead_time_days

    confidence = 1.0 if math.isinf(boundary_distance_days) else max(0.0, min(1.0, boundary_distance_days / normalizer))

    detail = (
        f"{days_of_supply:.1f} day(s) of supply against a {position.lead_time_days:.1f}-day lead time "
        f"(safety stock {position.safety_stock:.1f})"
        if has_demand
        else f"no measurable demand; current stock {position.current_stock:.1f}, safety stock {position.safety_stock:.1f}"
    )

    return StockoutRiskAssessment(
        sku=position.sku,
        days_of_supply=days_of_supply,
        risk_level=risk_level,
        confidence=confidence,
        detail=detail,
    )
