"""Shared pure validation for a dataset mapping's column_mapping, regardless of
where its "real columns" came from - an uploaded file, a fetched Google Sheet,
or any other non-Postgres source added later.

Raises the same SchemaMappingError connection_profile.py's own Postgres-backed
validate_against_live_schema() raises for the same two failure shapes (a
required field with no mapping; a mapped column that doesn't actually exist),
so every source kind's caller shares one exception type - serve_data_console.py
needs no per-source-kind branch to turn this into a 400 response.
"""

from __future__ import annotations

from data_integration.connection_profile import REQUIRED_FIELDS_BY_DATASET_KIND, SchemaMappingError


def validate_column_mapping(
    dataset_kind: str,
    real_columns: list[str],
    column_mapping: dict[str, str],
    unavailable_fields: frozenset[str] = frozenset(),
) -> None:
    """`unavailable_fields` are canonical field names explicitly declared as
    genuinely absent from this source's real columns - exempt from the
    "every required field must be mapped" check below, the same exemption
    connection_profile.validate_mapping_completeness() grants a Postgres-backed
    profile for the identical reason."""
    required = REQUIRED_FIELDS_BY_DATASET_KIND[dataset_kind]
    missing = [f for f in required if f not in unavailable_fields and not column_mapping.get(f)]
    if missing:
        raise SchemaMappingError(
            f"dataset {dataset_kind!r} is missing a column mapping for required field(s): {', '.join(missing)}"
        )
    real_column_set = set(real_columns)
    not_found = [
        f"{canonical} -> '{actual}'" for canonical, actual in column_mapping.items() if actual not in real_column_set
    ]
    if not_found:
        raise SchemaMappingError(
            f"dataset {dataset_kind!r}: mapped column(s) not found in the real columns: {', '.join(not_found)}"
        )
