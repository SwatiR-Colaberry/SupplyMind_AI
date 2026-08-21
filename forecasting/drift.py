"""Detects forecasting model drift.

Drift here means: a previous forecast has diverged from what actually
happened, by more than a tolerable margin. Pure computation, no
persistence - the caller supplies the previous forecast's points and
the actual demand observed since then (typically the freshly-aggregated
history passed into the next forecast_demand() call); this module only
compares them and reports whether the divergence crossed the threshold.
"""

from __future__ import annotations

from dataclasses import dataclass

from forecasting.demand_model import DemandPoint, ForecastPoint

DEFAULT_DRIFT_THRESHOLD = 0.30  # 30% mean absolute percentage error


@dataclass(frozen=True)
class DriftReport:
    periods_compared: int
    mean_absolute_percentage_error: float
    drifted: bool
    detail: str


def detect_drift(
    previous_forecast_points: list[ForecastPoint],
    actuals: list[DemandPoint],
    threshold: float = DEFAULT_DRIFT_THRESHOLD,
) -> DriftReport:
    """Compare a previous forecast's points against actual demand for the same periods.

    Handles: no overlapping periods between the forecast and the
    actuals (returns drifted=False, periods_compared=0 - there is
    nothing to compare yet, which is not itself evidence of drift, so
    it must not be reported as "no drift" for the wrong reason without
    saying so). An actual of exactly 0 in a forecasted period is
    excluded from the percentage-error calculation (a divide-by-zero)
    but does not prevent scoring the remaining periods.
    """
    actuals_by_period = {p.period: p.quantity for p in actuals}
    errors = []
    for point in previous_forecast_points:
        actual = actuals_by_period.get(point.period)
        if actual is None or actual == 0:
            continue
        errors.append(abs(point.forecast_quantity - actual) / actual)

    if not errors:
        return DriftReport(
            periods_compared=0,
            mean_absolute_percentage_error=0.0,
            drifted=False,
            detail="no overlapping actuals to compare against the previous forecast",
        )

    mape = sum(errors) / len(errors)
    drifted = mape > threshold
    detail = (
        f"mean absolute percentage error {mape:.2%} "
        f"{'exceeds' if drifted else 'within'} drift threshold {threshold:.0%}"
    )
    return DriftReport(
        periods_compared=len(errors),
        mean_absolute_percentage_error=mape,
        drifted=drifted,
        detail=detail,
    )
