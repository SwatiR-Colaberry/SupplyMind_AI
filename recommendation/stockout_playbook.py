"""Deterministic stockout-recommendation rules (product spec's "AI Recommendation
Rules" section).

Pure computation - no I/O, no LLM call. Sibling to recommendation/synthesis.py,
not a replacement for it: synthesis.py combines *multiple agents'* outputs
into one RecommendationSet plus conflict detection; this module answers a
narrower, more specific question - "given one SKU's own stockout-risk
assessment (and what's known about its supplier/demand context), what
should someone actually do about it" - with the exact fixed rule set the
product spec names, in priority order, rather than a generic per-agent
recommendation pass-through.

The spec is explicit that "the AI should not invent the action" - every
StructuredRecommendation traces back to one of the named rules below, and
every one carries its own evidence (`reason`) rather than a bare verdict,
matching the spec's own "Risk -> Evidence -> Calculation -> Recommendation
-> Confidence" flow (risk/evidence/calculation already live on the
StockoutRiskAssessment this module consumes; `problem`/`recommendation`/
`reason`/`confidence` below are what this module adds on top of it).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from inventory_risk.risk_model import StockoutRiskAssessment

DemandTrend = Literal["increasing", "stable", "decreasing"]


@dataclass(frozen=True)
class StructuredRecommendation:
    problem: str
    recommendation: str
    reason: str
    confidence: float  # 0..1


def _fmt_days(days: float) -> str:
    return "no measurable consumption" if math.isinf(days) else f"{days:.1f} day(s) of supply"


def build_stockout_recommendation(
    assessment: StockoutRiskAssessment,
    has_supplier_delay: bool = False,
    demand_trend: DemandTrend | None = None,
) -> StructuredRecommendation:
    """One StructuredRecommendation for one SKU's StockoutRiskAssessment.

    Rule priority (first match wins - see module docstring for why this is
    a fixed, ordered rule set rather than a scored/blended decision):
    1. critical, with no confirmed incoming stock -> immediate replenishment review
    2. critical, but replenishment is already incoming -> expedite that shipment
    3. high risk, with a known supplier delay -> review an alternative supplier/incoming shipment
    4. (high or critical) risk with an increasing demand trend -> expedited replenishment
    5. high risk, none of the above -> replenish soon / review the reorder point
    6. medium risk -> monitor and review reorder timing
    7. low risk -> no action required

    `has_supplier_delay`/`demand_trend` are optional context this function
    doesn't compute itself - a caller wires them in from
    risk_detection.anomaly_detection's own SupplierDelayAnomaly/
    DemandAnomaly signals (the same signals root_cause/analysis.py already
    consumes), so this module stays a pure decision table over already-
    computed facts, never a data source of its own.
    """
    problem = (
        f"{assessment.sku} is {assessment.risk_level} risk "
        f"({_fmt_days(assessment.days_of_supply)}, {assessment.stockout_probability:.0%} stockout probability)"
    )
    if assessment.revenue_at_risk:
        problem += f", ${assessment.revenue_at_risk:,.2f} revenue at risk"

    if assessment.risk_level == "critical" and assessment.incoming_stock <= 0:
        return StructuredRecommendation(
            problem=problem,
            recommendation="Replenish within days - start an immediate replenishment review.",
            reason=(
                f"{assessment.detail}; no confirmed incoming stock is covering the shortfall"
            ),
            confidence=assessment.stockout_probability,
        )
    if assessment.risk_level == "critical":
        return StructuredRecommendation(
            problem=problem,
            recommendation="Expedite the incoming shipment - it may not arrive in time.",
            reason=f"{assessment.detail}; {assessment.incoming_stock:.0f} unit(s) are already incoming but may not close the gap fast enough",
            confidence=assessment.stockout_probability,
        )
    if assessment.risk_level == "high" and has_supplier_delay:
        return StructuredRecommendation(
            problem=problem,
            recommendation="Review an alternative supplier or expedite the incoming shipment.",
            reason=f"{assessment.detail}; the usual supplier has a recent delivery delay on record",
            confidence=assessment.stockout_probability,
        )
    if assessment.risk_level in ("high", "critical") and demand_trend == "increasing":
        return StructuredRecommendation(
            problem=problem,
            recommendation="Recommend expedited replenishment.",
            reason=f"{assessment.detail}; demand is trending upward, which will shorten this further",
            confidence=assessment.stockout_probability,
        )
    if assessment.risk_level == "high":
        return StructuredRecommendation(
            problem=problem,
            recommendation="Replenish soon - review the reorder point for this SKU.",
            reason=assessment.detail,
            confidence=assessment.stockout_probability,
        )
    if assessment.risk_level == "medium":
        return StructuredRecommendation(
            problem=problem,
            recommendation="Monitor and confirm the next reorder is timed correctly.",
            reason=assessment.detail,
            confidence=assessment.stockout_probability,
        )

    return StructuredRecommendation(
        problem=problem,
        recommendation="No action required.",
        reason=assessment.detail,
        confidence=assessment.confidence,
    )


def describe_recommendation(rec: StructuredRecommendation) -> str:
    """One line matching the product spec's own worked-example shape:
    "Problem: ... | Recommendation: ... | Reason: ... | Confidence: 91%"."""
    return (
        f"Problem: {rec.problem} | Recommendation: {rec.recommendation} | "
        f"Reason: {rec.reason} | Confidence: {rec.confidence:.0%}"
    )
