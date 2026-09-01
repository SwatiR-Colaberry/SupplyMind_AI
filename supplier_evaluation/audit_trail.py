"""Audit trail for supplier reliability evaluations (STORY-013 / REQ-007).

Every supplier evaluated in an evaluation run gets one persisted record:
which evaluation run, which supplier, the resulting score/severity/
flagged verdict, and a timestamp. Records are keyed by an idempotency key
- `f"{evaluation_id}:{supplier}"` - so re-recording the same supplier's
evaluation within the same run does not create a duplicate audit entry;
the existing record is returned instead of a new one being written. This
is what satisfies AC3 ("Trust: given supplier evaluation, then an audit
trail of the evaluation process is recorded") and the "Audit trail not
recorded for evaluations" failure path.

This mirrors intelligence/audit_trail.py's StageAuditStore (STORY-012)
and data_integration/audit_trail.py's AuditStore (STORY-011) almost
exactly, just re-keyed for "one supplier of one evaluation run" instead
of "one stage of one pipeline run" / "one dataset pull" - same JSONL
persistence, same corrupted-line tolerance, same idempotent record()
semantics, so all three trust-spine implementations behave identically to
anyone auditing any of them.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from supplier_evaluation.logging_setup import get_logger

logger = get_logger()

EvaluationOutcome = Literal["success", "failure"]


@dataclass(frozen=True)
class SupplierEvaluationAuditRecord:
    record_id: str
    idempotency_key: str
    evaluation_id: str
    supplier: str
    outcome: EvaluationOutcome
    timestamp: str
    score: float | None = None
    severity: str | None = None
    flagged_for_review: bool | None = None
    detail: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "idempotency_key": self.idempotency_key,
            "evaluation_id": self.evaluation_id,
            "supplier": self.supplier,
            "outcome": self.outcome,
            "timestamp": self.timestamp,
            "score": self.score,
            "severity": self.severity,
            "flagged_for_review": self.flagged_for_review,
            "detail": self.detail,
        }


class SupplierEvaluationAuditWriteError(RuntimeError):
    """Raised when a supplier evaluation audit record can't be durably persisted or read back.

    Per this repo's failure-first rule, a broken audit trail must be a
    loud, typed failure - never a silently skipped write. This is what
    satisfies the "audit trail not recorded for evaluations" failure
    path: if the trail can't be written, the caller must know about it,
    not proceed as if the record landed.
    """


class SupplierEvaluationAuditStore:
    """JSONL-backed audit trail of supplier reliability evaluations, with an idempotent record().

    Not safe for concurrent multi-process writers (no file locking) -
    supplier evaluation runs as a single process today, same caveat as
    intelligence/audit_trail.py's StageAuditStore and
    data_integration/audit_trail.py's AuditStore.
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._records: dict[str, SupplierEvaluationAuditRecord] = {}
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
            raise SupplierEvaluationAuditWriteError(
                f"could not read existing supplier evaluation audit trail at {self._path}: {exc}"
            ) from exc

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                record = SupplierEvaluationAuditRecord(**data)
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                logger.warning(
                    "supplier_evaluation_audit_record_skipped_corrupted",
                    extra={
                        "event": "supplier_evaluation_audit_record_skipped_corrupted",
                        "outcome": "partial",
                        "error_class": exc.__class__.__name__,
                        "context": {"path": str(self._path)},
                    },
                )
                continue
            self._records[record.idempotency_key] = record

    def has_recorded(self, evaluation_id: str, supplier: str) -> bool:
        return f"{evaluation_id}:{supplier}" in self._records

    def records_for_evaluation(self, evaluation_id: str) -> list[SupplierEvaluationAuditRecord]:
        return [r for r in self._records.values() if r.evaluation_id == evaluation_id]

    def record(
        self,
        *,
        evaluation_id: str,
        supplier: str,
        outcome: EvaluationOutcome,
        score: float | None = None,
        severity: str | None = None,
        flagged_for_review: bool | None = None,
        detail: str | None = None,
    ) -> SupplierEvaluationAuditRecord:
        """Persist one supplier-evaluation record, unless (evaluation_id, supplier) was already seen.

        Returns the new record, or the existing one if this
        (evaluation_id, supplier) pair was already recorded -
        re-recording the same supplier's evaluation within the same run
        must not duplicate the trail.
        """
        idempotency_key = f"{evaluation_id}:{supplier}"
        with self._lock:
            existing = self._records.get(idempotency_key)
            if existing is not None:
                logger.info(
                    "supplier_evaluation_audit_duplicate_skipped",
                    extra={
                        "event": "supplier_evaluation_audit_duplicate_skipped",
                        "outcome": "success",
                        "correlation_id": evaluation_id,
                        "context": {"supplier": supplier, "idempotency_key": idempotency_key},
                    },
                )
                return existing

            entry = SupplierEvaluationAuditRecord(
                record_id=str(uuid.uuid4()),
                idempotency_key=idempotency_key,
                evaluation_id=evaluation_id,
                supplier=supplier,
                outcome=outcome,
                timestamp=datetime.now(timezone.utc).isoformat(),
                score=score,
                severity=severity,
                flagged_for_review=flagged_for_review,
                detail=detail,
            )
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry.to_json()) + "\n")
            except OSError as exc:
                logger.error(
                    "supplier_evaluation_audit_trail_write_failed",
                    extra={
                        "event": "supplier_evaluation_audit_trail_write_failed",
                        "outcome": "failure",
                        "error_class": exc.__class__.__name__,
                        "correlation_id": evaluation_id,
                        "context": {"supplier": supplier, "idempotency_key": idempotency_key},
                    },
                )
                raise SupplierEvaluationAuditWriteError(
                    f"failed to write supplier evaluation audit record for evaluation "
                    f"{evaluation_id!r} supplier {supplier!r}: {exc}"
                ) from exc

            self._records[idempotency_key] = entry
            return entry
