import json
import uuid
from datetime import datetime

import pytest

from chat_interface.audit_trail import ChatAuditStore, ChatAuditWriteError


def test_record_creates_entry_with_unique_id_and_timestamp(tmp_path):
    store = ChatAuditStore(tmp_path / "audit.jsonl")

    entry = store.record(
        interaction_id="int-1",
        outcome="success",
        query_text="what's our stockout risk?",
        status="answered",
        topic="stockout_risk_agent",
        answer="Stockout Risk: 2 SKUs at critical risk",
    )

    assert uuid.UUID(entry.record_id)  # raises ValueError if not a valid UUID
    datetime.fromisoformat(entry.timestamp)  # raises ValueError if not ISO-8601

    lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    on_disk = json.loads(lines[0])
    assert on_disk["interaction_id"] == "int-1"
    assert on_disk["query_text"] == "what's our stockout risk?"
    assert on_disk["status"] == "answered"
    assert on_disk["topic"] == "stockout_risk_agent"


def test_re_recording_same_interaction_does_not_duplicate(tmp_path):
    store = ChatAuditStore(tmp_path / "audit.jsonl")

    first = store.record(interaction_id="int-1", outcome="success", query_text="hi", status="unsupported")
    second = store.record(interaction_id="int-1", outcome="success", query_text="hi", status="unsupported")

    assert first.record_id == second.record_id
    lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1


def test_has_recorded_reflects_recorded_interactions(tmp_path):
    store = ChatAuditStore(tmp_path / "audit.jsonl")

    assert store.has_recorded("int-1") is False
    store.record(interaction_id="int-1", outcome="success", query_text="hi", status="unsupported")
    assert store.has_recorded("int-1") is True
    assert store.has_recorded("int-2") is False


def test_get_record_returns_none_when_not_recorded(tmp_path):
    store = ChatAuditStore(tmp_path / "audit.jsonl")

    assert store.get_record("int-1") is None
    store.record(interaction_id="int-1", outcome="success", query_text="hi", status="unsupported")
    assert store.get_record("int-1").query_text == "hi"


def test_record_without_interaction_id_generates_one(tmp_path):
    store = ChatAuditStore(tmp_path / "audit.jsonl")

    entry = store.record(outcome="success", query_text="hi", status="unsupported")

    assert uuid.UUID(entry.interaction_id)
    assert store.has_recorded(entry.interaction_id) is True


def test_crashed_interaction_records_failure_outcome_with_no_status(tmp_path):
    store = ChatAuditStore(tmp_path / "audit.jsonl")

    entry = store.record(
        interaction_id="int-1", outcome="failure", query_text="hi", detail="RuntimeError: boom"
    )

    assert entry.outcome == "failure"
    assert entry.status is None
    assert entry.detail == "RuntimeError: boom"


def test_records_survive_reload_from_disk(tmp_path):
    path = tmp_path / "audit.jsonl"
    ChatAuditStore(path).record(interaction_id="int-1", outcome="success", query_text="hi", status="unsupported")

    reloaded = ChatAuditStore(path)

    assert reloaded.has_recorded("int-1") is True


def test_corrupted_trailing_line_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "audit.jsonl"
    store = ChatAuditStore(path)
    store.record(interaction_id="int-1", outcome="success", query_text="hi", status="unsupported")
    with path.open("a", encoding="utf-8") as f:
        f.write("{not valid json\n")

    reloaded = ChatAuditStore(path)  # must not raise

    assert reloaded.has_recorded("int-1") is True


def test_record_raises_a_typed_error_when_the_write_fails(tmp_path):
    # "Audit trail not recorded for chat interactions" failure path: a
    # broken write must surface loudly, not be silently swallowed.
    # Simulated by pointing the store's path at a directory, so opening
    # it for append fails with OSError.
    path = tmp_path / "not_a_file"
    path.mkdir()
    store = ChatAuditStore(path)

    with pytest.raises(ChatAuditWriteError, match="int-1"):
        store.record(interaction_id="int-1", outcome="success", query_text="hi", status="unsupported")
