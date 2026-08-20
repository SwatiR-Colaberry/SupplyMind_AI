"""Orchestrates PostgreSQL and Google Sheets integration for analysis.

Pulls every configured dataset and returns one result per dataset. A
failure on one dataset (source unavailable, bad format, auth failure) is
recorded and logged, not raised — so one missing source never blocks the
datasets that are available. This is what "available for analysis" and
"missing data is logged as an error" (the STORY-001 acceptance criteria)
actually mean in code.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Union

from data_integration import postgres_connector, sheets_connector
from data_integration.audit_trail import AuditStore, AuditTrailWriteError
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


def _content_fingerprint(rows: list[dict[str, Any]]) -> str:
    """Deterministic fingerprint of fetched rows, used as the idempotency key.

    Two runs that fetch identical rows for the same dataset must fingerprint
    identically, so a rerun is recognized as a reprocessing of the same
    data rather than new data. Row order is not part of dataset identity —
    the sample queries have no ORDER BY, so the same underlying rows can
    come back in a different order across runs — so rows are sorted into a
    canonical order before hashing.
    """
    canonical_rows = sorted(json.dumps(row, sort_keys=True, default=str) for row in rows)
    payload = json.dumps(canonical_rows)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def run_integration_with_audit(
    datasets: list[Dataset], audit_store: AuditStore
) -> list[DatasetResult]:
    """run_integration(), plus one audit-trail record per dataset attempt.

    Successful attempts are keyed on dataset name + a content fingerprint
    of the rows fetched, so reprocessing identical data does not create a
    duplicate audit entry. Failed attempts are keyed on a fresh id every
    time instead — a repeated failure is itself diagnostic information
    (e.g. "this source has been down for 3 runs") and must not be
    collapsed away by dedup.

    An audit-trail write failure for one dataset is caught and logged, not
    raised — the same failure-isolation guarantee run_integration() gives
    per-dataset fetch failures applies here too, so a broken audit trail
    for one dataset can't discard the already-fetched results for every
    other dataset in this run.
    """
    results = run_integration(datasets)
    for result in results:
        if result.outcome == "success":
            key = f"{result.name}:{_content_fingerprint(result.rows)}"
        else:
            key = f"{result.name}:error:{uuid.uuid4()}"
        try:
            audit_store.record(
                idempotency_key=key,
                dataset=result.name,
                source_type=result.source_type,
                outcome=result.outcome,
                row_count=len(result.rows),
                error=result.error,
            )
        except AuditTrailWriteError as exc:
            logger.error(
                "audit_trail_unavailable",
                extra={
                    "event": "audit_trail_unavailable",
                    "source": result.source_type,
                    "outcome": "failure",
                    "error_class": exc.__class__.__name__,
                    "context": {"dataset": result.name},
                },
            )
    return results
