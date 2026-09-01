import pytest

from supplier_evaluation.reliability import (
    FLAG_SCORE_THRESHOLD,
    LOW_ON_TIME_RATE_THRESHOLD,
    MIN_DELIVERIES_FOR_CONFIDENT_SCORE,
    SupplierEvaluationError,
    evaluate_supplier_reliability,
)


def _row(supplier, po_id, expected, actual):
    return {"supplier": supplier, "po_id": po_id, "expected_date": expected, "actual_date": actual}


# --- AC1: given supplier data, when evaluated, a Supplier Risk Score is generated ---


def test_evaluate_supplier_reliability_generates_a_score_for_a_reliable_supplier():
    rows = [
        _row("Acme", "PO-1", "2025-01-01", "2025-01-01"),
        _row("Acme", "PO-2", "2025-01-05", "2025-01-05"),
        _row("Acme", "PO-3", "2025-01-10", "2025-01-10"),
        _row("Acme", "PO-4", "2025-01-15", "2025-01-15"),
    ]

    report = evaluate_supplier_reliability(rows)

    assert len(report.scores) == 1
    score = report.scores[0]
    assert score.supplier == "Acme"
    assert score.score == 0.0
    assert score.severity == "low"
    assert score.flagged_for_review is False
    assert score.metrics.on_time_rate == 1.0
    assert score.metrics.deliveries_evaluated == 4


def test_evaluate_supplier_reliability_scores_multiple_suppliers_independently_and_sorts_riskiest_first():
    rows = [
        _row("Reliable", "PO-1", "2025-01-01", "2025-01-01"),
        _row("Reliable", "PO-2", "2025-01-05", "2025-01-05"),
        _row("Reliable", "PO-3", "2025-01-10", "2025-01-10"),
        _row("Risky", "PO-4", "2025-01-01", "2025-01-20"),  # 19 days late -> critical
        _row("Risky", "PO-5", "2025-01-05", "2025-01-25"),  # 20 days late -> critical
        _row("Risky", "PO-6", "2025-01-10", "2025-01-30"),  # 20 days late -> critical
    ]

    report = evaluate_supplier_reliability(rows)

    assert [s.supplier for s in report.scores] == ["Risky", "Reliable"]
    assert report.scores[0].score == 90.0  # 3 critical delays (delay_days >= 3x threshold) * 30 pts each


# --- AC2: given unreliable supplier data, when processed, the supplier is flagged ---


def test_evaluate_supplier_reliability_flags_a_supplier_whose_score_crosses_the_review_threshold():
    rows = [
        _row("Unreliable", "PO-1", "2025-01-01", "2025-01-08"),  # 7 days late -> critical (30 pts)
        _row("Unreliable", "PO-2", "2025-01-05", "2025-01-08"),  # 3 days late -> medium (10 pts)
        _row("Unreliable", "PO-3", "2025-01-10", "2025-01-10"),  # on time
    ]

    report = evaluate_supplier_reliability(rows)

    score = report.scores[0]
    assert score.score >= FLAG_SCORE_THRESHOLD
    assert score.flagged_for_review is True
    assert any("review threshold" in r for r in score.flag_reasons)


def test_evaluate_supplier_reliability_flags_a_supplier_with_persistent_minor_lateness_even_below_the_score_threshold():
    # Every delay is only 2 days (medium severity, 10 pts each) - three of
    # them score 30, under FLAG_SCORE_THRESHOLD (35) - but a 25% on-time
    # rate is still unreliable by ordinary meaning.
    rows = [
        _row("SlightlyLate", "PO-1", "2025-01-01", "2025-01-03"),
        _row("SlightlyLate", "PO-2", "2025-01-05", "2025-01-07"),
        _row("SlightlyLate", "PO-3", "2025-01-10", "2025-01-12"),
        _row("SlightlyLate", "PO-4", "2025-01-15", "2025-01-15"),  # on time
    ]

    report = evaluate_supplier_reliability(rows)

    score = report.scores[0]
    assert score.score < FLAG_SCORE_THRESHOLD
    assert score.metrics.on_time_rate < LOW_ON_TIME_RATE_THRESHOLD
    assert score.flagged_for_review is True
    assert any("reliability floor" in r for r in score.flag_reasons)


