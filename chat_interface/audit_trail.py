"""Audit trail for chat interactions (STORY-010 / REQ-016).

Every call to answer_query() gets one persisted record: the query text,
the timestamp, what topic (if any) it matched, and the answer given - this
is what satisfies the Trust AC ("the system logs all chat interactions
with timestamps and query details") and the "chat interface failure"
failure path (a crashed interaction still gets a record, so a completed
call is never left with zero audit trace).

Mirrors data_quality_monitoring/audit_trail.py's QualityAuditStore (STORY-
015) almost exactly - re-keyed for "one chat interaction" instead of "one
dimension of one quality-check run" - same JSONL persistence, same
corrupted-line tolerance, same idempotent record() semantics, so every
trust-spine implementation in this repo behaves identically to anyone
auditing any of them. Idempotency here is simpler than QualityAuditStore's:
one interaction is exactly one record, keyed directly on the caller-
supplied `interaction_id` (or a fresh UUID per call if the caller has none)
- there is no sub-dimension to also key on.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from chat_interface.logging_setup import get_logger

logger = get_logger()

ChatAuditOutcome = Literal["success", "failure"]


@dataclass(frozen=True)
class ChatAuditRecord:
    record_id: str
    idempotency_key: str
    interaction_id: str
    outcome: ChatAuditOutcome
    timestamp: str
    query_text: str
    status: str | None = None  # ChatResponseStatus ("answered"/"unsupported"/"data_unavailable"); None if crashed
    topic: str | None = None
    answer: str | None = None
    detail: str | None = None

    def to_json(self) -> dict[str, Any]:
        # Every field is already a JSON-safe scalar or None, so asdict()
        # alone is the full serialization - no hand-listed field-by-field
        # mirror to fall out of sync with the dataclass definition above.
        return asdict(self)


class ChatAuditWriteError(RuntimeError):
    """Raised when a chat-interaction audit record can't be durably persisted or read back.

    Per this repo's failure-first rule, a broken audit trail must be a
    loud, typed failure - never a silently skipped write. This is what
    satisfies the "audit trail not recorded for chat interactions" reading
    of the Trust AC: if the trail can't be written, the caller must know
    about it, not proceed as if the record landed.
    """


class ChatAuditStore:
    """JSONL-backed audit trail of chat interactions, with an idempotent record().

    Not safe for concurrent multi-process writers (no file locking) - chat
    runs as a single process today, same caveat every sibling *AuditStore
    in this repo documents for itself.
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._records: dict[str, ChatAuditRecord] = {}
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
            raise ChatAuditWriteError(f"could not read existing chat audit trail at {self._path}: {exc}") from exc

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                record = ChatAuditRecord(**data)
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                logger.warning(
                    "chat_audit_record_skipped_corrupted",
                    extra={
                        "event": "chat_audit_record_skipped_corrupted",
                        "outcome": "partial",
                        "error_class": exc.__class__.__name__,
                        "context": {"path": str(self._path)},
                    },
                )
                continue
            self._records[record.idempotency_key] = record

    def has_recorded(self, interaction_id: str) -> bool:
        return interaction_id in self._records

    def get_record(self, interaction_id: str) -> ChatAuditRecord | None:
        return self._records.get(interaction_id)

    def record(
        self,
        *,
        interaction_id: str | None = None,
        outcome: ChatAuditOutcome,
        query_text: str,
        status: str | None = None,
        topic: str | None = None,
        answer: str | None = None,
        detail: str | None = None,
    ) -> ChatAuditRecord:
        """Persist one chat interaction, unless `interaction_id` was already recorded.

        Returns the new record, or the existing one if this
        `interaction_id` was already recorded - re-recording the same
        interaction (e.g. a caller retrying after a network hiccup on
        their side) must not duplicate the trail.
        """
        interaction_id = interaction_id or str(uuid.uuid4())
        with self._lock:
            existing = self._records.get(interaction_id)
            if existing is not None:
                logger.info(
                    "chat_audit_duplicate_skipped",
                    extra={
                        "event": "chat_audit_duplicate_skipped",
                        "outcome": "success",
                        "correlation_id": interaction_id,
                        "context": {"idempotency_key": interaction_id},
                    },
                )
                return existing

            entry = ChatAuditRecord(
                record_id=str(uuid.uuid4()),
                idempotency_key=interaction_id,
                interaction_id=interaction_id,
                outcome=outcome,
                timestamp=datetime.now(timezone.utc).isoformat(),
                query_text=query_text,
                status=status,
                topic=topic,
                answer=answer,
                detail=detail,
            )
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry.to_json()) + "\n")
            except OSError as exc:
                logger.error(
                    "chat_audit_trail_write_failed",
                    extra={
                        "event": "chat_audit_trail_write_failed",
                        "outcome": "failure",
                        "error_class": exc.__class__.__name__,
                        "correlation_id": interaction_id,
                        "context": {},
                    },
                )
                raise ChatAuditWriteError(
                    f"failed to write chat audit record for interaction {interaction_id!r}: {exc}"
                ) from exc

            self._records[interaction_id] = entry
            return entry
