"""Audit trail for Executive Control Tower dashboard updates (STORY-009 / REQ-015).

Every tile in a DashboardSnapshot (dashboard/metrics.py) gets one
persisted record: which dashboard build, which metric, its status/
severity, its source agent (the "data source" AC3 requires), and a
timestamp. Records are keyed by an idempotency key -
`f"{dashboard_id}:{metric_id}"` - so re-recording the same metric within
the same dashboard build does not create a duplicate entry; the existing
record is returned instead of a new one being written. This is what
satisfies AC3 ("logs all dashboard updates with timestamps and data
sources").

Mirrors data_quality_monitoring/audit_trail.py's QualityAuditStore
almost exactly - re-keyed for "one metric tile of one dashboard build"
instead of "one dimension of one quality-check run" - same JSONL
persistence, same corrupted-line tolerance, same idempotent record()
semantics, so this trust-spine implementation behaves identically to
every other one in this repo.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from dashboard.logging_setup import get_logger

logger = get_logger()

MetricOutcome = Literal["ok", "error"]


def _idempotency_key(dashboard_id: str, metric_id: str) -> str:
    return f"{dashboard_id}:{metric_id}"


@dataclass(frozen=True)
class DashboardAuditRecord:
    record_id: str
    idempotency_key: str
    dashboard_id: str
    metric_id: str
    source_agent: str
    outcome: MetricOutcome
    timestamp: str
    severity: str | None = None
    headline: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "idempotency_key": self.idempotency_key,
            "dashboard_id": self.dashboard_id,
            "metric_id": self.metric_id,
            "source_agent": self.source_agent,
            "outcome": self.outcome,
            "timestamp": self.timestamp,
            "severity": self.severity,
            "headline": self.headline,
        }


class DashboardAuditWriteError(RuntimeError):
    """Raised when a dashboard audit record can't be durably persisted or read back.

    Per this repo's failure-first rule, a broken audit trail must be a
    loud, typed failure - never a silently skipped write. This is what
    satisfies the "notification system failure" failure path at the
    trust-spine layer: if the trail can't be written, the caller must
    know about it, not proceed as if the update landed.
    """


class DashboardAuditStore:
    """JSONL-backed audit trail of dashboard updates, with an idempotent record().

    Not safe for concurrent multi-process writers (no file locking) -
    dashboard builds run as a single process today, same caveat as every
    other *AuditStore in this repo.
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._records: dict[str, DashboardAuditRecord] = {}
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
            raise DashboardAuditWriteError(
                f"could not read existing dashboard audit trail at {self._path}: {exc}"
            ) from exc

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                record = DashboardAuditRecord(**data)
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                logger.warning(
                    "dashboard_audit_record_skipped_corrupted",
                    extra={
                        "event": "dashboard_audit_record_skipped_corrupted",
                        "outcome": "partial",
                        "error_class": exc.__class__.__name__,
                        "context": {"path": str(self._path)},
                    },
                )
                continue
            self._records[record.idempotency_key] = record

    def has_recorded(self, dashboard_id: str, metric_id: str) -> bool:
        return _idempotency_key(dashboard_id, metric_id) in self._records

    def records_for_dashboard(self, dashboard_id: str) -> list[DashboardAuditRecord]:
        return [r for r in self._records.values() if r.dashboard_id == dashboard_id]

    def record(
        self,
        *,
        dashboard_id: str,
        metric_id: str,
        source_agent: str,
        outcome: MetricOutcome,
        severity: str | None = None,
        headline: str | None = None,
    ) -> DashboardAuditRecord:
        """Persist one metric's dashboard-update record, unless (dashboard_id, metric_id) was already seen.

        Returns the new record, or the existing one if this
        (dashboard_id, metric_id) pair was already recorded - re-recording
        the same tile within the same dashboard build must not duplicate
        the trail.
        """
        idempotency_key = _idempotency_key(dashboard_id, metric_id)
        with self._lock:
            existing = self._records.get(idempotency_key)
            if existing is not None:
                logger.info(
                    "dashboard_audit_duplicate_skipped",
                    extra={
                        "event": "dashboard_audit_duplicate_skipped",
                        "outcome": "success",
                        "correlation_id": dashboard_id,
                        "context": {"metric_id": metric_id, "idempotency_key": idempotency_key},
                    },
                )
                return existing

            entry = DashboardAuditRecord(
                record_id=str(uuid.uuid4()),
                idempotency_key=idempotency_key,
                dashboard_id=dashboard_id,
                metric_id=metric_id,
                source_agent=source_agent,
                outcome=outcome,
                timestamp=datetime.now(timezone.utc).isoformat(),
                severity=severity,
                headline=headline,
            )
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry.to_json()) + "\n")
            except OSError as exc:
                logger.error(
                    "dashboard_audit_trail_write_failed",
                    extra={
                        "event": "dashboard_audit_trail_write_failed",
                        "outcome": "failure",
                        "error_class": exc.__class__.__name__,
                        "correlation_id": dashboard_id,
                        "context": {"metric_id": metric_id, "idempotency_key": idempotency_key},
                    },
                )
                raise DashboardAuditWriteError(
                    f"failed to write dashboard audit record for dashboard {dashboard_id!r} "
                    f"metric {metric_id!r}: {exc}"
                ) from exc

            self._records[idempotency_key] = entry
            return entry
