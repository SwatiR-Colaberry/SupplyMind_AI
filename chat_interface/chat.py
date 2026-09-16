"""Pure query-answering logic for the chat interface (STORY-010 / REQ-016).

Ties router.classify_query() (deterministic intent routing) to a STORY-009
dashboard.metrics.DashboardSnapshot (the agent fleet's already-computed
results): a chat query never re-runs analysis or calls an LLM, it looks up
the answer the agent fleet already produced, the same "reuse, don't
reinvent" precedent scenario_simulation/simulation.py and
root_cause/analysis.py set for reusing existing computation. No I/O here -
see chat_interface/audit_trail.py for the durable trust-spine record and
chat_interface/evaluator.py for the orchestration boundary that catches
unexpected crashes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from chat_interface.router import TOPIC_DESCRIPTIONS, classify_query, find_mentioned_subject
from dashboard.charts import ChartSpec, build_chart_specs
from dashboard.metrics import DashboardMetric, DashboardSnapshot

ChatResponseStatus = Literal["answered", "unsupported", "data_unavailable"]

_UNSUPPORTED_ANSWER_PREFIX = "I don't have an answer for that yet. I can answer questions about: "


class ChatQueryError(ValueError):
    """Raised when answer_query()'s own parameters can't support answering.

    Not the same failure path as an unsupported *query* (a caller/parameter
    bug - a missing or malformed snapshot - rather than a user's question
    this chat interface simply doesn't cover), the same division every
    other *Error class in this repo draws between a bad parameter and bad
    input data.
    """


@dataclass(frozen=True)
class ChatResponse:
    query_text: str
    status: ChatResponseStatus
    answer: str
    topic: str | None = None  # the matched metric_id, or None if unsupported or a per-entity answer
    source_agent: str | None = None
    confidence: float | None = None
    # Set only for a per-entity answer (a specific SKU/PO/supplier/period
    # named in the query, e.g. "why is SKU-1 at risk?") - mutually
    # exclusive with `topic`, since an entity answer can span findings
    # from several agents at once rather than belonging to just one.
    subject: str | None = None
    # Reuses dashboard/charts.py's own per-(agent, subject_kind) "explore"
    # charts - the same ones the dashboard's own chart section builds from
    # this metric's findings - rather than building a second, chat-specific
    # chart pipeline. Only ever populated for status="answered": an
    # unsupported/data_unavailable answer has no metric to chart from.
    # Always empty for a metric with no per-subject findings (e.g. a
    # demand forecast run against data with no per-item grouping field
    # mapped, so only the aggregate total is available).
    chart_specs: list[ChartSpec] = field(default_factory=list)


def _metric_for_topic(snapshot: DashboardSnapshot, topic: str) -> DashboardMetric | None:
    for metric in snapshot.metrics:
        if metric.metric_id == topic:
            return metric
    return None


def _charts_for_metric(snapshot: DashboardSnapshot, metric_id: str) -> list[ChartSpec]:
    """The subset of build_chart_specs()'s "explore" charts that belong to one metric.

    build_chart_specs() operates on a whole DashboardSnapshot (it also
    returns cross-agent "quick review" charts, e.g. confidence-by-area,
    that make no sense for a single chat answer) - explore chart_ids are
    always f"{metric_id}-{subject_kind}" (see dashboard/charts.py's
    _explore_charts), so filtering on that prefix picks out just this
    metric's own chart(s) without needing a second, chat-specific entry
    point into that module.
    """
    _, explore_charts = build_chart_specs(snapshot)
    prefix = f"{metric_id}-"
    return [spec for spec in explore_charts if spec.chart_id.startswith(prefix)]


def _known_subjects(snapshot: DashboardSnapshot) -> set[str]:
    return {finding.subject for metric in snapshot.metrics for finding in metric.findings}


def _entity_response(query_text: str, snapshot: DashboardSnapshot, subject: str) -> ChatResponse:
    """Answer about one specific SKU/PO/supplier/period named in the query.

    Gathers every metric's finding for `subject` (not just the topic a
    keyword match would have picked) - a stockout-risk finding and a
    root-cause causal-chain finding can both name the same SKU, and both
    are relevant to "why is SKU-1 at risk?" in a way a single-topic
    answer can't express. Each finding's `.detail` already carries the
    full "Evidence -> Calculation -> Recommendation" text the underlying
    agent computed (see agents/stockout_risk_agent.py and
    agents/root_cause_agent.py), so this function only orders and joins
    them - it does not recompute or reword anything.
    """
    lines = [
        f"{metric.label}: {finding.detail}"
        for metric in snapshot.metrics
        for finding in metric.findings
        if finding.subject == subject
    ]
    return ChatResponse(
        query_text=query_text,
        status="answered",
        answer=f"{subject} - " + " | ".join(lines),
        subject=subject,
    )


def _unsupported_response(query_text: str) -> ChatResponse:
    supported = ", ".join(sorted(TOPIC_DESCRIPTIONS.values()))
    return ChatResponse(
        query_text=query_text,
        status="unsupported",
        answer=_UNSUPPORTED_ANSWER_PREFIX + supported + ".",
    )


def answer_query(query_text: str, snapshot: DashboardSnapshot) -> ChatResponse:
    """Answer `query_text` from `snapshot`'s already-computed tiles.

    First checks whether `query_text` names one specific subject already
    present in this snapshot's findings (a SKU, PO, supplier, or period -
    e.g. "why is SKU-1 at risk?") via find_mentioned_subject(); if so,
    answers with that subject's own findings across every metric
    (status="answered", `subject` set, `topic` left None) rather than
    falling through to topic routing at all - a genuinely more specific
    answer takes priority over a generic per-topic headline. Only when no
    single subject is (unambiguously) named does this fall back to
    classify_query()'s per-topic routing, described below.

    Handles: an unsupported query - classify_query() found no matching
    topic, including a blank query (AC2's "unsupported query... notify the
    user of limitations" failure path) - answered with status="unsupported"
    and a message naming every topic this chat interface does support,
    never a crash or a silent empty answer. A recognized topic with no
    corresponding tile in this particular snapshot (that agent wasn't part
    of the fleet this snapshot was built from) or a tile that failed to
    process (status="error") both answer with status="data_unavailable"
    rather than fabricating an answer or raising - the "Data processing
    errors" failure path this story names explicitly.

    Raises ChatQueryError if `snapshot` is not a DashboardSnapshot - a
    caller/parameter bug, not a data problem this function can route
    around.
    """
    if not isinstance(snapshot, DashboardSnapshot):
        raise ChatQueryError(f"expected DashboardSnapshot, got {type(snapshot).__name__}")

    mentioned_subject = find_mentioned_subject(query_text, _known_subjects(snapshot))
    if mentioned_subject is not None:
        return _entity_response(query_text, snapshot, mentioned_subject)

    topic = classify_query(query_text)
    if topic is None:
        return _unsupported_response(query_text)

    metric = _metric_for_topic(snapshot, topic)
    if metric is None:
        return ChatResponse(
            query_text=query_text,
            status="data_unavailable",
            answer=f"I don't have any {TOPIC_DESCRIPTIONS[topic]} data available right now.",
            topic=topic,
        )

    if metric.status == "error":
        return ChatResponse(
            query_text=query_text,
            status="data_unavailable",
            answer=f"I can't answer that right now - {metric.label} data could not be processed: {metric.headline}",
            topic=topic,
            source_agent=metric.source_agent,
        )

    return ChatResponse(
        query_text=query_text,
        status="answered",
        answer=f"{metric.label}: {metric.headline}",
        topic=topic,
        source_agent=metric.source_agent,
        confidence=metric.confidence,
        chart_specs=_charts_for_metric(snapshot, topic),
    )
