import json
import uuid
from datetime import datetime

from intelligence.audit_trail import StageAuditStore


def test_record_creates_entry_with_unique_id_and_timestamp(tmp_path):
    store = StageAuditStore(tmp_path / "audit.jsonl")

    entry = store.record(run_id="run-1", stage="observe", outcome="success", detail="4 rows")

    assert uuid.UUID(entry.record_id)  # raises ValueError if not a valid UUID
    datetime.fromisoformat(entry.timestamp)  # raises ValueError if not ISO-8601

    lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    on_disk = json.loads(lines[0])
    assert on_disk["run_id"] == "run-1"
    assert on_disk["stage"] == "observe"


def test_re_recording_same_run_and_stage_does_not_duplicate(tmp_path):
    store = StageAuditStore(tmp_path / "audit.jsonl")

    first = store.record(run_id="run-1", stage="observe", outcome="success")
    second = store.record(run_id="run-1", stage="observe", outcome="success")

    assert first.record_id == second.record_id
    lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1


def test_has_recorded_reflects_recorded_run_stage_pairs(tmp_path):
    store = StageAuditStore(tmp_path / "audit.jsonl")

    assert store.has_recorded("run-1", "observe") is False
    store.record(run_id="run-1", stage="observe", outcome="success")
    assert store.has_recorded("run-1", "observe") is True
    assert store.has_recorded("run-1", "understand") is False


def test_records_for_run_returns_only_that_runs_stages(tmp_path):
    store = StageAuditStore(tmp_path / "audit.jsonl")
    store.record(run_id="run-1", stage="observe", outcome="success")
    store.record(run_id="run-1", stage="understand", outcome="success")
    store.record(run_id="run-2", stage="observe", outcome="failure", detail="no data")

    run_1_stages = {r.stage for r in store.records_for_run("run-1")}
    assert run_1_stages == {"observe", "understand"}
    assert len(store.records_for_run("run-2")) == 1


def test_records_survive_reload_from_disk(tmp_path):
    path = tmp_path / "audit.jsonl"
    StageAuditStore(path).record(run_id="run-1", stage="observe", outcome="success")

    reloaded = StageAuditStore(path)

    assert reloaded.has_recorded("run-1", "observe") is True


def test_corrupted_trailing_line_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "audit.jsonl"
    store = StageAuditStore(path)
    store.record(run_id="run-1", stage="observe", outcome="success")
    with path.open("a", encoding="utf-8") as f:
        f.write("{not valid json\n")

    reloaded = StageAuditStore(path)  # must not raise

    assert reloaded.has_recorded("run-1", "observe") is True
