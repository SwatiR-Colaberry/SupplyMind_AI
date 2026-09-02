import pytest

from risk_detection.anomaly_detection import DemandAnomaly, SupplierDelayAnomaly
from root_cause.analysis import Issue, RootCauseAnalysisError, analyze_root_cause
from supplier_evaluation.reliability import SupplierRiskScore, SupplierReliabilityMetrics


def _demand_anomaly(period: str, severity: str = "critical", direction: str = "spike") -> DemandAnomaly:
    return DemandAnomaly(
        period=period,
        quantity=500,
        baseline_mean=100,
        baseline_stdev=10,
        z_score=40.0 if direction == "spike" else -40.0,
        direction=direction,
        severity=severity,
        detail="500.0 vs. baseline mean 100.0 (stdev 10.0) - z-score 40.00",
    )


def _supplier_delay(po_id: str, supplier: str, severity: str = "critical") -> SupplierDelayAnomaly:
    return SupplierDelayAnomaly(
        po_id=po_id,
        supplier=supplier,
        expected_date="2025-01-01",
        actual_date="2025-01-10",
        delay_days=9,
        severity=severity,
        detail="delivered 9 day(s) late",
    )


def _supplier_score(supplier: str, flagged: bool = True, severity: str = "high") -> SupplierRiskScore:
    metrics = SupplierReliabilityMetrics(
        supplier=supplier,
        deliveries_evaluated=10,
        on_time_count=4,
        late_count=6,
        on_time_rate=0.4,
        avg_delay_days=5.0,
        max_delay_days=12,
    )
    return SupplierRiskScore(
        supplier=supplier,
        score=80.0,
        severity=severity,
        flagged_for_review=flagged,
        flag_reasons=["high delay rate"],
        explanation="6 of 10 deliveries delayed",
        metrics=metrics,
        delay_anomalies=[],
        invalid_rows=[],
    )


def test_raises_when_no_signal_data_supplied_at_all():
    issue = Issue(subject="SKU-1", subject_kind="sku", as_of_period="2025-04", supplier="Acme")

    with pytest.raises(RootCauseAnalysisError):
        analyze_root_cause(issue)


def test_sku_stockout_correlates_with_demand_spike_in_same_period():
    issue = Issue(subject="SKU-1", subject_kind="sku", as_of_period="2025-04")
    anomalies = [_demand_anomaly("2025-04", severity="critical")]

    result = analyze_root_cause(issue, demand_anomalies=anomalies)

    assert len(result.candidates) == 1
    assert result.candidates[0].cause == "demand_spike"
    assert result.candidates[0].evidence_subject == "2025-04"
    assert result.confidence == pytest.approx(0.85)


def test_sku_stockout_correlates_with_supplier_delay():
    issue = Issue(subject="SKU-1", subject_kind="sku", supplier="Acme")
    delays = [_supplier_delay("PO-9", "Acme", severity="high")]

    result = analyze_root_cause(issue, supplier_delays=delays)

    assert len(result.candidates) == 1
    assert result.candidates[0].cause == "supplier_delay"
    assert result.candidates[0].evidence_subject == "PO-9"
    assert result.confidence == pytest.approx(0.7)


def test_sku_stockout_ranks_multiple_candidates_by_confidence():
    issue = Issue(subject="SKU-1", subject_kind="sku", as_of_period="2025-04", supplier="Acme")
    anomalies = [_demand_anomaly("2025-04", severity="medium")]
    delays = [_supplier_delay("PO-9", "Acme", severity="critical")]
    scores = [_supplier_score("Acme", flagged=True, severity="high")]

    result = analyze_root_cause(issue, demand_anomalies=anomalies, supplier_delays=delays, supplier_scores=scores)

    assert [c.cause for c in result.candidates] == ["supplier_delay", "supplier_reliability", "demand_spike"]
    assert result.confidence == pytest.approx(0.85)


