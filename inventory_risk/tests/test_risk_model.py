from __future__ import annotations

import math

import pytest

from inventory_risk.risk_model import (
    CRITICAL_DAYS_THRESHOLD,
    HIGH_DAYS_THRESHOLD,
    MEDIUM_DAYS_THRESHOLD,
    InventoryPosition,
    RiskModelError,
    assess_stockout_risk,
    compute_average_daily_demand,
    compute_recommended_safety_stock,
    compute_revenue_at_risk,
    compute_risk_score,
    estimate_stockout_probability,
)


def _position(**overrides) -> InventoryPosition:
    defaults = dict(sku="SKU-1", current_stock=100.0, safety_stock=20.0, daily_demand_rate=5.0, lead_time_days=10.0)
    defaults.update(overrides)
    return InventoryPosition(**defaults)


# --- risk-level classification (day-based thresholds) -----------------


def test_low_risk_when_more_than_30_days_of_supply_and_safety_stock_holds():
    # 500 / 5 = 100 days of supply; expected inventory at lead-time end
    # (100 - 50 = 450) is far above the 20-unit safety floor.
    result = assess_stockout_risk(_position(current_stock=500.0, daily_demand_rate=5.0, lead_time_days=10.0))
    assert result.risk_level == "low"
    assert result.days_of_supply == pytest.approx(100.0)
    assert result.confidence > 0.0


def test_medium_risk_when_stockout_expected_within_15_to_30_days():
    # 100 / 5 = 20 days of supply - inside (HIGH_DAYS_THRESHOLD, MEDIUM_DAYS_THRESHOLD].
    result = assess_stockout_risk(_position(current_stock=100.0, safety_stock=5.0, daily_demand_rate=5.0, lead_time_days=5.0))
    assert result.risk_level == "medium"
    assert result.days_of_supply == pytest.approx(20.0)


def test_high_risk_when_stockout_expected_within_8_to_14_days():
    # 100 / 10 = 10 days of supply - inside (CRITICAL_DAYS_THRESHOLD, HIGH_DAYS_THRESHOLD].
    result = assess_stockout_risk(_position(current_stock=100.0, safety_stock=5.0, daily_demand_rate=10.0, lead_time_days=5.0))
    assert result.risk_level == "high"
    assert result.days_of_supply == pytest.approx(10.0)


def test_critical_risk_when_stockout_expected_within_7_days():
    # 30 / 10 = 3 days of supply.
    result = assess_stockout_risk(_position(current_stock=30.0, safety_stock=5.0, daily_demand_rate=10.0, lead_time_days=5.0))
    assert result.risk_level == "critical"
    assert result.days_of_supply == pytest.approx(3.0)


def test_critical_risk_when_current_stock_is_already_zero():
    result = assess_stockout_risk(_position(current_stock=0.0, safety_stock=5.0, daily_demand_rate=1.0))
    assert result.risk_level == "critical"
    assert result.confidence == 1.0


def test_critical_risk_when_expected_inventory_before_replenishment_is_negative():
    # 100 / 5 = 20 days of supply on its own (would read "medium"), but a
    # 25-day lead time means demand-during-lead-time (125) exceeds current
    # stock -> expected inventory = 100 - 125 = -25, a projected shortage
    # regardless of the day-count bucket.
    result = assess_stockout_risk(_position(current_stock=100.0, safety_stock=5.0, daily_demand_rate=5.0, lead_time_days=25.0))
    assert result.expected_inventory == pytest.approx(-25.0)
    assert result.risk_level == "critical"


def test_high_risk_when_expected_inventory_dips_below_safety_stock_but_stays_non_negative():
    # 200 / 5 = 40 days of supply on its own (would read "low"), but
    # expected inventory at lead-time end (200 - 50 = 150) sits below the
    # 160-unit safety floor without going negative - "high", not "critical".
    result = assess_stockout_risk(_position(current_stock=200.0, safety_stock=160.0, daily_demand_rate=5.0, lead_time_days=10.0))
    assert result.expected_inventory == pytest.approx(150.0)
    assert result.risk_level == "high"


