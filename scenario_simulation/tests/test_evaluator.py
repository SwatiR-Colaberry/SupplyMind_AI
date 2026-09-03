from unittest.mock import patch

from inventory_risk.risk_model import InventoryPosition
from scenario_simulation.audit_trail import ScenarioSimulationAuditStore
from scenario_simulation.evaluator import ScenarioEvaluator
from scenario_simulation.simulation import ScenarioInput


def _baseline(**overrides) -> InventoryPosition:
    defaults = dict(sku="SKU-1", current_stock=100.0, safety_stock=20.0, daily_demand_rate=5.0, lead_time_days=10.0)
    defaults.update(overrides)
    return InventoryPosition(**defaults)


def test_run_simulates_the_scenario_and_returns_a_success_run(tmp_path):
    store = ScenarioSimulationAuditStore(tmp_path / "audit.jsonl")
    evaluator = ScenarioEvaluator(store)
    scenario = ScenarioInput(scenario_name="demand spike", baseline=_baseline(), demand_change_pct=3.0)

    run = evaluator.run(scenario, simulation_id="run-1")

    assert run.outcome == "success"
    assert run.crash_error is None
    assert run.impact is not None
    assert run.impact.risk_level_changed is True


def test_run_records_one_audit_entry_with_input_parameters(tmp_path):
    store = ScenarioSimulationAuditStore(tmp_path / "audit.jsonl")
    evaluator = ScenarioEvaluator(store)
    scenario = ScenarioInput(scenario_name="demand spike", baseline=_baseline(), demand_change_pct=3.0)

    evaluator.run(scenario, simulation_id="run-1")

    assert store.has_recorded("run-1", "demand spike", "SKU-1")
    records = store.records_for_simulation("run-1")
    assert len(records) == 1
    assert records[0].outcome == "success"
    assert records[0].input_parameters["demand_change_pct"] == 3.0


def test_run_is_idempotent_when_the_same_simulation_id_is_run_twice(tmp_path):
    store = ScenarioSimulationAuditStore(tmp_path / "audit.jsonl")
    evaluator = ScenarioEvaluator(store)
    scenario = ScenarioInput(scenario_name="demand spike", baseline=_baseline(), demand_change_pct=3.0)

    evaluator.run(scenario, simulation_id="run-1")
    evaluator.run(scenario, simulation_id="run-1")

    assert len(store.records_for_simulation("run-1")) == 1


def test_run_reports_invalid_input_for_a_scenario_that_drives_stock_negative(tmp_path):
    store = ScenarioSimulationAuditStore(tmp_path / "audit.jsonl")
    evaluator = ScenarioEvaluator(store)
    scenario = ScenarioInput(scenario_name="stock shock", baseline=_baseline(), stock_change=-500.0)

    run = evaluator.run(scenario, simulation_id="run-1")

    assert run.outcome == "invalid_input"
    assert run.impact is None
    assert run.limitation is not None
    records = store.records_for_simulation("run-1")
    assert len(records) == 1
    assert records[0].outcome == "failure"
    assert records[0].input_parameters["stock_change"] == -500.0


def test_run_records_a_failure_entry_for_an_unexpected_crash(tmp_path):
    # "Simulation API failure" failure path: a bug unrelated to bad/missing
    # input must still leave an auditable trace rather than propagate
    # with none.
    store = ScenarioSimulationAuditStore(tmp_path / "audit.jsonl")
    evaluator = ScenarioEvaluator(store)
    scenario = ScenarioInput(scenario_name="demand spike", baseline=_baseline(), demand_change_pct=3.0)

    with patch("scenario_simulation.evaluator.simulate_scenario", side_effect=RuntimeError("boom")):
        run = evaluator.run(scenario, simulation_id="run-1")

    assert run.outcome == "crashed"
    assert run.crash_error == "boom"
    records = store.records_for_simulation("run-1")
    assert len(records) == 1
    assert "RuntimeError" in records[0].detail


def test_run_generates_a_simulation_id_when_none_is_given(tmp_path):
    store = ScenarioSimulationAuditStore(tmp_path / "audit.jsonl")
    evaluator = ScenarioEvaluator(store)
    scenario = ScenarioInput(scenario_name="demand spike", baseline=_baseline(), demand_change_pct=3.0)

    run = evaluator.run(scenario)

    assert run.simulation_id
    assert store.records_for_simulation(run.simulation_id)


def test_run_returns_a_crashed_result_instead_of_raising_when_the_audit_store_cannot_be_written(tmp_path):
    unwritable_path = tmp_path / "not_a_file"
    unwritable_path.mkdir()
    store = ScenarioSimulationAuditStore(unwritable_path)
    evaluator = ScenarioEvaluator(store)
    scenario = ScenarioInput(scenario_name="demand spike", baseline=_baseline(), demand_change_pct=3.0)

    run = evaluator.run(scenario, simulation_id="run-1")

    assert run.outcome == "crashed"
    assert run.crash_error is not None


def test_fail_run_preserves_the_original_exception_even_when_the_audit_write_also_fails(tmp_path):
    unwritable_path = tmp_path / "not_a_file"
    unwritable_path.mkdir()
    store = ScenarioSimulationAuditStore(unwritable_path)
    evaluator = ScenarioEvaluator(store)
    scenario = ScenarioInput(scenario_name="stock shock", baseline=_baseline(), stock_change=-500.0)

    run = evaluator.run(scenario, simulation_id="run-1")  # invalid input -> invalid_input path

    assert run.outcome == "crashed"  # audit write itself is what fails here
    assert run.crash_error is not None