def test_evaluate_supplier_reliability_flags_and_conservatively_scores_a_supplier_with_no_valid_deliveries():
    rows = [
        _row("AllBadData", "PO-1", "not-a-date", "2025-01-01"),
        _row("AllBadData", "PO-2", "2025-01-01", None),
    ]

    report = evaluate_supplier_reliability(rows)

    score = report.scores[0]
    assert score.metrics.deliveries_evaluated == 0
    assert score.metrics.on_time_rate is None
    assert score.score == 100.0
    assert score.severity == "critical"
    assert score.flagged_for_review is True
    assert any("cannot verify reliability" in r for r in score.flag_reasons)


def test_evaluate_supplier_reliability_flags_too_few_deliveries_for_a_confident_score():
    rows = [_row("NewSupplier", "PO-1", "2025-01-01", "2025-01-01")]

    report = evaluate_supplier_reliability(rows)

    score = report.scores[0]
    assert score.metrics.deliveries_evaluated < MIN_DELIVERIES_FOR_CONFIDENT_SCORE
    assert score.flagged_for_review is True
    assert any("confident score" in r for r in score.flag_reasons)


def test_evaluate_supplier_reliability_reports_invalid_rows_excluded_from_an_otherwise_scoreable_supplier():
    rows = [
        _row("MixedQuality", "PO-1", "2025-01-01", "2025-01-01"),
        _row("MixedQuality", "PO-2", "2025-01-05", "2025-01-05"),
        _row("MixedQuality", "PO-3", "2025-01-10", "2025-01-10"),
        {"supplier": "MixedQuality", "po_id": "PO-4", "expected_date": "bad-date", "actual_date": "2025-01-01"},
    ]

    report = evaluate_supplier_reliability(rows)

    score = report.scores[0]
    assert score.metrics.deliveries_evaluated == 3
    assert len(score.invalid_rows) == 1
    assert any("invalid data and were excluded" in r for r in score.flag_reasons)


# --- data attribution and edge cases ---


def test_evaluate_supplier_reliability_routes_rows_with_no_supplier_to_unattributable_rows():
    rows = [
        {"po_id": "PO-1", "expected_date": "2025-01-01", "actual_date": "2025-01-01"},
        {"supplier": "  ", "po_id": "PO-2", "expected_date": "2025-01-01", "actual_date": "2025-01-01"},
    ]

    report = evaluate_supplier_reliability(rows)

    assert report.scores == []
    assert len(report.unattributable_rows) == 2
    assert any("missing a supplier" in w for w in report.warnings)


def test_evaluate_supplier_reliability_reports_a_warning_when_no_rows_are_provided():
    report = evaluate_supplier_reliability([])

    assert report.scores == []
    assert "no delivery data provided" in report.warnings[0]


def test_evaluate_supplier_reliability_rejects_a_non_positive_delay_threshold():
    with pytest.raises(SupplierEvaluationError, match="delay_threshold_days"):
        evaluate_supplier_reliability([], delay_threshold_days=0)


def test_evaluate_supplier_reliability_rejects_a_min_deliveries_below_one():
    with pytest.raises(SupplierEvaluationError, match="min_deliveries_for_confidence"):
        evaluate_supplier_reliability([], min_deliveries_for_confidence=0)


def test_evaluate_supplier_reliability_explanation_names_the_worst_delay():
    rows = [
        _row("Acme", "PO-1", "2025-01-01", "2025-01-01"),
        _row("Acme", "PO-2", "2025-01-05", "2025-01-05"),
        _row("Acme", "PO-3", "2025-01-10", "2025-01-25"),  # 15 days late, worst
    ]

    report = evaluate_supplier_reliability(rows)

    assert "PO-3" in report.scores[0].explanation
    assert "15 day(s) late" in report.scores[0].explanation
