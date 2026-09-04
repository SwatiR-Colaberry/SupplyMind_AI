from unittest.mock import patch

from data_quality_monitoring.audit_trail import QualityAuditStore
from data_quality_monitoring.evaluator import DataQualityEvaluator

REQUIRED = ("po_id", "expected_date", "actual_date")


def _row(po_id="PO-1", expected="2025-01-01", actual="2025-01-01"):
    return {"po_id": po_id, "expected_date": expected, "actual_date": actual}


def test_run_scores_quality_and_returns_a_success_run(tmp_path):
    store = QualityAuditStore(tmp_path / "audit.jsonl")
    evaluator = DataQualityEvaluator(store)
    rows = [_row("PO-1"), _row("PO-2")]

    run = evaluator.run(rows, required_fields=REQUIRED, check_id="check-1")

    assert run.outcome == "success"
    assert run.crash_error is None
    assert run.report.overall_score == 100.0
    assert run.poor_quality is False


def test_run_records_one_audit_entry_per_dimension(tmp_path):
    store = QualityAuditStore(tmp_path / "audit.jsonl")
    evaluator = DataQualityEvaluator(store)
    rows = [_row("PO-1"), _row("PO-2")]

    run = evaluator.run(rows, required_fields=REQUIRED, check_id="check-1")

    for result in run.report.dimension_results:
        assert store.has_recorded("check-1", result.dimension)
    records = store.records_for_check("check-1")
    assert len(records) == len(run.report.dimension_results)
    completeness_record = next(r for r in records if r.dimension == "completeness")
    assert completeness_record.outcome == "success"  # process succeeded, even if the score were low
    assert completeness_record.score == 100.0


def test_run_is_idempotent_when_the_same_check_id_is_run_twice(tmp_path):
    store = QualityAuditStore(tmp_path / "audit.jsonl")
    evaluator = DataQualityEvaluator(store)
    rows = [_row("PO-1")]

    evaluator.run(rows, required_fields=REQUIRED, check_id="check-1")
    evaluator.run(rows, required_fields=REQUIRED, check_id="check-1")

    assert len(store.records_for_check("check-1")) == 1


def test_run_records_a_run_level_failure_entry_for_an_invalid_parameter(tmp_path):
    store = QualityAuditStore(tmp_path / "audit.jsonl")
    evaluator = DataQualityEvaluator(store)

    run = evaluator.run([_row("PO-1")], required_fields=(), check_id="check-1")

    assert run.outcome == "crashed"
    assert run.crash_error is not None
    assert run.report is None
    records = store.records_for_check("check-1")
    assert len(records) == 1
    assert records[0].dimension is None
    assert records[0].outcome == "failure"


def test_run_records_a_run_level_failure_entry_for_an_unexpected_crash(tmp_path):
    # "Quality monitoring fails" failure path: even a bug unrelated to bad
    # input (not just the documented DataQualityError) must still leave
    # an auditable trace rather than propagate with none.
    store = QualityAuditStore(tmp_path / "audit.jsonl")
    evaluator = DataQualityEvaluator(store)

    with patch(
        "data_quality_monitoring.evaluator.assess_data_quality",
        side_effect=RuntimeError("boom"),
    ):
        run = evaluator.run([_row("PO-1")], required_fields=REQUIRED, check_id="check-1")

    assert run.outcome == "crashed"
    assert run.crash_error == "boom"
    records = store.records_for_check("check-1")
    assert len(records) == 1
    assert records[0].dimension is None
    assert "RuntimeError" in records[0].detail


def test_run_generates_a_check_id_when_none_is_given(tmp_path):
    store = QualityAuditStore(tmp_path / "audit.jsonl")
    evaluator = DataQualityEvaluator(store)

    run = evaluator.run([_row("PO-1")], required_fields=REQUIRED)

    assert run.check_id
    assert store.records_for_check(run.check_id)


def test_run_leaves_a_run_level_audit_record_and_alerts_when_quality_is_poor(tmp_path):
    # AC2: given poor data quality, when detected, the system alerts the
    # data steward - and the alert itself must leave an audit trace, the
    # same "an outcome with no trace at all" gap STORY-013/014 close for
    # their own trust-spine ACs.
    store = QualityAuditStore(tmp_path / "audit.jsonl")
    evaluator = DataQualityEvaluator(store)
    rows = [_row("PO-1")] + [{"po_id": f"PO-{i}"} for i in range(2, 10)]  # 1/9 complete

    run = evaluator.run(rows, required_fields=REQUIRED, check_id="check-1")

    assert run.outcome == "success"
    assert run.poor_quality is True
    records = store.records_for_check("check-1")
    alert_record = next(r for r in records if r.dimension is None)
    assert alert_record.outcome == "success"  # the check ran fine; it's the *data* that's poor
    assert "ALERT" in alert_record.detail


def test_run_does_not_leave_a_run_level_record_when_quality_is_good(tmp_path):
    store = QualityAuditStore(tmp_path / "audit.jsonl")
    evaluator = DataQualityEvaluator(store)
    rows = [_row("PO-1"), _row("PO-2")]

    run = evaluator.run(rows, required_fields=REQUIRED, check_id="check-1")

    assert run.poor_quality is False
    records = store.records_for_check("check-1")
    assert all(r.dimension is not None for r in records)


def test_run_returns_a_crashed_result_instead_of_raising_when_the_audit_store_cannot_be_written(tmp_path):
    # Regression-shaped (mirrors STORY-013/014): a broken audit store
    # (disk full, permissions) must not propagate QualityAuditWriteError
    # straight out of run() - callers that only expect a
    # DataQualityCheckRun back would crash instead of seeing a reported
    # failure.
    unwritable_path = tmp_path / "not_a_file"
    unwritable_path.mkdir()
    store = QualityAuditStore(unwritable_path)
    evaluator = DataQualityEvaluator(store)

    run = evaluator.run([_row("PO-1")], required_fields=REQUIRED, check_id="check-1")

    assert run.outcome == "crashed"
    assert run.crash_error is not None


def test_fail_run_preserves_the_original_exception_even_when_the_audit_write_also_fails(tmp_path):
    # If assess_data_quality() fails AND the subsequent audit write also
    # fails, the original, more useful exception must still be the one
    # reported - not masked by the secondary audit-store failure.
    unwritable_path = tmp_path / "not_a_file"
    unwritable_path.mkdir()
    store = QualityAuditStore(unwritable_path)
    evaluator = DataQualityEvaluator(store)

    run = evaluator.run([_row("PO-1")], required_fields=(), check_id="check-1")

    assert run.outcome == "crashed"
    assert "required_fields" in run.crash_error
