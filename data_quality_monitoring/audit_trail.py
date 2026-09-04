"""Audit trail for data quality checks (STORY-015 / REQ-017).

Every dimension scored in a quality-check run gets one persisted record:
which check run, which dimension, its score/issue counts, and a
timestamp. A run whose dimensions all fail to even compute (e.g. an
unexpected crash) still gets one run-level record (`dimension=None`), so
a completed check is never left with zero audit trace. Records are keyed
by an idempotency key - `f"{check_id}:{dimension or ''}"` - so
re-recording the same dimension's result within the same run does not
create a duplicate entry; the existing record is returned instead of a
new one being written. This is what satisfies the Trust AC ("given data
quality monitoring, an audit trail of quality checks is maintained") and
the "audit trail not recorded for quality checks" failure path.

This mirrors shipment_delay_analysis/audit_trail.py's
ShipmentDelayAuditStore (STORY-014) and, in turn,
supplier_evaluation/audit_trail.py's SupplierEvaluationAuditStore
(STORY-013) almost exactly - re-keyed for "one dimension of one
quality-check run" instead of "one PO of one delay-analysis run" - same
JSONL persistence, same corrupted-line tolerance, same idempotent
record() semantics, so every trust-spine implementation in this repo
behaves identically to anyone auditing any of them.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from data_quality_monitoring.logging_setup import get_logger

logger = get_logger()

CheckOutcome = Literal["success", "failure"]


def _idempotency_key(check_id: str, dimension: str | None) -> str:
    # dimension=None (a run-level record) maps to "" here, never to a
    # real dimension's key - QualityCheckResult.dimension is always a
    # non-empty Literal value, so "" is safely reserved for "no specific
    # dimension" without needing a magic sentinel string a real dimension
    # name could collide with.
    return f"{check_id}:{dimension or ''}"


@dataclass(frozen=True)
class QualityAuditRecord:
    record_id: str
    idempotency_key: str
    check_id: str
    dimension: str | None  # None marks a run-level record - see record()'s docstring
    outcome: CheckOutcome
    timestamp: str
    score: float | None = None
    checked_rows: int | None = None
    issue_rows: int | None = None
    detail: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "idempotency_key": self.idempotency_key,
            "check_id": self.check_id,
            "dimension": self.dimension,
            "outcome": self.outcome,
            "timestamp": self.timestamp,
            "score": self.score,
            "checked_rows": self.checked_rows,
            "issue_rows": self.issue_rows,
            "detail": self.detail,
        }


class QualityAuditWriteError(RuntimeError):
    """Raised when a quality-check audit record can't be durably persisted or read back.

    Per this repo's failure-first rule, a broken audit trail must be a
    loud, typed failure - never a silently skipped write. This is what
    satisfies the "audit trail not recorded for quality checks" failure
    path: if the trail can't be written, the caller must know about it,
    not proceed as if the record landed.
    """


class QualityAuditStore:
    """JSONL-backed audit trail of data-quality checks, with an idempotent record().

    Not safe for concurrent multi-process writers (no file locking) -
    quality monitoring runs as a single process today, same caveat as
    shipment_delay_analysis/audit_trail.py's ShipmentDelayAuditStore.
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._records: dict[str, QualityAuditRecord] = {}
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
            raise QualityAuditWriteError(
                f"could not read existing data quality audit trail at {self._path}: {exc}"
            ) from exc

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                record = QualityAuditRecord(**data)
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                logger.warning(
                    "quality_audit_record_skipped_corrupted",
                    extra={
                        "event": "quality_audit_record_skipped_corrupted",
                        "outcome": "partial",
                        "error_class": exc.__class__.__name__,
                        "context": {"path": str(self._path)},
                    },
                )
                continue
            self._records[record.idempotency_key] = record

    def has_recorded(self, check_id: str, dimension: str | None) -> bool:
        return _idempotency_key(check_id, dimension) in self._records

    def records_for_check(self, check_id: str) -> list[QualityAuditRecord]:
        return [r for r in self._records.values() if r.check_id == check_id]

    def record(
        self,
        *,
        check_id: str,
        dimension: str | None,
        outcome: CheckOutcome,
        score: float | None = None,
        checked_rows: int | None = None,
        issue_rows: int | None = None,
        detail: str | None = None,
    ) -> QualityAuditRecord:
        """Persist one dimension's quality-check record, unless (check_id, dimension) was already seen.

        `dimension=None` records a run-level event rather than one tied
        to a specific dimension - assess_data_quality's own dimension
        results always carry a real dimension name, so `None` can't
        collide with one.

        Returns the new record, or the existing one if this
        (check_id, dimension) pair was already recorded - re-recording
        the same dimension's result within the same run must not
        duplicate the trail.
        """
        idempotency_key = _idempotency_key(check_id, dimension)
        with self._lock:
            existing = self._records.get(idempotency_key)
            if existing is not None:
                logger.info(
                    "quality_audit_duplicate_skipped",
                    extra={
                        "event": "quality_audit_duplicate_skipped",
                        "outcome": "success",
                        "correlation_id": check_id,
                        "context": {"dimension": dimension, "idempotency_key": idempotency_key},
                    },
                )
                return existing

            entry = QualityAuditRecord(
                record_id=str(uuid.uuid4()),
                idempotency_key=idempotency_key,
                check_id=check_id,
                dimension=dimension,
                outcome=outcome,
                timestamp=datetime.now(timezone.utc).isoformat(),
                score=score,
                checked_rows=checked_rows,
                issue_rows=issue_rows,
                detail=detail,
            )
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry.to_json()) + "\n")
            except OSError as exc:
                logger.error(
                    "quality_audit_trail_write_failed",
                    extra={
                        "event": "quality_audit_trail_write_failed",
                        "outcome": "failure",
                        "error_class": exc.__class__.__name__,
                        "correlation_id": check_id,
                        "context": {"dimension": dimension, "idempotency_key": idempotency_key},
                    },
                )
                raise QualityAuditWriteError(
                    f"failed to write data quality audit record for check "
                    f"{check_id!r} dimension {dimension!r}: {exc}"
                ) from exc

            self._records[idempotency_key] = entry
            return entry
