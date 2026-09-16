"""Deterministic stockout-risk model (STORY-004 / REQ-006, REQ-011).

Pure computation - no I/O. Given one inventory position, predicts a
stockout risk level, a stockout probability, a 0-100 composite risk
score, and (when enough optional data is supplied) revenue at risk and a
recommended safety stock.

Per CLAUDE.md's core principle ("LLMs are probabilistic, production
systems must be deterministic"), every number here is plain arithmetic
or well-established inventory-theory statistics (a normal approximation
for lead-time demand, the same family of formula behind the classic
Z x sigma x sqrt(lead time) safety-stock rule) - never a call to an LLM,
a trained ML model, or a third-party forecasting API. `estimate_
stockout_probability`'s docstring is explicit that it is a statistical
formula standing in for a future trained model, not a prediction from one
- swapping in a real model later only needs to replace that one function.

Risk levels, in increasing severity - day-based per the product's stated
stockout-risk rules, not lead-time-relative like this module's previous
version (STORY-004's original design; see git history for that model if
it's ever needed for comparison):
    "critical" - current stock is already exhausted, expected inventory
                 at the end of the replenishment lead time is projected
                 negative, or fewer than CRITICAL_DAYS_THRESHOLD days of
                 supply remain
    "high"     - stockout expected within HIGH_DAYS_THRESHOLD days, or
                 expected inventory at lead-time end will sit below the
                 safety-stock floor even though it stays non-negative
    "medium"   - stockout expected within MEDIUM_DAYS_THRESHOLD days
    "low"      - more than MEDIUM_DAYS_THRESHOLD days of supply, and the
                 safety-stock floor holds through the lead time

All three day thresholds, the safety-stock service level, and the
composite risk-score weights are named module constants precisely so a
caller can tune them per the business's own risk tolerance instead of
treating them as universal supply-chain constants - the product spec
this rewrite implements says so explicitly.

daily_demand_rate is caller-supplied: either a turnover-derived average
(see compute_average_daily_demand()) or a forecasted-demand rate (e.g.
from STORY-003's forecast_demand(), divided into a daily figure by the
caller). This module doesn't care which source it came from - REQ-011's
"current and forecasted demand" distinction lives in the caller/agent
layer, not here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

RiskLevel = Literal["low", "medium", "high", "critical"]

# Day-based risk-level boundaries. Configurable: a business with faster
# turnover than the default assumption should tighten these, not treat
# them as fixed.
CRITICAL_DAYS_THRESHOLD = 7.0
HIGH_DAYS_THRESHOLD = 14.0
MEDIUM_DAYS_THRESHOLD = 30.0

# Z-score for a 95% service level - the conventional default for
# compute_recommended_safety_stock(); a caller wanting a different
# service level passes its own Z (e.g. 1.28 for 90%, 2.33 for 99%).
DEFAULT_SERVICE_LEVEL_Z = 1.65

# compute_risk_score()'s default weighting across the 5 signals the
# product spec names. Renormalized at call time over whichever signals
# are actually supplied (see compute_risk_score's own docstring) - a
# missing optional signal never silently drags the score toward a false
# neutral midpoint.
DEFAULT_RISK_SCORE_WEIGHTS: dict[str, float] = {
    "stockout_probability": 0.35,
    "days_of_supply": 0.25,
    "demand_volatility": 0.20,
    "supplier_lead_time_risk": 0.10,
    "incoming_shipment_risk": 0.10,
}


@dataclass(frozen=True)
class InventoryPosition:
    sku: str
    current_stock: float
    safety_stock: float
    daily_demand_rate: float
    lead_time_days: float
    # Optional - each degrades gracefully to "not computed" rather than
    # raising, so every existing caller supplying only the 5 original
    # fields keeps working unchanged.
    incoming_stock: float = 0.0  # confirmed inbound replenishment not yet received
    unit_price: float | None = None  # enables revenue_at_risk when known
    demand_std_dev: float | None = None  # enables stockout_probability's statistical mode and recommended_safety_stock
    supplier: str | None = None  # which supplier this item is sourced from - carried through unchanged, never used in the risk math itself


@dataclass(frozen=True)
class StockoutRiskAssessment:
    sku: str
    days_of_supply: float  # math.inf when daily_demand_rate is 0 (no consumption, no depletion)
    expected_inventory: float  # current + incoming - demand over the lead time; negative means a projected shortage
    incoming_stock: float  # carried forward from the input position - lets a downstream recommendation rule (e.g. recommendation/stockout_playbook.py) tell "nothing incoming" from "already ordered, just not enough"
    risk_level: RiskLevel
    confidence: float  # 0..1, how deep into the classified zone this position sits
    stockout_probability: float  # 0..1 - see estimate_stockout_probability()
    risk_score: float  # 0..100 composite - see compute_risk_score()
    revenue_at_risk: float | None  # None when the position has no unit_price
    recommended_safety_stock: float | None  # None when the position has no demand_std_dev
    supplier: str | None  # carried forward from the input position - see InventoryPosition.supplier
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


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _validate(position: InventoryPosition) -> None:
    # NaN fails every `< 0`/`<= 0` comparison below, so it must be
    # rejected explicitly first - otherwise it silently falls through as
    # "valid" and poisons every downstream comparison in
    # assess_stockout_risk with NaN. inventory_risk/data_quality.py
    # already rejects NaN rows before they reach this module in the
    # normal agent flow; this is the same guard applied here too, for a
    # caller that builds an InventoryPosition directly.
    required = (
        ("current_stock", position.current_stock),
        ("safety_stock", position.safety_stock),
        ("daily_demand_rate", position.daily_demand_rate),
        ("lead_time_days", position.lead_time_days),
        ("incoming_stock", position.incoming_stock),
    )
    optional = (
        ("unit_price", position.unit_price),
        ("demand_std_dev", position.demand_std_dev),
    )
    for name, value in required:
        if isinstance(value, float) and math.isnan(value):
            raise RiskModelError(f"{name} must be a real number, got NaN")
    for name, value in optional:
        if isinstance(value, float) and math.isnan(value):
            raise RiskModelError(f"{name} must be a real number or None, got NaN")
    if position.current_stock < 0:
        raise RiskModelError(f"current_stock must be >= 0, got {position.current_stock}")
    if position.safety_stock < 0:
        raise RiskModelError(f"safety_stock must be >= 0, got {position.safety_stock}")
    if position.daily_demand_rate < 0:
        raise RiskModelError(f"daily_demand_rate must be >= 0, got {position.daily_demand_rate}")
    if position.lead_time_days <= 0:
        raise RiskModelError(f"lead_time_days must be positive, got {position.lead_time_days}")
    if position.incoming_stock < 0:
        raise RiskModelError(f"incoming_stock must be >= 0, got {position.incoming_stock}")
    if position.unit_price is not None and position.unit_price < 0:
        raise RiskModelError(f"unit_price must be >= 0, got {position.unit_price}")
    if position.demand_std_dev is not None and position.demand_std_dev < 0:
        raise RiskModelError(f"demand_std_dev must be >= 0, got {position.demand_std_dev}")


def compute_average_daily_demand(units_sold: float, period_days: float = 30.0) -> float:
    """Average Daily Demand (ADD) = units sold over the window / window length.

    A convenience for a caller with raw order history and no pre-computed
    rate yet; assess_stockout_risk() itself takes daily_demand_rate as a
    given and doesn't call this - see InventoryPosition's own docstring.
    """
    if period_days <= 0:
        raise ValueError(f"period_days must be positive, got {period_days}")
    return units_sold / period_days


def compute_recommended_safety_stock(
    demand_std_dev: float, lead_time_days: float, service_z: float = DEFAULT_SERVICE_LEVEL_Z
) -> float:
    """Safety Stock = Z x demand-std-dev x sqrt(lead time) - the standard
    inventory-theory formula for the buffer needed to hit a target service
    level against demand variability during the replenishment window.

    This is a *recommendation* a caller can compare against the
    InventoryPosition's own (possibly stale, human-set) safety_stock
    field - it is not substituted into the risk classification
    automatically, since safety_stock is caller-supplied policy, not
    something this module overrides silently.
    """
    if demand_std_dev < 0:
        raise ValueError(f"demand_std_dev must be >= 0, got {demand_std_dev}")
    if lead_time_days <= 0:
        raise ValueError(f"lead_time_days must be positive, got {lead_time_days}")
    return service_z * demand_std_dev * math.sqrt(lead_time_days)


def estimate_stockout_probability(
    days_of_supply: float,
    lead_time_days: float,
    daily_demand_rate: float,
    demand_std_dev: float | None = None,
) -> float:
    """Probability that stock runs out before replenishment arrives.

    A statistical formula, not a trained model - a stand-in the product
    spec itself calls for "the ML/Data Science component" to eventually
    replace ("the ML model can later improve the prediction"); this
    function is written so that replacement only ever touches this one
    function, never its callers.

    With demand_std_dev supplied: models the actual days-of-supply as
    approximately normal around the point estimate, with a spread scaled
    by demand's coefficient of variation and sqrt(lead_time_days) - the
    same statistical family the safety-stock Z-formula above already
    uses - then returns P(actual days-of-supply <= lead_time_days) via
    the standard normal CDF (math.erf, no new dependency).

    Without it: a smooth logistic centered on coverage_ratio == 1.0 (the
    point where days_of_supply exactly equals the lead time) - lower
    coverage means higher probability, with no data-driven precision
    claimed since no variability data was given.
    """
    if math.isinf(days_of_supply):
        return 0.0
    if demand_std_dev is not None and demand_std_dev > 0 and daily_demand_rate > 0:
        coefficient_of_variation = demand_std_dev / daily_demand_rate
        sigma_days = coefficient_of_variation * math.sqrt(max(lead_time_days, 1e-9))
        if sigma_days <= 0:
            return 1.0 if days_of_supply <= lead_time_days else 0.0
        z = (lead_time_days - days_of_supply) / sigma_days
        return _clamp01(0.5 * (1.0 + math.erf(z / math.sqrt(2))))
    if lead_time_days <= 0:
        return 1.0
    coverage_ratio = days_of_supply / lead_time_days
    return _clamp01(1.0 / (1.0 + math.exp(4.0 * (coverage_ratio - 1.0))))


def compute_revenue_at_risk(expected_inventory: float, unit_price: float) -> float:
    """Revenue at Risk = expected unfulfilled units x selling price.

    expected_inventory < 0 is the projected shortage in units (see
    assess_stockout_risk's own expected_inventory field); a non-negative
    expected_inventory means no shortage is projected, so revenue at risk
    is 0, not negative.
    """
    if unit_price < 0:
        raise ValueError(f"unit_price must be >= 0, got {unit_price}")
    shortage_units = max(-expected_inventory, 0.0)
    return shortage_units * unit_price


def compute_risk_score(
    stockout_probability: float,
    days_of_supply: float,
    demand_volatility: float | None = None,
    supplier_lead_time_risk: float | None = None,
    incoming_shipment_risk: float | None = None,
    weights: dict[str, float] | None = None,
) -> float:
    """0-100 composite risk score blending up to 5 signals.

    Each optional signal (demand_volatility, supplier_lead_time_risk,
    incoming_shipment_risk - all 0..1, higher meaning riskier) is
    included only when supplied; the configured weight for any omitted
    signal is redistributed proportionally across the signals that were
    supplied, rather than silently treating a missing signal as a
    "neutral" 0.5 - a SKU with no known supplier-risk data shouldn't have
    its score quietly pulled toward the middle by a component that was
    never actually evaluated. stockout_probability and days_of_supply
    are always available (every InventoryPosition has enough to compute
    them), so the score is never based on fewer than those 2 signals.
    """
    weights = dict(weights or DEFAULT_RISK_SCORE_WEIGHTS)
    days_component = 0.0 if math.isinf(days_of_supply) else _clamp01(
        1.0 - min(days_of_supply, MEDIUM_DAYS_THRESHOLD * 2) / (MEDIUM_DAYS_THRESHOLD * 2)
    )
    components: dict[str, float] = {
        "stockout_probability": _clamp01(stockout_probability),
        "days_of_supply": days_component,
    }
    if demand_volatility is not None:
        components["demand_volatility"] = _clamp01(demand_volatility)
    if supplier_lead_time_risk is not None:
        components["supplier_lead_time_risk"] = _clamp01(supplier_lead_time_risk)
    if incoming_shipment_risk is not None:
        components["incoming_shipment_risk"] = _clamp01(incoming_shipment_risk)

    total_weight = sum(weights[name] for name in components)
    score = sum(components[name] * weights[name] for name in components) / total_weight
    return round(score * 100.0, 1)


def _confidence(
    risk_level: RiskLevel,
    days_of_supply: float,
    expected_inventory: float,
    current_stock: float,
    safety_stock: float,
    demand_during_lead_time: float,
) -> float:
    """How deep into its classified zone this position sits, 0..1.

    Branches by *why* the classification fired (a day-count boundary vs.
    the safety-stock-during-lead-time check) rather than one formula,
    since those are different units (days vs. units of stock) and a
    position can be classified by either depending on which condition
    tripped first - see assess_stockout_risk's own docstring for the
    exact rules.
    """
    if risk_level == "critical":
        if current_stock <= 0:
            return 1.0
        if expected_inventory < 0:
            return 1.0 if demand_during_lead_time <= 0 else _clamp01(-expected_inventory / demand_during_lead_time)
        return _clamp01((CRITICAL_DAYS_THRESHOLD - days_of_supply) / CRITICAL_DAYS_THRESHOLD)

    if risk_level == "high":
        if math.isinf(days_of_supply) or days_of_supply > HIGH_DAYS_THRESHOLD:
            # Driven purely by the safety-stock-during-lead-time check,
            # not by days_of_supply falling in the "high" day-bucket.
            gap = safety_stock - expected_inventory
            return _clamp01(gap / safety_stock) if safety_stock > 0 else 1.0
        half_width = (HIGH_DAYS_THRESHOLD - CRITICAL_DAYS_THRESHOLD) / 2.0
        distance = min(days_of_supply - CRITICAL_DAYS_THRESHOLD, HIGH_DAYS_THRESHOLD - days_of_supply)
        return _clamp01(distance / half_width)

    if risk_level == "medium":
        half_width = (MEDIUM_DAYS_THRESHOLD - HIGH_DAYS_THRESHOLD) / 2.0
        distance = min(days_of_supply - HIGH_DAYS_THRESHOLD, MEDIUM_DAYS_THRESHOLD - days_of_supply)
        return _clamp01(distance / half_width)

    # "low"
    if math.isinf(days_of_supply):
        return 1.0
    return _clamp01((days_of_supply - MEDIUM_DAYS_THRESHOLD) / MEDIUM_DAYS_THRESHOLD)


def assess_stockout_risk(position: InventoryPosition) -> StockoutRiskAssessment:
    """Classify one inventory position's stockout risk.

    Handles (raises RiskModelError for): negative stock/demand/incoming
    values, a negative unit_price/demand_std_dev, a non-positive lead
    time - values that make "days of supply" or "expected inventory"
    undefined rather than just low-confidence.

    Does not handle: whether the input values are themselves
    trustworthy (stale snapshot, missing field defaulted to 0) - that is
    inventory_risk/data_quality.py's job, run by the caller before this.
    """
    _validate(position)

    has_demand = position.daily_demand_rate > 0
    days_of_supply = position.current_stock / position.daily_demand_rate if has_demand else math.inf
    demand_during_lead_time = position.daily_demand_rate * position.lead_time_days
    expected_inventory = position.current_stock + position.incoming_stock - demand_during_lead_time

    if position.current_stock <= 0 or expected_inventory < 0 or days_of_supply <= CRITICAL_DAYS_THRESHOLD:
        risk_level: RiskLevel = "critical"
    elif days_of_supply <= HIGH_DAYS_THRESHOLD or expected_inventory < position.safety_stock:
        risk_level = "high"
    elif days_of_supply <= MEDIUM_DAYS_THRESHOLD:
        risk_level = "medium"
    else:
        risk_level = "low"

    confidence = _confidence(
        risk_level, days_of_supply, expected_inventory, position.current_stock, position.safety_stock, demand_during_lead_time
    )

    stockout_probability = estimate_stockout_probability(
        days_of_supply, position.lead_time_days, position.daily_demand_rate, position.demand_std_dev
    )

    demand_volatility = None
    if position.demand_std_dev is not None and has_demand:
        # Coefficient of variation, clamped to [0, 1] as a "volatility
        # score" - a demand_std_dev equal to or exceeding the mean itself
        # reads as maximally volatile (1.0) rather than an unbounded
        # ratio that would need its own separate scale downstream.
        demand_volatility = _clamp01(position.demand_std_dev / position.daily_demand_rate)

    risk_score = compute_risk_score(stockout_probability, days_of_supply, demand_volatility=demand_volatility)

    revenue_at_risk = (
        compute_revenue_at_risk(expected_inventory, position.unit_price) if position.unit_price is not None else None
    )
    recommended_safety_stock = (
        compute_recommended_safety_stock(position.demand_std_dev, position.lead_time_days)
        if position.demand_std_dev is not None
        else None
    )

    detail_parts = [
        f"{days_of_supply:.1f} day(s) of supply against a {position.lead_time_days:.1f}-day lead time"
        if has_demand
        else f"no measurable demand; current stock {position.current_stock:.1f}",
        f"expected inventory at lead-time end {expected_inventory:.1f} (safety stock {position.safety_stock:.1f})",
        f"stockout probability {stockout_probability:.0%}",
    ]
    if revenue_at_risk is not None and revenue_at_risk > 0:
        detail_parts.append(f"revenue at risk ${revenue_at_risk:,.2f}")
    detail = "; ".join(detail_parts)

    return StockoutRiskAssessment(
        sku=position.sku,
        days_of_supply=days_of_supply,
        expected_inventory=expected_inventory,
        incoming_stock=position.incoming_stock,
        risk_level=risk_level,
        confidence=confidence,
        stockout_probability=stockout_probability,
        risk_score=risk_score,
        revenue_at_risk=revenue_at_risk,
        recommended_safety_stock=recommended_safety_stock,
        supplier=position.supplier,
        detail=detail,
    )
