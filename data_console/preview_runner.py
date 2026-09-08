"""Fetches a fresh live schema for a selection's tables, then builds and runs its preview query.

Keeps query_builder.py's validate_selection() as the single source of
truth for "is this selection safe to run" - this module's only added
responsibility is turning a client-submitted QuerySelection into a
`{table: {columns}}` snapshot read from the database right now (never
trusted from whatever the browser last saw, since the schema could have
changed since the page loaded) and then executing the query
query_builder.py builds from it.
"""

from __future__ import annotations

from dataclasses import dataclass

from data_console import schema_inspector
from data_console.query_builder import PREVIEW_ROW_LIMIT, QuerySelection, build_preview_query
from data_integration.postgres_connector import fetch_rows


@dataclass(frozen=True)
class PreviewResult:
    sql: str
    rows: list[dict]
    row_count: int
    truncated: bool  # there may be more matching rows than PREVIEW_ROW_LIMIT actually shown


def _live_schema(tables: set[str]) -> dict[str, set[str]]:
    """Fresh column names per table, read right now. A table with zero columns back is treated as
    not existing, rather than as an empty-but-real table, so query_builder's "table doesn't exist"
    error fires with the right message instead of a confusing "column doesn't exist" one."""
    schema: dict[str, set[str]] = {}
    for table in tables:
        columns = schema_inspector.list_columns(table)
        if columns:
            schema[table] = {c.name for c in columns}
    return schema


def run_preview(selection: QuerySelection) -> PreviewResult:
    """Validates and runs `selection`. Raises InvalidSelectionError, MissingConfigError, or
    PostgresIntegrationError - the caller (serve_data_console.py) decides how each becomes a response."""
    tables = {selection.base_table}
    if selection.join is not None:
        tables.add(selection.join.left_table)
        tables.add(selection.join.right_table)

    schema = _live_schema(tables)
    query = build_preview_query(selection, schema)
    rows = fetch_rows(query)

    return PreviewResult(
        sql=query,
        rows=rows,
        row_count=len(rows),
        truncated=len(rows) == PREVIEW_ROW_LIMIT,
    )