def test_incoming_stock_can_pull_expected_inventory_back_to_non_negative():
    # Same shape as the negative-expected-inventory test above, but 50
    # units of confirmed incoming stock closes the gap exactly.
    result = assess_stockout_risk(
        _position(current_stock=100.0, safety_stock=5.0, daily_demand_rate=5.0, lead_time_days=25.0, incoming_stock=25.0)
    )
    assert result.expected_inventory == pytest.approx(0.0)
    assert result.risk_level != "critical"


def test_zero_demand_rate_is_low_risk_when_safety_stock_holds():
    result = assess_stockout_risk(_position(current_stock=100.0, safety_stock=20.0, daily_demand_rate=0.0))
    assert math.isinf(result.days_of_supply)
    assert result.risk_level == "low"
    assert result.confidence == 1.0


def test_zero_demand_rate_flags_high_not_critical_when_below_safety_stock():
    # No consumption means nothing is actively depleting stock further, so
    # a safety-stock shortfall alone reads "high" (a policy floor
    # breach), not "critical" (an active, worsening stockout) - contrast
    # with the next test, where current_stock <= 0 is critical regardless.
    result = assess_stockout_risk(_position(current_stock=5.0, safety_stock=20.0, daily_demand_rate=0.0))
    assert result.risk_level == "high"
    assert math.isinf(result.days_of_supply)


def test_zero_demand_and_zero_stock_is_still_critical():
    result = assess_stockout_risk(_position(current_stock=0.0, safety_stock=20.0, daily_demand_rate=0.0))
    assert result.risk_level == "critical"
    assert result.confidence == 1.0


# --- confidence -----------------------------------------------------


def test_confidence_is_bounded_between_zero_and_one():
    scenarios = [
        _position(current_stock=500.0, daily_demand_rate=5.0, lead_time_days=10.0),
        _position(current_stock=100.0, safety_stock=5.0, daily_demand_rate=5.0, lead_time_days=5.0),
        _position(current_stock=100.0, safety_stock=5.0, daily_demand_rate=10.0, lead_time_days=5.0),
        _position(current_stock=30.0, safety_stock=5.0, daily_demand_rate=10.0, lead_time_days=5.0),
        _position(current_stock=0.0, safety_stock=5.0, daily_demand_rate=1.0),
        _position(current_stock=200.0, safety_stock=160.0, daily_demand_rate=5.0, lead_time_days=10.0),
    ]
    for position in scenarios:
        result = assess_stockout_risk(position)
        assert 0.0 <= result.confidence <= 1.0


def test_confidence_near_a_boundary_is_lower_than_far_from_it_within_a_zone():
    # "medium" spans (14, 30] days of supply, centered at 22. 15 days sits
    # just inside the boundary with "high"; 22 sits at the zone's center.
    near_boundary = assess_stockout_risk(_position(current_stock=75.0, safety_stock=1.0, daily_demand_rate=5.0, lead_time_days=1.0))
    center_of_zone = assess_stockout_risk(_position(current_stock=110.0, safety_stock=1.0, daily_demand_rate=5.0, lead_time_days=1.0))
    assert near_boundary.days_of_supply == pytest.approx(15.0)
    assert center_of_zone.days_of_supply == pytest.approx(22.0)
    assert near_boundary.risk_level == center_of_zone.risk_level == "medium"
    assert near_boundary.confidence < center_of_zone.confidence


def test_critical_confidence_scales_with_depth_of_negative_expected_inventory():
    # Both critical via a negative expected_inventory, but one is far
    # deeper underwater relative to lead-time demand than the other.
    shallow = assess_stockout_risk(_position(current_stock=100.0, safety_stock=5.0, daily_demand_rate=5.0, lead_time_days=21.0))
    deep = assess_stockout_risk(_position(current_stock=100.0, safety_stock=5.0, daily_demand_rate=5.0, lead_time_days=60.0))
    assert shallow.risk_level == deep.risk_level == "critical"
    assert deep.confidence > shallow.confidence


