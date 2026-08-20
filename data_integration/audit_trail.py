"""Audit trail for data-processing attempts.

Every dataset processing attempt gets one persisted record: a unique id,
a UTC timestamp, and the outcome. Records are keyed by an idempotency key
supplied by the caller (typically dataset name + a content/run
fingerprint) so reprocessing the same data does not create a duplicate
audit entry — the existing record is returned instead of a new one being
written.

Persisted as JSON Lines to disk by default so the trail survives across
process runs, not just within one; that's what makes "reprocess the same
data in a later run and get no duplicate" possible to prove at all.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from data_integration.logging_setup import get_logger

logger = get_logger()

AuditOutcome = Literal["success", "failure"]


@dataclass(frozen=True)
class AuditRecord:
    record_id: str
    idempotency_key: str
    dataset: str
    source_type: str
    outcome: AuditOutcome
    timestamp: str
    row_count: int = 0
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "idempotency_key": self.idempotency_key,
            "dataset": self.dataset,
            "source_type": self.source_type,
            "outcome": self.outcome,
            "timestamp": self.timestamp,
            "row_count": self.row_count,
            "error": self.error,
        }


class AuditTrailWriteError(RuntimeError):
    """Raised when an audit record can't be durably persisted or read back.

    Per this repo's failure-first rule, a broken audit trail must be a
    loud, typed failure — never a silently skipped write.
    """


class AuditStore:
    """JSONL-backed audit trail with an idempotent record().

    Not safe for concurrent multi-process writers (no file locking) —
    the integration pipeline runs as a single process today; revisit if
    that changes.
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._records: dict[str, AuditRecord] = {}
        self._load_existing()

    def _load_existing(self) -> None:
        """Load prior records, tolerating individual corrupted lines.

        JSONL appends aren't atomic — a process killed mid-write can leave
        a truncated trailing line. That single bad line must not brick the
        whole audit trail (and therefore every future run of the caller)
        on the next startup, so it's logged and skipped rather than
        raised. Only a failure to open/read the file at all — a genuine
        "the trail is unavailable" condition, not "one record is bad" — is
        fatal.
        """
        if not self._path.exists() or not self._path.is_file():
            return
        try:
            with self._path.open("r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError as exc:
            raise AuditTrailWriteError(
                f"could not read existing audit trail at {self._path}: {exc}"
            ) from exc

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                record = AuditRecord(**data)
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                logger.warning(
                    "audit_record_skipped_corrupted",
                    extra={
                        "event": "audit_record_skipped_corrupted",
                        "outcome": "partial",
                        "error_class": exc.__class__.__name__,
                        "context": {"path": str(self._path)},
                    },
                )
                continue
            self._records[record.idempotency_key] = record

    def has_processed(self, idempotency_key: str) -> bool:
        return idempotency_key in self._records

    def record(
        self,
        *,
        idempotency_key: str,
        dataset: str,
        source_type: str,
        outcome: AuditOutcome,
        row_count: int = 0,
        error: str | None = None,
    ) -> AuditRecord:
        """Persist one audit record, unless idempotency_key was already seen.

        Returns the new record, or the existing one if this key was
        already recorded — reprocessing the same data must not duplicate
        the trail.
        """
        with self._lock:
            existing = self._records.get(idempotency_key)
            if existing is not None:
                logger.info(
                    "audit_duplicate_skipped",
                    extra={
                        "event": "audit_duplicate_skipped",
                        "source": source_type,
                        "outcome": "success",
                        "context": {"dataset": dataset, "idempotency_key": idempotency_key},
                    },
                )
                return existing

            entry = AuditRecord(
                record_id=str(uuid.uuid4()),
                idempotency_key=idempotency_key,
                dataset=dataset,
                source_type=source_type,
                outcome=outcome,
                timestamp=datetime.now(timezone.utc).isoformat(),
                row_count=row_count,
                error=error,
            )
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry.to_json()) + "\n")
            except OSError as exc:
                logger.error(
                    "audit_trail_write_failed",
                    extra={
                        "event": "audit_trail_write_failed",
                        "source": source_type,
                        "outcome": "failure",
                        "error_class": exc.__class__.__name__,
                        "context": {"dataset": dataset, "idempotency_key": idempotency_key},
                    },
                )
                raise AuditTrailWriteError(
                    f"failed to write audit record for {dataset!r}: {exc}"
                ) from exc

            self._records[idempotency_key] = entry
            return entry
