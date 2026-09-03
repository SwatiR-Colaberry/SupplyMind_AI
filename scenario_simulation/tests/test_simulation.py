import math

import pytest

from inventory_risk.risk_model import InventoryPosition
from scenario_simulation.simulation import ScenarioInput, ScenarioValidationError, simulate_scenario


def _baseline(**overrides) -> InventoryPosition:
    defaults = dict(sku="SKU-1", current_stock=100.0, safety_stock=20.0, daily_demand_rate=5.0, lead_time_days=10.0)
    defaults.update(overrides)
    return InventoryPosition(**defaults)


def test_demand_spike_scenario_worsens_risk_and_reports_impact():
    scenario = ScenarioInput(scenario_name="300% demand spike", baseline=_baseline(), demand_change_pct=3.0)

    result = simulate_scenario(scenario)

    assert result.baseline.risk_level == "low"
    assert result.projected.risk_level == "high"
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
    scenario = ScenarioInput(scenario_name="supplier lead time +15d", baseline=_baseline(), lead_time_change_days=15.0)

    result = simulate_scenario(scenario)

    assert result.projected.risk_level == "high"
    assert result.risk_level_changed is True


def test_safety_stock_increase_can_push_risk_to_critical_with_no_physical_change():
    # A large safety-stock raise, with no other change, can push current
    # stock at/under the new floor - risk worsens even though nothing
    # about the physical stock or demand changed.
    scenario = ScenarioInput(scenario_name="raise safety stock", baseline=_baseline(), safety_stock_change=90.0)

    result = simulate_scenario(scenario)

    assert result.projected.risk_level == "critical"
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

    expected_days_of_supply = 80.0 / 6.0
    assert result.projected.days_of_supply == pytest.approx(expected_days_of_supply)
    assert result.projected.risk_level == "high"
    assert result.days_of_supply_delta == pytest.approx(expected_days_of_supply - 20.0)


def test_stock_and_safety_stock_deltas_combine_to_cross_the_critical_threshold():
    # Neither delta alone breaches the safety-stock floor: -70 stock
    # alone leaves 30 > the baseline's own 20 safety stock; +15 safety
    # stock alone leaves the baseline's own 100 current stock untouched.
    # Only applying both together (current 30, safety 35) crosses it -
    # proving both deltas are genuinely combined, not just the larger or
    # the last one taking effect.
    scenario = ScenarioInput(
        scenario_name="stock cut + safety raise", baseline=_baseline(), stock_change=-70.0, safety_stock_change=15.0
    )

    result = simulate_scenario(scenario)

    assert result.projected.risk_level == "critical"
