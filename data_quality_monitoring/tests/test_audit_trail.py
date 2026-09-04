import json
import uuid
from datetime import datetime

import pytest

from data_quality_monitoring.audit_trail import (
    QualityAuditStore,
    QualityAuditWriteError,
)


def test_record_creates_entry_with_unique_id_and_timestamp(tmp_path):
    store = QualityAuditStore(tmp_path / "audit.jsonl")

    entry = store.record(
        check_id="check-1", dimension="completeness", outcome="success", score=92.5,
        checked_rows=10, issue_rows=1, detail="9/10 rows complete",
    )

    assert uuid.UUID(entry.record_id)  # raises ValueError if not a valid UUID
    datetime.fromisoformat(entry.timestamp)  # raises ValueError if not ISO-8601

    lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    on_disk = json.loads(lines[0])
    assert on_disk["check_id"] == "check-1"
    assert on_disk["dimension"] == "completeness"
    assert on_disk["score"] == 92.5


def test_re_recording_same_check_and_dimension_does_not_duplicate(tmp_path):
    store = QualityAuditStore(tmp_path / "audit.jsonl")

    first = store.record(check_id="check-1", dimension="completeness", outcome="success")
    second = store.record(check_id="check-1", dimension="completeness", outcome="success")

    assert first.record_id == second.record_id
    lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1


def test_has_recorded_reflects_recorded_check_dimension_pairs(tmp_path):
    store = QualityAuditStore(tmp_path / "audit.jsonl")

    assert store.has_recorded("check-1", "completeness") is False
    store.record(check_id="check-1", dimension="completeness", outcome="success")
    assert store.has_recorded("check-1", "completeness") is True
    assert store.has_recorded("check-1", "uniqueness") is False


def test_records_for_check_returns_only_that_runs_dimensions(tmp_path):
    store = QualityAuditStore(tmp_path / "audit.jsonl")
    store.record(check_id="check-1", dimension="completeness", outcome="success")
    store.record(check_id="check-1", dimension="uniqueness", outcome="success")
    store.record(check_id="check-2", dimension="completeness", outcome="failure", detail="no data")

    check_1_dims = {r.dimension for r in store.records_for_check("check-1")}
    assert check_1_dims == {"completeness", "uniqueness"}
    assert len(store.records_for_check("check-2")) == 1


def test_run_level_record_uses_a_none_dimension_distinct_from_any_real_dimension(tmp_path):
    store = QualityAuditStore(tmp_path / "audit.jsonl")

    run_level = store.record(check_id="check-1", dimension=None, outcome="failure", detail="crashed")

    assert run_level.dimension is None
    assert store.has_recorded("check-1", None) is True
    assert store.has_recorded("check-1", "completeness") is False


def test_records_survive_reload_from_disk(tmp_path):
    path = tmp_path / "audit.jsonl"
    QualityAuditStore(path).record(check_id="check-1", dimension="completeness", outcome="success")

    reloaded = QualityAuditStore(path)

    assert reloaded.has_recorded("check-1", "completeness") is True


def test_corrupted_trailing_line_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "audit.jsonl"
    store = QualityAuditStore(path)
    store.record(check_id="check-1", dimension="completeness", outcome="success")
    with path.open("a", encoding="utf-8") as f:
        f.write("{not valid json\n")

    reloaded = QualityAuditStore(path)  # must not raise

    assert reloaded.has_recorded("check-1", "completeness") is True


def test_record_raises_a_typed_error_when_the_write_fails(tmp_path):
    # "Audit trail not recorded for quality checks" failure path: a
    # broken write must surface loudly, not be silently swallowed.
    # Simulated by pointing the store's path at a directory, so opening
    # it for append fails with OSError.
    path = tmp_path / "not_a_file"
    path.mkdir()
    store = QualityAuditStore(path)

    with pytest.raises(QualityAuditWriteError, match="completeness"):
        store.record(check_id="check-1", dimension="completeness", outcome="success")
