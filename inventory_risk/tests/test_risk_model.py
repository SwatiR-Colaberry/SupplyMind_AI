from __future__ import annotations

import math

import pytest

from inventory_risk.risk_model import InventoryPosition, RiskModelError, assess_stockout_risk


def _position(**overrides) -> InventoryPosition:
    defaults = dict(sku="SKU-1", current_stock=100.0, safety_stock=20.0, daily_demand_rate=5.0, lead_time_days=10.0)
    defaults.update(overrides)
    return InventoryPosition(**defaults)


def test_low_risk_when_stock_covers_well_beyond_lead_time():
    # 100 units / 5 per day = 20 days of supply against a 10-day lead time -> coverage 2.0, above 1.5.
    result = assess_stockout_risk(_position(current_stock=100.0, daily_demand_rate=5.0, lead_time_days=10.0))
    assert result.risk_level == "low"
    assert result.confidence > 0.0


def test_medium_risk_when_coverage_between_one_and_medium_threshold():
    # 100 / 8 = 12.5 days vs 10-day lead time -> coverage 1.25, between 1.0 and 1.5.
    result = assess_stockout_risk(_position(current_stock=100.0, safety_stock=5.0, daily_demand_rate=8.0, lead_time_days=10.0))
    assert result.risk_level == "medium"


def test_high_risk_when_stock_runs_out_before_replenishment():
    # 50 / 10 = 5 days of supply vs 10-day lead time -> coverage 0.5, but still above safety stock.
    result = assess_stockout_risk(_position(current_stock=50.0, safety_stock=5.0, daily_demand_rate=10.0, lead_time_days=10.0))
    assert result.risk_level == "high"


def test_critical_risk_when_at_or_below_safety_stock():
    result = assess_stockout_risk(_position(current_stock=20.0, safety_stock=20.0, daily_demand_rate=5.0))
    assert result.risk_level == "critical"

    result_below = assess_stockout_risk(_position(current_stock=10.0, safety_stock=20.0, daily_demand_rate=5.0))
    assert result_below.risk_level == "critical"


def test_zero_demand_rate_yields_infinite_supply_low_risk_and_full_confidence():
    result = assess_stockout_risk(_position(current_stock=100.0, safety_stock=20.0, daily_demand_rate=0.0))
    assert math.isinf(result.days_of_supply)
    assert result.risk_level == "low"
    assert result.confidence == 1.0


def test_zero_demand_rate_still_flags_critical_if_below_safety_stock():
    # A safety-stock breach is a risk signal on its own, independent of consumption rate.
    result = assess_stockout_risk(_position(current_stock=5.0, safety_stock=20.0, daily_demand_rate=0.0))
    assert result.risk_level == "critical"
    assert math.isinf(result.days_of_supply)


def test_confidence_is_bounded_between_zero_and_one():
    scenarios = [
        _position(current_stock=100.0, daily_demand_rate=5.0, lead_time_days=10.0),
        _position(current_stock=50.0, safety_stock=5.0, daily_demand_rate=10.0, lead_time_days=10.0),
        _position(current_stock=20.0, safety_stock=20.0, daily_demand_rate=5.0),
        _position(current_stock=1000.0, safety_stock=5.0, daily_demand_rate=1.0, lead_time_days=3.0),
    ]
    for position in scenarios:
        result = assess_stockout_risk(position)
        assert 0.0 <= result.confidence <= 1.0


def test_confidence_near_a_boundary_is_lower_than_far_from_it():
    # Just above the coverage=1.0 boundary (low/medium's inner edge into "medium") vs deep in "low".
    near_boundary = assess_stockout_risk(
        _position(current_stock=101.0, safety_stock=5.0, daily_demand_rate=10.0, lead_time_days=10.0)
    )
    far_from_boundary = assess_stockout_risk(
        _position(current_stock=1000.0, safety_stock=5.0, daily_demand_rate=10.0, lead_time_days=10.0)
    )
    assert near_boundary.confidence < far_from_boundary.confidence


@pytest.mark.parametrize(
    "overrides",
    [
        {"current_stock": -1.0},
        {"safety_stock": -1.0},
        {"daily_demand_rate": -1.0},
        {"lead_time_days": 0.0},
        {"lead_time_days": -5.0},
    ],
)
def test_invalid_values_raise_risk_model_error(overrides):
    with pytest.raises(RiskModelError):
        assess_stockout_risk(_position(**overrides))


@pytest.mark.parametrize("field_name", ["current_stock", "safety_stock", "daily_demand_rate", "lead_time_days"])
def test_nan_value_raises_risk_model_error_instead_of_producing_a_silent_low_risk_result(field_name):
    # Regression: every NaN comparison evaluates False, so without an explicit
    # guard a NaN value fell through every risk-level branch to "low" with
    # confidence 1.0 - a confidently wrong result for corrupted input. This is
    # a defense-in-depth guard for a caller that builds an InventoryPosition
    # directly, bypassing inventory_risk/data_quality.py's own NaN check.
    with pytest.raises(RiskModelError, match="NaN"):
        assess_stockout_risk(_position(**{field_name: float("nan")}))
