import json
import uuid

import pytest

from data_integration.audit_trail import AuditStore, AuditTrailWriteError


def test_record_creates_entry_with_unique_id_and_timestamp(tmp_path):
    store = AuditStore(tmp_path / "audit.jsonl")

    entry = store.record(
        idempotency_key="orders-2026-08-20",
        dataset="customer_orders",
        source_type="postgresql",
        outcome="success",
        row_count=42,
    )

    assert uuid.UUID(entry.record_id)  # raises ValueError if not a valid UUID
    assert entry.timestamp  # ISO-8601 string; parseable
    from datetime import datetime

    datetime.fromisoformat(entry.timestamp)

    lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    on_disk = json.loads(lines[0])
    assert on_disk["record_id"] == entry.record_id
    assert on_disk["dataset"] == "customer_orders"


def test_reprocessing_same_key_does_not_duplicate(tmp_path):
    store = AuditStore(tmp_path / "audit.jsonl")

    first = store.record(
        idempotency_key="orders-2026-08-20",
        dataset="customer_orders",
        source_type="postgresql",
        outcome="success",
        row_count=42,
    )
    second = store.record(
        idempotency_key="orders-2026-08-20",
        dataset="customer_orders",
        source_type="postgresql",
        outcome="success",
        row_count=42,
    )

    assert first.record_id == second.record_id
    lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1


def test_has_processed_reflects_recorded_keys(tmp_path):
    store = AuditStore(tmp_path / "audit.jsonl")

    assert store.has_processed("orders-2026-08-20") is False
    store.record(
        idempotency_key="orders-2026-08-20",
        dataset="customer_orders",
        source_type="postgresql",
        outcome="success",
    )
    assert store.has_processed("orders-2026-08-20") is True


def test_idempotency_persists_across_store_instances(tmp_path):
    path = tmp_path / "audit.jsonl"
    first_store = AuditStore(path)
    first_store.record(
        idempotency_key="orders-2026-08-20",
        dataset="customer_orders",
        source_type="postgresql",
        outcome="success",
        row_count=7,
    )

    second_store = AuditStore(path)
    assert second_store.has_processed("orders-2026-08-20") is True

    reprocessed = second_store.record(
        idempotency_key="orders-2026-08-20",
        dataset="customer_orders",
        source_type="postgresql",
        outcome="success",
        row_count=7,
    )
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1
    assert reprocessed.row_count == 7


def test_error_outcome_is_recorded_with_detail(tmp_path):
    store = AuditStore(tmp_path / "audit.jsonl")

    entry = store.record(
        idempotency_key="warehouses-2026-08-20",
        dataset="warehouses",
        source_type="google_sheets",
        outcome="failure",
        error="ConnectionError: could not reach sheets API",
    )

    assert entry.outcome == "failure"
    assert "ConnectionError" in entry.error


def test_corrupted_line_is_skipped_with_warning_not_fatal(tmp_path):
    """A truncated trailing line (crash mid-append) must not brick the whole trail."""
    path = tmp_path / "audit.jsonl"
    good = {
        "record_id": "r1",
        "idempotency_key": "k1",
        "dataset": "customer_orders",
        "source_type": "postgresql",
        "outcome": "success",
        "timestamp": "2026-08-20T00:00:00+00:00",
        "row_count": 1,
        "error": None,
    }
    path.write_text(json.dumps(good) + "\n" + '{"record_id": "truncated_mid_wri')

    store = AuditStore(path)  # must not raise

    assert store.has_processed("k1") is True


def test_schema_mismatched_line_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text(json.dumps({"record_id": "x", "idempotency_key": "k"}) + "\n")

    store = AuditStore(path)  # must not raise

    assert store.has_processed("k") is False


def test_write_failure_is_surfaced_not_swallowed(tmp_path):
    readonly_dir = tmp_path / "readonly"
    readonly_dir.mkdir()
    audit_path = readonly_dir / "audit.jsonl"
    store = AuditStore(audit_path)

    readonly_dir.chmod(0o500)  # read+execute only: file creation inside must fail
    try:
        with pytest.raises(AuditTrailWriteError):
            store.record(
                idempotency_key="orders-2026-08-20",
                dataset="customer_orders",
                source_type="postgresql",
                outcome="success",
            )
    finally:
        readonly_dir.chmod(0o700)  # restore so tmp_path fixture cleanup can delete it
