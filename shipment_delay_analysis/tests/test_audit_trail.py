import json
import uuid
from datetime import datetime

import pytest

from shipment_delay_analysis.audit_trail import (
    ShipmentDelayAuditStore,
    ShipmentDelayAuditWriteError,
)


def test_record_creates_entry_with_unique_id_and_timestamp(tmp_path):
    store = ShipmentDelayAuditStore(tmp_path / "audit.jsonl")

    entry = store.record(
        analysis_id="analysis-1", po_id="PO-1", outcome="success", delay_days=5, severity="high",
        total_cost=1250.0, detail="5 day(s) late",
    )

    assert uuid.UUID(entry.record_id)  # raises ValueError if not a valid UUID
    datetime.fromisoformat(entry.timestamp)  # raises ValueError if not ISO-8601

    lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    on_disk = json.loads(lines[0])
    assert on_disk["analysis_id"] == "analysis-1"
    assert on_disk["po_id"] == "PO-1"
    assert on_disk["total_cost"] == 1250.0


def test_re_recording_same_analysis_and_po_does_not_duplicate(tmp_path):
    store = ShipmentDelayAuditStore(tmp_path / "audit.jsonl")

    first = store.record(analysis_id="analysis-1", po_id="PO-1", outcome="success")
    second = store.record(analysis_id="analysis-1", po_id="PO-1", outcome="success")

    assert first.record_id == second.record_id
    lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1


def test_has_recorded_reflects_recorded_analysis_po_pairs(tmp_path):
    store = ShipmentDelayAuditStore(tmp_path / "audit.jsonl")

    assert store.has_recorded("analysis-1", "PO-1") is False
    store.record(analysis_id="analysis-1", po_id="PO-1", outcome="success")
    assert store.has_recorded("analysis-1", "PO-1") is True
    assert store.has_recorded("analysis-1", "PO-2") is False


def test_records_for_analysis_returns_only_that_runs_pos(tmp_path):
    store = ShipmentDelayAuditStore(tmp_path / "audit.jsonl")
    store.record(analysis_id="analysis-1", po_id="PO-1", outcome="success")
    store.record(analysis_id="analysis-1", po_id="PO-2", outcome="success")
    store.record(analysis_id="analysis-2", po_id="PO-1", outcome="failure", detail="no data")

    analysis_1_pos = {r.po_id for r in store.records_for_analysis("analysis-1")}
    assert analysis_1_pos == {"PO-1", "PO-2"}
    assert len(store.records_for_analysis("analysis-2")) == 1


def test_run_level_record_uses_a_none_po_id_distinct_from_any_real_po(tmp_path):
    store = ShipmentDelayAuditStore(tmp_path / "audit.jsonl")

    run_level = store.record(analysis_id="analysis-1", po_id=None, outcome="success", detail="no delays found")

    assert run_level.po_id is None
    assert store.has_recorded("analysis-1", None) is True
    assert store.has_recorded("analysis-1", "PO-1") is False


def test_records_survive_reload_from_disk(tmp_path):
    path = tmp_path / "audit.jsonl"
    ShipmentDelayAuditStore(path).record(analysis_id="analysis-1", po_id="PO-1", outcome="success")

    reloaded = ShipmentDelayAuditStore(path)

    assert reloaded.has_recorded("analysis-1", "PO-1") is True


def test_corrupted_trailing_line_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "audit.jsonl"
    store = ShipmentDelayAuditStore(path)
    store.record(analysis_id="analysis-1", po_id="PO-1", outcome="success")
    with path.open("a", encoding="utf-8") as f:
        f.write("{not valid json\n")

    reloaded = ShipmentDelayAuditStore(path)  # must not raise

    assert reloaded.has_recorded("analysis-1", "PO-1") is True


def test_record_raises_a_typed_error_when_the_write_fails(tmp_path):
    # "Audit trail missing for shipment analysis" failure path: a broken
    # write must surface loudly, not be silently swallowed. Simulated by
    # pointing the store's path at a directory, so opening it for append
    # fails with OSError.
    path = tmp_path / "not_a_file"
    path.mkdir()
    store = ShipmentDelayAuditStore(path)

    with pytest.raises(ShipmentDelayAuditWriteError, match="PO-1"):
        store.record(analysis_id="analysis-1", po_id="PO-1", outcome="success")
