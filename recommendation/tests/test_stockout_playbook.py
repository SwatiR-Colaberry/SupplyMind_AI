from __future__ import annotations

import pytest

from inventory_risk.risk_model import InventoryPosition, assess_stockout_risk
from recommendation.stockout_playbook import build_stockout_recommendation, describe_recommendation


def _assess(**overrides):
    defaults = dict(sku="SKU-1", current_stock=100.0, safety_stock=20.0, daily_demand_rate=5.0, lead_time_days=10.0)
    defaults.update(overrides)
    return assess_stockout_risk(InventoryPosition(**defaults))


def test_critical_with_no_incoming_stock_recommends_immediate_replenishment():
    assessment = _assess(current_stock=10.0, daily_demand_rate=5.0, lead_time_days=5.0)  # 2 days of supply
    assert assessment.risk_level == "critical"
    assert assessment.incoming_stock == 0.0

    rec = build_stockout_recommendation(assessment)

    assert "immediate replenishment review" in rec.recommendation
    assert "no confirmed incoming stock" in rec.reason
    assert rec.confidence == pytest.approx(assessment.stockout_probability)


def test_critical_with_incoming_stock_recommends_expediting_the_shipment_instead():
    assessment = _assess(current_stock=10.0, daily_demand_rate=5.0, lead_time_days=5.0, incoming_stock=50.0)
    assert assessment.risk_level == "critical"

    rec = build_stockout_recommendation(assessment)

    assert "Expedite the incoming shipment" in rec.recommendation
    assert "50" in rec.reason


def test_high_risk_with_a_supplier_delay_recommends_reviewing_alternatives():
    assessment = _assess(current_stock=100.0, safety_stock=5.0, daily_demand_rate=10.0, lead_time_days=5.0)  # 10 days -> high
    assert assessment.risk_level == "high"

    rec = build_stockout_recommendation(assessment, has_supplier_delay=True)

    assert "alternative supplier" in rec.recommendation
    assert "delivery delay" in rec.reason


def test_high_or_critical_with_increasing_demand_recommends_expedited_replenishment():
    assessment = _assess(current_stock=100.0, safety_stock=5.0, daily_demand_rate=10.0, lead_time_days=5.0)  # high
    assert assessment.risk_level == "high"

    rec = build_stockout_recommendation(assessment, has_supplier_delay=False, demand_trend="increasing")

    assert rec.recommendation == "Recommend expedited replenishment."
    assert "trending upward" in rec.reason


def test_supplier_delay_rule_takes_priority_over_the_demand_trend_rule():
    # Both conditions are true; the more specific "known supplier problem"
    # rule fires first, matching the fixed priority order in the module
    # docstring rather than a blended/scored decision.
    assessment = _assess(current_stock=100.0, safety_stock=5.0, daily_demand_rate=10.0, lead_time_days=5.0)

    rec = build_stockout_recommendation(assessment, has_supplier_delay=True, demand_trend="increasing")

    assert "alternative supplier" in rec.recommendation


def test_plain_high_risk_with_no_extra_context_recommends_reviewing_the_reorder_point():
    assessment = _assess(current_stock=100.0, safety_stock=5.0, daily_demand_rate=10.0, lead_time_days=5.0)

    rec = build_stockout_recommendation(assessment)

    assert "reorder point" in rec.recommendation


def test_medium_risk_recommends_monitoring():
    assessment = _assess(current_stock=100.0, safety_stock=5.0, daily_demand_rate=5.0, lead_time_days=5.0)  # 20 days -> medium
    assert assessment.risk_level == "medium"

    rec = build_stockout_recommendation(assessment)

    assert "Monitor" in rec.recommendation


def test_low_risk_recommends_no_action_and_uses_classification_confidence():
    assessment = _assess(current_stock=1000.0, daily_demand_rate=5.0, lead_time_days=10.0)  # 200 days -> low
    assert assessment.risk_level == "low"

    rec = build_stockout_recommendation(assessment)

    assert rec.recommendation == "No action required."
    assert rec.confidence == pytest.approx(assessment.confidence)


def test_problem_statement_includes_revenue_at_risk_when_priced():
    assessment = _assess(current_stock=10.0, safety_stock=5.0, daily_demand_rate=10.0, lead_time_days=5.0, unit_price=20.0)

    rec = build_stockout_recommendation(assessment)

    assert "revenue at risk" in rec.problem


def test_problem_statement_omits_revenue_at_risk_when_zero():
    assessment = _assess(current_stock=1000.0, daily_demand_rate=5.0, lead_time_days=10.0, unit_price=20.0)

    rec = build_stockout_recommendation(assessment)

    assert "revenue at risk" not in rec.problem


def test_describe_recommendation_matches_the_product_specs_worked_example_shape():
    assessment = _assess(current_stock=10.0, daily_demand_rate=5.0, lead_time_days=5.0)

    rec = build_stockout_recommendation(assessment)
    line = describe_recommendation(rec)

    assert line.startswith("Problem: ")
    assert " | Recommendation: " in line
    assert " | Reason: " in line
    assert " | Confidence: " in line
    assert line.endswith("%")
