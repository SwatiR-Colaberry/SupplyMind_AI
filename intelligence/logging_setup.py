"""Structured JSON logging for intelligence-model stage activity.

Mirrors agents/logging_setup.py and data_integration/logging_setup.py's
JSON shape so log lines from all services parse identically downstream,
but stamps "service": "intelligence" so stage activity is distinguishable
from agent coordination and data integration activity in the logs.
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
            "service": "intelligence",
            "event": getattr(record, "event", record.getMessage()),
        }
        for key in ("outcome", "error_class", "correlation_id", "context"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload)


def get_logger(name: str = "intelligence") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(stream=sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger
