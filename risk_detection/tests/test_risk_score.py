from inventory_risk.risk_model import StockoutRiskAssessment
from risk_detection.anomaly_detection import DemandAnomaly, SupplierDelayAnomaly
from risk_detection.risk_score import compute_risk_score


def _demand_anomaly(period: str, severity: str) -> DemandAnomaly:
    return DemandAnomaly(
        period=period,
        quantity=1.0,
        baseline_mean=1.0,
        baseline_stdev=1.0,
        z_score=1.0,
        direction="spike",
        severity=severity,
        detail="detail",
    )


def _supplier_delay(po_id: str, severity: str) -> SupplierDelayAnomaly:
    return SupplierDelayAnomaly(
        po_id=po_id,
        supplier=None,
        expected_date="2025-01-01",
        actual_date="2025-01-05",
        delay_days=4,
        severity=severity,
        detail="detail",
    )


def _stockout(sku: str, risk_level: str) -> StockoutRiskAssessment:
    return StockoutRiskAssessment(
        sku=sku, days_of_supply=1.0, risk_level=risk_level, confidence=0.5, detail="detail"
    )


def test_compute_risk_score_with_no_inputs_is_zero_and_low():
    result = compute_risk_score()

    assert result.score == 0
    assert result.severity == "low"
    assert result.contributions == []
    assert "no anomalies" in result.explanation.lower()


def test_compute_risk_score_severity_never_falls_below_the_single_worst_contributor():
    critical_only = compute_risk_score(demand_anomalies=[_demand_anomaly("2025-01", "critical")])

    assert critical_only.severity == "critical"
    assert critical_only.score == 30.0


def test_compute_risk_score_compounds_several_lesser_factors_into_a_higher_severity():
    two_mediums = compute_risk_score(
        demand_anomalies=[_demand_anomaly("2025-01", "medium")],
        supplier_delays=[_supplier_delay("PO-1", "medium")],
    )
    four_mediums = compute_risk_score(
        demand_anomalies=[_demand_anomaly("2025-01", "medium"), _demand_anomaly("2025-02", "medium")],
        supplier_delays=[_supplier_delay("PO-1", "medium"), _supplier_delay("PO-2", "medium")],
    )

    assert two_mediums.severity == "medium"
    assert four_mediums.score == 40.0
    assert four_mediums.severity == "high"


def test_compute_risk_score_caps_at_max_score():
    many_criticals = [_demand_anomaly(f"2025-{i:02d}", "critical") for i in range(1, 6)]

    result = compute_risk_score(demand_anomalies=many_criticals)

    assert result.score == 100.0
    assert result.severity == "critical"


def test_compute_risk_score_ignores_low_stockout_risk_but_counts_medium_and_above():
    low_only = compute_risk_score(stockout_assessments=[_stockout("SKU-1", "low")])
    medium_only = compute_risk_score(stockout_assessments=[_stockout("SKU-1", "medium")])
    critical_only = compute_risk_score(stockout_assessments=[_stockout("SKU-1", "critical")])

    assert low_only.score == 0
    assert low_only.contributions == []
    assert medium_only.score == 5.0
    assert critical_only.score == 25.0
    assert critical_only.severity == "critical"


def test_compute_risk_score_explanation_names_the_top_contributing_factors():
    result = compute_risk_score(
        demand_anomalies=[_demand_anomaly("2025-01", "critical")],
        supplier_delays=[_supplier_delay("PO-1", "medium")],
    )

    assert "2025-01" in result.explanation
    assert "PO-1" in result.explanation


def test_compute_risk_score_explanation_truncates_beyond_the_top_factors_and_says_how_many_remain():
    many_criticals = [_demand_anomaly(f"2025-{i:02d}", "critical") for i in range(1, 8)]

    result = compute_risk_score(demand_anomalies=many_criticals)

    assert "2025-01" in result.explanation
    assert "+2 more" in result.explanation


def test_compute_risk_score_contributions_are_sorted_highest_points_first():
    result = compute_risk_score(
        demand_anomalies=[_demand_anomaly("2025-01", "medium")],
        supplier_delays=[_supplier_delay("PO-1", "critical")],
        stockout_assessments=[_stockout("SKU-1", "high")],
    )

    assert [c.points for c in result.contributions] == sorted(
        (c.points for c in result.contributions), reverse=True
    )
    assert result.contributions[0].source == "supplier_delay"
