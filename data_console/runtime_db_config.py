"""In-memory Postgres connection override, entered through "Map Your Data"'s
own "Connect a Database" form - lets this local tool be pointed at a
database without requiring the SUPPLYMIND_PG_* environment variables to be
set before the server starts.

Deliberately in-memory only, for the life of this one running server
process: never written to disk, never logged. Same "no secrets in files, no
secrets in logs" rule the Security Enforcement Layer requires for every
credential in this repo, applied here to a password typed into a local
browser tab instead of one read from an env var. Resets on every server
restart - that is intentional, not a gap: this is a local development
convenience, not a persisted credential store.
"""

from __future__ import annotations

from data_integration.config import PostgresConfig, load_postgres_config

_runtime_override: PostgresConfig | None = None


def set_runtime_config(config: PostgresConfig) -> None:
    global _runtime_override
    _runtime_override = config


def clear_runtime_config() -> None:
    global _runtime_override
    _runtime_override = None


def get_postgres_config() -> PostgresConfig:
    """The config to actually connect with: a runtime override entered through
    the UI this session, if any, otherwise the environment-variable path every
    other consumer of data_integration already uses. Raises MissingConfigError
    exactly as load_postgres_config() does when neither is available."""
    if _runtime_override is not None:
        return _runtime_override
    return load_postgres_config()
