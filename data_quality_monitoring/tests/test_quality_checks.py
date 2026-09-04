import pytest

from data_quality_monitoring.quality_checks import (
    POOR_QUALITY_ALERT_THRESHOLD,
    DataQualityError,
    assess_data_quality,
)

REQUIRED = ("po_id", "expected_date", "actual_date")


def _row(po_id="PO-1", expected="2025-01-01", actual="2025-01-01"):
    return {"po_id": po_id, "expected_date": expected, "actual_date": actual}


# --- AC1: given data inputs, when quality checks are performed, a Data Quality Score is provided ---


def test_assess_data_quality_gives_a_perfect_score_when_every_row_is_complete():
    rows = [_row("PO-1"), _row("PO-2"), _row("PO-3")]

    report = assess_data_quality(rows, required_fields=REQUIRED)

    assert report.overall_score == 100.0
    assert report.severity == "good"
    assert report.poor_quality is False
    assert report.alert_reasons == []
    assert report.total_rows == 3
    assert report.dimension_results[0].dimension == "completeness"
    assert report.dimension_results[0].issue_rows == 0


def test_assess_data_quality_scores_down_for_rows_missing_required_fields():
    rows = [_row("PO-1"), {"po_id": "PO-2", "expected_date": "2025-01-01"}]  # missing actual_date

    report = assess_data_quality(rows, required_fields=REQUIRED)

    assert report.overall_score == 50.0
    completeness = report.dimension_results[0]
    assert completeness.issue_rows == 1
    assert "actual_date" in completeness.sample_issues[0]


def test_assess_data_quality_treats_a_falsy_but_present_field_as_complete():
    # 0 is a legitimate po_id (e.g. a zero-indexed PK) - must not be
    # mistaken for a missing field, the same distinction
    # inventory_risk/data_quality.py and risk_detection/anomaly_detection.py
    # already draw for their own required-field checks.
    rows = [_row(po_id=0)]

    report = assess_data_quality(rows, required_fields=REQUIRED)

    assert report.overall_score == 100.0


# --- AC2: given poor data quality, when detected, the system alerts the data steward ---


def test_assess_data_quality_flags_poor_quality_when_score_drops_below_the_alert_threshold():
    rows = [_row("PO-1")] + [{"po_id": f"PO-{i}"} for i in range(2, 10)]  # 1/9 complete

    report = assess_data_quality(rows, required_fields=REQUIRED)

    assert report.overall_score < POOR_QUALITY_ALERT_THRESHOLD
    assert report.poor_quality is True
    assert report.alert_reasons
    assert "alert threshold" in report.alert_reasons[0]


def test_assess_data_quality_alerts_rather_than_fabricates_a_score_when_no_rows_are_given():
    report = assess_data_quality([], required_fields=REQUIRED)

    assert report.overall_score is None
    assert report.severity == "critical"
    assert report.poor_quality is True
    assert report.alert_reasons == ["no rows available to assess data quality"]
    assert report.warnings == ["no data provided"]


# --- failure path: bad caller parameters raise rather than silently no-op ---


def test_assess_data_quality_rejects_empty_required_fields():
    with pytest.raises(DataQualityError):
        assess_data_quality([_row()], required_fields=())
