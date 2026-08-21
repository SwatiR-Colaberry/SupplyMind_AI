"""Data-quality assessment for demand history, ahead of forecasting.

Pure computation - no I/O. This is what satisfies the acceptance
criterion "given incomplete demand data, when the system attempts
forecasting, then it should notify the user of potential inaccuracies":
this module decides what "incomplete" means and produces the warnings
that get surfaced, rather than the forecasting model silently producing
a number with no caveat attached.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from forecasting.demand_model import MIN_HISTORY_POINTS, DemandPoint, next_period, parse_period

MIN_RECOMMENDED_POINTS = 6


@dataclass(frozen=True)
class DataQualityReport:
    total_points: int
    missing_periods: list[str]
    non_positive_count: int
    is_sufficient: bool
    warnings: list[str] = field(default_factory=list)


def _missing_periods(ordered: list[DemandPoint]) -> list[str]:
    """Periods strictly between the first and last, absent from `ordered`.

    Walks month-by-month via demand_model.next_period() rather than
    reimplementing calendar rollover here, so there is exactly one
    place in the codebase that knows how to advance a "YYYY-MM" period.
    """
    seen = {p.period for p in ordered}
    end = ordered[-1].period
    missing: list[str] = []
    cursor = ordered[0].period
    while cursor < end:
        cursor = next_period(cursor, 1)
        if cursor not in seen:
            missing.append(cursor)
    return missing


def assess_data_quality(history: list[DemandPoint]) -> DataQualityReport:
    """Inspect demand history for gaps, sparsity, and non-positive values.

    Handles: empty history, too few points to trust a forecast, gaps in
    the monthly sequence, and zero/negative demand values (each surfaced
    as a distinct human-readable warning rather than merged into one
    generic message, so the caller can act on the specific issue).
    """
    if not history:
        return DataQualityReport(
            total_points=0,
            missing_periods=[],
            non_positive_count=0,
            is_sufficient=False,
            warnings=["no historical demand data provided"],
        )

    ordered = sorted(history, key=lambda p: parse_period(p.period))
    missing = _missing_periods(ordered)
    non_positive_count = sum(1 for p in ordered if p.quantity <= 0)

    warnings: list[str] = []
    if len(ordered) < MIN_RECOMMENDED_POINTS:
        warnings.append(
            f"only {len(ordered)} historical period(s) available "
            f"(recommend at least {MIN_RECOMMENDED_POINTS}) - forecast confidence will be low"
        )
    if missing:
        warnings.append(f"{len(missing)} gap period(s) in history: {', '.join(missing)}")
    if non_positive_count:
        warnings.append(f"{non_positive_count} period(s) with zero or negative demand")

    return DataQualityReport(
        total_points=len(ordered),
        missing_periods=missing,
        non_positive_count=non_positive_count,
        is_sufficient=len(ordered) >= MIN_HISTORY_POINTS,
        warnings=warnings,
    )
