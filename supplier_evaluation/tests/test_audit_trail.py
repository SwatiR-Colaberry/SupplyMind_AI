import json
import uuid
from datetime import datetime

import pytest

from supplier_evaluation.audit_trail import (
    SupplierEvaluationAuditStore,
    SupplierEvaluationAuditWriteError,
)


def test_record_creates_entry_with_unique_id_and_timestamp(tmp_path):
    store = SupplierEvaluationAuditStore(tmp_path / "audit.jsonl")

    entry = store.record(
        evaluation_id="eval-1", supplier="Acme", outcome="success", score=10.0, severity="low",
        flagged_for_review=False, detail="4 deliveries, 100% on-time",
    )

    assert uuid.UUID(entry.record_id)  # raises ValueError if not a valid UUID
    datetime.fromisoformat(entry.timestamp)  # raises ValueError if not ISO-8601

    lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    on_disk = json.loads(lines[0])
    assert on_disk["evaluation_id"] == "eval-1"
    assert on_disk["supplier"] == "Acme"
    assert on_disk["score"] == 10.0


def test_re_recording_same_evaluation_and_supplier_does_not_duplicate(tmp_path):
    store = SupplierEvaluationAuditStore(tmp_path / "audit.jsonl")

    first = store.record(evaluation_id="eval-1", supplier="Acme", outcome="success")
    second = store.record(evaluation_id="eval-1", supplier="Acme", outcome="success")

    assert first.record_id == second.record_id
    lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1


def test_has_recorded_reflects_recorded_evaluation_supplier_pairs(tmp_path):
    store = SupplierEvaluationAuditStore(tmp_path / "audit.jsonl")

    assert store.has_recorded("eval-1", "Acme") is False
    store.record(evaluation_id="eval-1", supplier="Acme", outcome="success")
    assert store.has_recorded("eval-1", "Acme") is True
    assert store.has_recorded("eval-1", "OtherCo") is False


def test_records_for_evaluation_returns_only_that_evaluations_suppliers(tmp_path):
    store = SupplierEvaluationAuditStore(tmp_path / "audit.jsonl")
    store.record(evaluation_id="eval-1", supplier="Acme", outcome="success")
    store.record(evaluation_id="eval-1", supplier="OtherCo", outcome="success")
    store.record(evaluation_id="eval-2", supplier="Acme", outcome="failure", detail="no data")

    eval_1_suppliers = {r.supplier for r in store.records_for_evaluation("eval-1")}
    assert eval_1_suppliers == {"Acme", "OtherCo"}
    assert len(store.records_for_evaluation("eval-2")) == 1


def test_records_survive_reload_from_disk(tmp_path):
    path = tmp_path / "audit.jsonl"
    SupplierEvaluationAuditStore(path).record(evaluation_id="eval-1", supplier="Acme", outcome="success")

    reloaded = SupplierEvaluationAuditStore(path)

    assert reloaded.has_recorded("eval-1", "Acme") is True


def test_corrupted_trailing_line_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "audit.jsonl"
    store = SupplierEvaluationAuditStore(path)
    store.record(evaluation_id="eval-1", supplier="Acme", outcome="success")
    with path.open("a", encoding="utf-8") as f:
        f.write("{not valid json\n")

    reloaded = SupplierEvaluationAuditStore(path)  # must not raise

    assert reloaded.has_recorded("eval-1", "Acme") is True


def test_record_raises_a_typed_error_when_the_write_fails(tmp_path):
    # "Audit trail not recorded for evaluations" failure path: a broken
    # write must surface loudly, not be silently swallowed. Simulated by
    # pointing the store's path at a directory, so opening it for append
    # fails with OSError.
    path = tmp_path / "not_a_file"
    path.mkdir()
    store = SupplierEvaluationAuditStore(path)

    with pytest.raises(SupplierEvaluationAuditWriteError, match="Acme"):
        store.record(evaluation_id="eval-1", supplier="Acme", outcome="success")
