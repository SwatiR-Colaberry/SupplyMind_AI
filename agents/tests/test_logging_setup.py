import json
import logging

from agents.logging_setup import JsonFormatter


def test_json_formatter_stamps_every_log_line_with_a_timestamp():
    record = logging.LogRecord(
        name="agents",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="orchestration_started",
        args=(),
        exc_info=None,
    )
    record.event = "orchestration_started"
    record.context = {"query": "Should we reorder SKU-123?"}

    payload = json.loads(JsonFormatter().format(record))

    assert payload["service"] == "agents"
    assert payload["event"] == "orchestration_started"
    assert "timestamp" in payload and payload["timestamp"]
