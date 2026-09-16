"""Demand forecasting agent (STORY-003 / REQ-005).

Wraps forecasting/ (pure, deterministic computation) as an Agent so it
plugs into the existing Orchestrator (STORY-002) without any changes to
orchestration logic. Turns raw historical demand rows into a forecast
recommendation, or an "error" AgentResponse when the data can't support
one.

Query contract (via AgentQuery.context):
    "demand_history": list[dict] - raw rows from data_integration
        (e.g. available_for_analysis(results)["customer_orders"]).
        Required.
    "date_field": str - which row key holds the order/transaction date.
        Defaults to "order_date".
    "quantity_field": str - which row key holds the demand quantity.
        Defaults to "quantity".
    "periods_ahead": int - how many months to forecast. Defaults to 3.
    "previous_forecast_points": list[dict] - optional. Each dict is
        {"period": "YYYY-MM", "forecast_quantity": float}, from a prior
        call's forecast. When supplied, this run checks it against the
        freshly-aggregated history (which is by definition the actual
        demand for whatever periods it covers) for model drift. There is
        no persistence layer in this repo yet - the caller is
        responsible for keeping and passing this along; when omitted,
        drift checking is skipped, not treated as a failure.
    "sku_field": str, optional - which row key holds the item ordered.
        Defaults to "sku".
    "category_field": str, optional - which row key holds the item's
        category. Defaults to "category".
    "region_field": str, optional - which row key holds the order's
        region. Defaults to "region".

    For each of the three group fields above, when at least one row
    carries that field, this agent also forecasts each distinct value's
    own history independently and attaches one AgentFinding per
    forecastable value (subject_kind "sku"/"category"/"region" to match)
    - the breakdown the aggregate-only recommendation text can't show.
    When no row carries a given field (the common case until a caller
    maps that column onto customer_orders), that one breakdown is simply
    omitted - findings only ever grows, it never degrades the aggregate
    forecast above it. All three are independent: a dataset can have
    none, one, or all three group fields mapped at once.
"""

from __future__ import annotations

from typing import Any

from agents.contracts import AgentFinding, AgentQuery, AgentResponse, FindingSeverity, FindingSubjectKind
from agents.logging_setup import get_logger
from forecasting.aggregation import AggregationError, aggregate_monthly_demand, aggregate_monthly_demand_by_group
from forecasting.data_quality import DataQualityReport, assess_data_quality
from forecasting.demand_model import DemandPoint, ForecastingError, ForecastPoint, ForecastResult, forecast_demand
from forecasting.drift import DriftReport, detect_drift
from risk_detection.anomaly_detection import AnomalyDetectionError, detect_demand_spikes

logger = get_logger()

DEFAULT_PERIODS_AHEAD = 3
DEFAULT_DATE_FIELD = "order_date"
DEFAULT_QUANTITY_FIELD = "quantity"
DEFAULT_SKU_FIELD = "sku"
DEFAULT_CATEGORY_FIELD = "category"
DEFAULT_REGION_FIELD = "region"

# Which subject_kind each group-breakdown field maps to - see
# _per_group_findings, called once per entry here from run().
_GROUP_FIELDS: tuple[tuple[str, str], ...] = (
    ("sku_field", "sku"),
    ("category_field", "category"),
    ("region_field", "region"),
)
_GROUP_FIELD_DEFAULTS: dict[str, str] = {
    "sku_field": DEFAULT_SKU_FIELD,
    "category_field": DEFAULT_CATEGORY_FIELD,
    "region_field": DEFAULT_REGION_FIELD,
}


