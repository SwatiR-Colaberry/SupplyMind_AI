"""Structured JSON logging for supplier-evaluation activity.

Mirrors intelligence/logging_setup.py's (and, in turn,
data_integration/logging_setup.py's) JSON shape so log lines from every
service in this repo parse identically downstream, but stamps
"service": "supplier_evaluation" so evaluation activity is distinguishable
from intelligence-model and data-integration activity in the logs.
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
            "service": "supplier_evaluation",
            "event": getattr(record, "event", record.getMessage()),
        }
        for key in ("outcome", "error_class", "correlation_id", "context"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload)


def get_logger(name: str = "supplier_evaluation") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(stream=sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger
