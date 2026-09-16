"""Human-approval trail for Executive Control Tower recommendations (Phase 2:
Human Approval & Audit Trail).

Every tile on a dashboard already carries an AI-generated recommendation
(dashboard/metrics.py's DashboardMetric.headline). This adds the other
half of "Human Approval & Audit Trail": a durable record of a person
approving or rejecting that recommendation, so the trail shows not just
what the system suggested but what a human actually decided to do about
it - the same distinction CLAUDE.md's Autonomy Model draws between an
AI's recommendation and a human's governance decision.

Keyed on (dashboard_id, metric_id) - the same identity dashboard/
audit_trail.py's DashboardAuditRecord already uses, and stable across
rebuilds for the 3 named scenarios this repo's demos use ("real_data",
"partial_failure", "synthetic_healthy" - not a fresh UUID per build), so
"I approved today's Stockout Risk recommendation" and "I approved
yesterday's" are the same identity across a Live Data page's repeated
refreshes.

Deliberately NOT idempotency-deduplicated like DashboardAuditStore's
record() is: a human approving, and later rejecting, the same
recommendation are two real, distinct decisions that both belong in the
trail - collapsing them the way "did we already log this tile built ok"
collapses retries would silently lose the earlier decision. A double
submission of the exact same form is instead guarded client-side (the
submit button disables after use), the same pattern every other local
form in this repo's suite (chat, data console, scenario simulator)
already follows - proportionate for a single-user local dev tool, not
a production multi-writer system.

Same JSONL persistence, corrupted-line tolerance, and loud write-failure
convention as every sibling *AuditStore in this repo - see
dashboard/audit_trail.py's own docstring for the canonical version this
mirrors.
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

ApprovalDecision = Literal["approved", "rejected"]


@dataclass(frozen=True)
class ApprovalRecord:
    decision_id: str
    dashboard_id: str
    metric_id: str
    decision: ApprovalDecision
    reviewer: str
    note: str
    timestamp: str

    def to_json(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "dashboard_id": self.dashboard_id,
            "metric_id": self.metric_id,
            "decision": self.decision,
            "reviewer": self.reviewer,
            "note": self.note,
            "timestamp": self.timestamp,
        }


class ApprovalWriteError(RuntimeError):
    """Raised when an approval decision can't be durably persisted or read back.

    Per this repo's failure-first rule, a broken approval trail must be a
    loud, typed failure - a reviewer's decision must never silently fail
    to record while the UI reports success.
    """


class ApprovalStore:
    """JSONL-backed, append-only log of human approve/reject decisions."""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._records: list[ApprovalRecord] = []
        self._load_existing()

    def _load_existing(self) -> None:
        if not self._path.exists() or not self._path.is_file():
            return
        try:
            with self._path.open("r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError as exc:
            raise ApprovalWriteError(f"could not read existing approval trail at {self._path}: {exc}") from exc

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                record = ApprovalRecord(**data)
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                logger.warning(
                    "approval_record_skipped_corrupted",
                    extra={
                        "event": "approval_record_skipped_corrupted",
                        "outcome": "partial",
                        "error_class": exc.__class__.__name__,
                        "context": {"path": str(self._path)},
                    },
                )
                continue
            self._records.append(record)

    def history_for(self, dashboard_id: str, metric_id: str) -> list[ApprovalRecord]:
        """All decisions for one (dashboard_id, metric_id), oldest first."""
        return [r for r in self._records if r.dashboard_id == dashboard_id and r.metric_id == metric_id]

    def latest_for(self, dashboard_id: str, metric_id: str) -> ApprovalRecord | None:
        history = self.history_for(dashboard_id, metric_id)
        return history[-1] if history else None

    def latest_by_metric(self, dashboard_id: str) -> dict[str, ApprovalRecord]:
        """The most recent decision per metric_id for one dashboard_id - what a tile's own badge shows."""
        latest: dict[str, ApprovalRecord] = {}
        for record in self._records:
            if record.dashboard_id == dashboard_id:
                latest[record.metric_id] = record  # records are appended in order, so last write wins
        return latest

    def records_for_dashboard(self, dashboard_id: str) -> list[ApprovalRecord]:
        """Every decision ever made on this dashboard_id, oldest first - the full audit trail section."""
        return [r for r in self._records if r.dashboard_id == dashboard_id]

    def record(
        self,
        *,
        dashboard_id: str,
        metric_id: str,
        decision: ApprovalDecision,
        reviewer: str,
        note: str = "",
    ) -> ApprovalRecord:
        """Persist one new approve/reject decision. Always appends - see module docstring for why this is not deduplicated."""
        with self._lock:
            entry = ApprovalRecord(
                decision_id=str(uuid.uuid4()),
                dashboard_id=dashboard_id,
                metric_id=metric_id,
                decision=decision,
                reviewer=reviewer,
                note=note,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry.to_json()) + "\n")
            except OSError as exc:
                logger.error(
                    "approval_trail_write_failed",
                    extra={
                        "event": "approval_trail_write_failed",
                        "outcome": "failure",
                        "error_class": exc.__class__.__name__,
                        "correlation_id": dashboard_id,
                        "context": {"metric_id": metric_id, "decision": decision},
                    },
                )
                raise ApprovalWriteError(
                    f"failed to write approval decision for dashboard {dashboard_id!r} metric {metric_id!r}: {exc}"
                ) from exc

            self._records.append(entry)
            return entry
