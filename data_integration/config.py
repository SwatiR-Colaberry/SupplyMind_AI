"""Environment-driven configuration for the data integration layer.

No credentials are hardcoded here. All values come from the process
environment so the same code runs unchanged across local, dev, and prod.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class MissingConfigError(RuntimeError):
    """Raised when a required environment variable is not set."""


@dataclass(frozen=True)
class PostgresConfig:
    host: str
    port: int
    database: str
    user: str
    password: str


@dataclass(frozen=True)
class GoogleSheetsConfig:
    service_account_json_path: str


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise MissingConfigError(f"Required environment variable '{name}' is not set")
    return value


def load_postgres_config() -> PostgresConfig:
    return PostgresConfig(
        host=_require_env("SUPPLYMIND_PG_HOST"),
        port=int(os.environ.get("SUPPLYMIND_PG_PORT", "5432")),
        database=_require_env("SUPPLYMIND_PG_DATABASE"),
        user=_require_env("SUPPLYMIND_PG_USER"),
        password=_require_env("SUPPLYMIND_PG_PASSWORD"),
    )


def load_google_sheets_config() -> GoogleSheetsConfig:
    return GoogleSheetsConfig(
        service_account_json_path=_require_env("SUPPLYMIND_GOOGLE_SERVICE_ACCOUNT_JSON"),
    )
