"""Data quality monitoring agent (STORY-015 / REQ-017).

Wraps data_quality_monitoring/ (DataQualityEvaluator, itself wrapping the
pure assess_data_quality() computation with a durable audit trail) as an
Agent so it plugs into the existing Orchestrator (STORY-002) without any
changes to orchestration logic - same shape as SupplierEvaluationAgent
(STORY-013) and ShipmentDelayAnalysisAgent (STORY-014). Turns raw
delivery rows into a Data Quality Score AgentResponse
STORY-006's RecommendationAgent can fold into a combined recommendation
alongside the other stage-1 agents' findings - this is what gets the
"alerts the data steward" AC (already satisfied at the logging/audit-trail
layer by data_quality_monitoring/evaluator.py) into the same
recommendation surface every other analysis story's alert-shaped signal
reaches, rather than leaving it log/audit-trail-only.

Like SupplierEvaluationAgent/ShipmentDelayAnalysisAgent, this agent is
stateful: it owns a QualityAuditStore so every check run through the
Orchestrator gets the same audited trust-spine guarantee
run_sample_data_quality_monitoring.py's direct DataQualityEvaluator calls
already get - STORY-015's own Trust AC applies to every entry point, not
just that one demo script.

No findings (AgentResponse.findings stays empty, the same shape
DemandForecastingAgent uses): FindingSubjectKind is a closed
Literal["sku", "po", "period", "supplier"] in agents/contracts.py, and a
completeness check is a whole-batch finding, not a per-subject one - it
doesn't naturally fit any existing kind, and extending that Literal would
be a breaking contract change to a file well outside this story. The
score, severity, and alert are carried entirely through `recommendation`
and `confidence` instead.

Unlike SupplierEvaluationAgent/ShipmentDelayAnalysisAgent (where empty
delivery_rows is reported as status="error" - there is nothing to
evaluate), empty delivery_rows here is deliberately let through to
DataQualityEvaluator.run() and reported as status="ok": "zero rows
available" is itself the most severe data-quality finding this agent
exists to surface (assess_data_quality() already turns it into a
poor_quality alert rather than raising - see quality_checks.py's
docstring), not an agent malfunction.

Query contract (via AgentQuery.context):
    "delivery_rows": list[dict] - raw rows, one per delivery. Same shape
        RiskDetectionAgent's/SupplierEvaluationAgent's/
        ShipmentDelayAnalysisAgent's own "delivery_rows" key expects -
        reused rather than a new dataset key, since data quality
        monitoring applies to data this repo already ingests. Defaults to
        an empty list if omitted (surfaced as the "zero rows" alert
        above, not an error).
    "required_fields": tuple[str, ...], optional - passed through to
        assess_data_quality(). Defaults to
        risk_detection.anomaly_detection.REQUIRED_DELIVERY_FIELDS, the
        same required-field set every other delivery-row consumer in
        this repo already validates against.
    "check_id": str, optional - passed through to
        DataQualityEvaluator.run() for idempotency; defaults to a fresh
        UUID per call if omitted.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import data_quality_monitoring
from agents.contracts import AgentQuery, AgentResponse
from agents.logging_setup import get_logger
from data_quality_monitoring.audit_trail import QualityAuditStore
from data_quality_monitoring.evaluator import DataQualityCheckRun, DataQualityEvaluator
from risk_detection.anomaly_detection import REQUIRED_DELIVERY_FIELDS

logger = get_logger()

DEFAULT_AUDIT_LOG_PATH = Path(data_quality_monitoring.__file__).resolve().parent / "quality_audit_log.jsonl"


def _default_audit_store() -> QualityAuditStore:
    path = os.environ.get("SUPPLYMIND_QUALITY_AUDIT_LOG_PATH", str(DEFAULT_AUDIT_LOG_PATH))
    return QualityAuditStore(path)


def _confidence(run: DataQualityCheckRun) -> float:
    """Degrade confidence per alert reason, same shape as the other agents' _confidence().

    Reflects trust in *this agent's own report*, not a passthrough of the
    quality score itself (that's what `recommendation`/severity already
    convey) - a run with no alert reasons is fully trusted; each distinct
    alert reason (a low score, or no data at all) chips away at it.
    """
    if run.report is None or not run.report.alert_reasons:
        return 1.0
    return max(0.5, 1.0 - 0.1 * len(run.report.alert_reasons))


class DataQualityMonitoringAgent:
    name = "data_quality_monitoring_agent"

    def __init__(self, audit_store: QualityAuditStore | None = None) -> None:
        self._evaluator = DataQualityEvaluator(audit_store or _default_audit_store())

    def run(self, query: AgentQuery) -> AgentResponse:
        """Produce a Data Quality Score AgentResponse from query.context["delivery_rows"].

        Handles (returns status="error" for, rather than raising - a
        raised exception here would surface in the Orchestrator as
        "agent_communication_failed", the wrong classification for a data
        problem the caller can act on): a crashed check run.
        DataQualityEvaluator.run() itself never raises - it already
        converts both a bad parameter and a genuinely unexpected crash
        into a DataQualityCheckRun(crash_error=...), which is surfaced
        here the same way ("Quality monitoring fails" failure path).

        A run that completes with zero rows, or a poor-quality score, is
        reported as status="ok" (see module docstring) with a degraded
        confidence and the alert folded into the recommendation text -
        that *is* this agent's job, not a failure of it.

        Any other, truly unexpected exception (a bug in this method
        itself) is left to propagate - the Orchestrator already has a
        dedicated, tested path for an agent raising
        (agent_communication_failed, isolated per-agent), so this agent
        does not duplicate that handling.
        """
        context = query.context
        raw_rows: list[dict[str, Any]] = context.get("delivery_rows") or []

        run = self._evaluator.run(
            raw_rows,
            required_fields=context.get("required_fields", REQUIRED_DELIVERY_FIELDS),
            check_id=context.get("check_id"),
        )

        if run.outcome == "crashed":
            return self._error_response(
                run.crash_error or "data quality check failed", error_class="DataQualityMonitoringError"
            )

        logger.info(
            "data_quality_monitoring_agent_completed",
            extra={
                "event": "data_quality_monitoring_agent_completed",
                "outcome": "success",
                "correlation_id": run.check_id,
                "context": {"overall_score": run.report.overall_score, "poor_quality": run.report.poor_quality},
            },
        )

        return AgentResponse(
            agent_name=self.name,
            status="ok",
            recommendation=self._format_recommendation(run),
            confidence=_confidence(run),
            findings=[],
        )

    def _error_response(self, message: str, error_class: str) -> AgentResponse:
        logger.warning(
            "data_quality_monitoring_agent_failed",
            extra={
                "event": "data_quality_monitoring_agent_failed",
                "outcome": "failure",
                "error_class": error_class,
                "context": {"detail": message},
            },
        )
        return AgentResponse(agent_name=self.name, status="error", error=message)

    @staticmethod
    def _format_recommendation(run: DataQualityCheckRun) -> str:
        report = run.report
        if report.overall_score is None:
            summary = f"Data quality check ({report.total_rows} row(s)): {'; '.join(report.alert_reasons)}"
        else:
            summary = (
                f"Data quality check ({report.total_rows} row(s)): "
                f"score {report.overall_score:.0f}/100 ({report.severity})"
            )
            if report.poor_quality:
                summary += " | ALERT: " + "; ".join(report.alert_reasons)
        return summary
