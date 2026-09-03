"""Audit trail for shipment delay analyses (STORY-014 / REQ-008).

Every delayed PO scored in an analysis run gets one persisted record:
which analysis run, which PO, its delay/severity/cost, and a timestamp.
A run with no delayed PO at all (e.g. no delivery data provided, or every
delivery was on time) still gets one run-level record (`po_id=None`), so
a completed analysis is never left with zero audit trace. Records are
keyed by an idempotency key - `f"{analysis_id}:{po_id or ''}"` - so
re-recording the same PO's analysis within the same run does not create
a duplicate entry; the existing record is returned instead of a new one
being written. This is what satisfies AC3 ("Trust: given shipment
analysis, an audit trail of delay analysis is maintained") and the
"audit trail missing for shipment analysis" failure path.

This mirrors supplier_evaluation/audit_trail.py's
SupplierEvaluationAuditStore (STORY-013) almost exactly - re-keyed for
"one PO of one delay-analysis run" instead of "one supplier of one
evaluation run" - same JSONL persistence, same corrupted-line tolerance,
same idempotent record() semantics, so every trust-spine implementation
in this repo behaves identically to anyone auditing any of them.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from shipment_delay_analysis.logging_setup import get_logger

logger = get_logger()

AnalysisOutcome = Literal["success", "failure"]


def _idempotency_key(analysis_id: str, po_id: str | None) -> str:
    # po_id=None (a run-level record) maps to "" here, never to a real
    # PO's key - detect_supplier_delays' REQUIRED_DELIVERY_FIELDS
    # guarantees a delay anomaly's po_id is always a non-empty str(...),
    # so "" is safely reserved for "no specific PO" without needing a
    # magic sentinel string a real po_id could collide with.
    return f"{analysis_id}:{po_id or ''}"


@dataclass(frozen=True)
class ShipmentDelayAuditRecord:
    record_id: str
    idempotency_key: str
    analysis_id: str
    po_id: str | None  # None marks a run-level record - see record()'s docstring
    outcome: AnalysisOutcome
    timestamp: str
    delay_days: int | None = None
    severity: str | None = None
    total_cost: float | None = None
    detail: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "idempotency_key": self.idempotency_key,
            "analysis_id": self.analysis_id,
            "po_id": self.po_id,
            "outcome": self.outcome,
            "timestamp": self.timestamp,
            "delay_days": self.delay_days,
            "severity": self.severity,
            "total_cost": self.total_cost,
            "detail": self.detail,
        }


class ShipmentDelayAuditWriteError(RuntimeError):
    """Raised when a shipment delay audit record can't be durably persisted or read back.

    Per this repo's failure-first rule, a broken audit trail must be a
    loud, typed failure - never a silently skipped write. This is what
    satisfies the "audit trail missing for shipment analysis" failure
    path: if the trail can't be written, the caller must know about it,
    not proceed as if the record landed.
    """


class ShipmentDelayAuditStore:
    """JSONL-backed audit trail of shipment delay analyses, with an idempotent record().

    Not safe for concurrent multi-process writers (no file locking) -
    delay analysis runs as a single process today, same caveat as
    supplier_evaluation/audit_trail.py's SupplierEvaluationAuditStore.
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._records: dict[str, ShipmentDelayAuditRecord] = {}
        self._load_existing()

    def _load_existing(self) -> None:
        """Load prior records, tolerating individual corrupted lines.

        JSONL appends aren't atomic - a process killed mid-write can leave
        a truncated trailing line. That single bad line must not brick the
        whole audit trail on the next startup, so it's logged and skipped
        rather than raised. Only a failure to open/read the file at all is
        fatal.
        """
        if not self._path.exists() or not self._path.is_file():
            return
        try:
            with self._path.open("r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError as exc:
            raise ShipmentDelayAuditWriteError(
                f"could not read existing shipment delay audit trail at {self._path}: {exc}"
            ) from exc

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                record = ShipmentDelayAuditRecord(**data)
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                logger.warning(
                    "shipment_delay_audit_record_skipped_corrupted",
                    extra={
                        "event": "shipment_delay_audit_record_skipped_corrupted",
                        "outcome": "partial",
                        "error_class": exc.__class__.__name__,
                        "context": {"path": str(self._path)},
                    },
                )
                continue
            self._records[record.idempotency_key] = record

    def has_recorded(self, analysis_id: str, po_id: str | None) -> bool:
        return _idempotency_key(analysis_id, po_id) in self._records

    def records_for_analysis(self, analysis_id: str) -> list[ShipmentDelayAuditRecord]:
        return [r for r in self._records.values() if r.analysis_id == analysis_id]

    def record(
        self,
        *,
        analysis_id: str,
        po_id: str | None,
        outcome: AnalysisOutcome,
        delay_days: int | None = None,
        severity: str | None = None,
        total_cost: float | None = None,
        detail: str | None = None,
    ) -> ShipmentDelayAuditRecord:
        """Persist one PO's delay-analysis record, unless (analysis_id, po_id) was already seen.

        `po_id=None` records a run-level event rather than one tied to a
        specific PO - detect_supplier_delays guarantees a real delay
        anomaly's po_id is never `None` or empty, so `None` can't collide
        with a real PO.

        Returns the new record, or the existing one if this
        (analysis_id, po_id) pair was already recorded - re-recording the
        same PO's analysis within the same run must not duplicate the
        trail.
        """
        idempotency_key = _idempotency_key(analysis_id, po_id)
        with self._lock:
            existing = self._records.get(idempotency_key)
            if existing is not None:
                logger.info(
                    "shipment_delay_audit_duplicate_skipped",
                    extra={
                        "event": "shipment_delay_audit_duplicate_skipped",
                        "outcome": "success",
                        "correlation_id": analysis_id,
                        "context": {"po_id": po_id, "idempotency_key": idempotency_key},
                    },
                )
                return existing

            entry = ShipmentDelayAuditRecord(
                record_id=str(uuid.uuid4()),
                idempotency_key=idempotency_key,
                analysis_id=analysis_id,
                po_id=po_id,
                outcome=outcome,
                timestamp=datetime.now(timezone.utc).isoformat(),
                delay_days=delay_days,
                severity=severity,
                total_cost=total_cost,
                detail=detail,
            )
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry.to_json()) + "\n")
            except OSError as exc:
                logger.error(
                    "shipment_delay_audit_trail_write_failed",
                    extra={
                        "event": "shipment_delay_audit_trail_write_failed",
                        "outcome": "failure",
                        "error_class": exc.__class__.__name__,
                        "correlation_id": analysis_id,
                        "context": {"po_id": po_id, "idempotency_key": idempotency_key},
                    },
                )
                raise ShipmentDelayAuditWriteError(
                    f"failed to write shipment delay audit record for analysis "
                    f"{analysis_id!r} PO {po_id!r}: {exc}"
                ) from exc

            self._records[idempotency_key] = entry
            return entry
