from agents.contracts import AgentResponse
from agents.orchestrator import CoordinationResult
from dashboard.audit_trail import DashboardAuditStore
from dashboard.data_freshness import DataFreshnessEntry
from dashboard.evaluator import _RUN_LEVEL_METRIC_ID, DashboardEvaluator


def _ok_result(agent_name):
    return CoordinationResult(
        agent_name=agent_name,
        outcome="success",
        response=AgentResponse(agent_name=agent_name, status="ok", recommendation="fine", confidence=0.9),
    )


def test_run_builds_snapshot_and_records_one_entry_per_tile(tmp_path):
    store = DashboardAuditStore(tmp_path / "audit.jsonl")
    evaluator = DashboardEvaluator(store)
    results = [_ok_result("stockout_risk_agent"), _ok_result("risk_detection_agent")]

    run = evaluator.run(results, dashboard_id="dash-1")

    assert run.outcome == "success"
    assert run.crash_error is None
    assert run.snapshot is not None
    assert len(run.snapshot.metrics) == 2
    records = store.records_for_dashboard("dash-1")
    assert len(records) == 2
    assert {r.metric_id for r in records} == {"stockout_risk_agent", "risk_detection_agent"}


def test_run_is_idempotent_when_the_same_dashboard_id_is_run_twice(tmp_path):
    store = DashboardAuditStore(tmp_path / "audit.jsonl")
    evaluator = DashboardEvaluator(store)
    results = [_ok_result("stockout_risk_agent")]

    evaluator.run(results, dashboard_id="dash-1")
    evaluator.run(results, dashboard_id="dash-1")

    assert len(store.records_for_dashboard("dash-1")) == 1


def test_broken_audit_store_reports_a_crashed_run_not_a_silent_gap(tmp_path):
    # "Notification system failure" failure path: a broken audit write
    # must surface as a crashed run, not a dashboard that looks like it
    # updated successfully while its trust-spine record silently didn't
    # land. Simulated by pointing the store's path at a directory, so
    # opening it for append fails with OSError - same technique
    # dashboard/tests/test_audit_trail.py already uses.
    path = tmp_path / "not_a_file"
    path.mkdir()
    store = DashboardAuditStore(path)
    evaluator = DashboardEvaluator(store)

    run = evaluator.run([_ok_result("stockout_risk_agent")], dashboard_id="dash-1")

    assert run.outcome == "crashed"
    assert run.crash_error is not None
    assert run.snapshot is None


def test_run_records_a_source_entry_per_dataset_alongside_agent_tiles(tmp_path):
    store = DashboardAuditStore(tmp_path / "audit.jsonl")
    evaluator = DashboardEvaluator(store)
    freshness = [
        DataFreshnessEntry(dataset="customer_orders", source_type="postgres", outcome="success", pulled_at="2026-09-04T12:00:00+00:00", row_count=500),
        DataFreshnessEntry(dataset="delivery_records", source_type="postgres", outcome="failure", pulled_at="2026-09-04T12:00:00+00:00", error="connection refused"),
    ]

    run = evaluator.run([_ok_result("stockout_risk_agent")], dashboard_id="dash-1", data_freshness=freshness)

    assert run.outcome == "success"
    records = {r.metric_id: r for r in store.records_for_dashboard("dash-1")}
    assert records["source:customer_orders"].outcome == "ok"
    assert records["source:customer_orders"].source_agent == "postgres"
    assert records["source:delivery_records"].outcome == "error"
    assert records["source:delivery_records"].headline == "connection refused"


def test_unexpected_build_failure_still_gets_a_run_level_audit_record(tmp_path):
    store = DashboardAuditStore(tmp_path / "audit.jsonl")
    evaluator = DashboardEvaluator(store)

    # Deliberately malformed input - a garbage item in place of a real
    # CoordinationResult - to trigger build_dashboard()'s own crash path,
    # not a single agent's failure - a single agent's failure never
    # raises; see dashboard/metrics.py. A caller assembling this list
    # wrong (e.g. from the wrong stage of a pipeline) is a genuine
    # "data processing error" here, the same reasoning
    # agents/recommendation_agent.py's own docstring applies to its
    # agent_outputs list.
    run = evaluator.run([object()], dashboard_id="dash-1")  # type: ignore[list-item]

    assert run.outcome == "crashed"
    assert run.crash_error is not None
    records = store.records_for_dashboard("dash-1")
    assert len(records) == 1
    assert records[0].metric_id == _RUN_LEVEL_METRIC_ID
    assert records[0].outcome == "error"
