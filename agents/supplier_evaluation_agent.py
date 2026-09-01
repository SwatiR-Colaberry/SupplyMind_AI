"""Supplier reliability evaluation agent (STORY-013 / REQ-007).

Wraps supplier_evaluation/ (SupplierEvaluator, itself wrapping the pure
evaluate_supplier_reliability() computation with a durable audit trail)
as an Agent so it plugs into the existing Orchestrator (STORY-002)
without any changes to orchestration logic - same shape as
StockoutRiskAgent (STORY-004) and RiskDetectionAgent (STORY-005). Turns
raw delivery rows into per-supplier Supplier Risk Score findings
STORY-006's RecommendationAgent can fold into a combined recommendation
alongside RiskDetectionAgent's own per-PO delay findings, or an "error"
AgentResponse when no usable delivery data is available.

Unlike StockoutRiskAgent/RiskDetectionAgent (which wrap pure, I/O-free
modules with no audit trail of their own), this agent is stateful: it
owns a SupplierEvaluationAuditStore so every evaluation run through the
Orchestrator gets the same audited trust-spine guarantee
run_sample_supplier_evaluation.py's direct SupplierEvaluator calls
already get - STORY-013's own AC3 ("audit trail of the evaluation
process") applies to every entry point, not just that one demo script.
Defaults to the same audit log path/env var
run_sample_supplier_evaluation.py uses, so an Orchestrator-driven
evaluation and a standalone one share one trail unless the caller
injects a different store.

Query contract (via AgentQuery.context):
    "delivery_rows": list[dict] - raw rows, one per delivery. Same shape
        RiskDetectionAgent's own "delivery_rows" key expects: po_id,
        expected_date, actual_date, supplier - see
        risk_detection/anomaly_detection.py's REQUIRED_DELIVERY_FIELDS.
        Unlike there, a usable `supplier` value is effectively required
        here since it's this module's grouping key - see
        supplier_evaluation/reliability.py's _supplier_key(). Required.
    "delay_threshold_days": float, optional - passed through to
        evaluate_supplier_reliability(). Defaults to
        reliability.DEFAULT_DELAY_THRESHOLD_DAYS.
    "evaluation_id": str, optional - passed through to
        SupplierEvaluator.run() for idempotency; defaults to a fresh UUID
        per call if omitted.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import supplier_evaluation
from agents.contracts import AgentFinding, AgentQuery, AgentResponse
from agents.logging_setup import get_logger
from supplier_evaluation.audit_trail import SupplierEvaluationAuditStore
from supplier_evaluation.evaluator import SupplierEvaluationRun, SupplierEvaluator
from supplier_evaluation.reliability import DEFAULT_DELAY_THRESHOLD_DAYS

logger = get_logger()

DEFAULT_AUDIT_LOG_PATH = Path(supplier_evaluation.__file__).resolve().parent / "evaluation_audit_log.jsonl"


def _default_audit_store() -> SupplierEvaluationAuditStore:
    path = os.environ.get("SUPPLYMIND_SUPPLIER_EVALUATION_AUDIT_LOG_PATH", str(DEFAULT_AUDIT_LOG_PATH))
    return SupplierEvaluationAuditStore(path)


def _confidence(run: SupplierEvaluationRun) -> float:
    """Degrade confidence per data-quality concern, same shape as RiskDetectionAgent._confidence().

    A concern is either a run-level warning (e.g. unattributable rows) or
    a supplier flagged specifically because its own data was too thin or
    too corrupted to score confidently - not a supplier flagged only for
    being genuinely risky, since a confidently-detected risky supplier
    isn't a reason to distrust the *evaluation*, only the *supplier*.
    """
    notes = list(run.warnings)
    for score in run.scores:
        if any(
            "valid delivery record" in reason or "cannot verify reliability" in reason or "invalid data" in reason
            for reason in score.flag_reasons
        ):
            notes.append(f"{score.supplier}: data-quality concern")
    if not notes:
        return 1.0
    return max(0.5, 1.0 - 0.1 * len(notes))


class SupplierEvaluationAgent:
    name = "supplier_evaluation_agent"

    def __init__(self, audit_store: SupplierEvaluationAuditStore | None = None) -> None:
        self._evaluator = SupplierEvaluator(audit_store or _default_audit_store())

    def run(self, query: AgentQuery) -> AgentResponse:
        """Produce a per-supplier reliability AgentResponse from query.context["delivery_rows"].

        Handles (returns status="error" for, rather than raising - a
        raised exception here would surface in the Orchestrator as
        "agent_communication_failed", the wrong classification for a
        data problem the caller can act on): missing/empty delivery_rows,
        and a run that completes but scores zero suppliers (e.g. every
        row missing a supplier). SupplierEvaluator.run() itself never
        raises - it already converts both a bad parameter and a genuinely
        unexpected crash into a SupplierEvaluationRun(crash_error=...),
        which is surfaced here the same way.

        Any other, truly unexpected exception (a bug in this method
        itself) is left to propagate - the Orchestrator already has a
        dedicated, tested path for an agent raising
        (agent_communication_failed, isolated per-agent), so this agent
        does not duplicate that handling.
        """
        context = query.context
        raw_rows: list[dict[str, Any]] = context.get("delivery_rows") or []
        if not raw_rows:
            return self._error_response(
                "no delivery data provided for supplier evaluation", error_class="SupplierEvaluationDataError"
            )

        run = self._evaluator.run(
            raw_rows,
            evaluation_id=context.get("evaluation_id"),
            delay_threshold_days=context.get("delay_threshold_days", DEFAULT_DELAY_THRESHOLD_DAYS),
        )

        if run.outcome == "crashed":
            return self._error_response(run.crash_error or "supplier evaluation failed", error_class="SupplierEvaluationError")

        if not run.scores:
            detail = "; ".join(run.warnings) if run.warnings else "no supplier could be evaluated"
            return self._error_response(detail, error_class="SupplierEvaluationDataError")

        logger.info(
            "supplier_evaluation_agent_completed",
            extra={
                "event": "supplier_evaluation_agent_completed",
                "outcome": "success",
                "correlation_id": run.evaluation_id,
                "context": {
                    "suppliers_evaluated": len(run.scores),
                    "suppliers_flagged": len(run.flagged_suppliers),
                },
            },
        )

        findings = [
            AgentFinding(subject=s.supplier, subject_kind="supplier", severity=s.severity, detail=s.explanation)
            for s in run.scores
        ]
        return AgentResponse(
            agent_name=self.name,
            status="ok",
            recommendation=self._format_recommendation(run),
            confidence=_confidence(run),
            findings=findings,
        )

    def _error_response(self, message: str, error_class: str) -> AgentResponse:
        logger.warning(
            "supplier_evaluation_agent_failed",
            extra={
                "event": "supplier_evaluation_agent_failed",
                "outcome": "failure",
                "error_class": error_class,
                "context": {"detail": message},
            },
        )
        return AgentResponse(agent_name=self.name, status="error", error=message)

    @staticmethod
    def _format_recommendation(run: SupplierEvaluationRun) -> str:
        # run.scores is already sorted riskiest-first by evaluate_supplier_reliability()
        summary = f"Supplier reliability evaluation ({len(run.scores)} supplier(s)): " + " | ".join(
            s.explanation for s in run.scores
        )
        if run.unattributable_rows:
            summary += f" | {len(run.unattributable_rows)} delivery row(s) missing a supplier and excluded"
        return summary