class DemandForecastingAgent:
    name = "demand_forecasting_agent"

    def run(self, query: AgentQuery) -> AgentResponse:
        """Produce a demand forecast AgentResponse from query.context["demand_history"].

        Handles (returns status="error" for, rather than raising - a
        raised exception here would surface in the Orchestrator as
        "agent_communication_failed", the wrong classification for a
        data/parameter problem the caller can act on):
        - missing/empty demand_history and rows that can't be
          aggregated into monthly periods ("data quality issues"
          failure path)
        - too little history to fit a trend ("model training failure"
          failure path)
        - a bad periods_ahead value or a malformed
          previous_forecast_points entry ("incorrect parameter
          settings" failure path)

        Any other, truly unexpected exception is left to propagate -
        that is the "forecasting API failure" failure path, and the
        Orchestrator already has a dedicated, tested path for an agent
        raising (agent_communication_failed, isolated per-agent so it
        can't take down a sibling agent's result), so this agent does
        not duplicate that handling.
        """
        context = query.context
        raw_rows: list[dict[str, Any]] = context.get("demand_history") or []
        date_field = context.get("date_field", DEFAULT_DATE_FIELD)
        quantity_field = context.get("quantity_field", DEFAULT_QUANTITY_FIELD)
        periods_ahead = context.get("periods_ahead", DEFAULT_PERIODS_AHEAD)

        if not raw_rows:
            return self._error_response("no historical demand data provided")

        try:
            history = aggregate_monthly_demand(raw_rows, date_field, quantity_field)
        except AggregationError as exc:
            return self._error_response(f"data quality issue: {exc}")

        quality = assess_data_quality(history)

        try:
            result = forecast_demand(history, periods_ahead=periods_ahead)
        except ForecastingError as exc:
            return self._error_response(str(exc))

        try:
            drift = self._check_drift(context.get("previous_forecast_points"), history)
        except (KeyError, TypeError) as exc:
            return self._error_response(f"invalid previous_forecast_points entry: {exc}")

        logger.info(
            "demand_forecast_generated",
            extra={
                "event": "demand_forecast_generated",
                "outcome": "success",
                "context": {
                    "periods_ahead": periods_ahead,
                    "confidence": result.confidence,
                    "data_quality_warnings": quality.warnings,
                    "drift_checked": drift is not None,
                    "drift_detected": drift.drifted if drift else False,
                },
            },
        )
        if drift and drift.drifted:
            logger.warning(
                "forecasting_model_drift_detected",
                extra={
                    "event": "forecasting_model_drift_detected",
                    "outcome": "success",
                    "context": {"detail": drift.detail},
                },
            )

        findings: list[AgentFinding] = []
        for context_key, subject_kind in _GROUP_FIELDS:
            group_field = context.get(context_key, _GROUP_FIELD_DEFAULTS[context_key])
            findings.extend(
                _per_group_findings(raw_rows, group_field, subject_kind, date_field, quantity_field, periods_ahead)
            )

        return AgentResponse(
            agent_name=self.name,
            status="ok",
            recommendation=self._format_recommendation(result, quality, drift),
            confidence=result.confidence,
            findings=findings,
        )

    @staticmethod
    def _check_drift(
        previous_forecast_points: list[dict[str, Any]] | None, history: list[DemandPoint]
    ) -> DriftReport | None:
        if not previous_forecast_points:
            return None
        points = [
            ForecastPoint(period=p["period"], forecast_quantity=p["forecast_quantity"])
            for p in previous_forecast_points
        ]
        return detect_drift(points, history)

    def _error_response(self, message: str) -> AgentResponse:
        logger.warning(
            "demand_forecast_failed",
            extra={
                "event": "demand_forecast_failed",
                "outcome": "failure",
                "error_class": "ForecastingError",
                "context": {"detail": message},
            },
        )
        return AgentResponse(agent_name=self.name, status="error", error=message)

    @staticmethod
    def _format_recommendation(
        result: ForecastResult, quality: DataQualityReport, drift: DriftReport | None
    ) -> str:
        points_text = "; ".join(f"{p.period}: {p.forecast_quantity:.1f}" for p in result.points)
        summary = f"Demand forecast ({result.model}, confidence {result.confidence:.2f}): {points_text}"
        if quality.warnings:
            summary += " | Data quality notes: " + "; ".join(quality.warnings)
        if drift and drift.drifted:
            summary += f" | Model drift warning: {drift.detail}"
        return summary


def _per_group_findings(
    raw_rows: list[dict[str, Any]],
    group_field: str,
    subject_kind: FindingSubjectKind,
    date_field: str,
    quantity_field: str,
    periods_ahead: int,
) -> list[AgentFinding]:
    """One AgentFinding per distinct group_field value with enough history of its own to forecast; [] if group_field isn't populated on any row.

    Shared by all three of run()'s group breakdowns (sku/category/region)
    - only the field name grouped on and the subject_kind stamped onto the
    resulting findings differ between them. Groups raw_rows by group_field
    (aggregate_monthly_demand_by_group() applies the exact same date/
    quantity parsing aggregate_monthly_demand() itself uses, just scoped to
    one group's rows at a time), then forecasts each group's own history
    independently. A group with too little history to fit a trend
    (ForecastingError - forecast_demand's own MIN_HISTORY_POINTS floor) is
    skipped, not fatal to the rest - the same per-subject isolation
    shipment_delay_analysis_agent/supplier_evaluation_agent already apply
    to their own per-subject loops. An AggregationError (an unparseable
    quantity within some group's own rows) is treated the same way as the
    aggregate path already treats it at the top of run() - this optional
    breakdown does not fail the whole response over it, findings are just
    omitted.
    """
    try:
        by_group = aggregate_monthly_demand_by_group(raw_rows, group_field, date_field, quantity_field)
    except AggregationError:
        return []

    findings: list[AgentFinding] = []
    for subject, history in sorted(by_group.items()):
        try:
            result = forecast_demand(history, periods_ahead=periods_ahead)
        except ForecastingError:
            continue
        severity, anomaly_note = _group_severity(history)
        next_point = result.points[0]
        detail = (
            f"next period ({next_point.period}) forecast: {next_point.forecast_quantity:.1f} "
            f"({result.model}, confidence {result.confidence:.2f}); {anomaly_note}"
        )
        findings.append(
            AgentFinding(
                subject=subject,
                subject_kind=subject_kind,
                severity=severity,
                detail=detail,
                metric_value=next_point.forecast_quantity,
            )
        )
    return findings


def _group_severity(history: list[DemandPoint]) -> tuple[FindingSeverity, str]:
    """This group's own severity, reusing risk_detection.anomaly_detection's existing z-score demand-spike detector.

    Deliberately reuses that detector's own severity rather than inventing
    a new rule here - see shipment_delay_analysis_agent/
    supplier_evaluation_agent for the same "severity lives in the pure
    computation module, the agent just passes it through" layering. Falls
    back to "low" (not a fabricated middle value) when either this group's
    most recent period isn't itself flagged as anomalous, or there isn't
    even enough history to run the detector at all - it needs
    MIN_HISTORY_POINTS_FOR_DETECTION, a stricter floor than
    forecast_demand's own MIN_HISTORY_POINTS, so a group can be
    forecastable while still having too little history for anomaly
    detection.
    """
    try:
        anomalies = detect_demand_spikes(history)
    except AnomalyDetectionError:
        return "low", "not enough history yet to detect demand anomalies for this group"
    last_period = history[-1].period
    recent = next((a for a in anomalies if a.period == last_period), None)
    if recent is None:
        return "low", "no recent demand anomaly detected"
    return recent.severity, f"recent demand anomaly: {recent.detail}"
