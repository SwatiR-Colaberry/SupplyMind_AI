"""Pure logic: turn a user's column+join selection into a safe, capped SQL query.

No I/O here - see preview_runner.py for fetching the live schema this
module validates a selection against, and for actually executing the
query it builds. Kept separate so query construction and its many edge
cases (unknown table/column, a join that leaves out the base table, no
columns picked at all) are testable without a database at all - the
same split chat_interface/router.py and dashboard/metrics.py already use
between "pure decision" and "I/O".

Table and column names never come from free text here - every name this
module accepts must already exist in the `available_schema` a caller
passes in (fetched fresh from the live database moments before, by
preview_runner.py), so a request referencing a table/column that
doesn't actually exist is rejected before any SQL is built, not after.
Every accepted identifier is then quoted per Postgres's own
identifier-quoting rule (wrapped in double quotes, any embedded double
quote doubled) rather than trusted as pre-safe just because it passed
the allow-list - two independent guards, neither of which alone would
be enough: allow-listing without quoting would still be injectable
through a real-but-adversarial column name (Postgres permits quoted
identifiers containing almost any character, including a double quote);
quoting without allow-listing would just be safely executing a query
against a table the caller was never shown.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Never widened by a caller - "millions of rows" must never reach a
# browser tab regardless of what's selected, per this project's own
# earlier design discussion on large live tables.
PREVIEW_ROW_LIMIT = 500


class InvalidSelectionError(ValueError):
    """A selection references something outside `available_schema`, or is otherwise malformed."""


@dataclass(frozen=True)
class SelectedColumn:
    table: str
    column: str


@dataclass(frozen=True)
class JoinSpec:
    left_table: str
    left_column: str
    right_table: str
    right_column: str


@dataclass(frozen=True)
class QuerySelection:
    base_table: str
    columns: list[SelectedColumn] = field(default_factory=list)
    join: JoinSpec | None = None


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _tables_in(selection: QuerySelection) -> set[str]:
    tables = {selection.base_table}
    if selection.join is not None:
        tables.add(selection.join.left_table)
        tables.add(selection.join.right_table)
    return tables


def validate_selection(selection: QuerySelection, available_schema: dict[str, set[str]]) -> None:
    """Raises InvalidSelectionError on anything build_preview_query must not be trusted to build a query from."""
    if not selection.columns:
        raise InvalidSelectionError("select at least one column")

    tables = _tables_in(selection)
    for table in tables:
        if table not in available_schema:
            raise InvalidSelectionError(f"table {table!r} does not exist")

    for col in selection.columns:
        if col.table not in tables:
            raise InvalidSelectionError(
                f"column {col.table}.{col.column} references a table that isn't part of this selection"
            )
        if col.column not in available_schema[col.table]:
            raise InvalidSelectionError(f"column {col.table}.{col.column} does not exist")

    if selection.join is None:
        if len(tables) > 1:
            raise InvalidSelectionError("more than one table was referenced but no join was given")
        return

    join = selection.join
    if join.left_table == join.right_table:
        raise InvalidSelectionError("cannot join a table to itself")
    if selection.base_table not in (join.left_table, join.right_table):
        raise InvalidSelectionError("the join must include the base table")
    for table, column in ((join.left_table, join.left_column), (join.right_table, join.right_column)):
        if table not in available_schema:
            raise InvalidSelectionError(f"join table {table!r} does not exist")
        if column not in available_schema[table]:
            raise InvalidSelectionError(f"join column {table}.{column} does not exist")


def build_preview_query(selection: QuerySelection, available_schema: dict[str, set[str]]) -> str:
    """Validates `selection` against `available_schema`, then returns a safe, LIMIT-capped SQL string.

    Raises InvalidSelectionError instead of ever building a query from an
    unvalidated name.
    """
    validate_selection(selection, available_schema)

    select_list = ", ".join(f"{_quote_identifier(c.table)}.{_quote_identifier(c.column)}" for c in selection.columns)
    query = f"SELECT {select_list} FROM {_quote_identifier(selection.base_table)}"

    if selection.join is not None:
        join = selection.join
        other_table = join.right_table if join.left_table == selection.base_table else join.left_table
        query += (
            f" JOIN {_quote_identifier(other_table)}"
            f" ON {_quote_identifier(join.left_table)}.{_quote_identifier(join.left_column)}"
            f" = {_quote_identifier(join.right_table)}.{_quote_identifier(join.right_column)}"
        )

    query += f" LIMIT {PREVIEW_ROW_LIMIT}"
    return query
