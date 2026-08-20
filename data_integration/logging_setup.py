"""Structured JSON logging for data integration attempts.

Every integration attempt (PostgreSQL or Google Sheets) is logged as a
single JSON line with a timestamp, so attempts can be traced and audited
after the fact regardless of outcome.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "service": "data-integration",
            "event": getattr(record, "event", record.getMessage()),
        }
        for key in ("source", "outcome", "duration_ms", "error_class", "context"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload)


def get_logger(name: str = "data_integration") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(stream=sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def log_integration_attempt(
    logger: logging.Logger,
    *,
    source: str,
    outcome: str,
    duration_ms: float | None = None,
    error_class: str | None = None,
    context: dict[str, Any] | None = None,
) -> None:
    level = logging.INFO if outcome == "success" else logging.ERROR
    logger.log(
        level,
        f"{source}_integration_attempt",
        extra={
            "event": f"{source}_integration_attempt",
            "source": source,
            "outcome": outcome,
            "duration_ms": duration_ms,
            "error_class": error_class,
            "context": context or {},
        },
    )
