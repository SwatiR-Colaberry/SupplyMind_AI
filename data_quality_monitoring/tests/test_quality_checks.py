from decimal import Decimal

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


# --- validity dimension: is a present numeric field's value actually well-formed ---


def test_no_numeric_fields_means_only_completeness_is_scored():
    # Backward compatibility: a caller that doesn't pass numeric_fields
    # gets exactly the pre-existing behavior - no validity dimension at all.
    rows = [_row("PO-1")]

    report = assess_data_quality(rows, required_fields=REQUIRED)

    assert [d.dimension for d in report.dimension_results] == ["completeness"]


def test_validity_scores_100_when_every_numeric_field_is_a_real_number():
    rows = [{**_row("PO-1"), "transportation_cost": 1200.0}, {**_row("PO-2"), "transportation_cost": 950}]

    report = assess_data_quality(rows, required_fields=REQUIRED, numeric_fields=("transportation_cost",))

    validity = next(d for d in report.dimension_results if d.dimension == "validity")
    assert validity.score == 100.0
    assert validity.checked_rows == 2
    assert validity.issue_rows == 0


def test_validity_flags_a_non_numeric_string_value():
    rows = [{**_row("PO-1"), "transportation_cost": "N/A"}]

    report = assess_data_quality(rows, required_fields=REQUIRED, numeric_fields=("transportation_cost",))

    validity = next(d for d in report.dimension_results if d.dimension == "validity")
    assert validity.score == 0.0
    assert validity.issue_rows == 1
    assert "transportation_cost" in validity.sample_issues[0]


def test_validity_accepts_a_numeric_looking_string_as_parseable():
    rows = [{**_row("PO-1"), "transportation_cost": "1200.50"}]

    report = assess_data_quality(rows, required_fields=REQUIRED, numeric_fields=("transportation_cost",))

    validity = next(d for d in report.dimension_results if d.dimension == "validity")
    assert validity.score == 100.0


def test_validity_accepts_a_decimal_value_from_a_real_database_row():
    rows = [{**_row("PO-1"), "transportation_cost": Decimal("1200.50")}]

    report = assess_data_quality(rows, required_fields=REQUIRED, numeric_fields=("transportation_cost",))

    validity = next(d for d in report.dimension_results if d.dimension == "validity")
    assert validity.score == 100.0


def test_validity_rejects_nan():
    rows = [{**_row("PO-1"), "transportation_cost": float("nan")}]

    report = assess_data_quality(rows, required_fields=REQUIRED, numeric_fields=("transportation_cost",))

    validity = next(d for d in report.dimension_results if d.dimension == "validity")
    assert validity.issue_rows == 1


def test_validity_rejects_a_bool_even_though_it_is_an_int_subclass():
    # isinstance(True, int) is True in Python - a boolean where a cost is
    # expected is a type mismatch, not a valid 0/1, and must not silently
    # pass as "numeric."
    rows = [{**_row("PO-1"), "transportation_cost": True}]

    report = assess_data_quality(rows, required_fields=REQUIRED, numeric_fields=("transportation_cost",))

    validity = next(d for d in report.dimension_results if d.dimension == "validity")
    assert validity.issue_rows == 1


def test_validity_does_not_penalize_a_row_missing_the_numeric_field_entirely():
    # A missing field is completeness's concern, not validity's - double-
    # counting it against both dimensions would understate the real score
    # for a mundane reason (field simply not supplied), not a genuine
    # validity problem.
    rows = [{**_row("PO-1"), "transportation_cost": 1200.0}, _row("PO-2")]  # no transportation_cost at all

    report = assess_data_quality(rows, required_fields=REQUIRED, numeric_fields=("transportation_cost",))

    validity = next(d for d in report.dimension_results if d.dimension == "validity")
    assert validity.checked_rows == 1  # only the row that actually carried the field
    assert validity.issue_rows == 0


def test_validity_scores_none_when_no_row_carries_the_full_numeric_field_set():
    rows = [_row("PO-1"), _row("PO-2")]  # neither carries transportation_cost

    report = assess_data_quality(rows, required_fields=REQUIRED, numeric_fields=("transportation_cost",))

    validity = next(d for d in report.dimension_results if d.dimension == "validity")
    assert validity.score is None
    assert validity.checked_rows == 0


def test_overall_score_averages_completeness_and_validity_when_both_are_scored():
    # completeness=100 (both rows carry po_id/expected_date/actual_date),
    # validity=50 (1 of 2 transportation_cost values is garbage) -> (100+50)/2 = 75.
    rows = [
        {**_row("PO-1"), "transportation_cost": 1200.0},
        {**_row("PO-2"), "transportation_cost": "garbage"},
    ]

    report = assess_data_quality(rows, required_fields=REQUIRED, numeric_fields=("transportation_cost",))

    assert report.overall_score == pytest.approx(75.0)
