"""Deterministic supplier reliability evaluation (STORY-013 / REQ-007).

Pure computation - no I/O. Given raw delivery rows (the same row shape
risk_detection/anomaly_detection.py's detect_supplier_delays already
consumes - po_id, expected_date, actual_date, supplier), groups them by
supplier and computes each supplier's delivery-performance metrics and a
0-100 Supplier Risk Score with severity, a flagged-for-review verdict,
and a plain-language explanation.

Per CLAUDE.md's core principle ("LLMs are probabilistic, production
systems must be deterministic"), the score is a fixed weighted sum over
per-delivery delay severity, not a model call - the same convention
risk_detection/risk_score.py already uses (0 = no risk, 100 = max risk;
higher is worse), kept consistent here so a score from either module
means the same thing to a reader.

This module deliberately reuses detect_supplier_delays for per-row
validation, date parsing, and delay-severity scoring rather than
reimplementing any of it - grouping by supplier first and calling it once
per supplier gives every supplier's on-time/late split "for free" from
logic STORY-005 already hardened, instead of a second, potentially
drifting copy of the same rules.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Literal

from risk_detection.anomaly_detection import (
    FlaggedDeliveryRow,
    SupplierDelayAnomaly,
    detect_supplier_delays,
)

RiskSeverity = Literal["low", "medium", "high", "critical"]

# Points added to a supplier's risk score per late delivery, by that
# delivery's own severity (detect_supplier_delays already classifies
# each delay this way) - mirrors risk_detection/risk_score.py's
# per-factor point scale so a "critical" delay means the same magnitude
# of risk whether it's read off a single PO or a supplier's aggregate
# score.
LATE_DELIVERY_PENALTY: dict[str, float] = {"critical": 30.0, "high": 20.0, "medium": 10.0}
MAX_SCORE = 100.0

# Score-bucket severity - intentionally mirrors
# risk_detection/risk_score.py's own bucket shape (not imported: that
# module scores a single combined risk snapshot, this one scores a
# supplier's aggregate reliability - two copies of a 3-line lookup is
# still below CLAUDE.md's "three strikes" extraction threshold).
_SCORE_SEVERITY_THRESHOLDS: list[tuple[float, RiskSeverity]] = [
    (60.0, "critical"),
    (35.0, "high"),
    (10.0, "medium"),
]
_DEFAULT_SEVERITY: RiskSeverity = "low"

# A supplier scored "high" or "critical" (score >= the "high" bucket
# floor) is unreliable enough to flag for review - this is what AC2
# ("given unreliable supplier data ... flags the supplier for review")
# means in score terms.
FLAG_SCORE_THRESHOLD = 35.0

# Logged assumption: below this many *valid* delivery records, a
# supplier's on-time rate is too thin a sample to trust - one late
# delivery out of two looks identical to "unreliable half the time" and
# to "one bad week." Rather than report a confident-looking score off
# too little data (the "Incorrect Supplier Risk Score" failure path),
# the score is still computed from what's there but the supplier is
# flagged for review with the sample size named as the reason.
MIN_DELIVERIES_FOR_CONFIDENT_SCORE = 3

# Logged assumption: below this on-time rate, a supplier reads as
# unreliable by ordinary meaning even if every individual delay was only
# "medium" severity (and so alone wouldn't push the risk score past
# FLAG_SCORE_THRESHOLD) - persistent minor lateness is still a
# reliability problem REQ-007 asks this module to surface.
LOW_ON_TIME_RATE_THRESHOLD = 0.7

DEFAULT_DELAY_THRESHOLD_DAYS = 2.0


class SupplierEvaluationError(ValueError):
    """Raised when evaluate_supplier_reliability's own parameters can't support evaluation.

    Not the same failure path as a malformed delivery row (flagged, not
    raised - see FlaggedDeliveryRow, reused from detect_supplier_delays)
    or a supplier with zero valid deliveries (also flagged, not raised -
    see SupplierRiskScore.flag_reasons) - this is a caller/parameter bug,
    the same division risk_detection/anomaly_detection.py draws between a
    bad delay_threshold_days and a bad data row.
    """


@dataclass(frozen=True)
class SupplierReliabilityMetrics:
    supplier: str
    deliveries_evaluated: int  # valid (complete, parseable) delivery rows only
    on_time_count: int
    late_count: int
    on_time_rate: float | None  # None when deliveries_evaluated == 0 - no rate is measurable
    avg_delay_days: float  # 0.0 when late_count == 0
    max_delay_days: int  # 0 when late_count == 0


@dataclass(frozen=True)
class SupplierRiskScore:
    supplier: str
    score: float  # 0..100, MAX_SCORE-capped; higher = more risk
    severity: RiskSeverity
    flagged_for_review: bool
    flag_reasons: list[str]
    explanation: str
    metrics: SupplierReliabilityMetrics
    delay_anomalies: list[SupplierDelayAnomaly]
    invalid_rows: list[FlaggedDeliveryRow]


@dataclass(frozen=True)
class SupplierEvaluationReport:
    scores: list[SupplierRiskScore]  # sorted by score descending - riskiest supplier first
    unattributable_rows: list[dict[str, Any]]  # rows with no usable supplier identifier
    warnings: list[str] = field(default_factory=list)


def _score_bucket_severity(score: float) -> RiskSeverity:
    for threshold, label in _SCORE_SEVERITY_THRESHOLDS:
        if score >= threshold:
            return label
    return _DEFAULT_SEVERITY


def _supplier_key(row: dict[str, Any]) -> str | None:
    # Unlike detect_supplier_delays' po_id handling (where a falsy-but-
    # present value like integer 0 is a legitimate identifier), a
    # supplier name has no meaningful falsy-but-valid form - an empty or
    # whitespace-only string can't be grouped on, so it's treated the
    # same as a missing field rather than becoming its own bogus
    # "supplier".
    value = row.get("supplier")
    if value is None:
        return None
    key = str(value).strip()
    return key or None


def _explain(
    supplier: str,
    score: float,
    severity: RiskSeverity,
    metrics: SupplierReliabilityMetrics,
    delays: list[SupplierDelayAnomaly],
    flag_reasons: list[str],
) -> str:
    if metrics.on_time_rate is None:
        rate_text = "no verifiable on-time rate"
    else:
        rate_text = f"{metrics.on_time_rate:.0%} on-time ({metrics.on_time_count}/{metrics.deliveries_evaluated})"

    detail = f"Supplier risk score {score:.0f}/100 ({severity}) for {supplier}: {rate_text}"
    if delays:
        worst = max(delays, key=lambda d: d.delay_days)
        detail += f"; worst delay {worst.delay_days} day(s) late on {worst.po_id}"
    if flag_reasons:
        detail += f". Flagged for review: {'; '.join(flag_reasons)}"
    return detail


def _evaluate_one_supplier(
    supplier: str,
    rows: list[dict[str, Any]],
    delay_threshold_days: float,
    min_deliveries_for_confidence: int,
) -> SupplierRiskScore:
    delay_report = detect_supplier_delays(rows, delay_threshold_days=delay_threshold_days)
    valid_deliveries = len(rows) - len(delay_report.flagged_rows)
    late_count = len(delay_report.delays)
    on_time_count = max(0, valid_deliveries - late_count)
    on_time_rate = (on_time_count / valid_deliveries) if valid_deliveries > 0 else None
    avg_delay_days = (sum(d.delay_days for d in delay_report.delays) / late_count) if late_count > 0 else 0.0
    max_delay_days = max((d.delay_days for d in delay_report.delays), default=0)

    metrics = SupplierReliabilityMetrics(
        supplier=supplier,
        deliveries_evaluated=valid_deliveries,
        on_time_count=on_time_count,
        late_count=late_count,
        on_time_rate=on_time_rate,
        avg_delay_days=avg_delay_days,
        max_delay_days=max_delay_days,
    )

    flag_reasons: list[str] = []

    if valid_deliveries == 0:
        # No usable delivery record at all for this supplier - every row
        # was invalid. There is no signal to score reliability from, so
        # rather than fabricate a number, the score is forced to the
        # conservative maximum-risk end: "can't verify this supplier's
        # reliability" defaults to "treat as high risk," not "assume
        # they're fine."
        score = MAX_SCORE
        flag_reasons.append(
            f"no valid delivery records ({len(delay_report.flagged_rows)} row(s) invalid) - cannot verify reliability"
        )
    else:
        score = min(MAX_SCORE, sum(LATE_DELIVERY_PENALTY.get(d.severity, 0.0) for d in delay_report.delays))
        if valid_deliveries < min_deliveries_for_confidence:
            flag_reasons.append(
                f"only {valid_deliveries} valid delivery record(s) - fewer than the "
                f"{min_deliveries_for_confidence} required for a confident score"
            )
        if on_time_rate is not None and on_time_rate < LOW_ON_TIME_RATE_THRESHOLD:
            flag_reasons.append(
                f"on-time rate {on_time_rate:.0%} is below the {LOW_ON_TIME_RATE_THRESHOLD:.0%} reliability floor"
            )
        if score >= FLAG_SCORE_THRESHOLD:
            flag_reasons.append(f"risk score {score:.0f}/100 meets the review threshold ({FLAG_SCORE_THRESHOLD:.0f})")

    if delay_report.flagged_rows and valid_deliveries > 0:
        flag_reasons.append(
            f"{len(delay_report.flagged_rows)} of {len(rows)} delivery row(s) had invalid data and were excluded"
        )

    severity = _score_bucket_severity(score)

    return SupplierRiskScore(
        supplier=supplier,
        score=score,
        severity=severity,
        flagged_for_review=bool(flag_reasons),
        flag_reasons=flag_reasons,
        explanation=_explain(supplier, score, severity, metrics, delay_report.delays, flag_reasons),
        metrics=metrics,
        delay_anomalies=delay_report.delays,
        invalid_rows=delay_report.flagged_rows,
    )


def evaluate_supplier_reliability(
    rows: list[dict[str, Any]],
    *,
    delay_threshold_days: float = DEFAULT_DELAY_THRESHOLD_DAYS,
    min_deliveries_for_confidence: int = MIN_DELIVERIES_FOR_CONFIDENT_SCORE,
) -> SupplierEvaluationReport:
    """Group delivery rows by supplier and compute each supplier's Supplier Risk Score.

    Handles: rows missing a usable `supplier` value (returned in
    `unattributable_rows`, excluded from every supplier's evaluation
    rather than silently dropped); a supplier whose rows are all
    individually invalid (scored conservatively at MAX_SCORE and flagged,
    not raised); a supplier with too few valid deliveries to score
    confidently, or a low on-time rate, or a risk score past the review
    threshold (each is its own, separately reported flag_reasons entry -
    the "given unreliable supplier data ... flags the supplier for
    review" failure path). Raises SupplierEvaluationError for a
    non-positive delay_threshold_days or a min_deliveries_for_confidence
    below 1 - caller/parameter bugs, not data problems.

    Does not handle: whether a per-row delay/date parsing decision is
    itself correct - that is entirely delegated to
    detect_supplier_delays, this module's only source of per-row
    validation and delay classification.
    """
    if delay_threshold_days <= 0:
        raise SupplierEvaluationError(f"delay_threshold_days must be positive, got {delay_threshold_days}")
    if min_deliveries_for_confidence < 1:
        raise SupplierEvaluationError(
            f"min_deliveries_for_confidence must be >= 1, got {min_deliveries_for_confidence}"
        )

    by_supplier: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unattributable_rows: list[dict[str, Any]] = []
    for row in rows:
        key = _supplier_key(row)
        if key is None:
            unattributable_rows.append(row)
        else:
            by_supplier[key].append(row)

    scores = [
        _evaluate_one_supplier(supplier, supplier_rows, delay_threshold_days, min_deliveries_for_confidence)
        for supplier, supplier_rows in sorted(by_supplier.items())
    ]

    warnings: list[str] = []
    if not rows:
        warnings.append("no delivery data provided")
    if unattributable_rows:
        warnings.append(
            f"{len(unattributable_rows)} of {len(rows)} delivery row(s) missing a supplier "
            f"and could not be evaluated"
        )

    return SupplierEvaluationReport(
        scores=sorted(scores, key=lambda s: s.score, reverse=True),
        unattributable_rows=unattributable_rows,
        warnings=warnings,
    )
