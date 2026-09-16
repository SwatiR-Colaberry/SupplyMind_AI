import json
import uuid
from datetime import datetime

import pytest

from dashboard.approval_store import ApprovalStore, ApprovalWriteError


def test_record_creates_entry_with_unique_id_and_timestamp(tmp_path):
    store = ApprovalStore(tmp_path / "approvals.jsonl")

    entry = store.record(
        dashboard_id="real_data", metric_id="stockout_risk_agent", decision="approved", reviewer="ali", note="looks right"
    )

    assert uuid.UUID(entry.decision_id)  # raises ValueError if not a valid UUID
    datetime.fromisoformat(entry.timestamp)  # raises ValueError if not ISO-8601

    lines = (tmp_path / "approvals.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    on_disk = json.loads(lines[0])
    assert on_disk["dashboard_id"] == "real_data"
    assert on_disk["metric_id"] == "stockout_risk_agent"
    assert on_disk["decision"] == "approved"
    assert on_disk["reviewer"] == "ali"
    assert on_disk["note"] == "looks right"


def test_re_recording_the_same_dashboard_and_metric_appends_a_new_decision_not_a_duplicate_skip(tmp_path):
    # Unlike DashboardAuditStore.record(), this is NOT deduplicated - a
    # later, different decision on the same (dashboard_id, metric_id)
    # must be recorded as its own entry, not silently dropped.
    store = ApprovalStore(tmp_path / "approvals.jsonl")

    first = store.record(dashboard_id="real_data", metric_id="stockout_risk_agent", decision="approved", reviewer="ali")
    second = store.record(dashboard_id="real_data", metric_id="stockout_risk_agent", decision="rejected", reviewer="ram")

    assert first.decision_id != second.decision_id
    lines = (tmp_path / "approvals.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2


def test_latest_for_returns_the_most_recent_decision(tmp_path):
    store = ApprovalStore(tmp_path / "approvals.jsonl")
    store.record(dashboard_id="real_data", metric_id="stockout_risk_agent", decision="approved", reviewer="ali")
    latest = store.record(dashboard_id="real_data", metric_id="stockout_risk_agent", decision="rejected", reviewer="ram")

    assert store.latest_for("real_data", "stockout_risk_agent").decision_id == latest.decision_id


def test_latest_for_returns_none_when_no_decision_exists(tmp_path):
    store = ApprovalStore(tmp_path / "approvals.jsonl")

    assert store.latest_for("real_data", "stockout_risk_agent") is None


def test_history_for_returns_every_decision_oldest_first(tmp_path):
    store = ApprovalStore(tmp_path / "approvals.jsonl")
    first = store.record(dashboard_id="real_data", metric_id="stockout_risk_agent", decision="approved", reviewer="ali")
    second = store.record(dashboard_id="real_data", metric_id="stockout_risk_agent", decision="rejected", reviewer="ram")

    history = store.history_for("real_data", "stockout_risk_agent")

    assert [r.decision_id for r in history] == [first.decision_id, second.decision_id]


def test_latest_by_metric_returns_one_entry_per_metric_for_the_given_dashboard(tmp_path):
    store = ApprovalStore(tmp_path / "approvals.jsonl")
    store.record(dashboard_id="real_data", metric_id="stockout_risk_agent", decision="approved", reviewer="ali")
    store.record(dashboard_id="real_data", metric_id="stockout_risk_agent", decision="rejected", reviewer="ram")
    store.record(dashboard_id="real_data", metric_id="risk_detection_agent", decision="approved", reviewer="ali")
    store.record(dashboard_id="partial_failure", metric_id="stockout_risk_agent", decision="approved", reviewer="ali")

    latest = store.latest_by_metric("real_data")

    assert set(latest) == {"stockout_risk_agent", "risk_detection_agent"}
    assert latest["stockout_risk_agent"].decision == "rejected"  # the more recent of the 2 real_data decisions


def test_records_for_dashboard_returns_only_that_dashboards_decisions_oldest_first(tmp_path):
    store = ApprovalStore(tmp_path / "approvals.jsonl")
    first = store.record(dashboard_id="real_data", metric_id="stockout_risk_agent", decision="approved", reviewer="ali")
    second = store.record(dashboard_id="real_data", metric_id="risk_detection_agent", decision="approved", reviewer="ali")
    store.record(dashboard_id="partial_failure", metric_id="stockout_risk_agent", decision="approved", reviewer="ali")

    records = store.records_for_dashboard("real_data")

    assert [r.decision_id for r in records] == [first.decision_id, second.decision_id]


def test_reloading_from_disk_recovers_full_history_in_order(tmp_path):
    path = tmp_path / "approvals.jsonl"
    store = ApprovalStore(path)
    store.record(dashboard_id="real_data", metric_id="stockout_risk_agent", decision="approved", reviewer="ali")
    store.record(dashboard_id="real_data", metric_id="stockout_risk_agent", decision="rejected", reviewer="ram")

    reloaded = ApprovalStore(path)

    assert [r.decision for r in reloaded.history_for("real_data", "stockout_risk_agent")] == ["approved", "rejected"]


def test_a_corrupted_line_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "approvals.jsonl"
    path.write_text('not valid json\n{"decision_id": "x"}\n')  # 2 bad lines: unparseable, then missing required fields

    store = ApprovalStore(path)

    assert store.records_for_dashboard("real_data") == []


def test_record_raises_a_typed_error_when_the_write_fails(tmp_path):
    # "Notification system failure" failure path: a broken write must
    # surface loudly, not be silently swallowed - a reviewer's decision
    # must never silently fail to record while the UI reports success.
    # Simulated the same way dashboard/audit_trail.py's own equivalent
    # test does: point the store's path at a directory, so opening it for
    # append fails with OSError.
    path = tmp_path / "not_a_file"
    path.mkdir()
    store = ApprovalStore(path)

    with pytest.raises(ApprovalWriteError, match="stockout_risk_agent"):
        store.record(dashboard_id="real_data", metric_id="stockout_risk_agent", decision="approved", reviewer="ali")
