from unittest.mock import patch

from shipment_delay_analysis.audit_trail import ShipmentDelayAuditStore
from shipment_delay_analysis.evaluator import ShipmentDelayEvaluator


def _row(supplier, po_id, expected, actual, transportation_cost=None):
    row = {"supplier": supplier, "po_id": po_id, "expected_date": expected, "actual_date": actual}
    if transportation_cost is not None:
        row["transportation_cost"] = transportation_cost
    return row


def test_run_analyzes_delays_and_returns_a_success_run(tmp_path):
    store = ShipmentDelayAuditStore(tmp_path / "audit.jsonl")
    evaluator = ShipmentDelayEvaluator(store)
    rows = [_row("Acme", "PO-1", "2025-01-01", "2025-01-06")]  # 5 days late

    run = evaluator.run(rows, analysis_id="analysis-1")

    assert run.outcome == "success"
    assert run.crash_error is None
    assert len(run.delay_costs) == 1
    assert run.delay_costs[0].po_id == "PO-1"
    assert run.total_cost > 0


def test_run_records_one_audit_entry_per_delayed_po(tmp_path):
    store = ShipmentDelayAuditStore(tmp_path / "audit.jsonl")
    evaluator = ShipmentDelayEvaluator(store)
    rows = [
        _row("Acme", "PO-1", "2025-01-01", "2025-01-06"),  # 5 days late
        _row("OtherCo", "PO-2", "2025-01-01", "2025-01-20"),  # 19 days late
    ]

    run = evaluator.run(rows, analysis_id="analysis-1")

    for cost in run.delay_costs:
        assert store.has_recorded("analysis-1", cost.po_id)
    records = store.records_for_analysis("analysis-1")
    assert len(records) == 2
    otherco_record = next(r for r in records if r.po_id == "PO-2")
    assert otherco_record.outcome == "success"  # process succeeded, even though this PO's delay is severe
    assert otherco_record.severity == "critical"
    assert otherco_record.total_cost is not None


def test_run_is_idempotent_when_the_same_analysis_id_is_run_twice(tmp_path):
    store = ShipmentDelayAuditStore(tmp_path / "audit.jsonl")
    evaluator = ShipmentDelayEvaluator(store)
    rows = [_row("Acme", "PO-1", "2025-01-01", "2025-01-06")]

    evaluator.run(rows, analysis_id="analysis-1")
    evaluator.run(rows, analysis_id="analysis-1")

    assert len(store.records_for_analysis("analysis-1")) == 1


def test_run_records_a_run_level_failure_entry_for_an_invalid_parameter(tmp_path):
    store = ShipmentDelayAuditStore(tmp_path / "audit.jsonl")
    evaluator = ShipmentDelayEvaluator(store)

    run = evaluator.run([], analysis_id="analysis-1", cost_per_day_late=-1)

    assert run.outcome == "crashed"
    assert run.crash_error is not None
    assert run.delay_costs == []
    records = store.records_for_analysis("analysis-1")
    assert len(records) == 1
    assert records[0].po_id is None
    assert records[0].outcome == "failure"


def test_run_records_a_run_level_failure_entry_for_an_unexpected_crash(tmp_path):
    # "Incorrect delay analysis" failure path: even a bug unrelated to bad
    # input (not just the documented DelayCostError) must still leave an
    # auditable trace rather than propagate with none.
    store = ShipmentDelayAuditStore(tmp_path / "audit.jsonl")
    evaluator = ShipmentDelayEvaluator(store)

    with patch(
        "shipment_delay_analysis.evaluator.analyze_shipment_delays",
        side_effect=RuntimeError("boom"),
    ):
        run = evaluator.run([_row("Acme", "PO-1", "2025-01-01", "2025-01-06")], analysis_id="analysis-1")

    assert run.outcome == "crashed"
    assert run.crash_error == "boom"
    records = store.records_for_analysis("analysis-1")
    assert len(records) == 1
    assert records[0].po_id is None
    assert "RuntimeError" in records[0].detail


def test_run_generates_an_analysis_id_when_none_is_given(tmp_path):
    store = ShipmentDelayAuditStore(tmp_path / "audit.jsonl")
    evaluator = ShipmentDelayEvaluator(store)

    run = evaluator.run([_row("Acme", "PO-1", "2025-01-01", "2025-01-06")])

    assert run.analysis_id
    assert store.records_for_analysis(run.analysis_id)


def test_run_leaves_a_run_level_audit_record_when_no_delivery_data_is_given(tmp_path):
    # Mirrors STORY-013's own regression: a "successful" run that finds
    # zero delayed POs (empty input) must not leave zero audit records -
    # indistinguishable from an analysis that never ran, which is exactly
    # the "audit trail missing for shipment analysis" failure path this
    # story exists to close.
    store = ShipmentDelayAuditStore(tmp_path / "audit.jsonl")
    evaluator = ShipmentDelayEvaluator(store)

    run = evaluator.run([], analysis_id="analysis-1")

    assert run.outcome == "success"
    assert run.delay_costs == []
    records = store.records_for_analysis("analysis-1")
    assert len(records) == 1
    assert records[0].po_id is None
    assert records[0].outcome == "success"


def test_run_leaves_a_run_level_audit_record_when_every_delivery_is_on_time(tmp_path):
    store = ShipmentDelayAuditStore(tmp_path / "audit.jsonl")
    evaluator = ShipmentDelayEvaluator(store)
    rows = [_row("Acme", "PO-1", "2025-01-01", "2025-01-01")]  # on time

    run = evaluator.run(rows, analysis_id="analysis-1")

    assert run.outcome == "success"
    assert run.delay_costs == []
    records = store.records_for_analysis("analysis-1")
    assert len(records) == 1
    assert records[0].po_id is None


def test_run_returns_a_crashed_result_instead_of_raising_when_the_audit_store_cannot_be_written(tmp_path):
    # Regression-shaped (mirrors STORY-013): a broken audit store (disk
    # full, permissions) must not propagate ShipmentDelayAuditWriteError
    # straight out of run() - callers that only expect a
    # ShipmentDelayAnalysisRun back would crash instead of seeing a
    # reported failure.
    unwritable_path = tmp_path / "not_a_file"
    unwritable_path.mkdir()
    store = ShipmentDelayAuditStore(unwritable_path)
    evaluator = ShipmentDelayEvaluator(store)

    run = evaluator.run([_row("Acme", "PO-1", "2025-01-01", "2025-01-06")], analysis_id="analysis-1")

    assert run.outcome == "crashed"
    assert run.crash_error is not None


def test_fail_run_preserves_the_original_exception_even_when_the_audit_write_also_fails(tmp_path):
    # If analyze_shipment_delays() fails AND the subsequent audit write
    # also fails, the original, more useful exception must still be the
    # one reported - not masked by the secondary audit-store failure.
    unwritable_path = tmp_path / "not_a_file"
    unwritable_path.mkdir()
    store = ShipmentDelayAuditStore(unwritable_path)
    evaluator = ShipmentDelayEvaluator(store)

    run = evaluator.run([], analysis_id="analysis-1", cost_per_day_late=-1)

    assert run.outcome == "crashed"
    assert "cost_per_day_late" in run.crash_error
