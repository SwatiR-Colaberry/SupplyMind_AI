"""Deterministic anomaly detection for demand spikes and supplier delays (STORY-005 / REQ-009).

Pure computation - no I/O. Two independent detectors:
- detect_demand_spikes(): given a monthly demand history, flags periods
  whose quantity deviates unusually far from the rest of the history.
- detect_supplier_delays(): given raw delivery rows, flags deliveries
  that arrived unusually late against their expected date.

Per CLAUDE.md's core principle ("LLMs are probabilistic, production
systems must be deterministic"), both are plain statistical/arithmetic
tests, not a call to an LLM or a third-party anomaly API.

detect_demand_spikes' method: for each period, compare its quantity
against the mean/stdev of every *other* period ("leave-one-out").
Comparing a point against a baseline that excludes itself avoids the
point dragging its own baseline toward it and masking its own deviation
- the standard failure mode of scoring a point against a baseline that
includes it, which matters here because history lengths in this repo are
typically small (a handful of months), so one extreme value can
noticeably shift a same-baseline mean and stdev.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

from forecasting.demand_model import DemandPoint, parse_period

# Below this many periods, a leave-one-out baseline of at least
# MIN_BASELINE_POINTS - 1 points isn't available for every period, so
# "no anomalies found" would be indistinguishable from "not enough data
# to judge" - the caller needs to see those as different conditions
# ("algorithm performance issues" failure path), not silently get an
# empty, falsely-reassuring list.
MIN_BASELINE_POINTS = 3
MIN_HISTORY_POINTS_FOR_DETECTION = MIN_BASELINE_POINTS + 1

DEFAULT_Z_THRESHOLD = 2.0

AnomalyDirection = Literal["spike", "drop"]
AnomalySeverity = Literal["medium", "high", "critical"]


class AnomalyDetectionError(ValueError):
    """Raised when detect_demand_spikes cannot judge normal vs. anomalous from the given history."""


@dataclass(frozen=True)
class DemandAnomaly:
    period: str
    quantity: float
    baseline_mean: float
    baseline_stdev: float
    z_score: float  # signed; +inf/-inf when baseline_stdev is 0 (see _score_period)
    direction: AnomalyDirection
    severity: AnomalySeverity
    detail: str


def _zone_severity(value: float, threshold: float) -> AnomalySeverity:
    # Shared by both detectors below (a z-score against z_threshold, or a
    # delay in days against delay_threshold_days) - zones are each one
    # threshold-width wide beyond the threshold itself, mirroring
    # inventory_risk/risk_model.py's zone-width approach to turning a
    # continuous score into a small, ordered set of severity levels.
    if value >= threshold + 2 * threshold:
        return "critical"
    if value >= threshold + threshold:
        return "high"
    return "medium"


def _score_period(period: DemandPoint, baseline: list[float], z_threshold: float) -> DemandAnomaly | None:
    mean_b = statistics.mean(baseline)
    stdev_b = statistics.pstdev(baseline)

    if stdev_b == 0:
        # A perfectly flat baseline makes any deviation unambiguous
        # (there is no "normal" spread to measure against) rather than
        # merely improbable, so it's scored as maximal certainty instead
        # of skipped or divided-by-zero.
        if period.quantity == mean_b:
            return None
        z_score = math.inf if period.quantity > mean_b else -math.inf
    else:
        z_score = (period.quantity - mean_b) / stdev_b

    if abs(z_score) < z_threshold:
        return None

    direction: AnomalyDirection = "spike" if z_score > 0 else "drop"
    severity = _zone_severity(abs(z_score), z_threshold)
    z_display = "inf" if math.isinf(z_score) else f"{z_score:.2f}"
    detail = (
        f"{period.quantity:.1f} vs. baseline mean {mean_b:.1f} (stdev {stdev_b:.1f}) - z-score {z_display}"
    )
    return DemandAnomaly(
        period=period.period,
        quantity=period.quantity,
        baseline_mean=mean_b,
        baseline_stdev=stdev_b,
        z_score=z_score,
        direction=direction,
        severity=severity,
        detail=detail,
    )


def detect_demand_spikes(
    history: list[DemandPoint], z_threshold: float = DEFAULT_Z_THRESHOLD
) -> list[DemandAnomaly]:
    """Flag periods in `history` whose quantity is an outlier against the rest.

    Handles (raises AnomalyDetectionError for): empty history, and
    history shorter than MIN_HISTORY_POINTS_FOR_DETECTION - too little
    data to build a leave-one-out baseline at all ("algorithm
    performance issues" failure path). Does not handle whether the input
    values are themselves trustworthy (duplicate periods, non-numeric
    quantities) - that's a data-quality concern for the caller, same
    division of responsibility forecasting/data_quality.py has relative
    to forecasting/demand_model.py.

    Returns anomalies sorted by period, oldest first, so a caller
    logging or displaying them gets a chronological read rather than
    severity-sorted noise.
    """
    if len(history) < MIN_HISTORY_POINTS_FOR_DETECTION:
        raise AnomalyDetectionError(
            f"at least {MIN_HISTORY_POINTS_FOR_DETECTION} historical period(s) required to detect "
            f"demand spikes, got {len(history)}"
        )
    if z_threshold <= 0:
        raise AnomalyDetectionError(f"z_threshold must be positive, got {z_threshold}")

    quantities = [p.quantity for p in history]
    anomalies: list[DemandAnomaly] = []
    for i, period in enumerate(history):
        baseline = quantities[:i] + quantities[i + 1 :]
        anomaly = _score_period(period, baseline, z_threshold)
        if anomaly is not None:
            anomalies.append(anomaly)

    return sorted(anomalies, key=lambda a: parse_period(a.period))


# --- Supplier delay detection --------------------------------------------

# Logged assumption (not escalated - same situation STORY-003/004 were in:
# no real delivery_records schema exists yet to confirm the true column
# names against; see data_integration/run_sample_integration.py's own
# logged assumption): a delivery row has one row per purchase order with
# po_id, expected_date, and actual_date. supplier is optional and carried
# through into the anomaly only for display - it is never validated.
REQUIRED_DELIVERY_FIELDS = ("po_id", "expected_date", "actual_date")

DEFAULT_DELAY_THRESHOLD_DAYS = 2


class SupplierDelayError(ValueError):
    """Raised when detect_supplier_delays' own parameters can't support detection.

    Not the same failure path as a malformed delivery row (flagged, not
    raised - see FlaggedDeliveryRow) - this is a caller/parameter bug,
    the same division forecasting.demand_model draws between a bad
    z_threshold and a bad data point.
    """


@dataclass(frozen=True)
class FlaggedDeliveryRow:
    row: dict[str, Any]
    reasons: list[str]


@dataclass(frozen=True)
class SupplierDelayAnomaly:
    po_id: str
    supplier: str | None
    expected_date: str  # ISO YYYY-MM-DD
    actual_date: str  # ISO YYYY-MM-DD
    delay_days: int
    severity: AnomalySeverity
    detail: str


@dataclass(frozen=True)
class SupplierDelayReport:
    delays: list[SupplierDelayAnomaly]
    flagged_rows: list[FlaggedDeliveryRow]
    warnings: list[str] = field(default_factory=list)


def parse_delivery_date(value: Any) -> date | None:
    """Accept a "YYYY-MM-DD" string or a native date/datetime - a
    connector reading from a real date/timestamp column would hand back
    the latter, not a string, so both are treated as valid input rather
    than only the string form. Any other type or an unparseable string
    returns None so the caller can flag the row rather than raise.

    Public (STORY-013) rather than module-private: supplier_evaluation
    parses the same delivery rows to compute per-supplier reliability, and
    two copies of this rule could silently drift - the same row would then
    be valid to one module and invalid to the other, so a delivery counted
    as on-time by one would be dropped as unparseable by the other.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def _delivery_row_issues(row: dict[str, Any]) -> list[str]:
    # `f not in row or row[f] is None`, not a truthiness check - the same
    # precise-missing-vs-falsy distinction inventory_risk/data_quality.py's
    # _row_issues() already draws. A truthiness check (`not row.get(f)`)
    # would treat a legitimately falsy po_id (e.g. integer 0, a real shape
    # for a zero-indexed database PK - this module itself str()s po_id
    # later, anticipating a non-string source value) as "missing" and
    # silently drop a real delay from detection.
    missing = [f for f in REQUIRED_DELIVERY_FIELDS if f not in row or row[f] is None]
    if missing:
        # Can't validate the dates below without the fields present at all.
        return [f"missing field(s): {', '.join(missing)}"]

    issues: list[str] = []
    if parse_delivery_date(row["expected_date"]) is None:
        issues.append(f"expected_date is not a valid date: {row['expected_date']!r}")
    if parse_delivery_date(row["actual_date"]) is None:
        issues.append(f"actual_date is not a valid date: {row['actual_date']!r}")
    return issues


def detect_supplier_delays(
    rows: list[dict[str, Any]], delay_threshold_days: float = DEFAULT_DELAY_THRESHOLD_DAYS
) -> SupplierDelayReport:
    """Flag delivery rows that arrived delay_threshold_days or more after their expected date.

    Handles: missing required fields and unparseable dates - each row is
    excluded from delay detection and returned in `flagged_rows` with a
    human-readable reason ("data inconsistency" failure path), rather
    than one bad row crashing detection for every other row. An early or
    on-time delivery (delay_days < delay_threshold_days, including
    negative) is neither a delay nor a flagged row - it is simply not
    reported, the same way ordinary variation produces no
    DemandAnomaly.

    Raises SupplierDelayError for a non-positive delay_threshold_days -
    that is a caller/parameter bug, not a data problem, so it is not
    something a per-row flag can express.

    Does not handle: whether a row's dates are *correct* - only whether
    they exist and parse. A malicious or corrupted-but-parseable date
    (e.g. actual_date far in the future) is scored the same as a genuine
    delay, since this module has no independent source of truth to check
    against.
    """
    if delay_threshold_days <= 0:
        raise SupplierDelayError(f"delay_threshold_days must be positive, got {delay_threshold_days}")

    delays: list[SupplierDelayAnomaly] = []
    flagged_rows: list[FlaggedDeliveryRow] = []

    for row in rows:
        issues = _delivery_row_issues(row)
        if issues:
            flagged_rows.append(FlaggedDeliveryRow(row=row, reasons=issues))
            continue

        expected = parse_delivery_date(row["expected_date"])
        actual = parse_delivery_date(row["actual_date"])
        delay_days = (actual - expected).days
        if delay_days < delay_threshold_days:
            continue

        delays.append(
            SupplierDelayAnomaly(
                po_id=str(row["po_id"]),
                supplier=row.get("supplier"),
                expected_date=expected.isoformat(),
                actual_date=actual.isoformat(),
                delay_days=delay_days,
                severity=_zone_severity(delay_days, delay_threshold_days),
                detail=(
                    f"delivered {delay_days} day(s) late "
                    f"(expected {expected.isoformat()}, actual {actual.isoformat()})"
                ),
            )
        )

    warnings: list[str] = []
    if not rows:
        warnings.append("no delivery data provided")
    elif flagged_rows:
        warnings.append(
            f"{len(flagged_rows)} of {len(rows)} delivery row(s) flagged for review "
            f"(missing/invalid fields); excluded from delay detection"
        )

    return SupplierDelayReport(
        delays=sorted(delays, key=lambda d: d.delay_days, reverse=True),
        flagged_rows=flagged_rows,
        warnings=warnings,
    )
