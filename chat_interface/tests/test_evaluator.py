from __future__ import annotations

from unittest.mock import patch

from chat_interface.audit_trail import ChatAuditStore
from chat_interface.evaluator import ChatEvaluator
from dashboard.metrics import DashboardMetric, DashboardSnapshot


def _snapshot(metrics: list[DashboardMetric] | None = None) -> DashboardSnapshot:
    return DashboardSnapshot(dashboard_id="snap-1", generated_at="2026-09-07T00:00:00+00:00", metrics=metrics or [])


def test_run_answers_a_supported_query_and_returns_a_success_run(tmp_path):
    store = ChatAuditStore(tmp_path / "audit.jsonl")
    evaluator = ChatEvaluator(store)
    snapshot = _snapshot(
        [
            DashboardMetric(
                metric_id="stockout_risk_agent",
                label="Stockout Risk",
                status="ok",
                headline="2 SKUs at critical risk",
                source_agent="stockout_risk_agent",
                confidence=0.9,
            )
        ]
    )

    run = evaluator.run("what's our stockout risk?", snapshot, interaction_id="int-1")

    assert run.outcome == "success"
    assert run.crash_error is None
    assert run.response.status == "answered"
    assert "Stockout Risk" in run.response.answer


def test_run_records_one_audit_entry_per_interaction(tmp_path):
    store = ChatAuditStore(tmp_path / "audit.jsonl")
    evaluator = ChatEvaluator(store)

    evaluator.run("what's the weather?", _snapshot(), interaction_id="int-1")

    assert store.has_recorded("int-1")


def test_run_is_idempotent_when_the_same_interaction_id_is_run_twice(tmp_path):
    store = ChatAuditStore(tmp_path / "audit.jsonl")
    evaluator = ChatEvaluator(store)

    evaluator.run("what's the weather?", _snapshot(), interaction_id="int-1")
    evaluator.run("what's the weather?", _snapshot(), interaction_id="int-1")

    lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1


def test_run_audits_an_unsupported_query_as_a_success_outcome(tmp_path):
    # The chat interface itself did not crash - it correctly identified
    # an unsupported query - so the audit outcome is "success" even though
    # the response status is "unsupported".
    store = ChatAuditStore(tmp_path / "audit.jsonl")
    evaluator = ChatEvaluator(store)

    run = evaluator.run("what's the weather?", _snapshot(), interaction_id="int-1")

    assert run.outcome == "success"
    assert run.response.status == "unsupported"


def test_run_records_a_failure_entry_for_an_invalid_snapshot(tmp_path):
    store = ChatAuditStore(tmp_path / "audit.jsonl")
    evaluator = ChatEvaluator(store)

    run = evaluator.run("what's our stockout risk?", {"not": "a snapshot"}, interaction_id="int-1")

    assert run.outcome == "crashed"
    assert run.crash_error is not None
    entry = store.get_record("int-1")
    assert entry.outcome == "failure"
    assert entry.status is None


def test_run_records_a_failure_entry_for_an_unexpected_crash(tmp_path):
    # "Chat interface failure" failure path: even a bug unrelated to bad
    # input must still leave an auditable trace rather than propagate with
    # none.
    store = ChatAuditStore(tmp_path / "audit.jsonl")
    evaluator = ChatEvaluator(store)

    with patch("chat_interface.evaluator.answer_query", side_effect=RuntimeError("boom")):
        run = evaluator.run("what's our stockout risk?", _snapshot(), interaction_id="int-1")

    assert run.outcome == "crashed"
    assert run.crash_error == "boom"
    entry = store.get_record("int-1")
    assert "RuntimeError" in entry.detail


def test_run_generates_an_interaction_id_when_none_is_given(tmp_path):
    store = ChatAuditStore(tmp_path / "audit.jsonl")
    evaluator = ChatEvaluator(store)

    run = evaluator.run("what's our stockout risk?", _snapshot())

    assert run.interaction_id
    assert store.has_recorded(run.interaction_id)


def test_run_returns_a_crashed_result_instead_of_raising_when_the_audit_store_cannot_be_written(tmp_path):
    # A broken audit store (disk full, permissions) must not propagate
    # ChatAuditWriteError straight out of run() - callers that only expect
    # a ChatInteractionRun back would crash instead of seeing a reported
    # failure.
    unwritable_path = tmp_path / "not_a_file"
    unwritable_path.mkdir()
    store = ChatAuditStore(unwritable_path)
    evaluator = ChatEvaluator(store)

    run = evaluator.run("what's our stockout risk?", _snapshot(), interaction_id="int-1")

    assert run.outcome == "crashed"
    assert run.crash_error is not None


def test_fail_run_preserves_the_original_exception_even_when_the_audit_write_also_fails(tmp_path):
    unwritable_path = tmp_path / "not_a_file"
    unwritable_path.mkdir()
    store = ChatAuditStore(unwritable_path)
    evaluator = ChatEvaluator(store)

    run = evaluator.run("what's our stockout risk?", {"not": "a snapshot"}, interaction_id="int-1")

    assert run.outcome == "crashed"
    assert "DashboardSnapshot" in run.crash_error
