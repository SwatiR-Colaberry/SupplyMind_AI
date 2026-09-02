"""Root cause analysis agent (STORY-007 / REQ-013).

Wraps root_cause/ (RootCauseEvaluator, itself wrapping the pure
analyze_root_cause() computation with a durable, timestamped audit trail)
as an Agent so it plugs into the existing Orchestrator (STORY-002)
without any changes to orchestration logic - same shape as
RiskDetectionAgent (STORY-005) and SupplierEvaluationAgent (STORY-013).

Unlike analyze_root_cause() itself, which expects already-computed
DemandAnomaly/SupplierDelayAnomaly/SupplierRiskScore signals, this agent
accepts the same raw rows RiskDetectionAgent and SupplierEvaluationAgent
already do and derives those signals itself via the exact same STORY-005/
013 pure functions - a caller investigating one issue doesn't need to
have already run those agents first. Each derivation is independently
best-effort: a problem with demand history doesn't prevent correlating
against delivery data, and vice versa - same degrade-not-fail posture
RiskDetectionAgent already applies to its own three signals.

Like SupplierEvaluationAgent, this agent is stateful: it owns a
RootCauseAuditStore so every analysis run through the Orchestrator gets
the same audited trust-spine guarantee run_sample_root_cause_analysis.py's
direct RootCauseEvaluator calls get - STORY-007's own AC3 applies to
every entry point, not just a standalone demo script.

Query contract (via AgentQuery.context):
    "subject": str - required. The issue's subject (e.g. "SKU-123",
        "PO-1003", "2025-07").
    "subject_kind": "sku" | "po" | "period" | "supplier" - required.
    "as_of_period": str, optional - the period the issue was observed in,
        used to correlate against a demand spike in that same period.
    "supplier": str, optional - the supplier tied to this issue, used to
        correlate against that supplier's delivery delays and reliability
        history.
    "demand_history": list[dict], optional - raw rows, same shape
        DemandForecastingAgent/RiskDetectionAgent expect.
    "date_field" / "quantity_field": str, optional - same defaults as
        RiskDetectionAgent.
    "delivery_rows": list[dict], optional - raw rows, same shape
        RiskDetectionAgent/SupplierEvaluationAgent expect (po_id,
        expected_date, actual_date, supplier). Used to derive both
        supplier-delay anomalies and supplier reliability scores.
    "delay_threshold_days": float, optional - passed through to both
        detect_supplier_delays and evaluate_supplier_reliability.
    "analysis_id": str, optional - passed through to
        RootCauseEvaluator.run() for idempotency; defaults to a fresh
        UUID per call if omitted.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import root_cause
from agents.contracts import AgentQuery, AgentResponse
from agents.logging_setup import get_logger
from forecasting.aggregation import AggregationError, aggregate_monthly_demand
from risk_detection.anomaly_detection import (
    DEFAULT_DELAY_THRESHOLD_DAYS,
    AnomalyDetectionError,
    DemandAnomaly,
    SupplierDelayAnomaly,
    SupplierDelayError,
    detect_demand_spikes,
    detect_supplier_delays,
)
from root_cause.analysis import Issue
from root_cause.audit_trail import RootCauseAuditStore
from root_cause.evaluator import RootCauseAnalysisRun, RootCauseEvaluator
from supplier_evaluation.reliability import SupplierEvaluationError, SupplierRiskScore, evaluate_supplier_reliability

logger = get_logger()

DEFAULT_DATE_FIELD = "order_date"
DEFAULT_QUANTITY_FIELD = "quantity"
DEFAULT_AUDIT_LOG_PATH = Path(root_cause.__file__).resolve().parent / "analysis_audit_log.jsonl"

_VALID_SUBJECT_KINDS = {"sku", "po", "period", "supplier"}


def _default_audit_store() -> RootCauseAuditStore:
    path = os.environ.get("SUPPLYMIND_ROOT_CAUSE_AUDIT_LOG_PATH", str(DEFAULT_AUDIT_LOG_PATH))
    return RootCauseAuditStore(path)


class RootCauseAnalysisAgent:
    name = "root_cause_analysis_agent"

    def __init__(self, audit_store: RootCauseAuditStore | None = None) -> None:
        self._evaluator = RootCauseEvaluator(audit_store or _default_audit_store())

    def run(self, query: AgentQuery) -> AgentResponse:
        """Produce a root cause AgentResponse for the issue named in query.context.

        Handles (returns status="error" for, rather than raising - a
        raised exception here would surface in the Orchestrator as
        "agent_communication_failed", the wrong classification for a
        request/data problem the caller can act on): a missing/invalid
        subject or subject_kind ("data processing errors" failure path -
        this is a caller bug, not an analysis outcome); the
        RootCauseEvaluator reporting "insufficient_data" (AC2's "notify
        the user of limitations" path - no anomaly/reliability data could
        be derived from anything supplied); and "crashed" (the "analysis
        failure" / "analysis API failure" failure path - the evaluator
        already converts any unexpected exception into a reported
        outcome rather than raising).

        Any other, truly unexpected exception (a bug in this method
        itself) is left to propagate - the Orchestrator already has a
        dedicated, tested path for an agent raising
        (agent_communication_failed, isolated per-agent), so this agent
        does not duplicate that handling.
        """
        context = query.context
        subject = context.get("subject")
        subject_kind = context.get("subject_kind")
        if not subject or subject_kind not in _VALID_SUBJECT_KINDS:
            return self._error_response(
                f"subject and a valid subject_kind (one of {sorted(_VALID_SUBJECT_KINDS)}) are required",
                error_class="RootCauseAnalysisRequestError",
            )

        issue = Issue(
            subject=subject,
            subject_kind=subject_kind,
            as_of_period=context.get("as_of_period"),
            supplier=context.get("supplier"),
        )

        demand_anomalies, demand_notes = self._detect_demand_anomalies(context)
        supplier_delays, delay_notes = self._detect_supplier_delays(context)
        supplier_scores, score_notes = self._evaluate_supplier_scores(context)

        run = self._evaluator.run(
            issue,
            analysis_id=context.get("analysis_id"),
            demand_anomalies=demand_anomalies,
            supplier_delays=supplier_delays,
            supplier_scores=supplier_scores,
        )

        if run.outcome == "insufficient_data":
            return self._error_response(
                run.limitation or "insufficient data to analyze this issue",
                error_class="RootCauseAnalysisInsufficientDataError",
            )
        if run.outcome == "crashed":
            return self._error_response(
                run.crash_error or "root cause analysis failed", error_class="RootCauseAnalysisError"
            )

        notes = demand_notes + delay_notes + score_notes
        logger.info(
            "root_cause_analysis_agent_completed",
            extra={
                "event": "root_cause_analysis_agent_completed",
                "outcome": "success",
                "correlation_id": run.analysis_id,
                "context": {
                    "subject": issue.subject,
                    "confidence": run.analysis.confidence,
                    "candidate_count": len(run.analysis.candidates),
                },
            },
        )
        return AgentResponse(
            agent_name=self.name,
            status="ok",
            recommendation=self._format_recommendation(run, notes),
            confidence=run.analysis.confidence,
        )

    def _detect_demand_anomalies(self, context: dict[str, Any]) -> tuple[list[DemandAnomaly], list[str]]:
        raw_rows: list[dict[str, Any]] = context.get("demand_history") or []
        if not raw_rows:
            return [], []

        date_field = context.get("date_field", DEFAULT_DATE_FIELD)
        quantity_field = context.get("quantity_field", DEFAULT_QUANTITY_FIELD)
        try:
            history = aggregate_monthly_demand(raw_rows, date_field, quantity_field)
            return detect_demand_spikes(history), []
        except (AggregationError, AnomalyDetectionError) as exc:
            logger.warning(
                "root_cause_demand_signal_skipped",
                extra={
                    "event": "root_cause_demand_signal_skipped",
                    "outcome": "failure",
                    "error_class": exc.__class__.__name__,
                    "context": {"detail": str(exc)},
                },
            )
            return [], [f"demand signal skipped: {exc}"]

    def _detect_supplier_delays(self, context: dict[str, Any]) -> tuple[list[SupplierDelayAnomaly], list[str]]:
        raw_rows: list[dict[str, Any]] = context.get("delivery_rows") or []
        if not raw_rows:
            return [], []

        threshold = context.get("delay_threshold_days", DEFAULT_DELAY_THRESHOLD_DAYS)
        try:
            report = detect_supplier_delays(raw_rows, delay_threshold_days=threshold)
        except SupplierDelayError as exc:
            logger.warning(
                "root_cause_delivery_signal_skipped",
                extra={
                    "event": "root_cause_delivery_signal_skipped",
                    "outcome": "failure",
                    "error_class": exc.__class__.__name__,
                    "context": {"detail": str(exc)},
                },
            )
            return [], [f"supplier delay signal skipped: {exc}"]
        return report.delays, []

    def _evaluate_supplier_scores(self, context: dict[str, Any]) -> tuple[list[SupplierRiskScore], list[str]]:
        raw_rows: list[dict[str, Any]] = context.get("delivery_rows") or []
        if not raw_rows:
            return [], []

        threshold = context.get("delay_threshold_days", DEFAULT_DELAY_THRESHOLD_DAYS)
        try:
            report = evaluate_supplier_reliability(raw_rows, delay_threshold_days=threshold)
        except SupplierEvaluationError as exc:
            logger.warning(
                "root_cause_reliability_signal_skipped",
                extra={
                    "event": "root_cause_reliability_signal_skipped",
                    "outcome": "failure",
                    "error_class": exc.__class__.__name__,
                    "context": {"detail": str(exc)},
                },
            )
            return [], [f"supplier reliability signal skipped: {exc}"]
        return report.scores, []

    def _error_response(self, message: str, error_class: str) -> AgentResponse:
        logger.warning(
            "root_cause_analysis_agent_failed",
            extra={
                "event": "root_cause_analysis_agent_failed",
                "outcome": "failure",
                "error_class": error_class,
                "context": {"detail": message},
            },
        )
        return AgentResponse(agent_name=self.name, status="error", error=message)

    @staticmethod
    def _format_recommendation(run: RootCauseAnalysisRun, notes: list[str]) -> str:
        analysis = run.analysis
        assert analysis is not None  # only called when run.outcome == "success"
        if not analysis.candidates:
            summary = f"Root cause analysis of {analysis.issue.subject_kind} {analysis.issue.subject}: {analysis.note}"
        else:
            top = analysis.candidates[0]
            summary = (
                f"Root cause analysis of {analysis.issue.subject_kind} {analysis.issue.subject}: "
                f"most likely cause is {top.cause} ({top.detail}), confidence {top.confidence:.2f}"
            )
            if len(analysis.candidates) > 1:
                others = ", ".join(f"{c.cause} ({c.confidence:.2f})" for c in analysis.candidates[1:])
                summary += f" | other candidate(s): {others}"
        if notes:
            summary += " | Data quality notes: " + "; ".join(notes)
        return summary
