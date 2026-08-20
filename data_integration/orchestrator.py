"""Orchestrates PostgreSQL and Google Sheets integration for analysis.

Pulls every configured dataset and returns one result per dataset. A
failure on one dataset (source unavailable, bad format, auth failure) is
recorded and logged, not raised — so one missing source never blocks the
datasets that are available. This is what "available for analysis" and
"missing data is logged as an error" (the STORY-001 acceptance criteria)
actually mean in code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Union

from data_integration import postgres_connector, sheets_connector
from data_integration.logging_setup import get_logger

logger = get_logger()

SourceType = Literal["postgresql", "google_sheets"]


@dataclass(frozen=True)
class PostgresDataset:
    name: str
    query: str
    source_type: SourceType = "postgresql"


@dataclass(frozen=True)
class SheetsDataset:
    name: str
    spreadsheet_id: str
    worksheet_name: str
    source_type: SourceType = "google_sheets"


Dataset = Union[PostgresDataset, SheetsDataset]


@dataclass
class DatasetResult:
    name: str
    source_type: SourceType
    outcome: Literal["success", "failure"]
    rows: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


def _fetch(dataset: Dataset) -> list[dict[str, Any]]:
    if isinstance(dataset, PostgresDataset):
        return postgres_connector.fetch_rows(dataset.query)
    if isinstance(dataset, SheetsDataset):
        return sheets_connector.fetch_rows(dataset.spreadsheet_id, dataset.worksheet_name)
    raise TypeError(f"Unknown dataset type: {type(dataset).__name__}")


def run_integration(datasets: list[Dataset]) -> list[DatasetResult]:
    """Pull every dataset, isolating failures so one bad source can't sink the run.

    Handles: connector-raised integration errors (source unavailable,
    auth failure, malformed query/worksheet) and missing configuration
    (unset env vars). Anything else is still caught here, at the
    per-dataset boundary, specifically so a single dataset failure can't
    abort datasets that would otherwise succeed — the error class is
    always preserved in the log, never masked.
    """
    results: list[DatasetResult] = []
    for dataset in datasets:
        try:
            rows = _fetch(dataset)
            results.append(
                DatasetResult(
                    name=dataset.name,
                    source_type=dataset.source_type,
                    outcome="success",
                    rows=rows,
                )
            )
        except Exception as exc:
            logger.error(
                "dataset_integration_failed",
                extra={
                    "event": "dataset_integration_failed",
                    "source": dataset.source_type,
                    "outcome": "failure",
                    "error_class": exc.__class__.__name__,
                    "context": {"dataset": dataset.name},
                },
            )
            results.append(
                DatasetResult(
                    name=dataset.name,
                    source_type=dataset.source_type,
                    outcome="failure",
                    error=str(exc),
                )
            )
    return results


def available_for_analysis(results: list[DatasetResult]) -> dict[str, list[dict[str, Any]]]:
    """The subset of pulled data that is ready to analyze — successes only."""
    return {r.name: r.rows for r in results if r.outcome == "success"}
