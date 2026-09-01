"""Orchestrates supplier reliability evaluation with a durable audit trail (STORY-013 / REQ-007).

Ties evaluate_supplier_reliability() (reliability.py, pure computation) to
SupplierEvaluationAuditStore (audit_trail.py, persistence) the same way
intelligence/model.py ties its stage functions to a StageAuditStore: the
computation stays I/O-free and independently testable, while this module
is the one place that decides what gets logged and durably recorded, and
when.

Every supplier the evaluation produces a score for gets its own audit
record, regardless of whether that supplier was flagged - "outcome" on an
audit record means "did the evaluation process succeed for this
supplier," not "is this supplier reliable" (that's flagged_for_review,
a separate field). A run that produces zero per-supplier scores (no
delivery data, or every row unattributable) still gets one run-level
audit record instead of none, and if evaluate_supplier_reliability()
itself cannot run at all (a bad parameter, or an unexpected crash), a
run-level failure record is written the same way - "Audit trail not
recorded for evaluations" must not happen for a completed run of any
outcome, mirroring intelligence/model.py's own "audit trail missing for
model stages must not happen even when the pipeline itself has a bug"
rule. Run-level records use `supplier=None` (see audit_trail.py), not a
sentinel string, so a real supplier name can never collide with one.

If the audit store itself can't be written to (disk full, permissions),
that failure is never silently swallowed, but it also never masks the
more useful signal when the evaluation itself failed first - see
_fail_run()'s handling.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from supplier_evaluation.audit_trail import SupplierEvaluationAuditStore, SupplierEvaluationAuditWriteError
from supplier_evaluation.logging_setup import get_logger
from supplier_evaluation.reliability import (
    DEFAULT_DELAY_THRESHOLD_DAYS,
    MIN_DELIVERIES_FOR_CONFIDENT_SCORE,
    SupplierRiskScore,
    evaluate_supplier_reliability,
)

logger = get_logger()

EvaluationRunOutcome = Literal["success", "crashed"]


@dataclass
class SupplierEvaluationRun:
    """The full outcome of one SupplierEvaluator.run() call."""

    evaluation_id: str
    scores: list[SupplierRiskScore] = field(default_factory=list)
    unattributable_rows: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    crash_error: str | None = None

    @property
    def outcome(self) -> EvaluationRunOutcome:
        return "crashed" if self.crash_error is not None else "success"

    @property
    def flagged_suppliers(self) -> list[SupplierRiskScore]:
        return [s for s in self.scores if s.flagged_for_review]


class SupplierEvaluator:
    """Runs evaluate_supplier_reliability() and audits every result, success or failure."""

    def __init__(self, audit_store: SupplierEvaluationAuditStore) -> None:
        self._audit_store = audit_store

    def run(
        self,
        rows: list[dict[str, Any]],
        *,
        evaluation_id: str | None = None,
        delay_threshold_days: float = DEFAULT_DELAY_THRESHOLD_DAYS,
        min_deliveries_for_confidence: int = MIN_DELIVERIES_FOR_CONFIDENT_SCORE,
    ) -> SupplierEvaluationRun:
        evaluation_id = evaluation_id or str(uuid.uuid4())
        logger.info(
            "evaluation_started",
            extra={
                "event": "evaluation_started",
                "correlation_id": evaluation_id,
                "context": {"row_count": len(rows)},
            },
        )

        try:
            report = evaluate_supplier_reliability(
                rows,
                delay_threshold_days=delay_threshold_days,
                min_deliveries_for_confidence=min_deliveries_for_confidence,
            )
        except Exception as exc:  # noqa: BLE001 - deliberate: see module docstring
            # Covers both the documented SupplierEvaluationError (a bad
            # parameter) and any genuinely unexpected bug in the
            # evaluation path - both must not leave the run with no audit
            # trail at all, caught here at the one orchestration boundary,
            # the same way intelligence/model.py.IntelligenceModel.run()
            # catches unexpected exceptions at its pipeline boundary.
            # error_class (set in _fail_run, from the real exception type)
            # means this still satisfies the Observability Framework's ban
            # on logging a bare "Error" classification.
            return self._fail_run(evaluation_id, exc)

        for score in report.scores:
            write_error = self._try_record(
                evaluation_id=evaluation_id,
                supplier=score.supplier,
                outcome="success",
                score=score.score,
                severity=score.severity,
                flagged_for_review=score.flagged_for_review,
                detail=score.explanation,
            )
            if write_error is not None:
                # The audit store itself is broken - retrying immediately
                # would just raise again (infinite retry loops are
                # prohibited), so the run is reported crashed rather than
                # left with a misleadingly "successful" outcome and a
                # silently incomplete audit trail for the remaining
                # suppliers.
                return SupplierEvaluationRun(evaluation_id=evaluation_id, crash_error=str(write_error))
            logger.info(
                "supplier_evaluated",
                extra={
                    "event": "supplier_evaluated",
                    "outcome": "success",
                    "correlation_id": evaluation_id,
                    "context": {
                        "supplier": score.supplier,
                        "score": score.score,
                        "severity": score.severity,
                        "flagged_for_review": score.flagged_for_review,
                    },
                },
            )

        if not report.scores:
            # No supplier was scoreable (no delivery data, or every row
            # was unattributable) - the evaluation process still ran and
            # completed, so it still needs an audit trace. Without this, a
            # completed run with zero suppliers is indistinguishable from
            # one that never ran at all.
            reason = "; ".join(report.warnings) if report.warnings else "no supplier could be evaluated"
            write_error = self._try_record(
                evaluation_id=evaluation_id, supplier=None, outcome="success", detail=reason
            )
            if write_error is not None:
                return SupplierEvaluationRun(evaluation_id=evaluation_id, crash_error=str(write_error))

        run = SupplierEvaluationRun(
            evaluation_id=evaluation_id,
            scores=report.scores,
            unattributable_rows=report.unattributable_rows,
            warnings=report.warnings,
        )
        logger.info(
            "evaluation_completed",
            extra={
                "event": "evaluation_completed",
                "outcome": "success",
                "correlation_id": evaluation_id,
                "context": {
                    "suppliers_evaluated": len(run.scores),
                    "suppliers_flagged": len(run.flagged_suppliers),
                },
            },
        )
        return run

    def _try_record(self, **kwargs: Any) -> SupplierEvaluationAuditWriteError | None:
        try:
            self._audit_store.record(**kwargs)
            return None
        except SupplierEvaluationAuditWriteError as exc:
            logger.error(
                "evaluation_audit_write_failed",
                extra={
                    "event": "evaluation_audit_write_failed",
                    "outcome": "failure",
                    "error_class": exc.__class__.__name__,
                    "correlation_id": kwargs.get("evaluation_id"),
                    "context": {"supplier": kwargs.get("supplier")},
                },
            )
            return exc

    def _fail_run(self, evaluation_id: str, exc: Exception) -> SupplierEvaluationRun:
        logger.error(
            "evaluation_failed",
            extra={
                "event": "evaluation_failed",
                "outcome": "failure",
                "error_class": exc.__class__.__name__,
                "correlation_id": evaluation_id,
                "context": {},
            },
        )
        # If the audit write itself also fails here, _try_record already
        # logs that separately - the *original* exc is still the more
        # useful signal to hand back to the caller (it names the real
        # root cause of the failed evaluation; a failed audit write on top
        # of that is a second, already-logged problem), so it is never
        # replaced or masked by an audit-store exception.
        self._try_record(
            evaluation_id=evaluation_id,
            supplier=None,
            outcome="failure",
            detail=f"{exc.__class__.__name__}: {exc}",
        )
        return SupplierEvaluationRun(evaluation_id=evaluation_id, crash_error=str(exc))