# --- optional fields: incoming_stock, unit_price, demand_std_dev ------


def test_revenue_at_risk_is_none_without_a_unit_price():
    result = assess_stockout_risk(_position(current_stock=100.0, daily_demand_rate=5.0, lead_time_days=25.0))
    assert result.revenue_at_risk is None


def test_revenue_at_risk_is_populated_and_zero_when_no_shortage_is_projected():
    result = assess_stockout_risk(_position(current_stock=500.0, daily_demand_rate=5.0, lead_time_days=10.0, unit_price=20.0))
    assert result.revenue_at_risk == 0.0


def test_revenue_at_risk_reflects_the_projected_shortage_times_unit_price():
    # expected_inventory = 100 - 125 = -25 -> 25 units short x $40 = $1,000.
    result = assess_stockout_risk(
        _position(current_stock=100.0, safety_stock=5.0, daily_demand_rate=5.0, lead_time_days=25.0, unit_price=40.0)
    )
    assert result.revenue_at_risk == pytest.approx(1000.0)


def test_recommended_safety_stock_is_none_without_demand_std_dev():
    result = assess_stockout_risk(_position())
    assert result.recommended_safety_stock is None


def test_recommended_safety_stock_is_populated_when_demand_std_dev_is_given():
    result = assess_stockout_risk(_position(demand_std_dev=10.0, lead_time_days=9.0))
    # 1.65 * 10 * sqrt(9) = 49.5, matching the product spec's own worked example.
    assert result.recommended_safety_stock == pytest.approx(49.5)


def test_stockout_probability_is_higher_with_demand_std_dev_when_short_on_supply():
    with_variability = assess_stockout_risk(_position(current_stock=30.0, daily_demand_rate=5.0, lead_time_days=10.0, demand_std_dev=5.0))
    assert 0.0 <= with_variability.stockout_probability <= 1.0


def test_stockout_probability_is_near_zero_for_a_deeply_healthy_position():
    result = assess_stockout_risk(_position(current_stock=1000.0, daily_demand_rate=5.0, lead_time_days=10.0))
    assert result.stockout_probability < 0.05


def test_supplier_is_none_by_default():
    result = assess_stockout_risk(_position())
    assert result.supplier is None


def test_supplier_is_carried_through_unchanged_from_the_input_position():
    result = assess_stockout_risk(_position(supplier="Acme Supply"))
    assert result.supplier == "Acme Supply"


def test_stockout_probability_is_near_one_for_an_already_out_of_stock_position():
    result = assess_stockout_risk(_position(current_stock=0.0, daily_demand_rate=5.0, lead_time_days=10.0))
    assert result.stockout_probability == 0.0 or result.stockout_probability > 0.9


def test_risk_score_is_between_zero_and_a_hundred():
    for position in [
        _position(current_stock=500.0, daily_demand_rate=5.0),
        _position(current_stock=0.0, daily_demand_rate=5.0),
        _position(current_stock=30.0, safety_stock=5.0, daily_demand_rate=10.0, lead_time_days=5.0),
    ]:
        result = assess_stockout_risk(position)
        assert 0.0 <= result.risk_score <= 100.0


def test_risk_score_is_higher_for_a_riskier_position():
    healthy = assess_stockout_risk(_position(current_stock=500.0, daily_demand_rate=5.0, lead_time_days=10.0))
    risky = assess_stockout_risk(_position(current_stock=10.0, safety_stock=5.0, daily_demand_rate=10.0, lead_time_days=10.0))
    assert risky.risk_score > healthy.risk_score


