"""Validates, saves, clears, and previews this console's dataset mappings.

Bridges data_console's own MappingStore (what's saved, and where) with
data_integration.connection_profile's real validation/fetch/remap engine
- the same engine risk_detection/run_sample_multi_tenant_risk_detection.py
already proves against this exact "differently-named columns for the
same canonical dataset" scenario. This module adds no new validation
logic of its own; it only wires the two together and turns
connection_profile's typed exceptions into results
serve_data_console.py can turn into HTTP responses.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import data_console
from data_console.mapping_store import DatasetMapping, MappingStore
from data_console.query_builder import PREVIEW_ROW_LIMIT, quote_identifier
from data_console.raw_query_validator import validate_read_only_query
from data_integration.audit_trail import AuditStore
from data_integration.config import load_postgres_config
from data_integration.connection_profile import ConnectionProfile, fetch_profile_data, validate_profile
from data_integration.postgres_connector import fetch_columns

DEFAULT_AUDIT_LOG_PATH = Path(data_console.__file__).resolve().parent / "mapping_audit_log.jsonl"

# A stable, single tenant id - this console maps exactly one connected
# data source at a time (whether Postgres or an uploaded file), so there
# is no real multi-tenant identity to thread through here the way
# risk_detection's own multi-tenant demo has one per fictitious company.
_TENANT_ID = "data_console"


def _audit_store() -> AuditStore:
    return AuditStore(DEFAULT_AUDIT_LOG_PATH)


def _build_profile(dataset_kind: str, query: str, column_mapping: dict[str, str]) -> ConnectionProfile:
    return ConnectionProfile(
        tenant_id=_TENANT_ID,
        dataset_kind=dataset_kind,
        postgres=load_postgres_config(),
        query=query,
        column_mapping=column_mapping,
    )


def _table_query(table: str) -> str:
    return f"SELECT * FROM {quote_identifier(table)}"


def save_mapping(dataset_kind: str, table: str, column_mapping: dict[str, str], store: MappingStore | None = None) -> None:
    """Validates via the real connection_profile engine, then persists.

    Raises SchemaMappingError (incomplete mapping, or a mapped column
    that doesn't actually exist), MissingConfigError, or
    PostgresIntegrationError - nothing is ever saved unless validation
    against the live database passes.
    """
    profile = _build_profile(dataset_kind, _table_query(table), column_mapping)
    validate_profile(profile)
    (store or MappingStore()).save(
        dataset_kind,
        DatasetMapping(status="mapped", table=table, column_mapping=column_mapping, source_kind="table"),
    )


def probe_query_columns(raw_query: str) -> list[str]:
    """Returns the column names `raw_query` would produce, without fetching any rows.

    Raises UnsafeQueryError if the query fails the read-only guardrail
    (checked before it ever reaches the database), or
    MissingConfigError/PostgresIntegrationError - including a genuine SQL
    syntax error, which surfaces through PostgresIntegrationError the same
    way any other malformed query does. Used to populate the mapping
    picker's column dropdowns from a typed query the same way
    schema_inspector.list_columns() populates them from a picked table.
    """
    cleaned = validate_read_only_query(raw_query)
    return fetch_columns(cleaned, config=load_postgres_config())


def save_mapping_from_query(
    dataset_kind: str, raw_query: str, column_mapping: dict[str, str], store: MappingStore | None = None
) -> None:
    """The raw-SQL escape hatch: same validate-before-persist contract as save_mapping(),
    for a dataset whose required columns are split across more tables than the guided
    picker's single table (and, in the guided flow, no join at all) can reach.

    Raises UnsafeQueryError (query fails the read-only guardrail), SchemaMappingError,
    MissingConfigError, or PostgresIntegrationError - nothing is ever saved unless both
    the guardrail and the live-schema validation pass.
    """
    cleaned = validate_read_only_query(raw_query)
    profile = _build_profile(dataset_kind, cleaned, column_mapping)
    validate_profile(profile)
    (store or MappingStore()).save(
        dataset_kind,
        DatasetMapping(status="mapped", query=cleaned, column_mapping=column_mapping, source_kind="query"),
    )


def mark_unavailable(dataset_kind: str, store: MappingStore | None = None) -> None:
    """Records "this data genuinely isn't in this database" - distinct from "not yet
    mapped" - so the rest of the system can proceed with whatever datasets are
    available instead of endlessly prompting for one that will never exist here."""
    (store or MappingStore()).save(dataset_kind, DatasetMapping(status="unavailable"))


def clear_mapping(dataset_kind: str, store: MappingStore | None = None) -> None:
    """Resets a dataset back to "not mapped yet" - lets a mapped-or-unavailable
    decision be redone, e.g. after the underlying database changes."""
    (store or MappingStore()).clear(dataset_kind)


@dataclass(frozen=True)
class MappingPreviewResult:
    rows: list[dict]
    row_count: int
    truncated: bool


def preview_mapping(dataset_kind: str, store: MappingStore | None = None) -> MappingPreviewResult:
    """Raises LookupError if this dataset has no saved mapping to preview,
    otherwise the same exceptions save_mapping()/save_mapping_from_query() can raise
    (for a "table"/"query" mapping), file_mapping_service.preview_file_mapping() can
    raise (for a "file" mapping), or sheet_mapping_service.preview_sheet_mapping()
    can raise (for a "sheet" mapping)."""
    mapping = (store or MappingStore()).get(dataset_kind)
    if mapping is None or mapping.status != "mapped":
        raise LookupError(f"{dataset_kind!r} has no saved mapping to preview")
    if mapping.source_kind == "table" and mapping.table is None:
        raise LookupError(f"{dataset_kind!r} has no saved mapping to preview")
    if mapping.source_kind == "query" and mapping.query is None:
        raise LookupError(f"{dataset_kind!r} has no saved mapping to preview")
    if mapping.source_kind == "file" and mapping.file_id is None:
        raise LookupError(f"{dataset_kind!r} has no saved mapping to preview")
    if mapping.source_kind == "sheet" and mapping.sheet_url is None:
        raise LookupError(f"{dataset_kind!r} has no saved mapping to preview")

    # No Postgres connection at all for a file- or sheet-backed mapping -
    # both are genuinely separate engines, not branches of the Postgres one
    # below (see each module's own docstring for why connection_profile.py's
    # Postgres-shaped ConnectionProfile doesn't fit either case).
    if mapping.source_kind == "file":
        from data_console.file_mapping_service import preview_file_mapping

        return preview_file_mapping(dataset_kind, mapping)
    if mapping.source_kind == "sheet":
        from data_console.sheet_mapping_service import preview_sheet_mapping

        return preview_sheet_mapping(dataset_kind, mapping)

    base_query = _table_query(mapping.table) if mapping.source_kind == "table" else mapping.query
    profile = _build_profile(dataset_kind, base_query, mapping.column_mapping)
    # fetch_profile_data() itself has no LIMIT concept (a real pull is
    # meant to get everything the query asks for) - capped here, the same
    # PREVIEW_ROW_LIMIT every other preview in this console already uses,
    # rather than inside connection_profile.py, which is shared,
    # pre-existing code this slice doesn't own. Wrapped as a subquery
    # (the same technique postgres_connector.fetch_columns() already uses
    # for its own schema probe) rather than string-concatenated onto the
    # end - a raw query can legally end in its own ORDER BY, a trailing
    # comment, or anything else that would make a bare `+ " LIMIT n"`
    # either invalid or, worse, silently swallowed by a trailing `--`
    # comment, defeating the row cap entirely.
    capped_profile = ConnectionProfile(
        tenant_id=profile.tenant_id,
        dataset_kind=profile.dataset_kind,
        postgres=profile.postgres,
        query=f"SELECT * FROM ({profile.query}) AS mapping_preview LIMIT {PREVIEW_ROW_LIMIT}",
        column_mapping=profile.column_mapping,
    )
    rows = fetch_profile_data(capped_profile, _audit_store())
    return MappingPreviewResult(rows=rows, row_count=len(rows), truncated=len(rows) == PREVIEW_ROW_LIMIT)
