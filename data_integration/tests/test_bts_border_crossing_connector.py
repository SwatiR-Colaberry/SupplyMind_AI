from datetime import date
from unittest.mock import MagicMock, patch

import pytest
import requests

from data_integration.bts_border_crossing_connector import (
    BtsBorderCrossingIntegrationError,
    fetch_monthly_truck_crossing_history,
)


def _mock_response(json_data, status_code=200):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_data
    if status_code >= 400:
        response.raise_for_status.side_effect = requests.exceptions.HTTPError(response=response)
    else:
        response.raise_for_status.side_effect = None
    return response


def test_fetch_monthly_truck_crossing_history_sums_ports_per_month():
    raw_rows = [
        {"date": "2025-01-01T00:00:00.000", "value": "100"},
        {"date": "2025-01-01T00:00:00.000", "value": "50"},
        {"date": "2025-02-01T00:00:00.000", "value": "80"},
    ]
    with patch("data_integration.bts_border_crossing_connector.requests.get") as mock_get:
        mock_get.return_value = _mock_response(raw_rows)

        rows = fetch_monthly_truck_crossing_history(as_of=date(2025, 3, 1))

    assert rows == [
        {"order_date": "2025-01-01", "quantity": 150.0},
        {"order_date": "2025-02-01", "quantity": 80.0},
    ]


def test_fetch_monthly_truck_crossing_history_skips_rows_missing_fields():
    raw_rows = [
        {"date": "2025-01-01T00:00:00.000", "value": "100"},
        {"date": None, "value": "50"},
        {"date": "2025-02-01T00:00:00.000", "value": None},
        {"date": "2025-03-01T00:00:00.000", "value": "not-a-number"},
    ]
    with patch("data_integration.bts_border_crossing_connector.requests.get") as mock_get:
        mock_get.return_value = _mock_response(raw_rows)

        rows = fetch_monthly_truck_crossing_history(as_of=date(2025, 4, 1))

    assert rows == [{"order_date": "2025-01-01", "quantity": 100.0}]


def test_fetch_sends_measure_and_lookback_where_clause():
    with patch("data_integration.bts_border_crossing_connector.requests.get") as mock_get:
        mock_get.return_value = _mock_response([])

        fetch_monthly_truck_crossing_history(measure="Trains", lookback_months=3, as_of=date(2025, 4, 15))

    _, kwargs = mock_get.call_args
    assert kwargs["params"]["measure"] == "Trains"
    assert "2025-01-01" in kwargs["params"]["$where"]
    assert kwargs["timeout"] == 15


def test_retries_then_raises_on_repeated_5xx(monkeypatch):
    monkeypatch.setattr("data_integration.bts_border_crossing_connector.RETRY_DELAY_SECONDS", 0)
    with patch("data_integration.bts_border_crossing_connector.requests.get") as mock_get:
        mock_get.return_value = _mock_response({}, status_code=503)

        with pytest.raises(BtsBorderCrossingIntegrationError):
            fetch_monthly_truck_crossing_history(as_of=date(2025, 1, 1))

    assert mock_get.call_count == 3


def test_does_not_retry_a_non_retryable_http_error():
    with patch("data_integration.bts_border_crossing_connector.requests.get") as mock_get:
        mock_get.return_value = _mock_response({}, status_code=400)

        with pytest.raises(BtsBorderCrossingIntegrationError):
            fetch_monthly_truck_crossing_history(as_of=date(2025, 1, 1))

    assert mock_get.call_count == 1


def test_retries_then_raises_on_connection_error(monkeypatch):
    monkeypatch.setattr("data_integration.bts_border_crossing_connector.RETRY_DELAY_SECONDS", 0)
    with patch("data_integration.bts_border_crossing_connector.requests.get") as mock_get:
        mock_get.side_effect = requests.exceptions.ConnectionError("could not resolve host")

        with pytest.raises(BtsBorderCrossingIntegrationError):
            fetch_monthly_truck_crossing_history(as_of=date(2025, 1, 1))

    assert mock_get.call_count == 3


def test_malformed_json_response_is_not_retried():
    with patch("data_integration.bts_border_crossing_connector.requests.get") as mock_get:
        response = MagicMock()
        response.raise_for_status.side_effect = None
        response.json.side_effect = ValueError("not valid json")
        mock_get.return_value = response

        with pytest.raises(BtsBorderCrossingIntegrationError):
            fetch_monthly_truck_crossing_history(as_of=date(2025, 1, 1))

    assert mock_get.call_count == 1
