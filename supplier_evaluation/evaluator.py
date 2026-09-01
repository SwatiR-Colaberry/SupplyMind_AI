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
a separate field). If evaluate_supplier_reliability() itself cannot run
at all (a bad parameter, or an unexpected crash), a single run-level
audit record is still written under a reserved supplier key - "Audit
trail not recorded for evaluations" must not happen even when the
evaluation process itself fails, mirroring intelligence/model.py's own
"audit trail missing for model stages must not happen even when the
pipeline itself has a bug" rule.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from supplier_evaluation.audit_trail import SupplierEvaluationAuditStore
from supplier_evaluation.logging_setup import get_logger
from supplier_evaluation.reliability import (
    DEFAULT_DELAY_THRESHOLD_DAYS,
    MIN_DELIVERIES_FOR_CONFIDENT_SCORE,
    SupplierEvaluationError,
    SupplierRiskScore,
    evaluate_supplier_reliability,
)

logger = get_logger()

EvaluationRunOutcome = Literal["success", "crashed"]

# Reserved audit-trail supplier key for a run-level failure (the
# evaluation process itself never produced any per-supplier scores) -
# distinguishes "the evaluation didn't run at all" from an ordinary
# supplier named e.g. "unknown". No real supplier value can collide with
# it: reliability.py's _supplier_key() only ever returns a stripped,
# non-empty string pulled from row data, never this literal.
RUN_LEVEL_AUDIT_SUPPLIER = "__evaluation_run__"


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
        except SupplierEvaluationError as exc:
            return self._fail_run(evaluation_id, exc)
        except Exception as exc:  # noqa: BLE001 - deliberate: see module docstring
            # An unexpected bug in the evaluation path must not leave the
            # run with no audit trail at all - caught here, at the one
            # orchestration boundary, the same way
            # intelligence/model.py.IntelligenceModel.run() catches
            # unexpected exceptions at its pipeline boundary. error_class
            # carries the real exception type, not a generic label, so
            # this still satisfies the Observability Framework's ban on
            # logging a bare "Error" classification.
            return self._fail_run(evaluation_id, exc)

        for score in report.scores:
            self._audit_store.record(
                evaluation_id=evaluation_id,
                supplier=score.supplier,
                outcome="success",
                score=score.score,
                severity=score.severity,
                flagged_for_review=score.flagged_for_review,
                detail=score.explanation,
            )
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
        self._audit_store.record(
            evaluation_id=evaluation_id,
            supplier=RUN_LEVEL_AUDIT_SUPPLIER,
            outcome="failure",
            detail=f"{exc.__class__.__name__}: {exc}",
        )
        return SupplierEvaluationRun(evaluation_id=evaluation_id, crash_error=str(exc))
