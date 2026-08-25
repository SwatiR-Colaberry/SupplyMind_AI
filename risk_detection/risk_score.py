"""Unified Supply Chain Risk Score (STORY-005 / REQ-012).

Pure computation - no I/O. Combines demand-spike/drop anomalies
(anomaly_detection.detect_demand_spikes), supplier-delay anomalies
(anomaly_detection.detect_supplier_delays), and, optionally, STORY-004's
per-SKU stockout-risk assessments
(inventory_risk.risk_model.assess_stockout_risk) into one 0-100 score
with a severity level and a plain-language explanation of which factors
drove it - REQ-012's "explain why it is high."

Per CLAUDE.md's core principle ("LLMs are probabilistic, production
systems must be deterministic"), the score is a fixed weighted sum over
each input's own severity classification, not a model call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from inventory_risk.risk_model import StockoutRiskAssessment
from risk_detection.anomaly_detection import DemandAnomaly, SupplierDelayAnomaly

RiskScoreSeverity = Literal["low", "medium", "high", "critical"]
RiskContributionSource = Literal["demand_anomaly", "supplier_delay", "stockout_risk"]

# Points contributed per factor, by that factor's own severity
# classification. Demand anomalies and supplier delays share a scale
# (both use anomaly_detection.py's medium/high/critical zones);
# stockout risk uses inventory_risk's four-level scale. A "low" stockout
# risk contributes nothing and a "medium" one contributes little - a
# single SKU with ordinary, expected inventory coverage isn't itself
# evidence of elevated supply-chain risk; flagging imminent stockouts on
# their own is STORY-004's job, not this module's.
DEMAND_ANOMALY_POINTS: dict[str, float] = {"critical": 30.0, "high": 20.0, "medium": 10.0}
SUPPLIER_DELAY_POINTS: dict[str, float] = {"critical": 30.0, "high": 20.0, "medium": 10.0}
STOCKOUT_RISK_POINTS: dict[str, float] = {"critical": 25.0, "high": 15.0, "medium": 5.0, "low": 0.0}

MAX_SCORE = 100.0

# Score-bucket severity, used only to let several lower-severity factors
# *compound* into a higher overall severity (e.g. four independent
# "medium" factors reads as more alarming than any one of them alone).
# Checked highest-first.
_SCORE_SEVERITY_THRESHOLDS: list[tuple[float, RiskScoreSeverity]] = [
    (60.0, "critical"),
    (35.0, "high"),
    (10.0, "medium"),
]
_DEFAULT_SEVERITY: RiskScoreSeverity = "low"
_SEVERITY_RANK: dict[RiskScoreSeverity, int] = {"low": 0, "medium": 1, "high": 2, "critical": 3}

# Explanation names at most this many contributing factors, most severe
# first - REQ-012 asks the score to explain why it's high, not to dump
# every input verbatim.
MAX_EXPLANATION_FACTORS = 5


@dataclass(frozen=True)
class RiskContribution:
    source: RiskContributionSource
    identifier: str  # period (demand), po_id (supplier delay), or sku (stockout)
    severity: str
    points: float
    detail: str


@dataclass(frozen=True)
class SupplyChainRiskScore:
    score: float  # 0..100, MAX_SCORE-capped sum of every contribution's points
    severity: RiskScoreSeverity
    explanation: str
    contributions: list[RiskContribution]


def _score_bucket_severity(score: float) -> RiskScoreSeverity:
    for threshold, label in _SCORE_SEVERITY_THRESHOLDS:
        if score >= threshold:
            return label
    return _DEFAULT_SEVERITY


def _overall_severity(contributions: list[RiskContribution], score: float) -> RiskScoreSeverity:
    # The final severity is never *milder* than any single contributing
    # factor's own severity - a lone critical factor must read as
    # critical overall even if its point value alone sits under the
    # score-bucket threshold. The score bucket exists to let several
    # lesser factors compound past what any one of them implies alone.
    candidates = [_score_bucket_severity(score)] + [c.severity for c in contributions]
    return max(candidates, key=lambda s: _SEVERITY_RANK.get(s, 0))


def _demand_contributions(anomalies: list[DemandAnomaly]) -> list[RiskContribution]:
    return [
        RiskContribution(
            source="demand_anomaly",
            identifier=a.period,
            severity=a.severity,
            points=DEMAND_ANOMALY_POINTS.get(a.severity, 0.0),
            detail=f"demand {a.direction} in {a.period}: {a.detail}",
        )
        for a in anomalies
    ]


def _supplier_contributions(delays: list[SupplierDelayAnomaly]) -> list[RiskContribution]:
    return [
        RiskContribution(
            source="supplier_delay",
            identifier=d.po_id,
            severity=d.severity,
            points=SUPPLIER_DELAY_POINTS.get(d.severity, 0.0),
            detail=f"supplier delay on {d.po_id}: {d.detail}",
        )
        for d in delays
    ]


def _stockout_contributions(assessments: list[StockoutRiskAssessment]) -> list[RiskContribution]:
    contributions = []
    for a in assessments:
        points = STOCKOUT_RISK_POINTS.get(a.risk_level, 0.0)
        if points <= 0:
            continue
        contributions.append(
            RiskContribution(
                source="stockout_risk",
                identifier=a.sku,
                severity=a.risk_level,
                points=points,
                detail=f"stockout risk for {a.sku}: {a.detail}",
            )
        )
    return contributions


def _explanation(contributions: list[RiskContribution], score: float, severity: RiskScoreSeverity) -> str:
    if not contributions:
        return f"Supply chain risk score {score:.0f}/100 (low): no anomalies or elevated risks detected."

    ordered = sorted(contributions, key=lambda c: c.points, reverse=True)
    top = ordered[:MAX_EXPLANATION_FACTORS]
    factors_text = "; ".join(c.detail for c in top)
    remainder = len(ordered) - len(top)
    remainder_text = f" (+{remainder} more contributing factor(s))" if remainder > 0 else ""
    return f"Supply chain risk score {score:.0f}/100 ({severity}): {factors_text}{remainder_text}"


def compute_risk_score(
    demand_anomalies: list[DemandAnomaly] | None = None,
    supplier_delays: list[SupplierDelayAnomaly] | None = None,
    stockout_assessments: list[StockoutRiskAssessment] | None = None,
) -> SupplyChainRiskScore:
    """Combine anomaly/risk inputs from any subset of the three sources into one score.

    Handles: any input omitted or empty - contributes nothing, not an
    error. All three omitted returns a "low" severity, zero score with an
    explicit no-risk explanation, not an empty/ambiguous result.

    Does not handle: whether the inputs themselves are trustworthy - that
    is each detector's/model's own job (already run before this).
    """
    contributions = [
        *_demand_contributions(demand_anomalies or []),
        *_supplier_contributions(supplier_delays or []),
        *_stockout_contributions(stockout_assessments or []),
    ]

    score = min(MAX_SCORE, sum(c.points for c in contributions))
    severity = _overall_severity(contributions, score)

    return SupplyChainRiskScore(
        score=score,
        severity=severity,
        explanation=_explanation(contributions, score, severity),
        contributions=sorted(contributions, key=lambda c: c.points, reverse=True),
    )
