from forecasting.data_quality import MIN_RECOMMENDED_POINTS, assess_data_quality
from forecasting.demand_model import DemandPoint


def _monthly_history(months: int) -> list[DemandPoint]:
    return [DemandPoint(f"2025-{m:02d}", 100.0 + m) for m in range(1, months + 1)]


def test_assess_data_quality_flags_no_issues_for_a_full_gap_free_series():
    history = _monthly_history(MIN_RECOMMENDED_POINTS)

    report = assess_data_quality(history)

    assert report.total_points == MIN_RECOMMENDED_POINTS
    assert report.missing_periods == []
    assert report.non_positive_count == 0
    assert report.is_sufficient is True
    assert report.warnings == []


def test_assess_data_quality_warns_on_sparse_history():
    history = _monthly_history(3)

    report = assess_data_quality(history)

    assert any("only 3 historical period" in w for w in report.warnings)
    assert report.is_sufficient is True  # still enough to attempt a forecast, just low-confidence


def test_assess_data_quality_detects_a_gap_in_the_monthly_sequence():
    history = [DemandPoint("2025-01", 100.0), DemandPoint("2025-04", 130.0)]

    report = assess_data_quality(history)

    assert report.missing_periods == ["2025-02", "2025-03"]
    assert any("2 gap period(s)" in w for w in report.warnings)


def test_assess_data_quality_counts_zero_and_negative_demand_periods():
    history = [
        DemandPoint("2025-01", 100.0),
        DemandPoint("2025-02", 0.0),
        DemandPoint("2025-03", -5.0),
        DemandPoint("2025-04", 110.0),
    ]

    report = assess_data_quality(history)

    assert report.non_positive_count == 2
    assert any("2 period(s) with zero or negative demand" in w for w in report.warnings)


def test_assess_data_quality_handles_empty_history_as_insufficient():
    report = assess_data_quality([])

    assert report.total_points == 0
    assert report.is_sufficient is False
    assert report.warnings == ["no historical demand data provided"]


def test_assess_data_quality_marks_a_single_point_as_insufficient_for_forecasting():
    report = assess_data_quality([DemandPoint("2025-01", 100.0)])

    assert report.total_points == 1
    assert report.is_sufficient is False


def test_assess_data_quality_sorts_unordered_history_before_checking_gaps():
    history = [DemandPoint("2025-03", 120.0), DemandPoint("2025-01", 100.0)]

    report = assess_data_quality(history)

    assert report.missing_periods == ["2025-02"]
