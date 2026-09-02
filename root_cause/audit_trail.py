"""Audit trail for root cause analyses (STORY-007 / REQ-013).

Every analysis attempt gets one persisted record: which analysis run,
which issue it investigated, the resulting confidence level, and a
timestamp - this is what satisfies AC3 ("Trust: the system logs all root
cause analyses with timestamps and confidence levels"). Records are keyed
by an idempotency key - `f"{analysis_id}:{subject_kind}:{subject}"` - so
re-recording the same issue's analysis within the same run does not
create a duplicate audit entry; the existing record is returned instead
of a new one being written.

This mirrors supplier_evaluation/audit_trail.py's SupplierEvaluationAuditStore
(STORY-013) almost exactly, just re-keyed for "one issue of one analysis
run" instead of "one supplier of one evaluation run" - same JSONL
persistence, same corrupted-line tolerance, same idempotent record()
semantics, so every trust-spine implementation in this repo behaves
identically to anyone auditing any of them.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from root_cause.logging_setup import get_logger

logger = get_logger()

AnalysisOutcome = Literal["success", "failure"]


def _idempotency_key(analysis_id: str, subject_kind: str, subject: str) -> str:
    return f"{analysis_id}:{subject_kind}:{subject}"


@dataclass(frozen=True)
class RootCauseAuditRecord:
    record_id: str
    idempotency_key: str
    analysis_id: str
    subject: str
    subject_kind: str
    outcome: AnalysisOutcome
    timestamp: str
    confidence: float | None = None
    candidate_count: int | None = None
    detail: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "idempotency_key": self.idempotency_key,
            "analysis_id": self.analysis_id,
            "subject": self.subject,
            "subject_kind": self.subject_kind,
            "outcome": self.outcome,
            "timestamp": self.timestamp,
            "confidence": self.confidence,
            "candidate_count": self.candidate_count,
            "detail": self.detail,
        }


class RootCauseAuditWriteError(RuntimeError):
    """Raised when a root cause analysis audit record can't be durably persisted or read back.

    Per this repo's failure-first rule, a broken audit trail must be a
    loud, typed failure - never a silently skipped write. This is what
    satisfies the "audit trail not recorded for analyses" failure mode:
    if the trail can't be written, the caller must know about it, not
    proceed as if the record landed.
    """


class RootCauseAuditStore:
    """JSONL-backed audit trail of root cause analyses, with an idempotent record().

    Not safe for concurrent multi-process writers (no file locking) - root
    cause analysis runs as a single process today, same caveat as every
    other audit store in this repo (supplier_evaluation/audit_trail.py,
    intelligence/audit_trail.py, data_integration/audit_trail.py).
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._records: dict[str, RootCauseAuditRecord] = {}
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
            raise RootCauseAuditWriteError(
                f"could not read existing root cause audit trail at {self._path}: {exc}"
            ) from exc

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                record = RootCauseAuditRecord(**data)
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                logger.warning(
                    "root_cause_audit_record_skipped_corrupted",
                    extra={
                        "event": "root_cause_audit_record_skipped_corrupted",
                        "outcome": "partial",
                        "error_class": exc.__class__.__name__,
                        "context": {"path": str(self._path)},
                    },
                )
                continue
            self._records[record.idempotency_key] = record

    def has_recorded(self, analysis_id: str, subject_kind: str, subject: str) -> bool:
        return _idempotency_key(analysis_id, subject_kind, subject) in self._records

    def records_for_analysis(self, analysis_id: str) -> list[RootCauseAuditRecord]:
        return [r for r in self._records.values() if r.analysis_id == analysis_id]

    def record(
        self,
        *,
        analysis_id: str,
        subject: str,
        subject_kind: str,
        outcome: AnalysisOutcome,
        confidence: float | None = None,
        candidate_count: int | None = None,
        detail: str | None = None,
    ) -> RootCauseAuditRecord:
        """Persist one root-cause-analysis record, unless (analysis_id, subject_kind, subject) was already seen.

        Returns the new record, or the existing one if this
        (analysis_id, subject_kind, subject) triple was already recorded -
        re-recording the same issue's analysis within the same run must
        not duplicate the trail.
        """
        idempotency_key = _idempotency_key(analysis_id, subject_kind, subject)
        with self._lock:
            existing = self._records.get(idempotency_key)
            if existing is not None:
                logger.info(
                    "root_cause_audit_duplicate_skipped",
                    extra={
                        "event": "root_cause_audit_duplicate_skipped",
                        "outcome": "success",
                        "correlation_id": analysis_id,
                        "context": {"subject": subject, "idempotency_key": idempotency_key},
                    },
                )
                return existing

            entry = RootCauseAuditRecord(
                record_id=str(uuid.uuid4()),
                idempotency_key=idempotency_key,
                analysis_id=analysis_id,
                subject=subject,
                subject_kind=subject_kind,
                outcome=outcome,
                timestamp=datetime.now(timezone.utc).isoformat(),
                confidence=confidence,
                candidate_count=candidate_count,
                detail=detail,
            )
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry.to_json()) + "\n")
            except OSError as exc:
                logger.error(
                    "root_cause_audit_trail_write_failed",
                    extra={
                        "event": "root_cause_audit_trail_write_failed",
                        "outcome": "failure",
                        "error_class": exc.__class__.__name__,
                        "correlation_id": analysis_id,
                        "context": {"subject": subject, "idempotency_key": idempotency_key},
                    },
                )
                raise RootCauseAuditWriteError(
                    f"failed to write root cause audit record for analysis "
                    f"{analysis_id!r} subject {subject!r}: {exc}"
                ) from exc

            self._records[idempotency_key] = entry
            return entry
