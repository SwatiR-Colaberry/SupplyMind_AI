from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest

from data_console.schema_inspector import ColumnInfo
from data_console.serve_data_console import DataConsoleHandler
from data_integration.config import MissingConfigError
from data_integration.postgres_connector import PostgresIntegrationError
from http.server import HTTPServer


@pytest.fixture
def server():
    httpd = HTTPServer(("127.0.0.1", 0), DataConsoleHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{httpd.server_port}"
    try:
        yield base_url
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def _get(base_url: str, path: str):
    try:
        resp = urllib.request.urlopen(base_url + path)
        return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_get_root_returns_html_page(server):
    resp = urllib.request.urlopen(server + "/")
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("text/html")
    assert "Connect Your Data" in resp.read().decode("utf-8")


def test_get_unknown_path_returns_404(server):
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(server + "/nonexistent")
    assert exc_info.value.code == 404


def test_api_tables_reports_not_connected_when_config_is_missing(server):
    with patch("data_console.schema_inspector.list_tables", side_effect=MissingConfigError("SUPPLYMIND_PG_HOST is not set")):
        status, data = _get(server, "/api/tables")

    assert status == 200
    assert data["connected"] is False
    assert "SUPPLYMIND_PG_HOST" in data["message"]


def test_api_tables_reports_not_connected_when_postgres_is_unreachable(server):
    with patch("data_console.schema_inspector.list_tables", side_effect=PostgresIntegrationError("could not connect")):
        status, data = _get(server, "/api/tables")

    assert status == 200
    assert data["connected"] is False
    assert "could not connect" in data["message"]


def test_api_tables_returns_the_table_list_when_connected(server):
    with patch("data_console.schema_inspector.list_tables", return_value=["customer_orders", "inventory"]):
        status, data = _get(server, "/api/tables")

    assert status == 200
    assert data == {"connected": True, "tables": ["customer_orders", "inventory"]}


def test_api_columns_reports_not_connected_when_config_is_missing(server):
    with patch("data_console.schema_inspector.list_columns", side_effect=MissingConfigError("SUPPLYMIND_PG_HOST is not set")):
        status, data = _get(server, "/api/tables/inventory/columns")

    assert status == 200
    assert data["connected"] is False


def test_api_columns_reports_not_found_for_an_empty_table(server):
    with patch("data_console.schema_inspector.list_columns", return_value=[]):
        status, data = _get(server, "/api/tables/does_not_exist/columns")

    assert status == 200
    assert data == {"connected": True, "found": False, "table": "does_not_exist"}


def test_api_columns_returns_columns_and_requirement_checks_when_table_satisfies_inventory(server):
    columns = [
        ColumnInfo(name="sku", data_type="text", nullable=False),
        ColumnInfo(name="current_stock", data_type="numeric", nullable=False),
        ColumnInfo(name="safety_stock", data_type="numeric", nullable=False),
        ColumnInfo(name="daily_demand_rate", data_type="numeric", nullable=False),
        ColumnInfo(name="lead_time_days", data_type="numeric", nullable=False),
    ]
    with patch("data_console.schema_inspector.list_columns", return_value=columns):
        status, data = _get(server, "/api/tables/inventory/columns")

    assert status == 200
    assert data["connected"] is True
    assert data["found"] is True
    assert data["table"] == "inventory"
    assert [c["name"] for c in data["columns"]] == [
        "sku", "current_stock", "safety_stock", "daily_demand_rate", "lead_time_days"
    ]
    by_name = {c["dataset_name"]: c for c in data["requirement_checks"]}
    assert by_name["inventory"]["satisfied"] is True
    assert by_name["customer_orders"]["satisfied"] is False


def test_api_columns_url_decodes_a_table_name_with_special_characters(server):
    with patch("data_console.schema_inspector.list_columns") as mock_list_columns:
        mock_list_columns.return_value = []
        _get(server, "/api/tables/my%20table/columns")

    mock_list_columns.assert_called_once_with("my table")
