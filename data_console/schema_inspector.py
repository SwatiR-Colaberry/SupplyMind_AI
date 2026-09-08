"""Reads what tables and columns actually exist in a connected Postgres database.

Reuses data_integration/postgres_connector.py's fetch_rows() rather than
opening a second connection path - the same retry/timeout/error-class
behavior every other Postgres read in this repo already gets
(PostgresIntegrationError on exhausted retries or a malformed query) applies
here unchanged. This module's own job is narrow: turn the standard
information_schema tables every Postgres database already exposes into the
typed shapes the data-console UI wants, nothing more.

Column names are read back as plain data (via a parameterized query - the
table name a caller passes in is a query *parameter*, never interpolated
into SQL text), so an unknown table name just comes back as zero rows, not
an error - the caller decides what "table not found" should look like.
"""

from __future__ import annotations

from dataclasses import dataclass

from data_integration.postgres_connector import fetch_rows

_LIST_TABLES_QUERY = """
    SELECT table_name
    FROM information_schema.tables
    WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
    ORDER BY table_name
"""

_LIST_COLUMNS_QUERY = """
    SELECT column_name, data_type, is_nullable
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = %s
    ORDER BY ordinal_position
"""


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    data_type: str
    nullable: bool


def list_tables() -> list[str]:
    """Every base table in the connected database's public schema, alphabetically."""
    rows = fetch_rows(_LIST_TABLES_QUERY)
    return [row["table_name"] for row in rows]


def list_columns(table_name: str) -> list[ColumnInfo]:
    """This table's columns in their real on-disk order. Empty if the table doesn't exist."""
    rows = fetch_rows(_LIST_COLUMNS_QUERY, params=(table_name,))
    return [
        ColumnInfo(name=row["column_name"], data_type=row["data_type"], nullable=row["is_nullable"] == "YES")
        for row in rows
    ]
