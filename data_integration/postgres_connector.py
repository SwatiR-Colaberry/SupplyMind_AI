"""PostgreSQL connector for the data integration layer.

Every attempt is logged with a timestamp and outcome. Connection errors
(source unavailable, network issues) are retried a bounded number of
times with a fixed delay; auth failures and malformed queries are not
retried since retrying them cannot succeed.
"""

from __future__ import annotations

import time
from typing import Any

import psycopg2
import psycopg2.extras

from data_integration.config import PostgresConfig, load_postgres_config
from data_integration.logging_setup import get_logger, log_integration_attempt

CONNECT_TIMEOUT_SECONDS = 10
MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2

logger = get_logger()


class PostgresIntegrationError(RuntimeError):
    """Raised when PostgreSQL data cannot be retrieved after retries."""


def _connect(config: PostgresConfig):
    return psycopg2.connect(
        host=config.host,
        port=config.port,
        dbname=config.database,
        user=config.user,
        password=config.password,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
    )


def fetch_rows(
    query: str,
    params: tuple[Any, ...] = (),
    config: PostgresConfig | None = None,
) -> list[dict[str, Any]]:
    """Run a read query against PostgreSQL and return rows as dicts.

    Retries up to MAX_ATTEMPTS times on transient connection errors
    (psycopg2.OperationalError — covers unreachable host and network
    issues). Auth failures and malformed queries surface immediately
    since another attempt cannot change the outcome.
    """
    cfg = config or load_postgres_config()
    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        start = time.monotonic()
        conn = None
        try:
            conn = _connect(cfg)
            with conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(query, params)
                    rows = [dict(row) for row in cur.fetchall()]
            log_integration_attempt(
                logger,
                source="postgresql",
                outcome="success",
                duration_ms=(time.monotonic() - start) * 1000,
                context={"row_count": len(rows), "attempt": attempt},
            )
            return rows
        except psycopg2.OperationalError as exc:
            last_error = exc
            log_integration_attempt(
                logger,
                source="postgresql",
                outcome="failure",
                duration_ms=(time.monotonic() - start) * 1000,
                error_class=exc.__class__.__name__,
                context={"attempt": attempt, "max_attempts": MAX_ATTEMPTS, "retryable": True},
            )
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_DELAY_SECONDS)
        except Exception as exc:
            log_integration_attempt(
                logger,
                source="postgresql",
                outcome="failure",
                duration_ms=(time.monotonic() - start) * 1000,
                error_class=exc.__class__.__name__,
                context={"attempt": attempt, "retryable": False},
            )
            raise PostgresIntegrationError(f"PostgreSQL query failed: {exc.__class__.__name__}") from exc
        finally:
            if conn is not None:
                conn.close()

    raise PostgresIntegrationError(
        f"PostgreSQL unavailable after {MAX_ATTEMPTS} attempts"
    ) from last_error


def fetch_columns(
    query: str,
    params: tuple[Any, ...] = (),
    config: PostgresConfig | None = None,
) -> list[str]:
    """Return the column names `query` would produce, without fetching any rows.

    Wraps `query` as a LIMIT-0 subquery so this works for an arbitrary
    SELECT (not just a bare table name), and reads column names off
    cursor.description - fetch_rows()'s RealDictCursor row dicts don't
    exist when zero rows come back, so column names can't be read off a
    fetched row the way fetch_rows() does. Same retry/error handling as
    fetch_rows(): transient connection errors retry up to MAX_ATTEMPTS
    times, auth failures and malformed queries surface immediately.
    """
    cfg = config or load_postgres_config()
    probe_query = f"SELECT * FROM ({query}) AS schema_probe LIMIT 0"
    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        start = time.monotonic()
        conn = None
        try:
            conn = _connect(cfg)
            with conn:
                with conn.cursor() as cur:
                    cur.execute(probe_query, params)
                    columns = [desc[0] for desc in cur.description]
            log_integration_attempt(
                logger,
                source="postgresql",
                outcome="success",
                duration_ms=(time.monotonic() - start) * 1000,
                context={"column_count": len(columns), "attempt": attempt, "probe": True},
            )
            return columns
        except psycopg2.OperationalError as exc:
            last_error = exc
            log_integration_attempt(
                logger,
                source="postgresql",
                outcome="failure",
                duration_ms=(time.monotonic() - start) * 1000,
                error_class=exc.__class__.__name__,
                context={"attempt": attempt, "max_attempts": MAX_ATTEMPTS, "retryable": True, "probe": True},
            )
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_DELAY_SECONDS)
        except Exception as exc:
            log_integration_attempt(
                logger,
                source="postgresql",
                outcome="failure",
                duration_ms=(time.monotonic() - start) * 1000,
                error_class=exc.__class__.__name__,
                context={"attempt": attempt, "retryable": False, "probe": True},
            )
            raise PostgresIntegrationError(f"PostgreSQL schema probe failed: {exc.__class__.__name__}") from exc
        finally:
            if conn is not None:
                conn.close()

    raise PostgresIntegrationError(
        f"PostgreSQL unavailable after {MAX_ATTEMPTS} attempts"
    ) from last_error
