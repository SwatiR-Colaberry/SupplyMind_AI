import math

import pytest

from inventory_risk.risk_model import InventoryPosition
from scenario_simulation.simulation import ScenarioInput, ScenarioValidationError, simulate_scenario


def _baseline(**overrides) -> InventoryPosition:
    defaults = dict(sku="SKU-1", current_stock=100.0, safety_stock=20.0, daily_demand_rate=5.0, lead_time_days=10.0)
    defaults.update(overrides)
    return InventoryPosition(**defaults)


def test_demand_spike_scenario_worsens_risk_and_reports_impact():
    # Baseline: 100/5 = 20 days of supply -> "medium" (14 < 20 <= 30).
    # 4x demand: 100/20 = 5 days of supply -> "critical" (<= 7).
    scenario = ScenarioInput(scenario_name="300% demand spike", baseline=_baseline(), demand_change_pct=3.0)

    result = simulate_scenario(scenario)

    assert result.baseline.risk_level == "medium"
    assert result.projected.risk_level == "critical"
    assert result.risk_level_changed is True
    assert result.days_of_supply_delta < 0
    assert "worsens" in result.detail


def test_no_op_scenario_leaves_risk_unchanged():
    scenario = ScenarioInput(scenario_name="no-op", baseline=_baseline())

    result = simulate_scenario(scenario)

    assert result.risk_level_changed is False
    assert result.days_of_supply_delta == pytest.approx(0.0)
    assert "leaves unchanged" in result.detail


def test_lead_time_increase_can_worsen_risk():
    # Lead time 10 -> 25 pushes demand-during-lead-time to 125, past the
    # unchanged 100 units of current stock -> expected inventory at
    # lead-time end goes negative (100 - 125 = -25) -> "critical".
    scenario = ScenarioInput(scenario_name="supplier lead time +15d", baseline=_baseline(), lead_time_change_days=15.0)

    result = simulate_scenario(scenario)

    assert result.projected.risk_level == "critical"
    assert result.risk_level_changed is True


def test_safety_stock_increase_can_push_risk_to_high_with_no_physical_change():
    # A safety-stock raise alone can never make expected_inventory go
    # negative (that formula doesn't involve safety_stock at all), so the
    # worst a pure policy-floor change can do is cross into "high" (the
    # expected-inventory-below-safety-stock condition) - not "critical",
    # which is reserved for an actual projected physical shortage.
    scenario = ScenarioInput(scenario_name="raise safety stock", baseline=_baseline(), safety_stock_change=90.0)

    result = simulate_scenario(scenario)

    assert result.projected.risk_level == "high"
    assert result.risk_level_changed is True


def test_demand_halting_entirely_is_a_valid_scenario_not_an_error():
    scenario = ScenarioInput(scenario_name="demand halts", baseline=_baseline(), demand_change_pct=-1.0)

    result = simulate_scenario(scenario)

    assert math.isinf(result.projected.days_of_supply)
    assert math.isnan(result.days_of_supply_delta)


def test_blank_scenario_name_is_rejected():
    scenario = ScenarioInput(scenario_name="   ", baseline=_baseline())

    with pytest.raises(ScenarioValidationError):
        simulate_scenario(scenario)


def test_demand_change_below_negative_one_is_rejected():
    scenario = ScenarioInput(scenario_name="impossible", baseline=_baseline(), demand_change_pct=-1.5)

    with pytest.raises(ScenarioValidationError):
        simulate_scenario(scenario)


def test_delta_that_drives_projected_stock_negative_is_rejected_as_invalid_scenario():
    scenario = ScenarioInput(scenario_name="stock shock", baseline=_baseline(), stock_change=-500.0)

    with pytest.raises(ScenarioValidationError):
        simulate_scenario(scenario)


# --- multi-delta interaction coverage ---
# Every test above varies exactly one delta at a time. These prove all
# four combine additively and independently - each applied to the
# baseline's own field, not chained onto one another or onto a partial
# result - since _apply_deltas() builds every field from `baseline`
# directly rather than threading an intermediate position through four
# sequential updates.


def test_all_four_deltas_combine_additively_and_independently():
    scenario = ScenarioInput(
        scenario_name="combined change",
        baseline=_baseline(),
        demand_change_pct=0.2,  # 5.0 -> 6.0
        lead_time_change_days=5.0,  # 10.0 -> 15.0
        safety_stock_change=10.0,  # 20.0 -> 30.0
        stock_change=-20.0,  # 100.0 -> 80.0
    )

    result = simulate_scenario(scenario)

    # Projected: current 80, daily 6, lead 15 -> expected inventory
    # 80 - 6*15 = -10 (negative) -> "critical", regardless of the
    # day-count bucket 80/6=13.3 days would otherwise fall into.
    expected_days_of_supply = 80.0 / 6.0
    assert result.projected.days_of_supply == pytest.approx(expected_days_of_supply)
    assert result.projected.risk_level == "critical"
    assert result.days_of_supply_delta == pytest.approx(expected_days_of_supply - 20.0)


def test_lead_time_and_safety_stock_deltas_combine_to_cross_the_high_threshold():
    # Neither delta alone crosses a boundary: +5 lead time alone leaves
    # expected inventory at 100 - 5*15 = 25, still >= the baseline's own
    # 20-unit safety stock; +10 safety stock alone leaves expected
    # inventory at 100 - 5*10 = 50, still >= the new 30-unit floor. Only
    # applying both together (expected inventory 25 < new floor 30)
    # crosses into "high" - proving both deltas are genuinely combined,
    # not just the larger or the last one taking effect.
    scenario = ScenarioInput(
        scenario_name="lead time +5d + safety stock +10", baseline=_baseline(), lead_time_change_days=5.0, safety_stock_change=10.0
    )

    result = simulate_scenario(scenario)

    assert result.projected.expected_inventory == pytest.approx(25.0)
    assert result.projected.risk_level == "high"
    assert result.risk_level_changed is True


def test_optional_baseline_fields_carry_through_to_the_projected_position():
    # Regression: _apply_deltas() built the projected InventoryPosition
    # from only the 4 original fields, silently dropping incoming_stock/
    # unit_price/demand_std_dev even when the baseline had them - the
    # projected side of a priced scenario would report revenue_at_risk as
    # None while the baseline correctly reported a real number.
    baseline = _baseline(lead_time_days=25.0, unit_price=40.0, demand_std_dev=2.0, incoming_stock=5.0)
    scenario = ScenarioInput(scenario_name="no-op", baseline=baseline)

    result = simulate_scenario(scenario)

    assert result.projected.revenue_at_risk is not None
    assert result.projected.recommended_safety_stock is not None
    assert result.projected.incoming_stock == 5.0
