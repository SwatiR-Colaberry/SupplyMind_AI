"""Shipment delay analysis agent (STORY-014 / REQ-008).

Wraps shipment_delay_analysis/ (ShipmentDelayEvaluator, itself wrapping
the pure analyze_shipment_delays() computation with a durable audit
trail) as an Agent so it plugs into the existing Orchestrator (STORY-002)
without any changes to orchestration logic - same shape as
SupplierEvaluationAgent (STORY-013). Turns raw delivery rows into
per-PO delay-cost findings STORY-006's RecommendationAgent can fold into
a combined recommendation alongside RiskDetectionAgent's own per-PO delay
findings and SupplierEvaluationAgent's per-supplier reliability findings,
or an "error" AgentResponse when no usable delivery data is available.

Like SupplierEvaluationAgent, this agent is stateful: it owns a
ShipmentDelayAuditStore so every analysis run through the Orchestrator
gets the same audited trust-spine guarantee
run_sample_shipment_delay_analysis.py's direct ShipmentDelayEvaluator
calls already get - STORY-014's own AC3 ("audit trail of delay analysis")
applies to every entry point, not just that one demo script. Defaults to
the same audit log path/env var pattern every other agent uses, so an
Orchestrator-driven analysis and a standalone one share one trail unless
the caller injects a different store.

Unlike SupplierEvaluationAgent (where zero scores means nothing could be
evaluated at all), zero delayed POs here is a legitimate, successful
outcome - "every delivery was on time" - not a data problem, so it is
reported as status="ok" with an empty findings list rather than an error.

Query contract (via AgentQuery.context):
    "delivery_rows": list[dict] - raw rows, one per delivery. Same shape
        RiskDetectionAgent's/SupplierEvaluationAgent's own "delivery_rows"
        key expects: po_id, expected_date, actual_date, supplier - see
        risk_detection/anomaly_detection.py's REQUIRED_DELIVERY_FIELDS.
        May optionally carry a "transportation_cost" field per row - see
        shipment_delay_analysis/delay_analysis.py's DEFAULT_COST_PER_DAY_LATE
        docstring for how it's used. Required.
    "delay_threshold_days": float, optional - passed through to
        analyze_shipment_delays(). Defaults to
        delay_analysis.DEFAULT_DELAY_THRESHOLD_DAYS.
    "cost_per_day_late": float, optional - passed through to
        analyze_shipment_delays(). Defaults to
        delay_analysis.DEFAULT_COST_PER_DAY_LATE.
    "analysis_id": str, optional - passed through to
        ShipmentDelayEvaluator.run() for idempotency; defaults to a fresh
        UUID per call if omitted.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import shipment_delay_analysis
from agents.contracts import AgentFinding, AgentQuery, AgentResponse
from agents.logging_setup import get_logger
from shipment_delay_analysis.audit_trail import ShipmentDelayAuditStore
from shipment_delay_analysis.delay_analysis import DEFAULT_COST_PER_DAY_LATE, DEFAULT_DELAY_THRESHOLD_DAYS
from shipment_delay_analysis.evaluator import ShipmentDelayAnalysisRun, ShipmentDelayEvaluator

logger = get_logger()

DEFAULT_AUDIT_LOG_PATH = Path(shipment_delay_analysis.__file__).resolve().parent / "delay_analysis_audit_log.jsonl"


def _default_audit_store() -> ShipmentDelayAuditStore:
    path = os.environ.get("SUPPLYMIND_SHIPMENT_DELAY_AUDIT_LOG_PATH", str(DEFAULT_AUDIT_LOG_PATH))
    return ShipmentDelayAuditStore(path)


def _confidence(run: ShipmentDelayAnalysisRun) -> float:
    """Degrade confidence per data-quality concern, same shape as SupplierEvaluationAgent._confidence().

    run.warnings already folds in both detect_supplier_delays' own
    data-quality warnings (invalid rows) and analyze_shipment_delays'
    cost-specific warning (an unusable transportation_cost) - one note
    per distinct concern, not per row, matching the granularity every
    other agent's confidence degradation already uses.
    """
    if not run.warnings:
        return 1.0
    return max(0.5, 1.0 - 0.1 * len(run.warnings))


class ShipmentDelayAnalysisAgent:
    name = "shipment_delay_analysis_agent"

    def __init__(self, audit_store: ShipmentDelayAuditStore | None = None) -> None:
        self._evaluator = ShipmentDelayEvaluator(audit_store or _default_audit_store())

    def run(self, query: AgentQuery) -> AgentResponse:
        """Produce a per-PO delay-cost AgentResponse from query.context["delivery_rows"].

        Handles (returns status="error" for, rather than raising - a
        raised exception here would surface in the Orchestrator as
        "agent_communication_failed", the wrong classification for a
        data problem the caller can act on): missing/empty delivery_rows.
        ShipmentDelayEvaluator.run() itself never raises - it already
        converts both a bad parameter and a genuinely unexpected crash
        into a ShipmentDelayAnalysisRun(crash_error=...), which is
        surfaced here the same way ("incorrect delay analysis" / "cost
        calculation errors" failure paths).

        A run that completes with zero delayed POs is reported as
        status="ok" (every delivery was on time is a real finding, not a
        failure) - unlike SupplierEvaluationAgent, where zero scores
        means the evaluation itself couldn't run for anyone.

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
                "no delivery data provided for shipment delay analysis", error_class="ShipmentDelayAnalysisDataError"
            )

        run = self._evaluator.run(
            raw_rows,
            analysis_id=context.get("analysis_id"),
            delay_threshold_days=context.get("delay_threshold_days", DEFAULT_DELAY_THRESHOLD_DAYS),
            cost_per_day_late=context.get("cost_per_day_late", DEFAULT_COST_PER_DAY_LATE),
        )

        if run.outcome == "crashed":
            return self._error_response(
                run.crash_error or "shipment delay analysis failed", error_class="ShipmentDelayAnalysisError"
            )

        logger.info(
            "shipment_delay_analysis_agent_completed",
            extra={
                "event": "shipment_delay_analysis_agent_completed",
                "outcome": "success",
                "correlation_id": run.analysis_id,
                "context": {"delays_analyzed": len(run.delay_costs), "total_cost": run.total_cost},
            },
        )

        findings = [
            AgentFinding(subject=c.po_id, subject_kind="po", severity=c.severity, detail=c.detail)
            for c in run.delay_costs
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
            "shipment_delay_analysis_agent_failed",
            extra={
                "event": "shipment_delay_analysis_agent_failed",
                "outcome": "failure",
                "error_class": error_class,
                "context": {"detail": message},
            },
        )
        return AgentResponse(agent_name=self.name, status="error", error=message)

    @staticmethod
    def _format_recommendation(run: ShipmentDelayAnalysisRun) -> str:
        if not run.delay_costs:
            summary = "Shipment delay analysis: no delays found"
        else:
            # run.delay_costs is already sorted worst-cost-first by analyze_shipment_delays()
            summary = (
                f"Shipment delay analysis ({len(run.delay_costs)} delayed PO(s), "
                f"${run.total_cost:,.2f} total cost): "
                + " | ".join(f"{c.po_id} ({c.detail})" for c in run.delay_costs)
            )
            if run.patterns:
                worst = run.patterns[0]
                summary += (
                    f" | worst pattern: {worst.supplier} ({worst.delay_count} delay(s), "
                    f"${worst.total_cost:,.2f})"
                )
        if run.warnings:
            summary += " | Data quality notes: " + "; ".join(run.warnings)
        return summary
