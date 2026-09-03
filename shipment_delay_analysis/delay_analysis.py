"""Deterministic shipment delay + transportation cost analysis (STORY-014 / REQ-008).

Pure computation - no I/O. Given raw delivery rows (the same row shape
risk_detection/anomaly_detection.py's detect_supplier_delays and
supplier_evaluation/reliability.py already consume - po_id,
expected_date, actual_date, supplier), this module:

- reuses detect_supplier_delays for all per-row validation, date
  parsing, and delay-severity classification rather than reimplementing
  any of it (the same reuse supplier_evaluation/reliability.py already
  established for STORY-013)
- groups the resulting delays into per-supplier delay patterns (AC1 -
  "identifies delay patterns"): count, total/average delay days, worst
  severity, and cost, so a caller can see which suppliers are the
  recurring source of delay rather than only a flat list of incidents
- calculates the transportation cost impact of each delay and the total
  across all delays (AC2 - "provides cost analysis")

Per CLAUDE.md's core principle ("LLMs are probabilistic, production
systems must be deterministic"), cost is a fixed arithmetic formula, not
a model call.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from risk_detection.anomaly_detection import (
    AnomalySeverity,
    FlaggedDeliveryRow,
    detect_supplier_delays,
)

DEFAULT_DELAY_THRESHOLD_DAYS = 2.0

# Logged assumption (STORY-014, not escalated - same situation
# supplier_evaluation/reliability.py's own delay-threshold default was
# in): no delivery row anywhere in this codebase carries a real
# transportation-cost field yet (checked across every data_integration
# connector and sample dataset). Rather than block cost analysis on data
# that doesn't exist, delay cost is modeled as a flat rate per day late -
# a deterministic, auditable figure that is real and comparable today.
# A row MAY also carry its own `transportation_cost` (the base freight
# cost for that shipment); when present it is layered underneath the
# delay cost rather than replacing it, so a caller with real freight
# data gets a more complete total without this module requiring it.
DEFAULT_COST_PER_DAY_LATE = 150.0

_SEVERITY_RANK: dict[AnomalySeverity, int] = {"medium": 0, "high": 1, "critical": 2}


class DelayCostError(ValueError):
    """Raised when analyze_shipment_delays' own parameters can't support cost calculation.

    Not the same failure path as a missing/invalid transportation_cost
    on a single row (flagged in cost_errors, not raised - see
    ShipmentDelayAnalysisReport.cost_errors) - this is a caller/parameter
    bug, the same division risk_detection/anomaly_detection.py draws
    between a bad delay_threshold_days and a bad data row.
    """


@dataclass(frozen=True)
class ShipmentDelayCost:
    po_id: str
    supplier: str | None
    delay_days: int
    severity: AnomalySeverity
    base_transportation_cost: float  # 0.0 when the row carried none (or an unusable one - see cost_errors)
    delay_cost: float  # cost_per_day_late * delay_days
    total_cost: float  # base_transportation_cost + delay_cost
    detail: str


@dataclass(frozen=True)
class SupplierDelayPattern:
    supplier: str  # "Unknown" for delays whose row had no usable supplier value
    delay_count: int
    total_delay_days: int
    avg_delay_days: float
    worst_severity: AnomalySeverity
    total_cost: float


@dataclass(frozen=True)
class ShipmentDelayAnalysisReport:
    delay_costs: list[ShipmentDelayCost]  # sorted by total_cost descending
    patterns: list[SupplierDelayPattern]  # per-supplier, sorted by total_cost descending
    flagged_rows: list[FlaggedDeliveryRow]
    cost_errors: list[dict[str, Any]]  # [{"po_id": ..., "reason": ...}] - present but unusable transportation_cost
    total_delay_cost: float  # sum of delay_cost only (cost_per_day_late * delay_days)
    total_transportation_cost: float  # sum of base_transportation_cost only (excludes delay_cost)
    warnings: list[str] = field(default_factory=list)


def _base_transportation_cost(row: dict[str, Any]) -> tuple[float, str | None]:
    """Returns (cost, error). cost is 0.0 when the field is absent or unusable;
    error is None unless the value was present but could not be used, so the
    caller can tell "no cost data" apart from "bad cost data" (the "cost
    calculation errors" failure path).
    """
    raw = row.get("transportation_cost")
    if raw is None:
        return 0.0, None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0, f"transportation_cost is not a valid number: {raw!r}"
    if value < 0:
        return 0.0, f"transportation_cost must be non-negative, got {value}"
    return value, None


def _build_patterns(delay_costs: list[ShipmentDelayCost]) -> list[SupplierDelayPattern]:
    by_supplier: dict[str, list[ShipmentDelayCost]] = defaultdict(list)
    for cost in delay_costs:
        by_supplier[cost.supplier or "Unknown"].append(cost)

    patterns = [
        SupplierDelayPattern(
            supplier=supplier,
            delay_count=len(costs),
            total_delay_days=sum(c.delay_days for c in costs),
            avg_delay_days=sum(c.delay_days for c in costs) / len(costs),
            worst_severity=max((c.severity for c in costs), key=lambda s: _SEVERITY_RANK[s]),
            total_cost=sum(c.total_cost for c in costs),
        )
        for supplier, costs in by_supplier.items()
    ]
    return sorted(patterns, key=lambda p: p.total_cost, reverse=True)


def analyze_shipment_delays(
    rows: list[dict[str, Any]],
    *,
    delay_threshold_days: float = DEFAULT_DELAY_THRESHOLD_DAYS,
    cost_per_day_late: float = DEFAULT_COST_PER_DAY_LATE,
) -> ShipmentDelayAnalysisReport:
    """Detect shipment delays, group them into per-supplier patterns, and cost them out.

    Handles: everything detect_supplier_delays already handles (missing
    fields, unparseable dates - flagged in flagged_rows, not raised); a
    row whose transportation_cost is present but non-numeric or negative
    (flagged in cost_errors, base cost treated as $0 for that row rather
    than crashing the whole analysis - the "cost calculation errors"
    failure path). Raises DelayCostError for a negative cost_per_day_late,
    and re-raises detect_supplier_delays' own SupplierDelayError for a
    non-positive delay_threshold_days - both are caller/parameter bugs,
    not data problems.

    Does not handle: whether a transportation_cost value is itself
    *correct* - only whether it is numeric and non-negative, the same
    boundary detect_supplier_delays draws for dates.
    """
    if cost_per_day_late < 0:
        raise DelayCostError(f"cost_per_day_late must be non-negative, got {cost_per_day_late}")

    delay_report = detect_supplier_delays(rows, delay_threshold_days=delay_threshold_days)

    # po_id is one of detect_supplier_delays' REQUIRED_DELIVERY_FIELDS, so
    # every row that produced a delay anomaly has one; later rows win on a
    # duplicate po_id, a deterministic (if arbitrary) tie-break consistent
    # with this being a data-quality concern outside this module's scope.
    row_by_po_id = {str(row["po_id"]): row for row in rows if row.get("po_id") is not None}

    delay_costs: list[ShipmentDelayCost] = []
    cost_errors: list[dict[str, Any]] = []
    for anomaly in delay_report.delays:
        row = row_by_po_id.get(anomaly.po_id, {})
        base_cost, error = _base_transportation_cost(row)
        if error is not None:
            cost_errors.append({"po_id": anomaly.po_id, "reason": error})

        delay_cost = cost_per_day_late * anomaly.delay_days
        total_cost = base_cost + delay_cost
        detail = f"{anomaly.delay_days} day(s) late - delay cost ${delay_cost:,.2f}"
        if base_cost:
            detail += f" + base transportation cost ${base_cost:,.2f}"

        delay_costs.append(
            ShipmentDelayCost(
                po_id=anomaly.po_id,
                supplier=anomaly.supplier,
                delay_days=anomaly.delay_days,
                severity=anomaly.severity,
                base_transportation_cost=base_cost,
                delay_cost=delay_cost,
                total_cost=total_cost,
                detail=detail,
            )
        )

    delay_costs.sort(key=lambda c: c.total_cost, reverse=True)

    warnings = list(delay_report.warnings)
    if cost_errors:
        warnings.append(
            f"{len(cost_errors)} row(s) had an invalid transportation_cost; treated as $0 base cost"
        )

    return ShipmentDelayAnalysisReport(
        delay_costs=delay_costs,
        patterns=_build_patterns(delay_costs),
        flagged_rows=delay_report.flagged_rows,
        cost_errors=cost_errors,
        total_delay_cost=sum(c.delay_cost for c in delay_costs),
        total_transportation_cost=sum(c.base_transportation_cost for c in delay_costs),
        warnings=warnings,
    )
