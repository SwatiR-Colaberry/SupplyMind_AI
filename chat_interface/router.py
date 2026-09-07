"""Deterministic query-to-topic classification for the chat interface (STORY-010 / REQ-016).

Pure computation - no I/O, no model call. Per CLAUDE.md's core principle
("LLMs are probabilistic, production systems must be deterministic"), a
user's free-text query is matched against a fixed keyword table rather
than sent to an LLM: the same keyword-in-text approach this repo already
uses for deterministic scoring elsewhere (risk_detection/risk_score.py,
supplier_evaluation/reliability.py), applied here to intent routing
instead of numeric scoring.

Each topic this module recognizes corresponds 1:1 to a `metric_id` on a
STORY-009 `dashboard.metrics.DashboardMetric` tile (the agent's own
`name`) - see dashboard/metrics.py's `_TILE_LABELS`. This module does not
import dashboard.metrics: the caller (chat_interface/chat.py) is the one
that actually holds a DashboardSnapshot and looks a topic up in it, so
this module only needs to agree with dashboard/metrics.py on the string
values, not on its types - the same "depend on the shared contract, not
on another module's internals" boundary dashboard/metrics.py itself
holds toward the agent fleet.
"""

from __future__ import annotations

# Table order does not affect matching (see classify_query's
# longest-match rule below) - grouped by topic only for readability.
# risk_detection_agent's keywords are deliberately short and generic
# ("risk", "anomaly") since every other topic already claims the more
# specific phrasing for its own domain.
_TOPIC_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("demand_forecasting_agent", ("demand forecast", "forecast", "demand")),
    ("stockout_risk_agent", ("stockout", "stock out", "out of stock", "inventory risk")),
    ("supplier_evaluation_agent", ("supplier", "vendor")),
    ("shipment_delay_analysis_agent", ("shipment delay", "shipment", "delivery delay", "late delivery", "freight")),
    ("data_quality_monitoring_agent", ("data quality", "data issue")),
    ("recommendation_agent", ("recommend", "what should i do", "suggestion")),
    ("risk_detection_agent", ("risk", "anomaly", "anomalies")),
)

# Human-readable phrasing for each topic, used to tell the user what the
# chat interface *can* answer when a query matches none of them (AC2:
# "notify the user of limitations"). Kept separate from dashboard/
# metrics.py's own `_TILE_LABELS` (executive-facing tile names) since this
# is user-facing chat copy, not a dashboard label, even where the wording
# happens to be similar.
TOPIC_DESCRIPTIONS: dict[str, str] = {
    "demand_forecasting_agent": "demand forecasts",
    "stockout_risk_agent": "stockout / inventory risk",
    "supplier_evaluation_agent": "supplier reliability and risk",
    "shipment_delay_analysis_agent": "shipment delays and their cost",
    "data_quality_monitoring_agent": "data quality",
    "recommendation_agent": "overall recommendations",
    "risk_detection_agent": "general supply chain risk and anomalies",
}


def classify_query(query_text: str) -> str | None:
    """Return the `metric_id` topic `query_text` matches, or None if unsupported.

    Handles: an empty or whitespace-only query (returns None - the
    "Unsupported query errors" failure path, not a crash); case
    differences (matching is case-insensitive); a query whose keywords for
    more than one topic are both present (e.g. "is our supplier causing
    shipment delays?" matches both "supplier" and "shipment delay") - the
    *longest* matching keyword wins, on the assumption that a longer,
    more specific phrase reflects the query's actual subject better than a
    shorter, more generic one. This was a real bug in an earlier version
    of this function, which instead picked whichever topic happened to be
    listed first in `_TOPIC_KEYWORDS` regardless of match specificity.

    This is a keyword heuristic, not real intent understanding - a query
    that genuinely, equally concerns two topics (e.g. "does our demand
    forecast have data quality issues?") still resolves to exactly one of
    them (whichever keyword is longer) rather than answering both. That
    residual ambiguity is an accepted limitation of deterministic
    substring matching, not something this function tries to fully solve;
    per CLAUDE.md's core determinism principle, resolving it "correctly"
    for every phrasing would mean reaching for an LLM, which is exactly
    what this module exists to avoid.
    """
    normalized = (query_text or "").strip().lower()
    if not normalized:
        return None
    best_topic: str | None = None
    best_length = -1
    for topic, keywords in _TOPIC_KEYWORDS:
        for keyword in keywords:
            if len(keyword) > best_length and keyword in normalized:
                best_topic = topic
                best_length = len(keyword)
    return best_topic
