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
from data_integration.audit_trail import AuditStore
from data_integration.config import load_postgres_config
from data_integration.connection_profile import ConnectionProfile, fetch_profile_data, validate_profile

DEFAULT_AUDIT_LOG_PATH = Path(data_console.__file__).resolve().parent / "mapping_audit_log.jsonl"

# A stable, single tenant id - this console maps exactly one connected
# database at a time (Slice 4's CSV/Sheets sources will need their own
# story), so there is no real multi-tenant identity to thread through
# here the way risk_detection's own multi-tenant demo has one per
# fictitious company.
_TENANT_ID = "data_console"


def _audit_store() -> AuditStore:
    return AuditStore(DEFAULT_AUDIT_LOG_PATH)


def _build_profile(dataset_kind: str, table: str, column_mapping: dict[str, str]) -> ConnectionProfile:
    query = f"SELECT * FROM {quote_identifier(table)}"
    return ConnectionProfile(
        tenant_id=_TENANT_ID,
        dataset_kind=dataset_kind,
        postgres=load_postgres_config(),
        query=query,
        column_mapping=column_mapping,
    )


def save_mapping(dataset_kind: str, table: str, column_mapping: dict[str, str], store: MappingStore | None = None) -> None:
    """Validates via the real connection_profile engine, then persists.

    Raises SchemaMappingError (incomplete mapping, or a mapped column
    that doesn't actually exist), MissingConfigError, or
    PostgresIntegrationError - nothing is ever saved unless validation
    against the live database passes.
    """
    profile = _build_profile(dataset_kind, table, column_mapping)
    validate_profile(profile)
    (store or MappingStore()).save(
        dataset_kind, DatasetMapping(status="mapped", table=table, column_mapping=column_mapping)
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
    otherwise the same exceptions save_mapping() can raise."""
    mapping = (store or MappingStore()).get(dataset_kind)
    if mapping is None or mapping.status != "mapped" or mapping.table is None:
        raise LookupError(f"{dataset_kind!r} has no saved mapping to preview")

    profile = _build_profile(dataset_kind, mapping.table, mapping.column_mapping)
    # fetch_profile_data() itself has no LIMIT concept (a real pull is
    # meant to get everything the query asks for) - capped here, the same
    # PREVIEW_ROW_LIMIT every other preview in this console already uses,
    # rather than inside connection_profile.py, which is shared,
    # pre-existing code this slice doesn't own.
    capped_profile = ConnectionProfile(
        tenant_id=profile.tenant_id,
        dataset_kind=profile.dataset_kind,
        postgres=profile.postgres,
        query=f"{profile.query} LIMIT {PREVIEW_ROW_LIMIT}",
        column_mapping=profile.column_mapping,
    )
    rows = fetch_profile_data(capped_profile, _audit_store())
    return MappingPreviewResult(rows=rows, row_count=len(rows), truncated=len(rows) == PREVIEW_ROW_LIMIT)
