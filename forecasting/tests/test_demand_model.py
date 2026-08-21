import pytest

from forecasting.demand_model import (
    DemandPoint,
    ForecastingError,
    forecast_demand,
    parse_period,
)


def _linear_history(months: int, start_value: float = 100.0, step: float = 5.0) -> list[DemandPoint]:
    return [DemandPoint(f"2025-{m:02d}", start_value + step * (m - 1)) for m in range(1, months + 1)]


def test_forecast_demand_projects_a_perfectly_linear_series_with_full_confidence():
    history = _linear_history(12)

    result = forecast_demand(history, periods_ahead=3)

    assert [p.period for p in result.points] == ["2026-01", "2026-02", "2026-03"]
    assert [round(p.forecast_quantity, 1) for p in result.points] == [160.0, 165.0, 170.0]
    assert result.confidence == pytest.approx(1.0)


def test_forecast_demand_carries_forward_across_a_year_boundary():
    history = _linear_history(11)  # ends at 2025-11

    result = forecast_demand(history, periods_ahead=2)

    assert [p.period for p in result.points] == ["2025-12", "2026-01"]


def test_forecast_demand_clips_negative_projections_to_zero():
    # A steep downward trend that would go negative if extrapolated raw.
    history = [DemandPoint(f"2025-{m:02d}", 100.0 - 40.0 * (m - 1)) for m in range(1, 4)]

    result = forecast_demand(history, periods_ahead=2)

    assert all(p.forecast_quantity >= 0.0 for p in result.points)


def test_forecast_demand_rejects_empty_history():
    with pytest.raises(ForecastingError, match="at least 2 historical points"):
        forecast_demand([], periods_ahead=1)


def test_forecast_demand_rejects_a_single_point_of_history():
    with pytest.raises(ForecastingError, match="at least 2 historical points"):
        forecast_demand([DemandPoint("2025-01", 100.0)], periods_ahead=1)


def test_forecast_demand_rejects_non_positive_periods_ahead():
    history = _linear_history(3)

    with pytest.raises(ForecastingError, match="periods_ahead must be positive"):
        forecast_demand(history, periods_ahead=0)


def test_forecast_demand_rejects_non_integer_periods_ahead_as_forecasting_error():
    # Regression: previously "abc" <= 0 raised an uncaught TypeError
    # instead of the documented ForecastingError - a caller passing a
    # bad value via the agent's untyped context dict would see a raw
    # Python error instead of a handled "incorrect parameter settings"
    # response.
    history = _linear_history(3)

    with pytest.raises(ForecastingError, match="must be a positive integer"):
        forecast_demand(history, periods_ahead="abc")


def test_forecast_demand_rejects_non_positive_season_length():
    history = _linear_history(3)

    with pytest.raises(ForecastingError, match="season_length must be positive"):
        forecast_demand(history, periods_ahead=1, season_length=0)


def test_forecast_demand_rejects_non_integer_season_length_as_forecasting_error():
    history = _linear_history(3)

    with pytest.raises(ForecastingError, match="must be a positive integer"):
        forecast_demand(history, periods_ahead=1, season_length="abc")


def test_forecast_demand_rejects_malformed_period_string():
    history = [DemandPoint("Jan-2025", 100.0), DemandPoint("2025-02", 110.0)]

    with pytest.raises(ForecastingError, match="not in YYYY-MM format"):
        forecast_demand(history, periods_ahead=1)


def test_forecast_demand_rejects_duplicate_periods():
    history = [DemandPoint("2025-01", 100.0), DemandPoint("2025-01", 110.0)]

    with pytest.raises(ForecastingError, match="duplicate periods"):
        forecast_demand(history, periods_ahead=1)


def test_forecast_demand_sorts_out_of_order_history_before_fitting():
    ordered = _linear_history(4)
    shuffled = [ordered[2], ordered[0], ordered[3], ordered[1]]

    result = forecast_demand(shuffled, periods_ahead=1)

    assert result.points[0].period == "2025-05"


def test_parse_period_rejects_malformed_period():
    with pytest.raises(ForecastingError, match="not in YYYY-MM format"):
        parse_period("2025/01")
