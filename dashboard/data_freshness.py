"""Turns a completed data_integration pull into per-dataset freshness entries (STORY-009 / REQ-015).

A separate module from dashboard/metrics.py on purpose: metrics.py's own
docstring states it depends only on the shared AgentResponse/
AgentFinding/CoordinationResult contracts, never on any one agent's (or,
here, any one data source's) internal types - importing
data_integration.orchestrator.DatasetResult there would break that
boundary. This module is the one place that bridges STORY-001/011's data
pull results into something the dashboard can display and audit.

`data_integration.audit_trail.AuditStore` has no method to list its past
records back out (only `has_processed()`), so "when was this dataset
last pulled" can't be read from its persisted trail without modifying a
file outside this story - not done here (see this story's own "stop and
ask before changing a file outside this story" rule). Instead, this
builds freshness entries straight from `run_integration_with_audit()`'s
own return value (`list[DatasetResult]`) at the moment the caller pulls
it, which is the freshest true statement this module can make about "is
this dashboard looking at current data."
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from data_integration.orchestrator import DatasetResult

PullOutcome = Literal["success", "failure"]


@dataclass(frozen=True)
class DataFreshnessEntry:
    dataset: str
    source_type: str
    outcome: PullOutcome
    pulled_at: str  # ISO-8601 UTC
    row_count: int = 0
    error: str | None = None


def build_data_freshness(
    dataset_results: list[DatasetResult], pulled_at: str | None = None
) -> list[DataFreshnessEntry]:
    """Pure. One DataFreshnessEntry per dataset in `dataset_results`.

    `pulled_at` defaults to now - a caller passing its own value (e.g.
    the timestamp it captured right before calling
    run_integration_with_audit()) gets a more precise record; omitting
    it still produces a truthful "as of roughly now" entry rather than
    requiring every caller to thread a clock through.
    """
    pulled_at = pulled_at or datetime.now(timezone.utc).isoformat()
    return [
        DataFreshnessEntry(
            dataset=result.name,
            source_type=result.source_type,
            outcome=result.outcome,
            pulled_at=pulled_at,
            row_count=len(result.rows),
            error=result.error,
        )
        for result in dataset_results
    ]
