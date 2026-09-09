"""Validates, saves, and previews a dataset mapping sourced from a public
Google Sheets CSV link - no OAuth, no credentials, no local database.

Sibling to file_mapping_service.py, not a variant of it: unlike an uploaded
file, a sheet is fetched fresh over the network on every probe/validate/
preview call rather than saved once locally - the whole point of a live
Sheet link is that editing the source sheet should be reflected here, the
same "never trust a snapshot" principle every other source in this console
already follows (a live Postgres table's schema, an uploaded file's current
headers on disk). Nothing about a sheet's content is ever written to disk.
"""

from __future__ import annotations

import uuid

from data_console.file_store import parse_csv_columns, parse_csv_rows
from data_console.mapping_service import DEFAULT_AUDIT_LOG_PATH, MappingPreviewResult
from data_console.mapping_store import DatasetMapping, MappingStore
from data_console.mapping_validation import validate_column_mapping
from data_console.query_builder import PREVIEW_ROW_LIMIT
from data_console.sheet_fetcher import fetch_sheet_csv
from data_integration.audit_trail import AuditStore
from data_integration.connection_profile import remap_rows

# Same stable single-tenant id mapping_service.py/file_mapping_service.py
# each use for their own audit records - duplicated for the same reason
# file_mapping_service.py already documents: it's module-private there, and
# this console still maps exactly one connected data source at a time
# regardless of source kind, so there is no real multi-tenant identity to
# share beyond this one literal.
_TENANT_ID = "data_console"


def _audit_store() -> AuditStore:
    return AuditStore(DEFAULT_AUDIT_LOG_PATH)


def _fetch_columns_and_rows(url: str) -> tuple[list[str], list[dict[str, str]]]:
    # utf-8-sig tolerates (and strips) a leading byte-order-mark - the same
    # tolerance file_store._read_text() already applies to an uploaded file,
    # applied here since a sheet exported from Excel-adjacent tooling can
    # carry one too.
    text = fetch_sheet_csv(url).decode("utf-8-sig")
    return parse_csv_columns(text), parse_csv_rows(text)


def probe_sheet_columns(url: str) -> list[str]:
    """Returns the sheet's real header row, fetched fresh right now.

    Raises InvalidSheetUrlError or SheetFetchError.
    """
    columns, _ = _fetch_columns_and_rows(url)
    return columns


def save_mapping_from_sheet(
    dataset_kind: str,
    url: str,
    column_mapping: dict[str, str],
    unavailable_fields: frozenset[str] = frozenset(),
    store: MappingStore | None = None,
) -> None:
    """Validates every mapped column against the sheet's real current headers, then persists.

    `unavailable_fields` are required fields explicitly declared genuinely
    absent from this sheet - exempt from the completeness check (see
    mapping_validation.validate_column_mapping()'s own docstring).

    Raises InvalidSheetUrlError, SheetFetchError, or SchemaMappingError -
    nothing is ever saved unless validation against the live sheet passes.
    """
    columns, _ = _fetch_columns_and_rows(url)
    validate_column_mapping(dataset_kind, columns, column_mapping, unavailable_fields)
    (store or MappingStore()).save(
        dataset_kind,
        DatasetMapping(
            status="mapped",
            sheet_url=url,
            column_mapping=column_mapping,
            source_kind="sheet",
            unavailable_fields=sorted(unavailable_fields),
        ),
    )


def preview_sheet_mapping(dataset_kind: str, mapping: DatasetMapping) -> MappingPreviewResult:
    """Re-fetches and re-validates against the sheet's current headers (it could
    have been edited, or its sharing revoked, since Save Mapping), remaps, caps
    at PREVIEW_ROW_LIMIT, and records the same audit trail every other preview
    path records.

    Raises InvalidSheetUrlError, SheetFetchError, or SchemaMappingError.
    """
    columns, rows = _fetch_columns_and_rows(mapping.sheet_url)
    validate_column_mapping(dataset_kind, columns, mapping.column_mapping, frozenset(mapping.unavailable_fields))

    remapped = remap_rows(rows, mapping.column_mapping)
    truncated = len(remapped) > PREVIEW_ROW_LIMIT
    capped = remapped[:PREVIEW_ROW_LIMIT]

    dataset_label = f"{_TENANT_ID}:{dataset_kind}"
    _audit_store().record(
        idempotency_key=f"{dataset_label}:sheet:{uuid.uuid4()}",
        dataset=dataset_label,
        source_type="google_sheet",
        outcome="success",
        row_count=len(capped),
    )
    return MappingPreviewResult(rows=capped, row_count=len(capped), truncated=truncated)
