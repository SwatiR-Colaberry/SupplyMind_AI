from datetime import date, datetime

import pytest

from risk_detection.anomaly_detection import SupplierDelayError, detect_supplier_delays


def test_detect_supplier_delays_does_not_treat_a_falsy_po_id_as_missing():
    # Regression: `not row.get("po_id")` (a truthiness check) previously
    # treated a legitimately falsy po_id - e.g. an integer 0, a real shape
    # for a zero-indexed database primary key - as "missing" and silently
    # dropped a real, late delivery from detection instead of reporting it.
    rows = [{"po_id": 0, "expected_date": "2025-01-01", "actual_date": "2025-01-20"}]  # 19 days late

    report = detect_supplier_delays(rows)

    assert report.flagged_rows == []
    assert len(report.delays) == 1
    assert report.delays[0].po_id == "0"
    assert report.delays[0].severity == "critical"


def test_detect_supplier_delays_ignores_on_time_and_early_deliveries():
    rows = [
        {"po_id": "A", "expected_date": "2025-01-01", "actual_date": "2025-01-01"},  # on time
        {"po_id": "B", "expected_date": "2025-01-01", "actual_date": "2025-01-02"},  # 1 day, under threshold
        {"po_id": "C", "expected_date": "2025-01-05", "actual_date": "2025-01-01"},  # early
    ]

    report = detect_supplier_delays(rows)

    assert report.delays == []
    assert report.flagged_rows == []


@pytest.mark.parametrize(
    "delay_days,expected_severity",
    [(2, "medium"), (3, "medium"), (4, "high"), (5, "high"), (6, "critical"), (10, "critical")],
)
def test_detect_supplier_delays_severity_scales_with_how_late_the_delivery_is(delay_days, expected_severity):
    rows = [{"po_id": "PO-1", "expected_date": "2025-01-01", "actual_date": f"2025-01-{1 + delay_days:02d}"}]

    report = detect_supplier_delays(rows)

    assert len(report.delays) == 1
    assert report.delays[0].delay_days == delay_days
    assert report.delays[0].severity == expected_severity


def test_detect_supplier_delays_flags_rows_missing_required_fields():
    rows = [
        {"po_id": "PO-1", "expected_date": "2025-01-01"},  # missing actual_date
        {"expected_date": "2025-01-01", "actual_date": "2025-01-05"},  # missing po_id
    ]

    report = detect_supplier_delays(rows)

    assert report.delays == []
    assert len(report.flagged_rows) == 2
    assert "actual_date" in report.flagged_rows[0].reasons[0]
    assert "po_id" in report.flagged_rows[1].reasons[0]


def test_detect_supplier_delays_flags_rows_with_unparseable_dates():
    rows = [{"po_id": "PO-1", "expected_date": "2025-01-01", "actual_date": "not-a-date"}]

    report = detect_supplier_delays(rows)

    assert report.delays == []
    assert len(report.flagged_rows) == 1
    assert "actual_date" in report.flagged_rows[0].reasons[0]


def test_detect_supplier_delays_accepts_native_date_and_datetime_values():
    rows = [
        {"po_id": "PO-1", "expected_date": date(2025, 1, 1), "actual_date": datetime(2025, 1, 10, 12, 0, 0)}
    ]

    report = detect_supplier_delays(rows)

    assert len(report.delays) == 1
    assert report.delays[0].delay_days == 9
    assert report.delays[0].expected_date == "2025-01-01"
    assert report.delays[0].actual_date == "2025-01-10"


def test_detect_supplier_delays_does_not_flag_a_row_for_a_missing_optional_supplier_field():
    rows = [{"po_id": "PO-1", "expected_date": "2025-01-01", "actual_date": "2025-01-10"}]

    report = detect_supplier_delays(rows)

    assert report.flagged_rows == []
    assert report.delays[0].supplier is None


def test_detect_supplier_delays_reports_a_warning_when_no_rows_are_provided():
    report = detect_supplier_delays([])

    assert report.delays == []
    assert report.flagged_rows == []
    assert "no delivery data provided" in report.warnings[0]


def test_detect_supplier_delays_reports_a_warning_when_rows_are_flagged():
    rows = [{"po_id": "PO-1", "expected_date": "2025-01-01", "actual_date": "bad-date"}]

    report = detect_supplier_delays(rows)

    assert any("flagged for review" in w for w in report.warnings)


def test_detect_supplier_delays_rejects_a_non_positive_threshold():
    with pytest.raises(SupplierDelayError, match="delay_threshold_days"):
        detect_supplier_delays([], delay_threshold_days=0)


def test_detect_supplier_delays_respects_a_custom_threshold():
    rows = [{"po_id": "PO-1", "expected_date": "2025-01-01", "actual_date": "2025-01-04"}]  # 3-day delay

    assert len(detect_supplier_delays(rows, delay_threshold_days=2).delays) == 1
    assert detect_supplier_delays(rows, delay_threshold_days=5).delays == []


def test_detect_supplier_delays_sorts_results_by_delay_days_descending():
    rows = [
        {"po_id": "SHORT", "expected_date": "2025-01-01", "actual_date": "2025-01-04"},  # 3 days
        {"po_id": "LONG", "expected_date": "2025-01-01", "actual_date": "2025-01-20"},  # 19 days
        {"po_id": "MEDIUM", "expected_date": "2025-01-01", "actual_date": "2025-01-10"},  # 9 days
    ]

    report = detect_supplier_delays(rows)

    assert [d.po_id for d in report.delays] == ["LONG", "MEDIUM", "SHORT"]
