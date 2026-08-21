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
"""

from __future__ import annotations

from typing import Any

from agents.contracts import AgentQuery, AgentResponse
from agents.logging_setup import get_logger
from forecasting.aggregation import AggregationError, aggregate_monthly_demand
from forecasting.data_quality import DataQualityReport, assess_data_quality
from forecasting.demand_model import DemandPoint, ForecastingError, ForecastPoint, ForecastResult, forecast_demand
from forecasting.drift import DriftReport, detect_drift

logger = get_logger()

DEFAULT_PERIODS_AHEAD = 3
DEFAULT_DATE_FIELD = "order_date"
DEFAULT_QUANTITY_FIELD = "quantity"


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

        return AgentResponse(
            agent_name=self.name,
            status="ok",
            recommendation=self._format_recommendation(result, quality, drift),
            confidence=result.confidence,
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
