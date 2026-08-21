"""Audit trail for intelligence-model stage executions (STORY-012 / REQ-003).

Every stage execution (Observe, Understand, Predict, Recommend) of a
pipeline run gets one persisted record: which run, which stage, its
outcome, and a timestamp. Records are keyed by an idempotency key -
`f"{run_id}:{stage}"` - so re-recording the same stage of the same run
does not create a duplicate audit entry; the existing record is returned
instead of a new one being written.

This mirrors data_integration/audit_trail.py's AuditStore (STORY-011)
almost exactly, just re-keyed for "one stage of one pipeline run" instead
of "one dataset pull" - same JSONL persistence, same corrupted-line
tolerance, same idempotent record() semantics, so the two trust-spine
implementations behave identically to anyone auditing either one.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from intelligence.contracts import StageName, StageOutcome
from intelligence.logging_setup import get_logger

logger = get_logger()


@dataclass(frozen=True)
class StageAuditRecord:
    record_id: str
    idempotency_key: str
    run_id: str
    stage: StageName
    outcome: StageOutcome
    timestamp: str
    detail: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "idempotency_key": self.idempotency_key,
            "run_id": self.run_id,
            "stage": self.stage,
            "outcome": self.outcome,
            "timestamp": self.timestamp,
            "detail": self.detail,
        }


class StageAuditWriteError(RuntimeError):
    """Raised when a stage audit record can't be durably persisted or read back.

    Per this repo's failure-first rule, a broken audit trail must be a
    loud, typed failure - never a silently skipped write. This is what
    satisfies the "audit trail missing for model stages" failure path:
    if the trail can't be written, the pipeline run must know about it,
    not proceed as if the record landed.
    """


class StageAuditStore:
    """JSONL-backed audit trail of intelligence-model stage runs, with an idempotent record().

    Not safe for concurrent multi-process writers (no file locking) - the
    intelligence pipeline runs as a single process today, same caveat as
    data_integration/audit_trail.py's AuditStore.
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._records: dict[str, StageAuditRecord] = {}
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
            raise StageAuditWriteError(
                f"could not read existing stage audit trail at {self._path}: {exc}"
            ) from exc

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                record = StageAuditRecord(**data)
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                logger.warning(
                    "stage_audit_record_skipped_corrupted",
                    extra={
                        "event": "stage_audit_record_skipped_corrupted",
                        "outcome": "partial",
                        "error_class": exc.__class__.__name__,
                        "context": {"path": str(self._path)},
                    },
                )
                continue
            self._records[record.idempotency_key] = record

    def has_recorded(self, run_id: str, stage: StageName) -> bool:
        return f"{run_id}:{stage}" in self._records

    def records_for_run(self, run_id: str) -> list[StageAuditRecord]:
        return [r for r in self._records.values() if r.run_id == run_id]

    def record(
        self,
        *,
        run_id: str,
        stage: StageName,
        outcome: StageOutcome,
        detail: str | None = None,
    ) -> StageAuditRecord:
        """Persist one stage-execution record, unless (run_id, stage) was already seen.

        Returns the new record, or the existing one if this (run_id, stage)
        pair was already recorded - re-recording the same stage of the
        same run must not duplicate the trail.
        """
        idempotency_key = f"{run_id}:{stage}"
        with self._lock:
            existing = self._records.get(idempotency_key)
            if existing is not None:
                logger.info(
                    "stage_audit_duplicate_skipped",
                    extra={
                        "event": "stage_audit_duplicate_skipped",
                        "outcome": "success",
                        "correlation_id": run_id,
                        "context": {"stage": stage, "idempotency_key": idempotency_key},
                    },
                )
                return existing

            entry = StageAuditRecord(
                record_id=str(uuid.uuid4()),
                idempotency_key=idempotency_key,
                run_id=run_id,
                stage=stage,
                outcome=outcome,
                timestamp=datetime.now(timezone.utc).isoformat(),
                detail=detail,
            )
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry.to_json()) + "\n")
            except OSError as exc:
                logger.error(
                    "stage_audit_trail_write_failed",
                    extra={
                        "event": "stage_audit_trail_write_failed",
                        "outcome": "failure",
                        "error_class": exc.__class__.__name__,
                        "correlation_id": run_id,
                        "context": {"stage": stage, "idempotency_key": idempotency_key},
                    },
                )
                raise StageAuditWriteError(
                    f"failed to write stage audit record for run {run_id!r} stage {stage!r}: {exc}"
                ) from exc

            self._records[idempotency_key] = entry
            return entry
