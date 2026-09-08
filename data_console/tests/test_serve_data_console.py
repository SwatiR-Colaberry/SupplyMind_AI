from __future__ import annotations

import datetime
import decimal
import http.client
import json
import threading
import urllib.error
import urllib.request
from unittest.mock import patch
from urllib.parse import urlparse

import pytest

from data_console.query_builder import PREVIEW_ROW_LIMIT
from data_console.schema_inspector import ColumnInfo
from data_console.serve_data_console import MAX_REQUEST_BODY_BYTES, DataConsoleHandler
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


def _post(base_url: str, path: str, payload: dict | None = None, raw_body: bytes | None = None):
    data = raw_body if raw_body is not None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base_url + path, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        resp = urllib.request.urlopen(req)
        return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _post_with_raw_content_length(base_url: str, path: str, content_length: object, body: bytes = b"{}"):
    parsed = urlparse(base_url)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    try:
        conn.putrequest("POST", path)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(content_length))
        conn.endheaders()
        conn.send(body)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())
    finally:
        conn.close()


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


def test_post_preview_returns_rows_sql_and_requirement_checks(server):
    columns = [ColumnInfo(name="sku", data_type="text", nullable=False)]
    with patch("data_console.preview_runner.schema_inspector.list_columns", return_value=columns), \
         patch("data_console.preview_runner.fetch_rows", return_value=[{"sku": "SKU-1"}]):
        status, data = _post(server, "/api/preview", {"base_table": "inventory", "columns": [{"table": "inventory", "column": "sku"}]})

    assert status == 200
    assert data["connected"] is True
    assert data["rows"] == [{"sku": "SKU-1"}]
    assert data["row_count"] == 1
    assert data["truncated"] is False
    assert "sku" in data["sql"]
    assert any(c["dataset_name"] == "inventory" for c in data["requirement_checks"])


def test_post_preview_serializes_decimal_and_date_values_that_json_cannot_handle_natively(server):
    # Regression guard: psycopg2 hands back Decimal for numeric columns and
    # date/datetime for date columns - a real preview of almost any real
    # table would crash json.dumps() without explicit conversion.
    row = {
        "current_stock": decimal.Decimal("12.50"),
        "order_date": datetime.date(2025, 8, 15),
        "created_at": datetime.datetime(2025, 8, 15, 9, 30, 0),
    }
    with patch("data_console.preview_runner.schema_inspector.list_columns", return_value=[ColumnInfo("current_stock", "numeric", True)]), \
         patch("data_console.preview_runner.fetch_rows", return_value=[row]):
        status, data = _post(server, "/api/preview", {"base_table": "inventory", "columns": [{"table": "inventory", "column": "current_stock"}]})

    assert status == 200
    assert data["rows"] == [{"current_stock": 12.5, "order_date": "2025-08-15", "created_at": "2025-08-15T09:30:00"}]


def test_post_preview_marks_truncated_at_the_row_limit(server):
    full_page = [{"sku": f"SKU-{i}"} for i in range(PREVIEW_ROW_LIMIT)]
    with patch("data_console.preview_runner.schema_inspector.list_columns", return_value=[ColumnInfo("sku", "text", False)]), \
         patch("data_console.preview_runner.fetch_rows", return_value=full_page):
        status, data = _post(server, "/api/preview", {"base_table": "inventory", "columns": [{"table": "inventory", "column": "sku"}]})

    assert data["truncated"] is True
    assert data["row_count"] == PREVIEW_ROW_LIMIT


def test_post_preview_returns_400_for_an_invalid_selection(server):
    with patch("data_console.preview_runner.schema_inspector.list_columns", return_value=[ColumnInfo("sku", "text", False)]):
        status, data = _post(server, "/api/preview", {"base_table": "inventory", "columns": [{"table": "inventory", "column": "not_a_real_column"}]})

    assert status == 400
    assert "does not exist" in data["error"]


def test_post_preview_returns_400_when_base_table_is_missing(server):
    status, data = _post(server, "/api/preview", {"columns": [{"table": "inventory", "column": "sku"}]})

    assert status == 400
    assert "base_table" in data["error"]


def test_post_preview_returns_400_when_columns_is_empty(server):
    status, data = _post(server, "/api/preview", {"base_table": "inventory", "columns": []})

    assert status == 400
    assert "columns" in data["error"]


def test_post_preview_returns_400_for_malformed_json(server):
    status, data = _post(server, "/api/preview", raw_body=b"{not valid json")

    assert status == 400
    assert "invalid request" in data["error"]


def test_post_preview_reports_not_connected_when_config_is_missing(server):
    with patch("data_console.preview_runner.schema_inspector.list_columns", side_effect=MissingConfigError("SUPPLYMIND_PG_HOST is not set")):
        status, data = _post(server, "/api/preview", {"base_table": "inventory", "columns": [{"table": "inventory", "column": "sku"}]})

    assert status == 200
    assert data["connected"] is False


def test_post_unknown_path_returns_404(server):
    status, data = _post(server, "/api/nope", {"query": "hi"})
    assert status == 404


def test_post_preview_non_numeric_content_length_returns_400_not_a_crash(server):
    status, data = _post_with_raw_content_length(server, "/api/preview", "notanumber")
    assert status == 400
    assert "invalid request" in data["error"]


def test_post_preview_negative_content_length_returns_400_and_does_not_hang_the_server(server):
    status, data = _post_with_raw_content_length(server, "/api/preview", -1)
    assert status == 400
    assert "must not be negative" in data["error"]

    resp = urllib.request.urlopen(server + "/")
    assert resp.status == 200


def test_post_preview_oversized_content_length_returns_400(server):
    status, data = _post_with_raw_content_length(server, "/api/preview", MAX_REQUEST_BODY_BYTES + 1)
    assert status == 400
    assert "exceeds" in data["error"]
