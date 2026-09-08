"""Guardrails for user-typed raw SQL, the escape hatch for "Map Your Data" when
the guided table picker can't express what's needed - most commonly a dataset
whose required columns are split across more tables than the guided flow's
single-table mapping supports.

This is a blunt, text-level guard, not a substitute for a read-only database
role: it rejects the common, obvious ways a pasted query could write to the
database or smuggle in a second statement, with a clear error message instead
of an opaque driver-level syntax error. It is not a SQL parser - a string
literal or column alias that happens to contain a blocked word (e.g. a WHERE
clause comparing against the literal 'DELETE') is also rejected, and a query
starting with a comment is rejected as not starting with SELECT/WITH rather
than having its comment stripped. Both are deliberate: favoring a rejected
legitimate query over an accepted unsafe one. If genuine read-only enforcement
is ever required, it has to come from the database connection's own
permissions, not from text pattern matching here.
"""

from __future__ import annotations

import re


class UnsafeQueryError(ValueError):
    """Raised when a raw SQL query fails a read-only guardrail check."""


_LEADING_STATEMENT_RE = re.compile(r"^\s*(WITH|SELECT)\b", re.IGNORECASE)

# Statement keywords and functions with no legitimate place in a read-only
# SELECT/CTE. Word-boundary matching, so "selected" or "collection" don't
# false-positive on "SELECT"/"COPY" substrings.
_BLOCKED_KEYWORDS = (
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE",
    "GRANT", "REVOKE", "EXECUTE", "CALL", "COPY", "VACUUM", "MERGE",
    "INTO", "REFRESH", "LISTEN", "NOTIFY",
    "PG_SLEEP", "PG_TERMINATE_BACKEND", "DBLINK", "DBLINK_EXEC",
)
_BLOCKED_KEYWORD_PATTERNS = [(kw, re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE)) for kw in _BLOCKED_KEYWORDS]


def validate_read_only_query(sql: str) -> str:
    """Raises UnsafeQueryError if `sql` fails a guardrail check.

    Otherwise returns `sql` trimmed, with a single trailing semicolon (if
    present) removed - the cleaned form every caller should actually use,
    so validation and cleanup can never drift apart into two different
    strings.
    """
    if not isinstance(sql, str) or not sql.strip():
        raise UnsafeQueryError("Query is empty.")

    cleaned = sql.strip()
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].rstrip()

    if ";" in cleaned:
        raise UnsafeQueryError('Only a single statement is allowed - remove the extra ";".')

    if not _LEADING_STATEMENT_RE.match(cleaned):
        raise UnsafeQueryError("Query must start with SELECT (or WITH ... SELECT).")

    for keyword, pattern in _BLOCKED_KEYWORD_PATTERNS:
        if pattern.search(cleaned):
            raise UnsafeQueryError(f'"{keyword}" is not allowed in this query.')

    return cleaned
