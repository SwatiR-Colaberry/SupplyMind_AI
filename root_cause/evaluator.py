"""Orchestrates root cause analysis with a durable audit trail (STORY-007 / REQ-013).

Ties analyze_root_cause() (analysis.py, pure computation) to
RootCauseAuditStore (audit_trail.py, persistence), the same way
supplier_evaluation/evaluator.py (STORY-013) ties evaluate_supplier_reliability()
to SupplierEvaluationAuditStore: the computation stays I/O-free and
independently testable, while this module is the one place that decides
what gets logged and durably recorded, and when.

Every call to run() gets exactly one audit record, whatever the outcome -
"audit trail not recorded for analyses" must never happen, mirroring
every other trust-spine implementation in this repo (STORY-011/012/013).

Three outcomes, not the two-way success/crashed split
supplier_evaluation/evaluator.py uses, because STORY-007 (unlike
STORY-013) has its own acceptance criterion for one of them:
- "success": analyze_root_cause() produced a RootCauseAnalysis (which
  may itself have zero candidates - see analysis.py's docstring on why
  that's a genuine finding, not a limitation).
- "insufficient_data": analyze_root_cause() raised RootCauseAnalysisError
  because no signal data was supplied at all. This is AC2's "notify the
  user of limitations" path - an expected, handled outcome, not a crash.
- "crashed": any other exception - the "analysis API failure" /
  "analysis failure" failure path this module does not attempt to
  interpret, only to audit and report back rather than let propagate
  uncaught.

If the audit store itself can't be written to (disk full, permissions),
that failure is never silently swallowed but also never masks a more
useful analysis-side failure that happened first - see _fail_run()'s
handling, identical in spirit to supplier_evaluation/evaluator.py's own.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

from risk_detection.anomaly_detection import DemandAnomaly, SupplierDelayAnomaly
from root_cause.analysis import Issue, RootCauseAnalysis, RootCauseAnalysisError, analyze_root_cause
from root_cause.audit_trail import RootCauseAuditStore, RootCauseAuditWriteError
from root_cause.logging_setup import get_logger
from supplier_evaluation.reliability import SupplierRiskScore

logger = get_logger()

AnalysisRunOutcome = Literal["success", "insufficient_data", "crashed"]


@dataclass
class RootCauseAnalysisRun:
    """The full outcome of one RootCauseEvaluator.run() call."""

    analysis_id: str
    issue: Issue
    analysis: RootCauseAnalysis | None = None
    limitation: str | None = None  # set only when outcome == "insufficient_data"
    crash_error: str | None = None

    @property
    def outcome(self) -> AnalysisRunOutcome:
        if self.crash_error is not None:
            return "crashed"
        if self.limitation is not None:
            return "insufficient_data"
        return "success"


class RootCauseEvaluator:
    """Runs analyze_root_cause() for one issue and audits the result, whatever it is."""

    def __init__(self, audit_store: RootCauseAuditStore) -> None:
        self._audit_store = audit_store

    def run(
        self,
        issue: Issue,
        *,
        analysis_id: str | None = None,
        demand_anomalies: list[DemandAnomaly] | None = None,
        supplier_delays: list[SupplierDelayAnomaly] | None = None,
        supplier_scores: list[SupplierRiskScore] | None = None,
    ) -> RootCauseAnalysisRun:
        analysis_id = analysis_id or str(uuid.uuid4())
        logger.info(
            "root_cause_analysis_started",
            extra={
                "event": "root_cause_analysis_started",
                "correlation_id": analysis_id,
                "context": {"subject": issue.subject, "subject_kind": issue.subject_kind},
            },
        )

        try:
            analysis = analyze_root_cause(
                issue,
                demand_anomalies=demand_anomalies,
                supplier_delays=supplier_delays,
                supplier_scores=supplier_scores,
            )
        except RootCauseAnalysisError as exc:
            return self._insufficient_data(analysis_id, issue, exc)
        except Exception as exc:  # noqa: BLE001 - deliberate: see module docstring
            return self._fail_run(analysis_id, issue, exc)

        write_error = self._try_record(
            analysis_id=analysis_id,
            subject=issue.subject,
            subject_kind=issue.subject_kind,
            outcome="success",
            confidence=analysis.confidence,
            candidate_count=len(analysis.candidates),
            detail=analysis.note,
        )
        if write_error is not None:
            return RootCauseAnalysisRun(analysis_id=analysis_id, issue=issue, crash_error=str(write_error))

        logger.info(
            "root_cause_analysis_completed",
            extra={
                "event": "root_cause_analysis_completed",
                "outcome": "success",
                "correlation_id": analysis_id,
                "context": {
                    "subject": issue.subject,
                    "confidence": analysis.confidence,
                    "candidate_count": len(analysis.candidates),
                },
            },
        )
        return RootCauseAnalysisRun(analysis_id=analysis_id, issue=issue, analysis=analysis)

    def _insufficient_data(
        self, analysis_id: str, issue: Issue, exc: RootCauseAnalysisError
    ) -> RootCauseAnalysisRun:
        limitation = str(exc)
        logger.warning(
            "root_cause_analysis_insufficient_data",
            extra={
                "event": "root_cause_analysis_insufficient_data",
                "outcome": "failure",
                "error_class": exc.__class__.__name__,
                "correlation_id": analysis_id,
                "context": {"subject": issue.subject, "subject_kind": issue.subject_kind},
            },
        )
        write_error = self._try_record(
            analysis_id=analysis_id,
            subject=issue.subject,
            subject_kind=issue.subject_kind,
            outcome="failure",
            detail=limitation,
        )
        if write_error is not None:
            return RootCauseAnalysisRun(analysis_id=analysis_id, issue=issue, crash_error=str(write_error))
        return RootCauseAnalysisRun(analysis_id=analysis_id, issue=issue, limitation=limitation)

    def _try_record(self, **kwargs: Any) -> RootCauseAuditWriteError | None:
        try:
            self._audit_store.record(**kwargs)
            return None
        except RootCauseAuditWriteError as exc:
            logger.error(
                "root_cause_audit_write_failed",
                extra={
                    "event": "root_cause_audit_write_failed",
                    "outcome": "failure",
                    "error_class": exc.__class__.__name__,
                    "correlation_id": kwargs.get("analysis_id"),
                    "context": {"subject": kwargs.get("subject")},
                },
            )
            return exc

    def _fail_run(self, analysis_id: str, issue: Issue, exc: Exception) -> RootCauseAnalysisRun:
        logger.error(
            "root_cause_analysis_failed",
            extra={
                "event": "root_cause_analysis_failed",
                "outcome": "failure",
                "error_class": exc.__class__.__name__,
                "correlation_id": analysis_id,
                "context": {"subject": issue.subject},
            },
        )
        # If the audit write itself also fails here, _try_record already
        # logs that separately - the *original* exc is still the more
        # useful signal to hand back to the caller, so it is never
        # replaced or masked by a secondary audit-store exception. Same
        # reasoning as supplier_evaluation/evaluator.py's _fail_run().
        self._try_record(
            analysis_id=analysis_id,
            subject=issue.subject,
            subject_kind=issue.subject_kind,
            outcome="failure",
            detail=f"{exc.__class__.__name__}: {exc}",
        )
        return RootCauseAnalysisRun(analysis_id=analysis_id, issue=issue, crash_error=str(exc))
