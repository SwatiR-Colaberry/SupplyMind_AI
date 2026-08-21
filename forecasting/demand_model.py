"""Deterministic demand forecasting core.

Pure computation - no I/O, no external API calls. Projects future demand
from a monthly history using a linear trend fitted by least squares,
optionally adjusted by a seasonal index. Per CLAUDE.md's core principle
("LLMs are probabilistic, production systems must be deterministic"),
this is a plain statistical model, not a call to an LLM or a third-party
forecasting API.

Assumption (logged, not escalated): periods are calendar months in
"YYYY-MM" format. This is the standard grain for demand-planning
forecasts and matches the "seasonality" language in REQ-005. No real
customer_orders schema exists yet to confirm the true grain against
(see data_integration/run_sample_integration.py's own logged
assumption) - if the real data is daily/weekly, the caller aggregates
into monthly buckets before calling this module. That aggregation step
is added when this module is wired to data_integration in a later step.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

MIN_HISTORY_POINTS = 2


@dataclass(frozen=True)
class DemandPoint:
    period: str  # "YYYY-MM"
    quantity: float


@dataclass(frozen=True)
class ForecastPoint:
    period: str
    forecast_quantity: float


@dataclass(frozen=True)
class ForecastResult:
    points: list[ForecastPoint]
    confidence: float  # 0..1, derived from trend fit quality (R^2)
    model: str = "weighted_linear_trend"


class ForecastingError(ValueError):
    """Raised when forecast_demand cannot produce a forecast from the given input/parameters."""


def parse_period(period: str) -> datetime:
    try:
        return datetime.strptime(period, "%Y-%m")
    except ValueError as exc:
        raise ForecastingError(f"period '{period}' is not in YYYY-MM format") from exc


def next_period(period: str, offset: int) -> str:
    """The period `offset` calendar months after `period`. Shared with
    forecasting/data_quality.py so month-rollover arithmetic lives in
    exactly one place."""
    dt = parse_period(period)
    month_index = dt.month - 1 + offset
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    return f"{year:04d}-{month:02d}"


def _validate_positive_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ForecastingError(f"{name} must be a positive integer, got {value!r}")
    if value <= 0:
        raise ForecastingError(f"{name} must be positive, got {value}")
    return value


def _fit_linear_trend(values: list[float]) -> tuple[float, float, float]:
    """Least-squares fit of values against their index. Returns (intercept, slope, r_squared)."""
    n = len(values)
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(values) / n
    ss_xy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, values))
    ss_xx = sum((x - mean_x) ** 2 for x in xs)
    slope = ss_xy / ss_xx if ss_xx else 0.0
    intercept = mean_y - slope * mean_x
    predicted = [intercept + slope * x for x in xs]
    ss_res = sum((y - p) ** 2 for y, p in zip(values, predicted))
    ss_tot = sum((y - mean_y) ** 2 for y in values)
    r_squared = 1.0 - (ss_res / ss_tot) if ss_tot else 1.0
    return intercept, slope, max(0.0, min(1.0, r_squared))


def _seasonal_factors(
    values: list[float], trend: list[float], season_length: int
) -> dict[int, float]:
    """Average ratio of actual-to-trend per position in the seasonal cycle.

    A position with no usable (trend > 0) observations defaults to a
    neutral factor of 1.0 rather than guessing from partial data.
    """
    ratios_by_position: dict[int, list[float]] = {i: [] for i in range(season_length)}
    for i, (actual, t) in enumerate(zip(values, trend)):
        if t > 0:
            ratios_by_position[i % season_length].append(actual / t)
    return {
        pos: (sum(ratios) / len(ratios)) if ratios else 1.0
        for pos, ratios in ratios_by_position.items()
    }


def forecast_demand(
    history: list[DemandPoint],
    periods_ahead: int,
    season_length: int = 12,
) -> ForecastResult:
    """Project future demand from a monthly history using a linear trend + seasonal index.

    Handles (raises ForecastingError for):
    - empty or too-short history (fewer than MIN_HISTORY_POINTS points -
      no trend can be fit from 0 or 1 point) - the "model training
      failure" failure path
    - periods_ahead or season_length that is missing, non-integer, or
      <= 0 - "incorrect parameter settings" failure path
    - a period string that isn't "YYYY-MM"
    - duplicate periods in the history (ambiguous ordering - a data
      quality issue the caller resolves via forecasting/data_quality.py
      before calling this function)

    Does not handle: whether the history is *complete* (no gaps) - that
    is forecasting/data_quality.py's job, checked by the caller before
    or after this call, not silently patched here.
    """
    periods_ahead = _validate_positive_int(periods_ahead, "periods_ahead")
    season_length = _validate_positive_int(season_length, "season_length")
    if len(history) < MIN_HISTORY_POINTS:
        raise ForecastingError(
            f"need at least {MIN_HISTORY_POINTS} historical points to fit a trend, "
            f"got {len(history)}"
        )

    ordered = sorted(history, key=lambda p: parse_period(p.period))
    periods_seen = [p.period for p in ordered]
    if len(set(periods_seen)) != len(periods_seen):
        raise ForecastingError("history contains duplicate periods")

    values = [p.quantity for p in ordered]
    intercept, slope, r_squared = _fit_linear_trend(values)
    trend = [intercept + slope * i for i in range(len(values))]
    seasonal = _seasonal_factors(values, trend, season_length)

    last_period = ordered[-1].period
    forecast_points = []
    for step in range(1, periods_ahead + 1):
        index = len(values) - 1 + step
        base = intercept + slope * index
        factor = seasonal[index % season_length]
        forecast_points.append(
            ForecastPoint(
                period=next_period(last_period, step),
                forecast_quantity=max(0.0, base * factor),
            )
        )

    return ForecastResult(points=forecast_points, confidence=r_squared)
