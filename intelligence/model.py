"""Four-stage intelligence model: Observe -> Understand -> Predict -> Recommend (STORY-012 / REQ-003).

Wraps forecasting/'s already-audited demand model (STORY-003) as an
explicit, individually-logged, individually-audited pipeline rather than
introducing new prediction math:

    Observe    - aggregate_monthly_demand() (forecasting/aggregation.py):
                 raw rows -> structured DemandPoints, no interpretation.
    Understand - assess_data_quality() (forecasting/data_quality.py) plus
                 a simple trend read: what the observed data means.
    Predict    - forecast_demand() (forecasting/demand_model.py): a
                 forecast derived from the understanding.
    Recommend  - a deterministic rule translating the forecast + trend
                 into a plain-language action.

Each stage is logged and recorded to a StageAuditStore regardless of
outcome. A stage failure halts the pipeline immediately - later stages
are recorded "not_processed" rather than silently skipped, so "data not
processed through all stages" is always visible in the audit trail, never
inferred by absence. An unexpected (non-StageError) exception is caught
at the pipeline level, logged, and recorded the same way - "audit trail
missing for model stages" must not happen even when the pipeline itself
has a bug - and returned as a crashed IntelligenceRun rather than
propagating, mirroring agents/orchestrator.py's crash handling.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from forecasting.aggregation import AggregationError, aggregate_monthly_demand
from forecasting.data_quality import assess_data_quality
from forecasting.demand_model import DemandPoint, ForecastingError, forecast_demand
from intelligence.audit_trail import StageAuditStore
from intelligence.contracts import (
    Observation,
    Prediction,
    Recommendation,
    StageError,
    StageName,
    StageOutcome,
    TrendDirection,
    Understanding,
)
from intelligence.logging_setup import get_logger

logger = get_logger()

STAGE_ORDER: tuple[StageName, ...] = ("observe", "understand", "predict", "recommend")

DEFAULT_DATE_FIELD = "order_date"
DEFAULT_QUANTITY_FIELD = "quantity"
DEFAULT_PERIODS_AHEAD = 3
# Below this forecast confidence (R^2, 0..1), Recommend defers to "gather
# more data" rather than proposing a quantity - a low-confidence trend
# line is not a sound basis for a stocking action. Logged assumption, not
# escalated: an implementation-level default, tunable if it proves wrong
# in practice.
LOW_CONFIDENCE_THRESHOLD = 0.4

IntelligenceOutcome = Literal["success", "failure", "crashed"]


@dataclass(frozen=True)
class StageResult:
    stage: StageName
    outcome: StageOutcome
    output: Observation | Understanding | Prediction | Recommendation | None = None
    error: str | None = None


@dataclass
class IntelligenceRun:
    """The full outcome of one IntelligenceModel.run() call."""

    run_id: str
    results: list[StageResult] = field(default_factory=list)
    crash_error: str | None = None

    @property
    def outcome(self) -> IntelligenceOutcome:
        # `is not None`, not truthiness - see agents/orchestrator.py's
        # CoordinationRun.outcome for why an empty crash message must
        # still read as "crashed".
        if self.crash_error is not None:
            return "crashed"
        if self.results and all(r.outcome == "success" for r in self.results):
            return "success"
        return "failure"

    def stage_output(self, stage: StageName) -> Observation | Understanding | Prediction | Recommendation | None:
        for result in self.results:
            if result.stage == stage and result.outcome == "success":
                return result.output
        return None

    @property
    def observation(self) -> Observation | None:
        return self.stage_output("observe")  # type: ignore[return-value]

    @property
    def understanding(self) -> Understanding | None:
        return self.stage_output("understand")  # type: ignore[return-value]

    @property
    def prediction(self) -> Prediction | None:
        return self.stage_output("predict")  # type: ignore[return-value]

    @property
    def recommendation(self) -> Recommendation | None:
        return self.stage_output("recommend")  # type: ignore[return-value]


def _trend_direction(points: list[DemandPoint]) -> TrendDirection:
    if len(points) < 2:
        return "unknown"
    ordered = sorted(points, key=lambda p: p.period)
    first, last = ordered[0].quantity, ordered[-1].quantity
    if last > first:
        return "increasing"
    if last < first:
        return "decreasing"
    return "flat"


def _observe(raw_rows: list[dict[str, Any]], date_field: str, quantity_field: str) -> Observation:
    if not raw_rows:
        raise StageError("no raw data provided to observe")
    try:
        points = aggregate_monthly_demand(raw_rows, date_field, quantity_field)
    except AggregationError as exc:
        raise StageError(f"could not observe: {exc}") from exc
    if not points:
        raise StageError(
            f"no usable rows after aggregation - all rows missing '{date_field}' or '{quantity_field}'"
        )
    return Observation(row_count=len(raw_rows), points=points, period_range=(points[0].period, points[-1].period))


def _understand(observation: Observation) -> Understanding:
    quality = assess_data_quality(observation.points)
    trend_direction = _trend_direction(observation.points)
    insights = list(quality.warnings)
    insights.append(f"trend is {trend_direction} across {quality.total_points} observed period(s)")
    return Understanding(quality=quality, trend_direction=trend_direction, insights=insights)


def _predict(understanding: Understanding, points: list[DemandPoint], periods_ahead: int) -> Prediction:
    try:
        forecast = forecast_demand(points, periods_ahead=periods_ahead)
    except ForecastingError as exc:
        raise StageError(f"could not predict: {exc}") from exc
    return Prediction(forecast=forecast)


def _recommend(prediction: Prediction, understanding: Understanding) -> Recommendation:
    next_point = prediction.forecast.points[0]
    confidence = prediction.forecast.confidence

    if confidence < LOW_CONFIDENCE_THRESHOLD:
        return Recommendation(
            action="collect more historical data before acting on this forecast",
            rationale=f"forecast confidence {confidence:.2f} is below the {LOW_CONFIDENCE_THRESHOLD} reliability threshold",
        )
    if understanding.trend_direction == "increasing":
        action = f"increase stock toward {next_point.forecast_quantity:.0f} units for {next_point.period}"
    elif understanding.trend_direction == "decreasing":
        action = f"reduce next-order quantity toward {next_point.forecast_quantity:.0f} units for {next_point.period}"
    else:
        action = f"maintain current order quantity near {next_point.forecast_quantity:.0f} units for {next_point.period}"
    return Recommendation(
        action=action,
        rationale=f"demand trend is {understanding.trend_direction} (confidence {confidence:.2f})",
    )


def _summarize(stage: StageName, output: Any) -> str:
    if stage == "observe":
        return f"{output.row_count} raw row(s) observed, {len(output.points)} period(s) after aggregation"
    if stage == "understand":
        return f"trend {output.trend_direction}, {len(output.quality.warnings)} data-quality warning(s)"
    if stage == "predict":
        return f"{len(output.forecast.points)} period(s) forecast, confidence {output.forecast.confidence:.2f}"
    return output.action  # stage == "recommend"


class IntelligenceModel:
    """Runs raw data through the Observe -> Understand -> Predict -> Recommend pipeline."""

    def __init__(self, audit_store: StageAuditStore) -> None:
        self._audit_store = audit_store

    def run(
        self,
        raw_rows: list[dict[str, Any]],
        *,
        run_id: str | None = None,
        date_field: str = DEFAULT_DATE_FIELD,
        quantity_field: str = DEFAULT_QUANTITY_FIELD,
        periods_ahead: int = DEFAULT_PERIODS_AHEAD,
    ) -> IntelligenceRun:
        run_id = run_id or str(uuid.uuid4())
        results: list[StageResult] = []
        logger.info(
            "pipeline_started",
            extra={
                "event": "pipeline_started",
                "correlation_id": run_id,
                "context": {"row_count": len(raw_rows)},
            },
        )
        try:
            observation = self._run_stage(run_id, "observe", results, lambda: _observe(raw_rows, date_field, quantity_field))
            if observation is None:
                return self._finish(run_id, results, skip=["understand", "predict", "recommend"])

            understanding = self._run_stage(run_id, "understand", results, lambda: _understand(observation))
            if understanding is None:
                return self._finish(run_id, results, skip=["predict", "recommend"])

            prediction = self._run_stage(
                run_id, "predict", results, lambda: _predict(understanding, observation.points, periods_ahead)
            )
            if prediction is None:
                return self._finish(run_id, results, skip=["recommend"])

            self._run_stage(run_id, "recommend", results, lambda: _recommend(prediction, understanding))
            return self._finish(run_id, results, skip=[])
        except Exception as exc:
            logger.error(
                "pipeline_crashed",
                extra={
                    "event": "pipeline_crashed",
                    "outcome": "failure",
                    "error_class": exc.__class__.__name__,
                    "correlation_id": run_id,
                    "context": {},
                },
            )
            already_recorded = {r.stage for r in results}
            remaining = [s for s in STAGE_ORDER if s not in already_recorded]
            return self._finish(run_id, results, skip=remaining, crash_error=str(exc))

    def _run_stage(self, run_id: str, stage: StageName, results: list[StageResult], fn: Any) -> Any:
        logger.info(
            "stage_started",
            extra={"event": "stage_started", "correlation_id": run_id, "context": {"stage": stage}},
        )
        try:
            output = fn()
        except StageError as exc:
            logger.warning(
                "stage_failed",
                extra={
                    "event": "stage_failed",
                    "outcome": "failure",
                    "error_class": "StageError",
                    "correlation_id": run_id,
                    "context": {"stage": stage, "detail": str(exc)},
                },
            )
            self._audit_store.record(run_id=run_id, stage=stage, outcome="failure", detail=str(exc))
            results.append(StageResult(stage=stage, outcome="failure", output=None, error=str(exc)))
            return None

        detail = _summarize(stage, output)
        logger.info(
            "stage_completed",
            extra={
                "event": "stage_completed",
                "outcome": "success",
                "correlation_id": run_id,
                "context": {"stage": stage, "detail": detail},
            },
        )
        self._audit_store.record(run_id=run_id, stage=stage, outcome="success", detail=detail)
        results.append(StageResult(stage=stage, outcome="success", output=output, error=None))
        return output

    def _skip_remaining(self, run_id: str, results: list[StageResult], stages: list[StageName]) -> None:
        for stage in stages:
            logger.warning(
                "stage_not_processed",
                extra={
                    "event": "stage_not_processed",
                    "outcome": "failure",
                    "correlation_id": run_id,
                    "context": {"stage": stage},
                },
            )
            self._audit_store.record(run_id=run_id, stage=stage, outcome="not_processed")
            results.append(StageResult(stage=stage, outcome="not_processed", output=None, error=None))

    def _finish(
        self, run_id: str, results: list[StageResult], skip: list[StageName], crash_error: str | None = None
    ) -> IntelligenceRun:
        if skip:
            self._skip_remaining(run_id, results, skip)
        run = IntelligenceRun(run_id=run_id, results=results, crash_error=crash_error)
        logger.info(
            "pipeline_completed",
            extra={
                "event": "pipeline_completed",
                "outcome": run.outcome,
                "correlation_id": run_id,
                "context": {"stages_recorded": len(run.results)},
            },
        )
        return run
