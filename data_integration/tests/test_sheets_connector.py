from unittest.mock import MagicMock, patch

import google.auth.exceptions
import gspread.exceptions
import pytest

from data_integration.config import GoogleSheetsConfig
from data_integration.sheets_connector import SheetsIntegrationError, fetch_rows

TEST_CONFIG = GoogleSheetsConfig(service_account_json_path="/fake/service-account.json")


def _mock_client(rows):
    worksheet = MagicMock()
    worksheet.get_all_records.return_value = rows

    spreadsheet = MagicMock()
    spreadsheet.worksheet.return_value = worksheet

    client = MagicMock()
    client.open_by_key.return_value = spreadsheet
    return client


def test_fetch_rows_happy_path_returns_data():
    expected_rows = [{"sku": "A1", "on_hand": 10}, {"sku": "A2", "on_hand": 3}]
    with patch("data_integration.sheets_connector.gspread.service_account") as mock_auth:
        mock_auth.return_value = _mock_client(expected_rows)

        rows = fetch_rows("sheet-id", "Inventory", config=TEST_CONFIG)

    assert rows == expected_rows


def test_fetch_rows_auth_failure_does_not_retry():
    with patch("data_integration.sheets_connector.gspread.service_account") as mock_auth:
        mock_auth.side_effect = google.auth.exceptions.DefaultCredentialsError("bad credentials")

        with pytest.raises(SheetsIntegrationError):
            fetch_rows("sheet-id", "Inventory", config=TEST_CONFIG)

    assert mock_auth.call_count == 1


def test_fetch_rows_missing_spreadsheet_does_not_retry():
    with patch("data_integration.sheets_connector.gspread.service_account") as mock_auth:
        client = MagicMock()
        client.open_by_key.side_effect = gspread.exceptions.SpreadsheetNotFound("not found")
        mock_auth.return_value = client

        with pytest.raises(SheetsIntegrationError):
            fetch_rows("sheet-id", "Inventory", config=TEST_CONFIG)

    assert client.open_by_key.call_count == 1


def test_fetch_rows_retries_transient_api_error(monkeypatch):
    monkeypatch.setattr("data_integration.sheets_connector.RETRY_DELAY_SECONDS", 0)

    response = MagicMock(status_code=503)
    with patch("data_integration.sheets_connector.gspread.service_account") as mock_auth:
        client = MagicMock()
        client.open_by_key.side_effect = gspread.exceptions.APIError(response)
        mock_auth.return_value = client

        with pytest.raises(SheetsIntegrationError):
            fetch_rows("sheet-id", "Inventory", config=TEST_CONFIG)

    assert client.open_by_key.call_count == 3
