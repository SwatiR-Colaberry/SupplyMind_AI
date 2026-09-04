"""Orchestrates data quality monitoring with a durable audit trail and steward alerting (STORY-015 / REQ-017).

Ties assess_data_quality() (quality_checks.py, pure computation) to
QualityAuditStore (audit_trail.py, persistence) the same way
shipment_delay_analysis/evaluator.py ties analyze_shipment_delays() to
its own audit store: the computation stays I/O-free and independently
testable, while this module is the one place that decides what gets
logged and durably recorded, and when.

Every dimension the assessment scores gets its own audit record,
regardless of how good or bad that dimension's score is - "outcome" on
an audit record means "did the quality check itself run successfully for
this dimension," not "did this dimension pass." If
assess_data_quality() cannot run at all (a bad parameter, or an
unexpected crash), a run-level failure record is written the same way -
"audit trail not recorded for quality checks" must not happen for a
completed run of any outcome.

Alerting the data steward (AC2: "given poor data quality, when detected,
then the system alerts the data steward"): logged assumption, not
escalated - no notification service (email, Slack, etc.) is wired up
for this project yet, the same situation every other *Evaluator in this
repo is in. The alert is surfaced two ways instead: a distinct, loudly
logged `data_quality_alert_raised` event (`outcome: "failure"`,
correlation_id=check_id) any downstream alerting/notification service
can subscribe to, and a run-level audit record (dimension=None)
persisting the alert reasons, so the alert itself leaves a durable trace
tied to the check that raised it, not just a log line.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

from data_quality_monitoring.audit_trail import QualityAuditStore, QualityAuditWriteError
from data_quality_monitoring.logging_setup import get_logger
from data_quality_monitoring.quality_checks import DataQualityReport, assess_data_quality

logger = get_logger()

CheckRunOutcome = Literal["success", "crashed"]


@dataclass
class DataQualityCheckRun:
    """The full outcome of one DataQualityEvaluator.run() call."""

    check_id: str
    report: DataQualityReport | None = None
    crash_error: str | None = None

    @property
    def outcome(self) -> CheckRunOutcome:
        return "crashed" if self.crash_error is not None else "success"

    @property
    def poor_quality(self) -> bool:
        return self.report is not None and self.report.poor_quality


class DataQualityEvaluator:
    """Runs assess_data_quality() and audits every result, success or failure, alerting on poor quality."""

    def __init__(self, audit_store: QualityAuditStore) -> None:
        self._audit_store = audit_store

    def run(
        self,
        rows: list[dict[str, Any]],
        *,
        required_fields: tuple[str, ...],
        check_id: str | None = None,
    ) -> DataQualityCheckRun:
        check_id = check_id or str(uuid.uuid4())
        logger.info(
            "data_quality_check_started",
            extra={
                "event": "data_quality_check_started",
                "correlation_id": check_id,
                "context": {"row_count": len(rows)},
            },
        )

        try:
            report = assess_data_quality(rows, required_fields=required_fields)
        except Exception as exc:  # noqa: BLE001 - deliberate: see module docstring
            # Covers both the documented DataQualityError (a bad
            # parameter) and any genuinely unexpected bug in the
            # assessment path - both must not leave the run with no audit
            # trail at all, caught here at the one orchestration
            # boundary, the same way every other *Evaluator in this repo
            # catches unexpected exceptions at its own orchestration
            # boundary.
            return self._fail_run(check_id, exc)

        for result in report.dimension_results:
            write_error = self._try_record(
                check_id=check_id,
                dimension=result.dimension,
                outcome="success",
                score=result.score,
                checked_rows=result.checked_rows,
                issue_rows=result.issue_rows,
                detail=f"{result.issue_rows}/{result.checked_rows} row(s) failed this check"
                if result.score is not None
                else "no rows available to check this dimension",
            )
            if write_error is not None:
                # The audit store itself is broken - retrying immediately
                # would just raise again (infinite retry loops are
                # prohibited), so the run is reported crashed rather than
                # left with a misleadingly "successful" outcome and a
                # silently incomplete audit trail for the remaining
                # dimensions.
                return DataQualityCheckRun(check_id=check_id, crash_error=str(write_error))
            logger.info(
                "data_quality_dimension_checked",
                extra={
                    "event": "data_quality_dimension_checked",
                    "outcome": "success",
                    "correlation_id": check_id,
                    "context": {
                        "dimension": result.dimension,
                        "score": result.score,
                        "issue_rows": result.issue_rows,
                        "checked_rows": result.checked_rows,
                    },
                },
            )

        if report.poor_quality:
            logger.error(
                "data_quality_alert_raised",
                extra={
                    "event": "data_quality_alert_raised",
                    "outcome": "failure",
                    "correlation_id": check_id,
                    "context": {
                        "overall_score": report.overall_score,
                        "severity": report.severity,
                        "alert_reasons": report.alert_reasons,
                    },
                },
            )
            write_error = self._try_record(
                check_id=check_id,
                dimension=None,
                outcome="success",
                score=report.overall_score,
                checked_rows=report.total_rows,
                detail="ALERT: " + "; ".join(report.alert_reasons),
            )
            if write_error is not None:
                return DataQualityCheckRun(check_id=check_id, crash_error=str(write_error))

        run = DataQualityCheckRun(check_id=check_id, report=report)
        logger.info(
            "data_quality_check_completed",
            extra={
                "event": "data_quality_check_completed",
                "outcome": "success",
                "correlation_id": check_id,
                "context": {
                    "overall_score": report.overall_score,
                    "severity": report.severity,
                    "poor_quality": report.poor_quality,
                },
            },
        )
        return run

    def _try_record(self, **kwargs: Any) -> QualityAuditWriteError | None:
        try:
            self._audit_store.record(**kwargs)
            return None
        except QualityAuditWriteError as exc:
            logger.error(
                "data_quality_audit_write_failed",
                extra={
                    "event": "data_quality_audit_write_failed",
                    "outcome": "failure",
                    "error_class": exc.__class__.__name__,
                    "correlation_id": kwargs.get("check_id"),
                    "context": {"dimension": kwargs.get("dimension")},
                },
            )
            return exc

    def _fail_run(self, check_id: str, exc: Exception) -> DataQualityCheckRun:
        logger.error(
            "data_quality_check_failed",
            extra={
                "event": "data_quality_check_failed",
                "outcome": "failure",
                "error_class": exc.__class__.__name__,
                "correlation_id": check_id,
                "context": {},
            },
        )
        # If the audit write itself also fails here, _try_record already
        # logs that separately - the *original* exc is still the more
        # useful signal to hand back to the caller (it names the real
        # root cause of the failed check; a failed audit write on top of
        # that is a second, already-logged problem), so it is never
        # replaced or masked by an audit-store exception.
        self._try_record(
            check_id=check_id,
            dimension=None,
            outcome="failure",
            detail=f"{exc.__class__.__name__}: {exc}",
        )
        return DataQualityCheckRun(check_id=check_id, crash_error=str(exc))
