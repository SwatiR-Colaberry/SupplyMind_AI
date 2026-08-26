"""Risk detection and anomaly analysis agent (STORY-005 / REQ-009, REQ-012).

Wraps risk_detection/ (pure, deterministic computation) as an Agent so it
plugs into the existing Orchestrator (STORY-002) without any changes to
orchestration logic. Combines up to three independent signals into one
Supply Chain Risk Score:
- demand-spike/drop anomalies, from demand history (reuses STORY-003's
  aggregate_monthly_demand)
- supplier delivery delays, from raw delivery rows
- stockout risk, from inventory rows (reuses STORY-004's
  inventory_risk/ exactly as StockoutRiskAgent does)

Each signal is independently optional and independently degradable: a
problem with one (too little demand history, a malformed delivery row, a
corrupted inventory row) is logged and excluded rather than failing the
whole run, so a caller who only has two of the three data sources still
gets a score from what's available. The agent only returns an "error"
AgentResponse when there is nothing at all to analyze.

Query contract (via AgentQuery.context), all keys optional but at least
one of demand_history/delivery_rows/inventory_rows is required:
    "demand_history": list[dict] - raw rows, same shape
        DemandForecastingAgent expects (e.g. customer_orders rows).
    "date_field" / "quantity_field": str - column names within
        demand_history, same defaults as DemandForecastingAgent.
    "delivery_rows": list[dict] - raw rows, one per delivery. Expected
        fields: po_id, expected_date, actual_date, and optionally
        supplier - see risk_detection/anomaly_detection.py's logged
        schema assumption (no real delivery_records schema exists yet).
    "delay_threshold_days": float - minimum lateness to count as a
        delay. Defaults to anomaly_detection.DEFAULT_DELAY_THRESHOLD_DAYS.
    "inventory_rows": list[dict] - raw rows, same shape
        StockoutRiskAgent expects - see inventory_risk/data_quality.py's
        REQUIRED_FIELDS.
"""

from __future__ import annotations

import math
from typing import Any

from agents.contracts import AgentFinding, AgentQuery, AgentResponse
from agents.logging_setup import get_logger
from forecasting.aggregation import AggregationError, aggregate_monthly_demand
from inventory_risk.data_quality import REQUIRED_FIELDS, assess_inventory_data_quality
from inventory_risk.risk_model import InventoryPosition, RiskModelError, StockoutRiskAssessment, assess_stockout_risk
from risk_detection.anomaly_detection import (
    DEFAULT_DELAY_THRESHOLD_DAYS,
    AnomalyDetectionError,
    DemandAnomaly,
    SupplierDelayAnomaly,
    SupplierDelayError,
    detect_demand_spikes,
    detect_supplier_delays,
)
from risk_detection.risk_score import RiskContribution, SupplyChainRiskScore, compute_risk_score

logger = get_logger()

DEFAULT_DATE_FIELD = "order_date"
DEFAULT_QUANTITY_FIELD = "quantity"

# Maps each RiskContribution.source (this agent's own internal signal
# taxonomy) to AgentFinding.subject_kind (the shared, cross-agent
# vocabulary STORY-006's RecommendationAgent compares subjects against -
# e.g. lining this agent's per-SKU stockout findings up against
# StockoutRiskAgent's own).
_SUBJECT_KIND_BY_SOURCE = {
    "demand_anomaly": "period",
    "supplier_delay": "po",
    "stockout_risk": "sku",
}


def _finding_from_contribution(contribution: RiskContribution) -> AgentFinding:
    return AgentFinding(
        subject=contribution.identifier,
        subject_kind=_SUBJECT_KIND_BY_SOURCE[contribution.source],
        severity=contribution.severity,
        detail=contribution.detail,
    )


def _finite_or_none(value: float) -> float | None:
    return None if math.isinf(value) or math.isnan(value) else round(value, 2)


