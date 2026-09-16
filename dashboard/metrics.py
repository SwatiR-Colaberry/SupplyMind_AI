"""Pure metric-aggregation logic for the Executive Control Tower (STORY-009 / REQ-015).

Turns the stage-1 analysis fleet's CoordinationResults (STORY-002's
Orchestrator, the same DemandForecastingAgent/StockoutRiskAgent/
RiskDetectionAgent/SupplierEvaluationAgent/ShipmentDelayAnalysisAgent/
DataQualityMonitoringAgent fleet recommendation/run_sample_recommendation_demo.py
already coordinates) into one DashboardSnapshot an executive can read at a
glance, or a clean "nothing to show" snapshot when every agent failed.

Deliberately depends only on the shared AgentResponse/AgentFinding/
CoordinationResult contracts (agents/contracts.py, agents/orchestrator.py),
never on any one agent's identity or internal computation types - the same
boundary RecommendationAgent (STORY-006) already holds to
("RecommendationAgent... only depends on the shared AgentResponse/
AgentFinding contract, not on any specific agent's identity", per
PROGRESS.md's STORY-006 entry). This is what lets a dashboard tile render
correctly for every current agent and any future one added to the fleet,
without this module needing a change each time.

No I/O here - see dashboard/audit_trail.py for the durable trust-spine
record and dashboard/render.py for turning a DashboardSnapshot into HTML.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from agents.contracts import AgentFinding, FindingSeverity
from agents.orchestrator import CoordinationResult
from dashboard.data_freshness import DataFreshnessEntry

MetricStatus = Literal["ok", "error"]
DashboardStatus = Literal["ok", "degraded", "error"]

# Same ordering risk_detection/risk_score.py's own _SEVERITY_RANK uses -
# duplicated here (rather than imported) precisely because this module
# must not depend on any one agent's internal module, per this file's own
# docstring. FindingSeverity is the shared, agent-agnostic contract type.
_SEVERITY_RANK: dict[FindingSeverity, int] = {"low": 0, "medium": 1, "high": 2, "critical": 3}


@dataclass(frozen=True)
class DashboardMetric:
    """One tile on the Executive Control Tower - one agent's contribution.

    `status` reflects whether this agent's data was successfully
    processed (AC1/AC2's "data processing errors" distinction) - it is
    deliberately separate from `severity`, which reflects the business
    severity of what a successfully-processed agent found (e.g. a
    critical stockout risk is `status="ok"` - the data processed fine -
    with `severity="critical"` - and that finding needs attention).
    """

    metric_id: str  # the source agent's `name`, e.g. "risk_detection_agent"
    label: str
    status: MetricStatus
    headline: str  # human-readable summary: the agent's own recommendation, or its error
    source_agent: str
    confidence: float | None = None
    severity: FindingSeverity | None = None  # highest-severity finding this agent reported, if any
    critical_findings: int = 0
    high_findings: int = 0
    # Carried through unfiltered so a consumer (dashboard/charts.py's
    # per-subject breakdowns) can chart individual SKUs/suppliers/periods -
    # critical_findings/high_findings above are only ever an aggregate count,
    # which is exactly the granularity this dataclass previously threw away.
    findings: list[AgentFinding] = field(default_factory=list)


@dataclass(frozen=True)
class DashboardSnapshot:
    dashboard_id: str
    generated_at: str  # ISO-8601 UTC
    metrics: list[DashboardMetric] = field(default_factory=list)
    data_processing_errors: list[dict[str, str]] = field(default_factory=list)  # [{"agent", "error"}]
    notification: str | None = None  # set whenever data_processing_errors is non-empty - AC2
    overall_status: DashboardStatus = "ok"
    total_critical_findings: int = 0
    total_high_findings: int = 0
    data_freshness: list[DataFreshnessEntry] = field(default_factory=list)


# metric_id -> executive-facing label. An agent not in this map still gets
# a tile (falls back to its raw agent_name as the label) rather than being
# silently dropped - a future agent added to the fleet must still show up
# somewhere on the dashboard, even before this map is updated for it.
_TILE_LABELS: dict[str, str] = {
    "demand_forecasting_agent": "Demand Forecast",
    "stockout_risk_agent": "Stockout Risk",
    "risk_detection_agent": "Supply Chain Risk",
    "supplier_evaluation_agent": "Supplier Risk",
    "shipment_delay_analysis_agent": "Shipment Delay",
    "data_quality_monitoring_agent": "Data Quality",
    "delivery_intelligence_agent": "Delivery Intelligence",
    "recommendation_agent": "Recommendation",
}


def _severity_summary(findings: list[AgentFinding]) -> tuple[FindingSeverity | None, int, int]:
    """One pass over findings: highest severity, critical count, high count."""
    highest: FindingSeverity | None = None
    critical = 0
    high = 0
    for finding in findings:
        if highest is None or _SEVERITY_RANK[finding.severity] > _SEVERITY_RANK[highest]:
            highest = finding.severity
        if finding.severity == "critical":
            critical += 1
        elif finding.severity == "high":
            high += 1
    return highest, critical, high


def _metric_from_result(result: CoordinationResult) -> DashboardMetric:
    label = _TILE_LABELS.get(result.agent_name, result.agent_name)

    # Two distinct failure shapes both become an "error" tile: an
    # orchestration-level failure (timeout, crash, still-invalid after
    # re-evaluation - result.response is None, only result.error exists)
    # and an agent-level clean failure (e.g. "no supply chain data
    # provided" - the response validated fine, but the agent itself
    # reports an error; see agents/risk_detection_agent.py's own no-data
    # path). Distinguished only by where the message comes from - same
    # two cases recommendation/run_sample_recommendation_demo.py's
    # _stage2_failure_reason() checks separately.
    error: str | None = None
    if result.outcome == "failure" or result.response is None:
        error = result.error or "agent did not return a response"
    elif result.response.status == "error":
        error = result.response.error or "agent reported an error with no message"

    if error is not None:
        return DashboardMetric(
            metric_id=result.agent_name, label=label, status="error", headline=error, source_agent=result.agent_name
        )

    response = result.response
    severity, critical, high = _severity_summary(response.findings)
    return DashboardMetric(
        metric_id=result.agent_name,
        label=label,
        status="ok",
        headline=response.recommendation or "",
        source_agent=result.agent_name,
        confidence=response.confidence,
        severity=severity,
        critical_findings=critical,
        high_findings=high,
        findings=response.findings,
    )


def build_dashboard(
    coordination_results: list[CoordinationResult],
    dashboard_id: str | None = None,
    data_freshness: list[DataFreshnessEntry] | None = None,
) -> DashboardSnapshot:
    """Build one DashboardSnapshot from a stage-1 (and, when present, stage-2
    recommendation) Orchestrator run's results.

    `data_freshness` is computed separately (dashboard/data_freshness.py,
    from the same data_integration pull that fed the agents) and simply
    carried through onto the snapshot here - this function's own job
    stays "turn agent outputs into tiles," not "know how data gets
    pulled."

    Pure and deterministic given its input - no clock reads except the
    snapshot's own `generated_at` stamp, no randomness except
    `dashboard_id` when the caller doesn't supply one. Never raises: an
    agent that failed becomes an "error" tile plus a data_processing_errors
    entry (AC2), not an exception that would take the whole dashboard
    down - a single broken agent must never prevent every other agent's
    metric from being displayed (STORY-009's "Data processing errors"
    failure path).
    """
    metrics = [_metric_from_result(r) for r in coordination_results]

    data_processing_errors = [
        {"agent": m.source_agent, "error": m.headline} for m in metrics if m.status == "error"
    ]

    notification: str | None = None
    if data_processing_errors:
        failed_agents = ", ".join(e["agent"] for e in data_processing_errors)
        notification = (
            f"{len(data_processing_errors)} of {len(metrics)} data source(s) could not be processed "
            f"({failed_agents}) - the metrics shown below reflect only what was successfully processed."
        )
    elif not metrics:
        notification = "no agent data available - the dashboard has nothing to display."

    if not metrics or len(data_processing_errors) == len(metrics):
        overall_status: DashboardStatus = "error"
    elif data_processing_errors:
        overall_status = "degraded"
    else:
        overall_status = "ok"

    return DashboardSnapshot(
        dashboard_id=dashboard_id or str(uuid.uuid4()),
        generated_at=datetime.now(timezone.utc).isoformat(),
        metrics=metrics,
        data_processing_errors=data_processing_errors,
        notification=notification,
        overall_status=overall_status,
        total_critical_findings=sum(m.critical_findings for m in metrics),
        total_high_findings=sum(m.high_findings for m in metrics),
        data_freshness=data_freshness or [],
    )


@dataclass(frozen=True)
class KPISummary:
    """A handful of dollar rollups an executive scans before anything else.

    Deliberately narrow, not an exhaustive KPI set: only includes numbers
    this repo actually computes from real per-finding data
    (stockout_risk_agent's per-SKU revenue_at_risk, shipment_delay_
    analysis_agent's per-PO delay cost, both carried on AgentFinding.
    metric_value). Two KPIs a product vision might reasonably also want
    here - overstock and orders-pending - are deliberately not included:
    this repo has no overstock threshold defined anywhere and no
    incoming/pending-order data source, so a number for either would be
    fabricated, not computed. Add them once a real data source and
    definition exist, not before.
    """

    # None means "unknown" (that tile wasn't in this snapshot's fleet, it
    # errored, or none of its findings carried this number - e.g. no SKU
    # has a unit_price yet) - distinct from 0.0, which means this was
    # genuinely computed and came out to zero (e.g. shipment_delay_
    # analysis_agent ran cleanly and found no delays at all). Never
    # collapse the two, per this repo's "never default an unavailable
    # signal to a false neutral midpoint" principle
    # (inventory_risk/risk_model.py's compute_risk_score already applies
    # this same rule to its own optional signals).
    total_revenue_at_risk: float | None
    total_shipment_delay_cost: float | None


def _sum_metric_value(snapshot: DashboardSnapshot, metric_id: str) -> float | None:
    for metric in snapshot.metrics:
        if metric.metric_id != metric_id:
            continue
        if metric.status != "ok":
            return None
        values = [f.metric_value for f in metric.findings if f.metric_value is not None]
        if not values and metric.findings:
            return None  # findings exist but none carry this number - unknown, not zero
        return sum(values)
    return None  # this metric wasn't part of this snapshot's fleet at all


def compute_kpi_summary(snapshot: DashboardSnapshot) -> KPISummary:
    """Roll up the dollar KPIs this repo can actually compute from `snapshot`.

    Pure and deterministic given its input, like build_dashboard() itself
    - reads metric_value off findings already present on the snapshot,
    never re-runs or re-fetches anything.
    """
    return KPISummary(
        total_revenue_at_risk=_sum_metric_value(snapshot, "stockout_risk_agent"),
        total_shipment_delay_cost=_sum_metric_value(snapshot, "shipment_delay_analysis_agent"),
    )
