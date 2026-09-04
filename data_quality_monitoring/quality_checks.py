"""Deterministic data-quality assessment tools (STORY-015 / REQ-017).

Pure computation - no I/O. Given raw rows from any tabular data source in
this repo (inventory, delivery/shipment, or any other row shape) and the
set of fields a caller expects every row to carry, scores completeness -
the first of the quality dimensions this module checks - and combines
dimension results into a single 0-100 Data Quality Score.

Deliberately domain-agnostic and parameterized by `required_fields`,
unlike inventory_risk/data_quality.py's assess_inventory_data_quality()
(hardcoded to the inventory row shape) or
risk_detection/anomaly_detection.py's delivery-row validation (hardcoded
to REQUIRED_DELIVERY_FIELDS): REQ-017 asks for a general data-quality
monitoring *tool*, not a third hand-rolled, single-dataset validator, and
those two existing modules already own their domain's business rules
(non-negative stock, positive lead time, parseable delivery dates) - this
module does not re-validate those; it scores the generic shape a "data
quality" check applies to any dataset - are the fields a caller expects
even there.

Per CLAUDE.md's core principle ("LLMs are probabilistic, production
systems must be deterministic"), the score is a fixed arithmetic formula,
not a model call - the same convention every other scorer in this repo
(risk_detection/risk_score.py, supplier_evaluation/reliability.py)
already follows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

QualityDimension = Literal["completeness"]
QualitySeverity = Literal["good", "fair", "poor", "critical"]

# Score-bucket severity, higher-is-better - opposite polarity from this
# repo's risk scores (risk_detection/risk_score.py, supplier_evaluation's
# SupplierRiskScore), since a "quality" score reads naturally that
# direction. Shape mirrors supplier_evaluation/reliability.py's
# _SCORE_SEVERITY_THRESHOLDS, just inverted.
_SCORE_SEVERITY_THRESHOLDS: list[tuple[float, QualitySeverity]] = [
    (90.0, "good"),
    (70.0, "fair"),
    (50.0, "poor"),
]
_DEFAULT_SEVERITY: QualitySeverity = "critical"

# A score below the "fair" bucket floor reads as poor enough to alert the
# data steward over (AC2: "given poor data quality, when detected, then
# the system alerts the data steward"). Looked up from
# _SCORE_SEVERITY_THRESHOLDS rather than a second hardcoded 70.0, so it
# can't silently drift from the "fair" bucket floor it's meant to mirror.
POOR_QUALITY_ALERT_THRESHOLD = dict((label, threshold) for threshold, label in _SCORE_SEVERITY_THRESHOLDS)["fair"]


class DataQualityError(ValueError):
    """Raised when assess_data_quality's own parameters can't support assessment.

    Not the same failure path as a malformed row (counted against that
    row's completeness score, not raised) - this is a caller/parameter
    bug, the same division every other *Error class in this repo draws
    between a bad parameter and a bad data row.
    """


@dataclass(frozen=True)
class QualityCheckResult:
    dimension: QualityDimension
    score: float | None  # 0..100; None when no row could be checked for this dimension
    checked_rows: int
    issue_rows: int
    sample_issues: list[str]  # first few human-readable reasons, not exhaustive


@dataclass(frozen=True)
class DataQualityReport:
    overall_score: float | None  # 0..100; None when no dimension could be scored (e.g. zero rows)
    severity: QualitySeverity
    poor_quality: bool
    alert_reasons: list[str]
    dimension_results: list[QualityCheckResult]
    total_rows: int
    warnings: list[str] = field(default_factory=list)


def _severity_for(score: float) -> QualitySeverity:
    for threshold, label in _SCORE_SEVERITY_THRESHOLDS:
        if score >= threshold:
            return label
    return _DEFAULT_SEVERITY


def _completeness_check(rows: list[dict[str, Any]], required_fields: tuple[str, ...]) -> QualityCheckResult:
    # `f not in row or row[f] is None`, not a truthiness check - the same
    # missing-vs-falsy distinction inventory_risk/data_quality.py's
    # _row_issues() and risk_detection/anomaly_detection.py's
    # _delivery_row_issues() both draw, so a legitimately falsy value
    # (e.g. integer 0) is never mistaken for a missing field here either.
    sample_issues: list[str] = []
    issue_rows = 0
    for row in rows:
        missing = [f for f in required_fields if f not in row or row[f] is None]
        if missing:
            issue_rows += 1
            if len(sample_issues) < 5:
                sample_issues.append(f"missing field(s): {', '.join(missing)}")

    score = ((len(rows) - issue_rows) / len(rows)) * 100.0 if rows else None
    return QualityCheckResult(
        dimension="completeness",
        score=score,
        checked_rows=len(rows),
        issue_rows=issue_rows,
        sample_issues=sample_issues,
    )


def assess_data_quality(
    rows: list[dict[str, Any]],
    *,
    required_fields: tuple[str, ...],
) -> DataQualityReport:
    """Score `rows` for completeness against `required_fields` and combine into a Data Quality Score.

    Handles: an empty `rows` list (scored None, not 0 or 100 - "no data"
    is a different condition from "bad data" and must not read as either
    a passing or failing score, but is still alert-worthy since quality
    cannot be verified at all - the same conservative-default precedent
    supplier_evaluation/reliability.py sets for a supplier with zero
    valid deliveries); rows missing one or more required fields (counted
    against completeness, never raised - the "Quality monitoring fails"
    failure path is reserved for a caller/parameter bug, not a bad row).
    Raises DataQualityError for empty `required_fields` - nothing would
    be checked, so that is a caller/parameter bug, not a data problem.

    Does not handle: whether a present field's *value* is well-formed
    (numeric, in range, parseable) - that is a later dimension, not this
    one.
    """
    if not required_fields:
        raise DataQualityError("required_fields must be non-empty")

    completeness = _completeness_check(rows, required_fields)
    dimension_results = [completeness]

    scored = [d.score for d in dimension_results if d.score is not None]
    overall_score = sum(scored) / len(scored) if scored else None
    severity = _severity_for(overall_score) if overall_score is not None else _DEFAULT_SEVERITY

    alert_reasons: list[str] = []
    if overall_score is None:
        alert_reasons.append("no rows available to assess data quality")
    elif overall_score < POOR_QUALITY_ALERT_THRESHOLD:
        alert_reasons.append(
            f"data quality score {overall_score:.0f}/100 is below the "
            f"{POOR_QUALITY_ALERT_THRESHOLD:.0f} alert threshold"
        )

    warnings: list[str] = []
    if not rows:
        warnings.append("no data provided")

    return DataQualityReport(
        overall_score=overall_score,
        severity=severity,
        poor_quality=bool(alert_reasons),
        alert_reasons=alert_reasons,
        dimension_results=dimension_results,
        total_rows=len(rows),
        warnings=warnings,
    )
