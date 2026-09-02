import json
import uuid
from datetime import datetime

import pytest

from root_cause.audit_trail import RootCauseAuditStore, RootCauseAuditWriteError


def test_record_creates_entry_with_unique_id_and_timestamp(tmp_path):
    store = RootCauseAuditStore(tmp_path / "audit.jsonl")

    entry = store.record(
        analysis_id="run-1", subject="SKU-1", subject_kind="sku", outcome="success",
        confidence=0.85, candidate_count=1, detail="1 candidate cause(s) found",
    )

    assert uuid.UUID(entry.record_id)  # raises ValueError if not a valid UUID
    datetime.fromisoformat(entry.timestamp)  # raises ValueError if not ISO-8601

    lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    on_disk = json.loads(lines[0])
    assert on_disk["analysis_id"] == "run-1"
    assert on_disk["subject"] == "SKU-1"
    assert on_disk["confidence"] == 0.85


def test_re_recording_same_analysis_and_subject_does_not_duplicate(tmp_path):
    store = RootCauseAuditStore(tmp_path / "audit.jsonl")

    first = store.record(analysis_id="run-1", subject="SKU-1", subject_kind="sku", outcome="success")
    second = store.record(analysis_id="run-1", subject="SKU-1", subject_kind="sku", outcome="success")

    assert first.record_id == second.record_id
    lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1


def test_has_recorded_reflects_recorded_analysis_subject_pairs(tmp_path):
    store = RootCauseAuditStore(tmp_path / "audit.jsonl")

    assert store.has_recorded("run-1", "sku", "SKU-1") is False
    store.record(analysis_id="run-1", subject="SKU-1", subject_kind="sku", outcome="success")
    assert store.has_recorded("run-1", "sku", "SKU-1") is True
    assert store.has_recorded("run-1", "sku", "SKU-2") is False


def test_records_for_analysis_returns_only_that_runs_issues(tmp_path):
    store = RootCauseAuditStore(tmp_path / "audit.jsonl")
    store.record(analysis_id="run-1", subject="SKU-1", subject_kind="sku", outcome="success")
    store.record(analysis_id="run-1", subject="PO-9", subject_kind="po", outcome="success")
    store.record(analysis_id="run-2", subject="SKU-1", subject_kind="sku", outcome="failure", detail="no data")

    run_1_subjects = {r.subject for r in store.records_for_analysis("run-1")}
    assert run_1_subjects == {"SKU-1", "PO-9"}
    assert len(store.records_for_analysis("run-2")) == 1


def test_records_survive_reload_from_disk(tmp_path):
    path = tmp_path / "audit.jsonl"
    RootCauseAuditStore(path).record(analysis_id="run-1", subject="SKU-1", subject_kind="sku", outcome="success")

    reloaded = RootCauseAuditStore(path)

    assert reloaded.has_recorded("run-1", "sku", "SKU-1") is True


def test_corrupted_trailing_line_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "audit.jsonl"
    store = RootCauseAuditStore(path)
    store.record(analysis_id="run-1", subject="SKU-1", subject_kind="sku", outcome="success")
    with path.open("a", encoding="utf-8") as f:
        f.write("{not valid json\n")

    reloaded = RootCauseAuditStore(path)  # must not raise

    assert reloaded.has_recorded("run-1", "sku", "SKU-1") is True


def test_record_raises_a_typed_error_when_the_write_fails(tmp_path):
    # "Audit trail not recorded for analyses" failure path: a broken
    # write must surface loudly, not be silently swallowed. Simulated by
    # pointing the store's path at a directory, so opening it for append
    # fails with OSError.
    path = tmp_path / "not_a_file"
    path.mkdir()
    store = RootCauseAuditStore(path)

    with pytest.raises(RootCauseAuditWriteError, match="SKU-1"):
        store.record(analysis_id="run-1", subject="SKU-1", subject_kind="sku", outcome="success")
