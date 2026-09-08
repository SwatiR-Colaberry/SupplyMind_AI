"""Validates, saves, and previews a dataset mapping sourced from an uploaded CSV
rather than a live Postgres table or query.

Sibling to mapping_service.py, not a branch inside it: connection_profile.py's
ConnectionProfile/validate_profile()/fetch_profile_data() are explicitly
Postgres-shaped (a ConnectionProfile always carries a PostgresConfig), so a
file-backed mapping - which has no database connection at all - needs its own
small validate/fetch path rather than being forced through that one. It still
reuses what's genuinely shared: REQUIRED_FIELDS_BY_DATASET_KIND (the same
canonical field list every source kind is checked against) and remap_rows()
(pure column-renaming, with no opinion about where the rows came from).

Raises SchemaMappingError for the same two failure shapes
connection_profile.validate_against_live_schema() already raises it for
(a required field with no mapping; a mapped column that doesn't actually
exist) - one exception type across all three source kinds, so
serve_data_console.py's existing SchemaMappingError -> 400 handling needs no
new branch for this one.
"""

from __future__ import annotations

import uuid

from data_console import file_store
from data_console.mapping_service import DEFAULT_AUDIT_LOG_PATH, MappingPreviewResult
from data_console.mapping_store import DatasetMapping, MappingStore
from data_console.mapping_validation import validate_column_mapping
from data_console.query_builder import PREVIEW_ROW_LIMIT
from data_integration.audit_trail import AuditStore
from data_integration.connection_profile import remap_rows

# Same stable single-tenant id mapping_service.py uses for its own audit
# records - duplicated rather than imported since it's module-private
# there (this console still maps exactly one connected data source at a
# time regardless of source kind, so there is no real multi-tenant
# identity to share beyond this one literal).
_TENANT_ID = "data_console"


def _audit_store() -> AuditStore:
    return AuditStore(DEFAULT_AUDIT_LOG_PATH)


def probe_upload_columns(file_id: str) -> list[str]:
    """Returns the uploaded file's real header row, read fresh from disk - never
    trusted from whatever the browser saw when the file was first uploaded.

    Raises UnknownUploadError if file_id doesn't correspond to a saved upload.
    """
    return file_store.read_upload_columns(file_id)


def save_mapping_from_file(
    dataset_kind: str, file_id: str, filename: str, column_mapping: dict[str, str], store: MappingStore | None = None
) -> None:
    """Validates every mapped column against the file's real current headers, then persists.

    Raises SchemaMappingError (incomplete mapping, or a mapped column that
    doesn't actually exist in the file) or UnknownUploadError - nothing is
    ever saved unless validation against the real file passes, the same
    validate-before-persist contract every other source kind follows.
    """
    real_columns = file_store.read_upload_columns(file_id)
    validate_column_mapping(dataset_kind, real_columns, column_mapping)
    (store or MappingStore()).save(
        dataset_kind,
        DatasetMapping(
            status="mapped", file_id=file_id, filename=filename, column_mapping=column_mapping, source_kind="file"
        ),
    )


def preview_file_mapping(dataset_kind: str, mapping: DatasetMapping) -> MappingPreviewResult:
    """Re-validates against the file's current headers (it could have been
    re-uploaded/changed since Save Mapping), remaps, caps at PREVIEW_ROW_LIMIT,
    and records the same audit trail every other preview path records.

    Raises SchemaMappingError or UnknownUploadError.
    """
    real_columns = file_store.read_upload_columns(mapping.file_id)
    validate_column_mapping(dataset_kind, real_columns, mapping.column_mapping)

    raw_rows = file_store.read_upload_rows(mapping.file_id)
    remapped = remap_rows(raw_rows, mapping.column_mapping)
    truncated = len(remapped) > PREVIEW_ROW_LIMIT
    capped = remapped[:PREVIEW_ROW_LIMIT]

    dataset_label = f"{_TENANT_ID}:{dataset_kind}"
    _audit_store().record(
        idempotency_key=f"{dataset_label}:file:{mapping.file_id}:{uuid.uuid4()}",
        dataset=dataset_label,
        source_type="csv_upload",
        outcome="success",
        row_count=len(capped),
    )
    return MappingPreviewResult(rows=capped, row_count=len(capped), truncated=truncated)
