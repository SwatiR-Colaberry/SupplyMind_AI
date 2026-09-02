from unittest.mock import patch

from risk_detection.anomaly_detection import DemandAnomaly
from root_cause.analysis import Issue
from root_cause.audit_trail import RootCauseAuditStore
from root_cause.evaluator import RootCauseEvaluator


def _demand_anomaly(period: str, severity: str = "critical") -> DemandAnomaly:
    return DemandAnomaly(
        period=period,
        quantity=500,
        baseline_mean=100,
        baseline_stdev=10,
        z_score=40.0,
        direction="spike",
        severity=severity,
        detail="500.0 vs. baseline mean 100.0 (stdev 10.0) - z-score 40.00",
    )


def test_run_analyzes_the_issue_and_returns_a_success_run(tmp_path):
    store = RootCauseAuditStore(tmp_path / "audit.jsonl")
    evaluator = RootCauseEvaluator(store)
    issue = Issue(subject="SKU-1", subject_kind="sku", as_of_period="2025-04")

    run = evaluator.run(issue, analysis_id="run-1", demand_anomalies=[_demand_anomaly("2025-04")])

    assert run.outcome == "success"
    assert run.crash_error is None
    assert run.analysis is not None
    assert run.analysis.candidates[0].cause == "demand_spike"


def test_run_records_one_audit_entry_for_the_issue(tmp_path):
    store = RootCauseAuditStore(tmp_path / "audit.jsonl")
    evaluator = RootCauseEvaluator(store)
    issue = Issue(subject="SKU-1", subject_kind="sku", as_of_period="2025-04")

    evaluator.run(issue, analysis_id="run-1", demand_anomalies=[_demand_anomaly("2025-04")])

    assert store.has_recorded("run-1", "sku", "SKU-1")
    records = store.records_for_analysis("run-1")
    assert len(records) == 1
    assert records[0].outcome == "success"
    assert records[0].confidence == 0.85


def test_run_is_idempotent_when_the_same_analysis_id_is_run_twice(tmp_path):
    store = RootCauseAuditStore(tmp_path / "audit.jsonl")
    evaluator = RootCauseEvaluator(store)
    issue = Issue(subject="SKU-1", subject_kind="sku", as_of_period="2025-04")
    anomalies = [_demand_anomaly("2025-04")]

    evaluator.run(issue, analysis_id="run-1", demand_anomalies=anomalies)
    evaluator.run(issue, analysis_id="run-1", demand_anomalies=anomalies)

    assert len(store.records_for_analysis("run-1")) == 1


def test_run_reports_insufficient_data_when_no_signals_are_supplied(tmp_path):
    store = RootCauseAuditStore(tmp_path / "audit.jsonl")
    evaluator = RootCauseEvaluator(store)
    issue = Issue(subject="SKU-1", subject_kind="sku")

    run = evaluator.run(issue, analysis_id="run-1")

    assert run.outcome == "insufficient_data"
    assert run.analysis is None
    assert run.limitation is not None
    records = store.records_for_analysis("run-1")
    assert len(records) == 1
    assert records[0].outcome == "failure"
    assert records[0].confidence is None


def test_run_records_a_failure_entry_for_an_unexpected_crash(tmp_path):
    # "Analysis API failure" failure path: a bug unrelated to bad/missing
    # input must still leave an auditable trace rather than propagate
    # with none.
    store = RootCauseAuditStore(tmp_path / "audit.jsonl")
    evaluator = RootCauseEvaluator(store)
    issue = Issue(subject="SKU-1", subject_kind="sku", as_of_period="2025-04")

    with patch("root_cause.evaluator.analyze_root_cause", side_effect=RuntimeError("boom")):
        run = evaluator.run(issue, analysis_id="run-1", demand_anomalies=[_demand_anomaly("2025-04")])

    assert run.outcome == "crashed"
    assert run.crash_error == "boom"
    records = store.records_for_analysis("run-1")
    assert len(records) == 1
    assert "RuntimeError" in records[0].detail


def test_run_generates_an_analysis_id_when_none_is_given(tmp_path):
    store = RootCauseAuditStore(tmp_path / "audit.jsonl")
    evaluator = RootCauseEvaluator(store)
    issue = Issue(subject="SKU-1", subject_kind="sku", as_of_period="2025-04")

    run = evaluator.run(issue, demand_anomalies=[_demand_anomaly("2025-04")])

    assert run.analysis_id
    assert store.records_for_analysis(run.analysis_id)


def test_run_returns_a_crashed_result_instead_of_raising_when_the_audit_store_cannot_be_written(tmp_path):
    unwritable_path = tmp_path / "not_a_file"
    unwritable_path.mkdir()
    store = RootCauseAuditStore(unwritable_path)
    evaluator = RootCauseEvaluator(store)
    issue = Issue(subject="SKU-1", subject_kind="sku", as_of_period="2025-04")

    run = evaluator.run(issue, analysis_id="run-1", demand_anomalies=[_demand_anomaly("2025-04")])

    assert run.outcome == "crashed"
    assert run.crash_error is not None


def test_fail_run_preserves_the_original_exception_even_when_the_audit_write_also_fails(tmp_path):
    unwritable_path = tmp_path / "not_a_file"
    unwritable_path.mkdir()
    store = RootCauseAuditStore(unwritable_path)
    evaluator = RootCauseEvaluator(store)
    issue = Issue(subject="SKU-1", subject_kind="sku")

    run = evaluator.run(issue, analysis_id="run-1")  # no signal data -> insufficient_data path

    assert run.outcome == "crashed"  # audit write itself is what fails here
    assert run.crash_error is not None
