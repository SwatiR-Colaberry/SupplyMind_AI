import json
import math
import uuid
from datetime import datetime

import pytest

from scenario_simulation.audit_trail import ScenarioSimulationAuditStore, ScenarioSimulationAuditWriteError


def test_record_creates_entry_with_unique_id_timestamp_and_input_parameters(tmp_path):
    store = ScenarioSimulationAuditStore(tmp_path / "audit.jsonl")

    entry = store.record(
        simulation_id="run-1",
        scenario_name="20% demand spike",
        sku="SKU-1",
        outcome="success",
        input_parameters={"demand_change_pct": 0.2, "lead_time_change_days": 0.0},
        risk_level_changed=True,
        days_of_supply_delta=-3.5,
        detail="scenario worsens risk from low to medium",
    )

    assert uuid.UUID(entry.record_id)  # raises ValueError if not a valid UUID
    datetime.fromisoformat(entry.timestamp)  # raises ValueError if not ISO-8601

    lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    on_disk = json.loads(lines[0])
    assert on_disk["simulation_id"] == "run-1"
    assert on_disk["scenario_name"] == "20% demand spike"
    assert on_disk["sku"] == "SKU-1"
    assert on_disk["input_parameters"] == {"demand_change_pct": 0.2, "lead_time_change_days": 0.0}
    assert on_disk["days_of_supply_delta"] == -3.5


def test_nan_and_inf_deltas_are_normalized_to_null_for_valid_json(tmp_path):
    store = ScenarioSimulationAuditStore(tmp_path / "audit.jsonl")

    store.record(simulation_id="run-1", scenario_name="demand halts", sku="SKU-1", outcome="success", days_of_supply_delta=math.nan)

    on_disk = json.loads((tmp_path / "audit.jsonl").read_text().strip())
    assert on_disk["days_of_supply_delta"] is None


def test_re_recording_same_simulation_scenario_and_sku_does_not_duplicate(tmp_path):
    store = ScenarioSimulationAuditStore(tmp_path / "audit.jsonl")

    first = store.record(simulation_id="run-1", scenario_name="s1", sku="SKU-1", outcome="success")
    second = store.record(simulation_id="run-1", scenario_name="s1", sku="SKU-1", outcome="success")

    assert first.record_id == second.record_id
    lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1


def test_has_recorded_reflects_recorded_simulation_scenario_sku_triples(tmp_path):
    store = ScenarioSimulationAuditStore(tmp_path / "audit.jsonl")

    assert store.has_recorded("run-1", "s1", "SKU-1") is False
    store.record(simulation_id="run-1", scenario_name="s1", sku="SKU-1", outcome="success")
    assert store.has_recorded("run-1", "s1", "SKU-1") is True
    assert store.has_recorded("run-1", "s1", "SKU-2") is False


def test_records_for_simulation_returns_only_that_runs_scenarios(tmp_path):
    store = ScenarioSimulationAuditStore(tmp_path / "audit.jsonl")
    store.record(simulation_id="run-1", scenario_name="s1", sku="SKU-1", outcome="success")
    store.record(simulation_id="run-1", scenario_name="s1", sku="SKU-2", outcome="success")
    store.record(simulation_id="run-2", scenario_name="s1", sku="SKU-1", outcome="failure", detail="invalid scenario")

    run_1_skus = {r.sku for r in store.records_for_simulation("run-1")}
    assert run_1_skus == {"SKU-1", "SKU-2"}
    assert len(store.records_for_simulation("run-2")) == 1


def test_records_survive_reload_from_disk(tmp_path):
    path = tmp_path / "audit.jsonl"
    ScenarioSimulationAuditStore(path).record(simulation_id="run-1", scenario_name="s1", sku="SKU-1", outcome="success")

    reloaded = ScenarioSimulationAuditStore(path)

    assert reloaded.has_recorded("run-1", "s1", "SKU-1") is True


def test_corrupted_trailing_line_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "audit.jsonl"
    store = ScenarioSimulationAuditStore(path)
    store.record(simulation_id="run-1", scenario_name="s1", sku="SKU-1", outcome="success")
    with path.open("a", encoding="utf-8") as f:
        f.write("{not valid json\n")

    reloaded = ScenarioSimulationAuditStore(path)  # must not raise

    assert reloaded.has_recorded("run-1", "s1", "SKU-1") is True


def test_record_raises_a_typed_error_when_the_write_fails(tmp_path):
    # "Audit trail not recorded for simulations" failure path: a broken
    # write must surface loudly, not be silently swallowed. Simulated by
    # pointing the store's path at a directory, so opening it for append
    # fails with OSError.
    path = tmp_path / "not_a_file"
    path.mkdir()
    store = ScenarioSimulationAuditStore(path)

    with pytest.raises(ScenarioSimulationAuditWriteError, match="SKU-1"):
        store.record(simulation_id="run-1", scenario_name="s1", sku="SKU-1", outcome="success")