def test_no_correlation_found_is_a_successful_result_not_an_error():
    issue = Issue(subject="SKU-1", subject_kind="sku", as_of_period="2025-04", supplier="Acme")
    anomalies = [_demand_anomaly("2025-01", severity="critical")]  # different period, no match

    result = analyze_root_cause(issue, demand_anomalies=anomalies)

    assert result.candidates == []
    assert result.confidence == 0.0
    assert "no correlated cause" in result.note


def test_demand_drop_is_not_treated_as_a_stockout_cause():
    issue = Issue(subject="SKU-1", subject_kind="sku", as_of_period="2025-04")
    anomalies = [_demand_anomaly("2025-04", severity="critical", direction="drop")]

    result = analyze_root_cause(issue, demand_anomalies=anomalies)

    assert result.candidates == []


def test_period_issue_excludes_its_own_demand_anomaly_as_self_evidence():
    issue = Issue(subject="2025-04", subject_kind="period", as_of_period="2025-04")
    anomalies = [_demand_anomaly("2025-04", severity="critical")]

    result = analyze_root_cause(issue, demand_anomalies=anomalies)

    assert result.candidates == []
    assert result.confidence == 0.0


def test_po_issue_excludes_its_own_supplier_delay_as_self_evidence():
    issue = Issue(subject="PO-9", subject_kind="po", supplier="Acme")
    delays = [_supplier_delay("PO-9", "Acme", severity="critical")]

    result = analyze_root_cause(issue, supplier_delays=delays)

    assert result.candidates == []


def test_unflagged_supplier_score_is_not_a_candidate():
    issue = Issue(subject="SKU-1", subject_kind="sku", supplier="Acme")
    scores = [_supplier_score("Acme", flagged=False)]

    result = analyze_root_cause(issue, supplier_scores=scores)

    assert result.candidates == []


# --- regression coverage for the pre-commit code review fixes ---


def test_supplier_kind_issue_surfaces_its_own_flagged_reliability_score():
    # Regression: the self-reference guard used to also exclude a
    # supplier's own SupplierRiskScore when investigating a supplier-kind
    # issue about that exact supplier - "supplier" has no finer-grained
    # identity to distinguish "the issue" from "the evidence" the way
    # period/po do, so this always suppressed the one candidate a
    # supplier-kind issue most needs. See analysis.py's
    # _supplier_reliability_candidate for why this isn't circular.
    issue = Issue(subject="Acme", subject_kind="supplier", supplier="Acme")
    scores = [_supplier_score("Acme", flagged=True, severity="critical")]

    result = analyze_root_cause(issue, supplier_scores=scores)

    assert len(result.candidates) == 1
    assert result.candidates[0].cause == "supplier_reliability"
    assert result.confidence == pytest.approx(0.85)


def test_flagged_low_severity_supplier_score_gets_nonzero_confidence():
    # Regression: _SEVERITY_CONFIDENCE had no "low" entry, so a supplier
    # flagged for a reason other than a high risk score (e.g. too few
    # deliveries for a confident score - a real, reachable case in
    # supplier_evaluation/reliability.py) silently scored 0.0 confidence,
    # indistinguishable from "no candidate found."
    issue = Issue(subject="SKU-1", subject_kind="sku", supplier="Acme")
    scores = [_supplier_score("Acme", flagged=True, severity="low")]

    result = analyze_root_cause(issue, supplier_scores=scores)

    assert len(result.candidates) == 1
    assert result.candidates[0].confidence > 0.0


def test_supplier_reliability_candidate_picks_the_most_severe_of_several_scores():
    # Regression: picked matches[0] instead of the max-by-severity the
    # other two candidate builders use - only observable when a caller
    # hand-builds a supplier_scores list with more than one entry for the
    # same supplier (evaluate_supplier_reliability() itself never
    # produces duplicates), but the function's own contract ("strongest
    # wins") should hold regardless of input order.
    issue = Issue(subject="SKU-1", subject_kind="sku", supplier="Acme")
    scores = [
        _supplier_score("Acme", flagged=True, severity="low"),
        _supplier_score("Acme", flagged=True, severity="critical"),
    ]

    result = analyze_root_cause(issue, supplier_scores=scores)

    assert result.confidence == pytest.approx(0.85)
