from unittest.mock import MagicMock, patch

import psycopg2
import pytest

from data_integration.config import PostgresConfig
from data_integration.postgres_connector import PostgresIntegrationError, fetch_rows

TEST_CONFIG = PostgresConfig(
    host="localhost", port=5432, database="supplymind", user="test", password="test"
)


def _mock_connection(rows):
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchall.return_value = rows

    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value = cursor
    return conn


def test_fetch_rows_happy_path_returns_data():
    expected_rows = [{"id": 1, "name": "Widget"}, {"id": 2, "name": "Gadget"}]
    with patch("data_integration.postgres_connector.psycopg2.connect") as mock_connect:
        mock_connect.return_value = _mock_connection(expected_rows)

        rows = fetch_rows("SELECT * FROM products", config=TEST_CONFIG)

    assert rows == expected_rows
    mock_connect.assert_called_once()


def test_fetch_rows_retries_then_raises_when_source_unavailable(monkeypatch):
    monkeypatch.setattr("data_integration.postgres_connector.RETRY_DELAY_SECONDS", 0)

    with patch("data_integration.postgres_connector.psycopg2.connect") as mock_connect:
        mock_connect.side_effect = psycopg2.OperationalError("could not connect to server")

        with pytest.raises(PostgresIntegrationError):
            fetch_rows("SELECT * FROM products", config=TEST_CONFIG)

    assert mock_connect.call_count == 3


def test_fetch_rows_does_not_retry_non_operational_errors():
    with patch("data_integration.postgres_connector.psycopg2.connect") as mock_connect:
        mock_connect.side_effect = ValueError("malformed dsn")

        with pytest.raises(PostgresIntegrationError):
            fetch_rows("SELECT * FROM products", config=TEST_CONFIG)

    assert mock_connect.call_count == 1
