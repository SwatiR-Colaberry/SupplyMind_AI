from unittest.mock import patch

from supplier_evaluation.audit_trail import SupplierEvaluationAuditStore
from supplier_evaluation.evaluator import RUN_LEVEL_AUDIT_SUPPLIER, SupplierEvaluator


def _row(supplier, po_id, expected, actual):
    return {"supplier": supplier, "po_id": po_id, "expected_date": expected, "actual_date": actual}


def test_run_evaluates_suppliers_and_returns_a_success_run(tmp_path):
    store = SupplierEvaluationAuditStore(tmp_path / "audit.jsonl")
    evaluator = SupplierEvaluator(store)
    rows = [
        _row("Acme", "PO-1", "2025-01-01", "2025-01-01"),
        _row("Acme", "PO-2", "2025-01-05", "2025-01-05"),
        _row("Acme", "PO-3", "2025-01-10", "2025-01-10"),
    ]

    run = evaluator.run(rows, evaluation_id="eval-1")

    assert run.outcome == "success"
    assert run.crash_error is None
    assert len(run.scores) == 1
    assert run.scores[0].supplier == "Acme"


def test_run_records_one_audit_entry_per_supplier(tmp_path):
    store = SupplierEvaluationAuditStore(tmp_path / "audit.jsonl")
    evaluator = SupplierEvaluator(store)
    rows = [
        _row("Acme", "PO-1", "2025-01-01", "2025-01-01"),
        _row("OtherCo", "PO-2", "2025-01-01", "2025-01-20"),  # 19 days late
    ]

    run = evaluator.run(rows, evaluation_id="eval-1")

    for score in run.scores:
        assert store.has_recorded("eval-1", score.supplier)
    records = store.records_for_evaluation("eval-1")
    assert len(records) == 2
    otherco_record = next(r for r in records if r.supplier == "OtherCo")
    assert otherco_record.outcome == "success"  # process succeeded, even though OtherCo itself is risky
    assert otherco_record.flagged_for_review is True
    assert otherco_record.score is not None


def test_run_is_idempotent_when_the_same_evaluation_id_is_run_twice(tmp_path):
    store = SupplierEvaluationAuditStore(tmp_path / "audit.jsonl")
    evaluator = SupplierEvaluator(store)
    rows = [_row("Acme", "PO-1", "2025-01-01", "2025-01-01")]

    evaluator.run(rows, evaluation_id="eval-1")
    evaluator.run(rows, evaluation_id="eval-1")

    assert len(store.records_for_evaluation("eval-1")) == 1


def test_run_records_a_run_level_failure_entry_for_an_invalid_parameter(tmp_path):
    store = SupplierEvaluationAuditStore(tmp_path / "audit.jsonl")
    evaluator = SupplierEvaluator(store)

    run = evaluator.run([], evaluation_id="eval-1", delay_threshold_days=0)

    assert run.outcome == "crashed"
    assert run.crash_error is not None
    assert run.scores == []
    records = store.records_for_evaluation("eval-1")
    assert len(records) == 1
    assert records[0].supplier == RUN_LEVEL_AUDIT_SUPPLIER
    assert records[0].outcome == "failure"


def test_run_records_a_run_level_failure_entry_for_an_unexpected_crash(tmp_path):
    # "Evaluation process fails" failure path: even a bug unrelated to bad
    # input (not just the documented SupplierEvaluationError) must still
    # leave an auditable trace rather than propagate with none.
    store = SupplierEvaluationAuditStore(tmp_path / "audit.jsonl")
    evaluator = SupplierEvaluator(store)

    with patch(
        "supplier_evaluation.evaluator.evaluate_supplier_reliability",
        side_effect=RuntimeError("boom"),
    ):
        run = evaluator.run([_row("Acme", "PO-1", "2025-01-01", "2025-01-01")], evaluation_id="eval-1")

    assert run.outcome == "crashed"
    assert run.crash_error == "boom"
    records = store.records_for_evaluation("eval-1")
    assert len(records) == 1
    assert records[0].supplier == RUN_LEVEL_AUDIT_SUPPLIER
    assert "RuntimeError" in records[0].detail


def test_run_generates_an_evaluation_id_when_none_is_given(tmp_path):
    store = SupplierEvaluationAuditStore(tmp_path / "audit.jsonl")
    evaluator = SupplierEvaluator(store)

    run = evaluator.run([_row("Acme", "PO-1", "2025-01-01", "2025-01-01")])

    assert run.evaluation_id
    assert store.records_for_evaluation(run.evaluation_id)


def test_flagged_suppliers_returns_only_scores_flagged_for_review(tmp_path):
    store = SupplierEvaluationAuditStore(tmp_path / "audit.jsonl")
    evaluator = SupplierEvaluator(store)
    rows = [
        _row("Reliable", "PO-1", "2025-01-01", "2025-01-01"),
        _row("Reliable", "PO-2", "2025-01-05", "2025-01-05"),
        _row("Reliable", "PO-3", "2025-01-10", "2025-01-10"),
        _row("Risky", "PO-4", "2025-01-01", "2025-01-20"),
    ]

    run = evaluator.run(rows, evaluation_id="eval-1")

    assert [s.supplier for s in run.flagged_suppliers] == ["Risky"]
