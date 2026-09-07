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

from dataclasses import dataclass
from typing import Literal

from chat_interface.router import TOPIC_DESCRIPTIONS, classify_query
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
    topic: str | None = None  # the matched metric_id, or None if unsupported
    source_agent: str | None = None
    confidence: float | None = None


def _metric_for_topic(snapshot: DashboardSnapshot, topic: str) -> DashboardMetric | None:
    for metric in snapshot.metrics:
        if metric.metric_id == topic:
            return metric
    return None


def _unsupported_response(query_text: str) -> ChatResponse:
    supported = ", ".join(sorted(TOPIC_DESCRIPTIONS.values()))
    return ChatResponse(
        query_text=query_text,
        status="unsupported",
        answer=_UNSUPPORTED_ANSWER_PREFIX + supported + ".",
    )


def answer_query(query_text: str, snapshot: DashboardSnapshot) -> ChatResponse:
    """Answer `query_text` from `snapshot`'s already-computed tiles.

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
    )
