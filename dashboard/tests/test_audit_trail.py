import json
import uuid
from datetime import datetime

import pytest

from dashboard.audit_trail import DashboardAuditStore, DashboardAuditWriteError


def test_record_creates_entry_with_unique_id_and_timestamp(tmp_path):
    store = DashboardAuditStore(tmp_path / "audit.jsonl")

    entry = store.record(
        dashboard_id="dash-1",
        metric_id="risk_detection_agent",
        source_agent="risk_detection_agent",
        outcome="ok",
        severity="high",
        headline="Supply chain risk score 65/100 (high): ...",
    )

    assert uuid.UUID(entry.record_id)  # raises ValueError if not a valid UUID
    datetime.fromisoformat(entry.timestamp)  # raises ValueError if not ISO-8601

    lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    on_disk = json.loads(lines[0])
    assert on_disk["dashboard_id"] == "dash-1"
    assert on_disk["metric_id"] == "risk_detection_agent"
    assert on_disk["source_agent"] == "risk_detection_agent"
    assert on_disk["severity"] == "high"


def test_re_recording_same_dashboard_and_metric_does_not_duplicate(tmp_path):
    store = DashboardAuditStore(tmp_path / "audit.jsonl")

    first = store.record(dashboard_id="dash-1", metric_id="risk_detection_agent", source_agent="risk_detection_agent", outcome="ok")
    second = store.record(dashboard_id="dash-1", metric_id="risk_detection_agent", source_agent="risk_detection_agent", outcome="ok")

    assert first.record_id == second.record_id
    lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1


def test_has_recorded_reflects_recorded_dashboard_metric_pairs(tmp_path):
    store = DashboardAuditStore(tmp_path / "audit.jsonl")

    assert store.has_recorded("dash-1", "risk_detection_agent") is False
    store.record(dashboard_id="dash-1", metric_id="risk_detection_agent", source_agent="risk_detection_agent", outcome="ok")
    assert store.has_recorded("dash-1", "risk_detection_agent") is True
    assert store.has_recorded("dash-1", "stockout_risk_agent") is False


def test_records_for_dashboard_returns_only_that_builds_metrics(tmp_path):
    store = DashboardAuditStore(tmp_path / "audit.jsonl")
    store.record(dashboard_id="dash-1", metric_id="risk_detection_agent", source_agent="risk_detection_agent", outcome="ok")
    store.record(dashboard_id="dash-1", metric_id="stockout_risk_agent", source_agent="stockout_risk_agent", outcome="ok")
    store.record(dashboard_id="dash-2", metric_id="risk_detection_agent", source_agent="risk_detection_agent", outcome="error", headline="no data")

    dash_1_metrics = {r.metric_id for r in store.records_for_dashboard("dash-1")}
    assert dash_1_metrics == {"risk_detection_agent", "stockout_risk_agent"}
    assert len(store.records_for_dashboard("dash-2")) == 1


def test_records_survive_reload_from_disk(tmp_path):
    path = tmp_path / "audit.jsonl"
    DashboardAuditStore(path).record(dashboard_id="dash-1", metric_id="risk_detection_agent", source_agent="risk_detection_agent", outcome="ok")

    reloaded = DashboardAuditStore(path)

    assert reloaded.has_recorded("dash-1", "risk_detection_agent") is True


def test_corrupted_trailing_line_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "audit.jsonl"
    store = DashboardAuditStore(path)
    store.record(dashboard_id="dash-1", metric_id="risk_detection_agent", source_agent="risk_detection_agent", outcome="ok")
    with path.open("a", encoding="utf-8") as f:
        f.write("{not valid json\n")

    reloaded = DashboardAuditStore(path)  # must not raise

    assert reloaded.has_recorded("dash-1", "risk_detection_agent") is True


def test_record_raises_a_typed_error_when_the_write_fails(tmp_path):
    # "Notification system failure" failure path: a broken write must
    # surface loudly, not be silently swallowed. Simulated by pointing
    # the store's path at a directory, so opening it for append fails
    # with OSError.
    path = tmp_path / "not_a_file"
    path.mkdir()
    store = DashboardAuditStore(path)

    with pytest.raises(DashboardAuditWriteError, match="risk_detection_agent"):
        store.record(dashboard_id="dash-1", metric_id="risk_detection_agent", source_agent="risk_detection_agent", outcome="ok")
