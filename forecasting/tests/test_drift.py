from forecasting.demand_model import DemandPoint, ForecastPoint
from forecasting.drift import DEFAULT_DRIFT_THRESHOLD, detect_drift


def test_detect_drift_reports_no_drift_when_actuals_match_forecast_closely():
    previous_forecast = [ForecastPoint("2025-06", 100.0), ForecastPoint("2025-07", 105.0)]
    actuals = [DemandPoint("2025-06", 102.0), DemandPoint("2025-07", 103.0)]

    report = detect_drift(previous_forecast, actuals)

    assert report.periods_compared == 2
    assert report.drifted is False
    assert report.mean_absolute_percentage_error < DEFAULT_DRIFT_THRESHOLD


def test_detect_drift_flags_large_divergence_as_drifted():
    previous_forecast = [ForecastPoint("2025-06", 100.0), ForecastPoint("2025-07", 100.0)]
    actuals = [DemandPoint("2025-06", 250.0), DemandPoint("2025-07", 260.0)]

    report = detect_drift(previous_forecast, actuals)

    assert report.drifted is True
    assert report.mean_absolute_percentage_error > DEFAULT_DRIFT_THRESHOLD


def test_detect_drift_only_compares_overlapping_periods():
    previous_forecast = [ForecastPoint("2025-06", 100.0), ForecastPoint("2025-08", 100.0)]
    actuals = [DemandPoint("2025-06", 100.0)]  # 2025-08 has no actual yet

    report = detect_drift(previous_forecast, actuals)

    assert report.periods_compared == 1


def test_detect_drift_reports_nothing_comparable_when_no_periods_overlap():
    previous_forecast = [ForecastPoint("2025-06", 100.0)]
    actuals = [DemandPoint("2025-09", 100.0)]

    report = detect_drift(previous_forecast, actuals)

    assert report.periods_compared == 0
    assert report.drifted is False
    assert "no overlapping actuals" in report.detail


def test_detect_drift_excludes_zero_actual_periods_from_the_error_calculation():
    previous_forecast = [ForecastPoint("2025-06", 100.0), ForecastPoint("2025-07", 100.0)]
    actuals = [DemandPoint("2025-06", 0.0), DemandPoint("2025-07", 105.0)]

    report = detect_drift(previous_forecast, actuals)

    assert report.periods_compared == 1


def test_detect_drift_respects_a_custom_threshold():
    previous_forecast = [ForecastPoint("2025-06", 100.0)]
    actuals = [DemandPoint("2025-06", 110.0)]  # 10% error

    assert detect_drift(previous_forecast, actuals, threshold=0.05).drifted is True
    assert detect_drift(previous_forecast, actuals, threshold=0.20).drifted is False
