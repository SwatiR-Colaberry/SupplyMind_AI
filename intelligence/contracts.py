"""Per-stage output contracts for the four-stage intelligence model (STORY-012 / REQ-003).

Observe -> Understand -> Predict -> Recommend. Each stage's output is a
plain, frozen dataclass with no behavior of its own, so intelligence/model.py's
pipeline logic (and its tests) can reason about stage data independently of
how a stage computed it. Predict and Recommend build on forecasting/'s
already-audited demand model (STORY-003) rather than introducing new
prediction math - see intelligence/model.py for how the stages are wired.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from forecasting.data_quality import DataQualityReport
from forecasting.demand_model import DemandPoint, ForecastResult

TrendDirection = Literal["increasing", "decreasing", "flat", "unknown"]
StageName = Literal["observe", "understand", "predict", "recommend"]
StageOutcome = Literal["success", "failure", "not_processed"]


@dataclass(frozen=True)
class Observation:
    """Output of the Observe stage: raw inputs structured into facts, no interpretation yet."""

    row_count: int
    points: list[DemandPoint]
    period_range: tuple[str, str] | None  # (earliest, latest); None when points is empty


@dataclass(frozen=True)
class Understanding:
    """Output of the Understand stage: what the observed data means."""

    quality: DataQualityReport
    trend_direction: TrendDirection
    insights: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Prediction:
    """Output of the Predict stage: a forecast derived from the understanding."""

    forecast: ForecastResult


@dataclass(frozen=True)
class Recommendation:
    """Output of the Recommend stage: an action derived from the prediction."""

    action: str
    rationale: str


class StageError(ValueError):
    """Raised by a stage function when it cannot produce its output from its input.

    Distinct from an unexpected exception: a StageError means the stage ran
    and made a deterministic judgment call that it cannot proceed (e.g. no
    rows to observe, or too little history to predict from) - that is the
    "incorrect model predictions" / "data not processed through all stages"
    failure path, and intelligence/model.py stops the pipeline there rather
    than letting a later stage run on missing input.
    """
