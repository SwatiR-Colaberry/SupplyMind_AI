"""Ties dashboard/metrics.py's pure build_dashboard() to a durable audit trail (STORY-009 / REQ-015).

Same shape as every other *Evaluator in this repo (e.g.
data_quality_monitoring/evaluator.py's DataQualityEvaluator,
shipment_delay_analysis/evaluator.py's ShipmentDelayEvaluator): the
computation (build_dashboard()) stays pure and I/O-free, while this
module is the one place that decides what gets logged and durably
recorded, and when.

Every tile in the built snapshot gets its own audit record, regardless
of whether that tile's status is "ok" or "error" - an audit record means
"this tile was produced as part of this dashboard build," not "this
tile's data processed cleanly." If build_dashboard() itself crashes (a
genuinely unexpected bug, not a single agent's failure - individual
agent failures are already turned into "error" tiles inside
build_dashboard() and never raise), a run-level failure record is
written the same way DataQualityEvaluator writes a dimension=None
record on crash - "logs all dashboard updates" must not have a gap for
a completed build of any outcome.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from agents.orchestrator import CoordinationResult
from dashboard.audit_trail import DashboardAuditStore, DashboardAuditWriteError
from dashboard.data_freshness import DataFreshnessEntry
from dashboard.logging_setup import get_logger
from dashboard.metrics import DashboardSnapshot, build_dashboard

logger = get_logger()

BuildOutcome = Literal["success", "crashed"]

# Synthetic metric_id for a run-level audit record - distinct from any
# real agent name (agents/*.py's `name` attributes are always plain
# snake_case identifiers with no leading underscore), the same way
# QualityAuditStore reserves dimension=None for a run-level record.
_RUN_LEVEL_METRIC_ID = "_build"

# Prefix distinguishing a data-source freshness record from an agent
# tile's record in the same audit trail - a dataset name (e.g.
# "customer_orders") could otherwise collide with a future agent name.
_FRESHNESS_METRIC_PREFIX = "source:"


@dataclass
class DashboardBuildRun:
    """The full outcome of one DashboardEvaluator.run() call."""

    dashboard_id: str
    snapshot: DashboardSnapshot | None = None
    crash_error: str | None = None

    @property
    def outcome(self) -> BuildOutcome:
        return "crashed" if self.crash_error is not None else "success"


class DashboardEvaluator:
    """Builds a DashboardSnapshot and audits every tile, success or failure."""

    def __init__(self, audit_store: DashboardAuditStore) -> None:
        self._audit_store = audit_store

    def run(
        self,
        coordination_results: list[CoordinationResult],
        dashboard_id: str | None = None,
        data_freshness: list[DataFreshnessEntry] | None = None,
    ) -> DashboardBuildRun:
        dashboard_id = dashboard_id or str(uuid.uuid4())
        logger.info(
            "dashboard_build_started",
            extra={
                "event": "dashboard_build_started",
                "correlation_id": dashboard_id,
                "context": {"agent_count": len(coordination_results)},
            },
        )

        try:
            snapshot = build_dashboard(coordination_results, dashboard_id=dashboard_id, data_freshness=data_freshness)
        except Exception as exc:  # noqa: BLE001 - deliberate: see module docstring
            return self._fail_run(dashboard_id, exc)

        records = [
            (
                dict(
                    metric_id=metric.metric_id,
                    source_agent=metric.source_agent,
                    outcome=metric.status,
                    severity=metric.severity,
                    headline=metric.headline,
                ),
                "dashboard_metric_recorded",
                {"metric_id": metric.metric_id, "status": metric.status},
            )
            for metric in snapshot.metrics
        ] + [
            (
                dict(
                    metric_id=f"{_FRESHNESS_METRIC_PREFIX}{entry.dataset}",
                    source_agent=entry.source_type,
                    outcome="ok" if entry.outcome == "success" else "error",
                    headline=entry.error or f"{entry.row_count} row(s) pulled",
                ),
                "dashboard_data_source_recorded",
                {"dataset": entry.dataset, "pull_outcome": entry.outcome},
            )
            for entry in snapshot.data_freshness
        ]
        crash_error = self._record_all(dashboard_id, records)
        if crash_error is not None:
            # The audit store itself is broken - retrying immediately
            # would just raise again (infinite retry loops are
            # prohibited), so the build is reported crashed rather than
            # left with a misleadingly "successful" outcome and a
            # silently incomplete audit trail for whatever didn't get
            # recorded.
            return DashboardBuildRun(dashboard_id=dashboard_id, crash_error=crash_error)

        logger.info(
            "dashboard_build_completed",
            extra={
                "event": "dashboard_build_completed",
                "outcome": "success",
                "correlation_id": dashboard_id,
                "context": {
                    "overall_status": snapshot.overall_status,
                    "metric_count": len(snapshot.metrics),
                    "notification_raised": snapshot.notification is not None,
                },
            },
        )
        return DashboardBuildRun(dashboard_id=dashboard_id, snapshot=snapshot)

    def _record_all(self, dashboard_id: str, records: list[tuple[dict, str, dict]]) -> str | None:
        """Writes each (record_kwargs, success_log_event, success_log_context) in order.

        Stops at the first write failure and returns it as a string
        (the caller reports the whole build crashed); returns None only
        if every record in `records` was written successfully.
        """
        for record_kwargs, log_event, log_context in records:
            write_error = self._try_record(dashboard_id=dashboard_id, **record_kwargs)
            if write_error is not None:
                return str(write_error)
            logger.info(
                log_event,
                extra={"event": log_event, "outcome": "success", "correlation_id": dashboard_id, "context": log_context},
            )
        return None

    def _try_record(self, **kwargs) -> DashboardAuditWriteError | None:
        try:
            self._audit_store.record(**kwargs)
            return None
        except DashboardAuditWriteError as exc:
            logger.error(
                "dashboard_audit_write_failed",
                extra={
                    "event": "dashboard_audit_write_failed",
                    "outcome": "failure",
                    "error_class": exc.__class__.__name__,
                    "correlation_id": kwargs.get("dashboard_id"),
                    "context": {"metric_id": kwargs.get("metric_id")},
                },
            )
            return exc

    def _fail_run(self, dashboard_id: str, exc: Exception) -> DashboardBuildRun:
        logger.error(
            "dashboard_build_failed",
            extra={
                "event": "dashboard_build_failed",
                "outcome": "failure",
                "error_class": exc.__class__.__name__,
                "correlation_id": dashboard_id,
                "context": {},
            },
        )
        # If the audit write itself also fails here, _try_record already
        # logs that separately - the *original* exc is still the more
        # useful signal to hand back to the caller (it names the real
        # root cause of the failed build; a failed audit write on top of
        # that is a second, already-logged problem), so it is never
        # replaced or masked by an audit-store exception.
        self._try_record(
            dashboard_id=dashboard_id,
            metric_id=_RUN_LEVEL_METRIC_ID,
            source_agent="dashboard_evaluator",
            outcome="error",
            headline=f"{exc.__class__.__name__}: {exc}",
        )
        return DashboardBuildRun(dashboard_id=dashboard_id, crash_error=str(exc))
