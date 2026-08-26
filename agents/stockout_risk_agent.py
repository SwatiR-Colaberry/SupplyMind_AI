"""Stockout risk agent (STORY-004 / REQ-006, REQ-011).

Wraps inventory_risk/ (pure, deterministic computation) as an Agent so
it plugs into the existing Orchestrator (STORY-002) without any changes
to orchestration logic. Turns raw inventory rows into a stockout-risk
recommendation, or an "error" AgentResponse when no usable inventory
data is available.

Query contract (via AgentQuery.context):
    "inventory_rows": list[dict] - raw rows, one per SKU (e.g. from
        data_integration's available_for_analysis(results)["inventory"]).
        Each row is expected to carry sku, current_stock, safety_stock,
        daily_demand_rate, lead_time_days - see
        inventory_risk/data_quality.py's REQUIRED_FIELDS. Required.
        daily_demand_rate is caller-supplied and source-agnostic: a
        turnover-derived average or a forecasted-demand rate (e.g. a
        STORY-003 forecast_demand() point divided into a daily figure) -
        REQ-011's "current and forecasted demand" distinction lives in
        whatever populates this row, not in this agent.
"""

from __future__ import annotations

import math
from typing import Any

from agents.contracts import AgentFinding, AgentQuery, AgentResponse
from agents.logging_setup import get_logger
from inventory_risk.data_quality import REQUIRED_FIELDS, FlaggedRow, assess_inventory_data_quality
from inventory_risk.risk_model import InventoryPosition, RiskModelError, StockoutRiskAssessment, assess_stockout_risk

logger = get_logger()

# Severity order for sorting the recommendation so the riskiest SKUs surface first.
# .get() with a fallback, not [] - a future RiskLevel value this dict hasn't been
# updated for should sort last, not raise a KeyError that would propagate out of
# _format_recommendation and get misclassified by the Orchestrator as
# agent_communication_failed instead of the trivial ordering gap it actually is.
_RISK_SEVERITY = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_UNKNOWN_SEVERITY = len(_RISK_SEVERITY)


def _describe_flagged_row(flagged: FlaggedRow) -> str:
    sku = flagged.row.get("sku", "<unknown sku>")
    return f"{sku} ({'; '.join(flagged.reasons)})"


class StockoutRiskAgent:
    name = "stockout_risk_agent"

    def run(self, query: AgentQuery) -> AgentResponse:
        """Produce a stockout-risk AgentResponse from query.context["inventory_rows"].

        Handles (returns status="error" for, rather than raising - a
        raised exception here would surface in the Orchestrator as
        "agent_communication_failed", the wrong classification for a
        data problem the caller can act on):
        - missing/empty inventory_rows, and rows with no usable data
          left after quality filtering ("data synchronization issues" /
          "inventory data corruption" failure paths)
        - a clean row the risk model itself still rejects, isolated
          per-SKU so it doesn't discard every other SKU's valid
          prediction ("model prediction errors" / "incorrect risk
          thresholds" failure paths)

        Any other, truly unexpected exception is left to propagate -
        that is the "prediction API failure" failure path, and the
        Orchestrator already has a dedicated, tested path for an agent
        raising (agent_communication_failed, isolated per-agent so it
        can't take down a sibling agent's result), so this agent does
        not duplicate that handling.
        """
        raw_rows: list[dict[str, Any]] = query.context.get("inventory_rows") or []
        quality = assess_inventory_data_quality(raw_rows)

        flagged_notes = [_describe_flagged_row(f) for f in quality.flagged_rows]
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

        if not quality.clean_rows:
            detail = "inventory data flagged for review: " + "; ".join(flagged_notes) if flagged_notes else "no inventory data provided"
            return self._error_response(detail, error_class="InventoryDataQualityError")

        assessments: list[StockoutRiskAssessment] = []
        prediction_errors: list[str] = []
        for row in quality.clean_rows:
            position = InventoryPosition(**{f: row[f] for f in REQUIRED_FIELDS})
            try:
                assessment = assess_stockout_risk(position)
            except RiskModelError as exc:
                prediction_errors.append(f"{position.sku}: {exc}")
                continue

            assessments.append(assessment)
            logger.info(
                "stockout_risk_predicted",
                extra={
                    "event": "stockout_risk_predicted",
                    "outcome": "success",
                    "context": {
                        "sku": assessment.sku,
                        "risk_level": assessment.risk_level,
                        "confidence": assessment.confidence,
                        # math.inf/math.nan aren't valid strict JSON (json.dumps
                        # emits the non-standard `Infinity`/`NaN` tokens) -
                        # normalize to null so every log line stays parseable
                        # downstream. NaN shouldn't reach here in practice
                        # (assess_stockout_risk's _validate rejects it), but this
                        # is a cheap independent guard on the logging boundary.
                        "days_of_supply": None
                        if math.isinf(assessment.days_of_supply) or math.isnan(assessment.days_of_supply)
                        else round(assessment.days_of_supply, 2),
                    },
                },
            )

        if not assessments:
            detail = "; ".join(prediction_errors) or "no SKU could be assessed"
            return self._error_response(detail, error_class="RiskModelError")

        mean_confidence = sum(a.confidence for a in assessments) / len(assessments)
        findings = [
            AgentFinding(subject=a.sku, subject_kind="sku", severity=a.risk_level, detail=a.detail)
            for a in assessments
        ]
        return AgentResponse(
            agent_name=self.name,
            status="ok",
            recommendation=self._format_recommendation(assessments, flagged_notes, prediction_errors),
            confidence=mean_confidence,
            findings=findings,
        )

    def _error_response(self, message: str, error_class: str) -> AgentResponse:
        logger.warning(
            "stockout_risk_prediction_failed",
            extra={
                "event": "stockout_risk_prediction_failed",
                "outcome": "failure",
                "error_class": error_class,
                "context": {"detail": message},
            },
        )
        return AgentResponse(agent_name=self.name, status="error", error=message)

    @staticmethod
    def _format_recommendation(
        assessments: list[StockoutRiskAssessment], flagged_notes: list[str], prediction_errors: list[str]
    ) -> str:
        ordered = sorted(assessments, key=lambda a: _RISK_SEVERITY.get(a.risk_level, _UNKNOWN_SEVERITY))
        points_text = "; ".join(
            f"{a.sku}: {a.risk_level} (confidence {a.confidence:.2f}, {a.detail})" for a in ordered
        )
        summary = f"Stockout risk assessment ({len(assessments)} SKU(s)): {points_text}"
        if flagged_notes:
            summary += " | Data quality notes: " + "; ".join(flagged_notes)
        if prediction_errors:
            summary += " | Flagged for review (prediction error): " + "; ".join(prediction_errors)
        return summary