class RiskDetectionAgent:
    name = "risk_detection_agent"

    def run(self, query: AgentQuery) -> AgentResponse:
        """Produce a unified Supply Chain Risk Score from whatever context data is available.

        Handles (returns status="error" for, rather than raising - a
        raised exception here would surface in the Orchestrator as
        "agent_communication_failed", the wrong classification for a
        data problem the caller can act on): none of
        demand_history/delivery_rows/inventory_rows supplied at all
        ("anomaly detection failure" failure path - there is nothing to
        analyze).

        Handles (logs a warning and continues with a degraded signal,
        rather than failing the whole run - "algorithm performance
        issues" / "data inconsistency" failure paths): demand history
        too short or unparseable for spike detection, malformed delivery
        rows, corrupted inventory rows or a per-SKU risk-model error.
        Each degradation is folded into the response's "Data quality
        notes" and lowers the returned confidence, so a caller sees the
        result is partial rather than mistaking it for a clean run.

        Any other, truly unexpected exception is left to propagate -
        that is the "notification system failure" / underlying
        infrastructure failure path, and the Orchestrator already has a
        dedicated, tested path for an agent raising
        (agent_communication_failed, isolated per-agent), so this agent
        does not duplicate that handling.
        """
        context = query.context
        demand_rows = context.get("demand_history") or []
        delivery_rows = context.get("delivery_rows") or []
        inventory_rows = context.get("inventory_rows") or []

        if not demand_rows and not delivery_rows and not inventory_rows:
            return self._error_response(
                "no supply chain data provided for risk detection", error_class="RiskDetectionError"
            )

        demand_anomalies, demand_notes = self._detect_demand_anomalies(context)
        supplier_delays, delivery_notes = self._detect_supplier_delays(context)
        stockout_assessments, inventory_notes = self._assess_stockout_risk(inventory_rows)

        self._log_detected_anomalies(demand_anomalies, supplier_delays)

        risk_score = compute_risk_score(
            demand_anomalies=demand_anomalies,
            supplier_delays=supplier_delays,
            stockout_assessments=stockout_assessments,
        )
        logger.info(
            "supply_chain_risk_score_computed",
            extra={
                "event": "supply_chain_risk_score_computed",
                "outcome": "success",
                "context": {
                    "score": risk_score.score,
                    "severity": risk_score.severity,
                    "contributing_factors": len(risk_score.contributions),
                },
            },
        )

        notes = demand_notes + delivery_notes + inventory_notes
        findings = [_finding_from_contribution(c) for c in risk_score.contributions]
        return AgentResponse(
            agent_name=self.name,
            status="ok",
            recommendation=self._format_recommendation(risk_score, notes),
            confidence=self._confidence(notes),
            findings=findings,
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
                "demand_spike_detection_skipped",
                extra={
                    "event": "demand_spike_detection_skipped",
                    "outcome": "failure",
                    "error_class": exc.__class__.__name__,
                    "context": {"detail": str(exc)},
                },
            )
            return [], [f"demand spike detection skipped: {exc}"]

    def _detect_supplier_delays(self, context: dict[str, Any]) -> tuple[list[SupplierDelayAnomaly], list[str]]:
        raw_rows: list[dict[str, Any]] = context.get("delivery_rows") or []
        if not raw_rows:
            return [], []

        threshold = context.get("delay_threshold_days", DEFAULT_DELAY_THRESHOLD_DAYS)
        try:
            report = detect_supplier_delays(raw_rows, delay_threshold_days=threshold)
        except SupplierDelayError as exc:
            logger.warning(
                "supplier_delay_detection_skipped",
                extra={
                    "event": "supplier_delay_detection_skipped",
                    "outcome": "failure",
                    "error_class": exc.__class__.__name__,
                    "context": {"detail": str(exc)},
                },
            )
            return [], [f"supplier delay detection skipped: {exc}"]

        notes: list[str] = []
        for flagged in report.flagged_rows:
            logger.warning(
                "delivery_row_flagged_for_review",
                extra={
                    "event": "delivery_row_flagged_for_review",
                    "outcome": "failure",
                    "error_class": "SupplierDeliveryDataQualityError",
                    "context": {"po_id": flagged.row.get("po_id"), "reasons": flagged.reasons},
                },
            )
        if report.flagged_rows:
            notes.append(f"{len(report.flagged_rows)} delivery row(s) flagged for review")

        return report.delays, notes

    def _assess_stockout_risk(self, raw_rows: list[dict[str, Any]]) -> tuple[list[StockoutRiskAssessment], list[str]]:
        if not raw_rows:
            return [], []

        quality = assess_inventory_data_quality(raw_rows)
        notes: list[str] = []
        for flagged in quality.flagged_rows:
            logger.warning(
                "inventory_row_flagged_for_review",
                extra={
                    "event": "inventory_row_flagged_for_review",
                    "outcome": "failure",
                    "error_class": "InventoryDataQualityError",
                    "context": {"sku": flagged.row.get("sku"), "reasons": flagged.reasons},
                },
            )
        if quality.flagged_rows:
            notes.append(f"{len(quality.flagged_rows)} inventory row(s) flagged for review")

        assessments: list[StockoutRiskAssessment] = []
        for row in quality.clean_rows:
            position = InventoryPosition(**{f: row[f] for f in REQUIRED_FIELDS})
            try:
                assessments.append(assess_stockout_risk(position))
            except RiskModelError as exc:
                notes.append(f"{position.sku}: stockout risk assessment skipped ({exc})")

        return assessments, notes

    @staticmethod
    def _log_detected_anomalies(
        demand_anomalies: list[DemandAnomaly], supplier_delays: list[SupplierDelayAnomaly]
    ) -> None:
        for anomaly in demand_anomalies:
            logger.info(
                "demand_anomaly_detected",
                extra={
                    "event": "demand_anomaly_detected",
                    "outcome": "success",
                    "context": {
                        "period": anomaly.period,
                        "direction": anomaly.direction,
                        "severity": anomaly.severity,
                        "z_score": _finite_or_none(anomaly.z_score),
                    },
                },
            )
        for delay in supplier_delays:
            logger.info(
                "supplier_delay_detected",
                extra={
                    "event": "supplier_delay_detected",
                    "outcome": "success",
                    "context": {
                        "po_id": delay.po_id,
                        "severity": delay.severity,
                        "delay_days": delay.delay_days,
                    },
                },
            )

    def _error_response(self, message: str, error_class: str) -> AgentResponse:
        logger.warning(
            "risk_detection_failed",
            extra={
                "event": "risk_detection_failed",
                "outcome": "failure",
                "error_class": error_class,
                "context": {"detail": message},
            },
        )
        return AgentResponse(agent_name=self.name, status="error", error=message)

    @staticmethod
    def _format_recommendation(risk_score: SupplyChainRiskScore, notes: list[str]) -> str:
        summary = risk_score.explanation
        if notes:
            summary += " | Data quality notes: " + "; ".join(notes)
        return summary

    @staticmethod
    def _confidence(notes: list[str]) -> float:
        if not notes:
            return 1.0
        return max(0.5, 1.0 - 0.1 * len(notes))