# --- validation -------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"current_stock": -1.0},
        {"safety_stock": -1.0},
        {"daily_demand_rate": -1.0},
        {"lead_time_days": 0.0},
        {"lead_time_days": -5.0},
        {"incoming_stock": -1.0},
        {"unit_price": -1.0},
        {"demand_std_dev": -1.0},
    ],
)
def test_invalid_values_raise_risk_model_error(overrides):
    with pytest.raises(RiskModelError):
        assess_stockout_risk(_position(**overrides))


@pytest.mark.parametrize(
    "field_name", ["current_stock", "safety_stock", "daily_demand_rate", "lead_time_days", "incoming_stock", "unit_price", "demand_std_dev"]
)
def test_nan_value_raises_risk_model_error_instead_of_producing_a_silent_low_risk_result(field_name):
    # Regression: every NaN comparison evaluates False, so without an explicit
    # guard a NaN value fell through every risk-level branch to "low" with
    # confidence 1.0 - a confidently wrong result for corrupted input. This is
    # a defense-in-depth guard for a caller that builds an InventoryPosition
    # directly, bypassing inventory_risk/data_quality.py's own NaN check.
    with pytest.raises(RiskModelError, match="NaN"):
        assess_stockout_risk(_position(**{field_name: float("nan")}))


# --- standalone helper functions ---------------------------------------


def test_compute_average_daily_demand():
    assert compute_average_daily_demand(500.0, period_days=30.0) == pytest.approx(500.0 / 30.0)


def test_compute_average_daily_demand_rejects_a_non_positive_period():
    with pytest.raises(ValueError):
        compute_average_daily_demand(500.0, period_days=0.0)


def test_compute_recommended_safety_stock_matches_the_product_specs_worked_example():
    assert compute_recommended_safety_stock(demand_std_dev=10.0, lead_time_days=9.0) == pytest.approx(49.5)


def test_estimate_stockout_probability_is_zero_for_infinite_supply():
    assert estimate_stockout_probability(math.inf, lead_time_days=10.0, daily_demand_rate=0.0) == 0.0


def test_estimate_stockout_probability_without_variability_data_is_half_at_the_lead_time_boundary():
    # coverage_ratio == 1.0 (days_of_supply exactly equals lead time) is the
    # logistic's center by construction.
    probability = estimate_stockout_probability(10.0, lead_time_days=10.0, daily_demand_rate=5.0)
    assert probability == pytest.approx(0.5, abs=1e-9)


def test_estimate_stockout_probability_falls_as_coverage_improves():
    tight = estimate_stockout_probability(5.0, lead_time_days=10.0, daily_demand_rate=5.0)
    comfortable = estimate_stockout_probability(50.0, lead_time_days=10.0, daily_demand_rate=5.0)
    assert tight > comfortable


def test_compute_revenue_at_risk_is_zero_when_no_shortage_is_projected():
    assert compute_revenue_at_risk(expected_inventory=50.0, unit_price=10.0) == 0.0


def test_compute_revenue_at_risk_multiplies_shortage_by_unit_price():
    assert compute_revenue_at_risk(expected_inventory=-40.0, unit_price=25.0) == pytest.approx(1000.0)


def test_compute_risk_score_uses_only_supplied_optional_signals():
    # Same 2 always-available signals; adding a maxed-out optional signal
    # should only be able to push the score up, never leave it unchanged,
    # since its full weight is folded in rather than defaulted to neutral.
    base = compute_risk_score(stockout_probability=0.5, days_of_supply=20.0)
    with_extra_risk = compute_risk_score(stockout_probability=0.5, days_of_supply=20.0, supplier_lead_time_risk=1.0)
    assert with_extra_risk > base


def test_compute_risk_score_with_every_signal_matches_the_product_specs_weights():
    score = compute_risk_score(
        stockout_probability=1.0,
        days_of_supply=0.0,
        demand_volatility=1.0,
        supplier_lead_time_risk=1.0,
        incoming_shipment_risk=1.0,
    )
    assert score == pytest.approx(100.0)
