"""Structured JSON logging for root-cause-analysis activity.

Mirrors supplier_evaluation/logging_setup.py's (and, in turn,
intelligence/logging_setup.py's and data_integration/logging_setup.py's)
JSON shape so log lines from every service in this repo parse identically
downstream, but stamps "service": "root_cause" so analysis activity is
distinguishable from other services' activity in the logs.
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
            "service": "root_cause",
            "event": getattr(record, "event", record.getMessage()),
        }
        for key in ("outcome", "error_class", "correlation_id", "context"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload)


def get_logger(name: str = "root_cause") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(stream=sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger
