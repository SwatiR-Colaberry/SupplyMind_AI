from __future__ import annotations

import datetime
import decimal
import http.client
import io
import json
import threading
import urllib.error
import urllib.request
import zipfile
from unittest.mock import patch
from urllib.parse import urlparse

import pytest

from data_console.mapping_store import MappingStore
from data_console.query_builder import PREVIEW_ROW_LIMIT
from data_console.schema_inspector import ColumnInfo
from data_console.serve_data_console import MAX_REQUEST_BODY_BYTES, DataConsoleHandler
from data_integration.config import MissingConfigError
from data_integration.connection_profile import SchemaMappingError
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


def _delete(base_url: str, path: str):
    req = urllib.request.Request(base_url + path, method="DELETE")
    try:
        resp = urllib.request.urlopen(req)
        return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@pytest.fixture
def isolated_mapping_store(tmp_path):
    """Points every module that constructs a bare MappingStore() at a throwaway file,
    so mapping-endpoint tests never touch the real data_console/.mappings.json.

    Each of these is its own `from data_console.mapping_store import MappingStore`
    name binding - patching one module's binding leaves the others pointed at the
    real class, so every one of them has to be patched here, not just the modules
    the HTTP layer calls directly. (This exact gap already bit file_mapping_service
    once this session - sheet_mapping_service has the same import shape.)"""
    test_path = tmp_path / "mappings.json"
    factory = lambda *a, **k: MappingStore(test_path)  # noqa: E731
    with patch("data_console.mapping_service.MappingStore", new=factory), \
         patch("data_console.file_mapping_service.MappingStore", new=factory), \
         patch("data_console.sheet_mapping_service.MappingStore", new=factory), \
         patch("data_console.serve_data_console.MappingStore", new=factory):
        yield test_path


@pytest.fixture
def isolated_uploads_dir(tmp_path):
    """Points file_store at a throwaway directory, so upload-endpoint tests never
    touch the real data_console/uploads/."""
    uploads_path = tmp_path / "uploads"
    with patch("data_console.file_store.UPLOADS_DIR", uploads_path):
        yield uploads_path


@pytest.fixture(autouse=True)
def reset_runtime_db_config():
    """A successful /api/db-connection call sets a process-global override that
    would otherwise leak into every test that runs after it in the same pytest
    process - autouse so no test can forget this the way it's easy to forget an
    opt-in fixture."""
    from data_console.runtime_db_config import clear_runtime_config

    clear_runtime_config()
    yield
    clear_runtime_config()


def _post_raw(base_url: str, path: str, body: bytes, headers: dict):
    req = urllib.request.Request(base_url + path, data=body, headers=headers, method="POST")
    try:
        resp = urllib.request.urlopen(req)
        return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


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


def test_an_unexpected_exception_returns_a_clean_500_instead_of_dropping_the_connection(server):
    # Regression test for the failure mode a real bug hit in production: an
    # exception type no specific `except` clause anywhere on this path was
    # prepared for used to propagate all the way up to BaseHTTPRequestHandler's
    # default handling, which logs a traceback server-side and drops the
    # connection with zero bytes sent - indistinguishable, from the client's
    # side, from an indefinite hang. `_dispatch_safely` is the safety net that
    # guarantees a real HTTP response (500, with a readable error body) instead.
    with patch("data_console.schema_inspector.list_tables", side_effect=RuntimeError("boom")):
        status, data = _get(server, "/api/tables")

    assert status == 500
    assert "boom" in data["error"]


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


def test_api_columns_returns_columns_when_table_is_found(server):
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


def test_api_columns_url_decodes_a_table_name_with_special_characters(server):
    with patch("data_console.schema_inspector.list_columns") as mock_list_columns:
        mock_list_columns.return_value = []
        _get(server, "/api/tables/my%20table/columns")

    mock_list_columns.assert_called_once_with("my table")


def test_post_preview_returns_rows_and_sql(server):
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


def test_api_columns_includes_suggested_mappings_for_a_differently_named_table(server):
    # The exact acme_inventory shape from risk_detection's multi-tenant
    # fixture - the real case that surfaced the need for mapping at all.
    columns = [
        ColumnInfo(name="item_sku", data_type="text", nullable=False),
        ColumnInfo(name="on_hand", data_type="numeric", nullable=False),
        ColumnInfo(name="min_stock", data_type="numeric", nullable=False),
        ColumnInfo(name="daily_use", data_type="numeric", nullable=False),
        ColumnInfo(name="lead_days", data_type="numeric", nullable=False),
    ]
    with patch("data_console.schema_inspector.list_columns", return_value=columns):
        status, data = _get(server, "/api/tables/acme_inventory/columns")

    assert status == 200
    inventory_suggestions = data["suggested_mappings"]["inventory"]
    assert inventory_suggestions == {
        "sku": "item_sku",
        "current_stock": "on_hand",
        "safety_stock": "min_stock",
        "daily_demand_rate": "daily_use",
        "lead_time_days": "lead_days",
    }


def test_api_datasets_returns_all_three_known_datasets_with_field_descriptions(server):
    status, data = _get(server, "/api/datasets")

    assert status == 200
    names = [d["dataset_name"] for d in data["datasets"]]
    assert names == ["customer_orders", "inventory", "delivery_records"]
    inventory = next(d for d in data["datasets"] if d["dataset_name"] == "inventory")
    assert {f["name"] for f in inventory["required"]} == {
        "sku", "current_stock", "safety_stock", "daily_demand_rate", "lead_time_days"
    }
    assert all(f["description"] and f["example"] for f in inventory["required"])


def test_api_mappings_starts_with_every_dataset_not_mapped(server, isolated_mapping_store):
    status, data = _get(server, "/api/mappings")

    assert status == 200
    assert data["mappings"] == {
        "customer_orders": {"status": "not_mapped"},
        "inventory": {"status": "not_mapped"},
        "delivery_records": {"status": "not_mapped"},
    }


def test_post_mapping_validates_and_saves_then_shows_up_in_get_mappings(server, isolated_mapping_store):
    with patch("data_console.mapping_service.get_postgres_config"), \
         patch("data_console.mapping_service.validate_profile"):
        status, data = _post(
            server, "/api/mappings/inventory",
            {"table": "acme_inventory", "column_mapping": {"sku": "item_sku", "current_stock": "on_hand"}},
        )

    assert status == 200
    assert data == {"saved": True}

    status, data = _get(server, "/api/mappings")
    assert data["mappings"]["inventory"] == {
        "status": "mapped",
        "source_kind": "table",
        "table": "acme_inventory",
        "query": None,
        "file_id": None,
        "filename": None,
        "sheet_url": None,
        "column_mapping": {"sku": "item_sku", "current_stock": "on_hand"},
        "unavailable_fields": [],
        "computed_date_fields": {},
    }


def test_post_mapping_with_unavailable_fields_saves_and_shows_up_in_get_mappings(server, isolated_mapping_store):
    # A real dataset that genuinely has no safety_stock/daily_demand_rate/
    # lead_time_days column - the per-field "Not available in this dataset"
    # checkbox sends these as unavailable_fields instead of blocking the save.
    with patch("data_console.mapping_service.get_postgres_config"), \
         patch("data_console.mapping_service.validate_profile"):
        status, data = _post(
            server, "/api/mappings/inventory",
            {
                "table": "acme_inventory",
                "column_mapping": {"sku": "item_sku", "current_stock": "on_hand"},
                "unavailable_fields": ["safety_stock", "daily_demand_rate", "lead_time_days"],
            },
        )

    assert status == 200
    assert data == {"saved": True}

    status, data = _get(server, "/api/mappings")
    assert sorted(data["mappings"]["inventory"]["unavailable_fields"]) == [
        "daily_demand_rate", "lead_time_days", "safety_stock"
    ]


def test_post_mapping_returns_400_when_unavailable_fields_is_not_a_list_of_strings(server, isolated_mapping_store):
    status, data = _post(
        server, "/api/mappings/inventory",
        {"table": "acme_inventory", "column_mapping": {"sku": "sku"}, "unavailable_fields": "safety_stock"},
    )
    assert status == 400
    assert "unavailable_fields" in data["error"]


def test_post_mapping_with_computed_date_fields_saves_and_shows_up_in_get_mappings(server, isolated_mapping_store):
    # A real delivery export with an order date plus a "days scheduled"
    # count instead of an explicit expected-delivery-date column.
    with patch("data_console.mapping_service.get_postgres_config"), \
         patch("data_console.mapping_service.validate_profile"):
        status, data = _post(
            server, "/api/mappings/delivery_records",
            {
                "table": "acme_deliveries",
                "column_mapping": {"po_id": "po_id", "actual_date": "actual_date"},
                "computed_date_fields": {
                    "expected_date": {"base_date_column": "order_date", "offset_days_column": "days_scheduled"}
                },
            },
        )

    assert status == 200
    assert data == {"saved": True}

    status, data = _get(server, "/api/mappings")
    assert data["mappings"]["delivery_records"]["computed_date_fields"] == {
        "expected_date": {"base_date_column": "order_date", "offset_days_column": "days_scheduled"}
    }


def test_post_mapping_returns_400_when_a_computed_date_field_spec_is_incomplete(server, isolated_mapping_store):
    status, data = _post(
        server, "/api/mappings/delivery_records",
        {
            "table": "acme_deliveries",
            "column_mapping": {"po_id": "po_id"},
            "computed_date_fields": {"expected_date": {"base_date_column": "order_date"}},  # missing offset_days_column
        },
    )
    assert status == 400
    assert "computed_date_fields" in data["error"]


def test_post_mapping_returns_400_and_saves_nothing_when_validation_fails(server, isolated_mapping_store):
    with patch("data_console.mapping_service.get_postgres_config"), \
         patch("data_console.mapping_service.validate_profile", side_effect=SchemaMappingError("missing required field 'sku'")):
        status, data = _post(server, "/api/mappings/inventory", {"table": "acme_inventory", "column_mapping": {"current_stock": "on_hand"}})

    assert status == 400
    assert "missing required field" in data["error"]

    status, data = _get(server, "/api/mappings")
    assert data["mappings"]["inventory"] == {"status": "not_mapped"}


def test_post_mapping_returns_404_for_an_unknown_dataset(server, isolated_mapping_store):
    status, data = _post(server, "/api/mappings/not_a_real_dataset", {"table": "x", "column_mapping": {"a": "b"}})
    assert status == 404


def test_post_mapping_returns_400_when_table_is_missing(server, isolated_mapping_store):
    status, data = _post(server, "/api/mappings/inventory", {"column_mapping": {"sku": "sku"}})
    assert status == 400
    assert "table" in data["error"]


def test_post_mapping_returns_400_when_column_mapping_is_empty(server, isolated_mapping_store):
    status, data = _post(server, "/api/mappings/inventory", {"table": "inventory", "column_mapping": {}})
    assert status == 400
    assert "column_mapping" in data["error"]


def test_post_mapping_reports_not_connected_when_config_is_missing(server, isolated_mapping_store):
    with patch("data_console.mapping_service.get_postgres_config", side_effect=MissingConfigError("SUPPLYMIND_PG_HOST is not set")):
        status, data = _post(server, "/api/mappings/inventory", {"table": "inventory", "column_mapping": {"sku": "sku"}})

    assert status == 200
    assert data["connected"] is False


def test_post_mark_unavailable_saves_unavailable_status(server, isolated_mapping_store):
    status, data = _post(server, "/api/mappings/delivery_records/unavailable")

    assert status == 200
    status, data = _get(server, "/api/mappings")
    assert data["mappings"]["delivery_records"] == {"status": "unavailable"}


def test_delete_mapping_resets_to_not_mapped(server, isolated_mapping_store):
    with patch("data_console.mapping_service.get_postgres_config"), \
         patch("data_console.mapping_service.validate_profile"):
        _post(server, "/api/mappings/inventory", {"table": "inventory", "column_mapping": {"sku": "sku"}})

    status, data = _delete(server, "/api/mappings/inventory")
    assert status == 200

    status, data = _get(server, "/api/mappings")
    assert data["mappings"]["inventory"] == {"status": "not_mapped"}


def test_get_mapping_preview_returns_400_when_nothing_is_saved(server, isolated_mapping_store):
    status, data = _get(server, "/api/mappings/inventory/preview")
    assert status == 400
    assert "no saved mapping" in data["error"]


def test_get_mapping_preview_returns_remapped_rows(server, isolated_mapping_store):
    with patch("data_console.mapping_service.get_postgres_config"), \
         patch("data_console.mapping_service.validate_profile"):
        _post(server, "/api/mappings/inventory", {"table": "acme_inventory", "column_mapping": {"sku": "item_sku"}})

    with patch("data_console.mapping_service.get_postgres_config"), \
         patch("data_console.mapping_service.fetch_profile_data", return_value=[{"sku": "SKU-1"}, {"sku": "SKU-2"}]):
        status, data = _get(server, "/api/mappings/inventory/preview")

    assert status == 200
    assert data["connected"] is True
    assert data["rows"] == [{"sku": "SKU-1"}, {"sku": "SKU-2"}]
    assert data["row_count"] == 2
    assert data["truncated"] is False


def test_get_mapping_preview_reports_a_schema_change_as_a_clear_error(server, isolated_mapping_store):
    # Simulates the database changing after a mapping was saved - the
    # mapped column no longer exists, caught by fetch_profile_data()'s
    # own internal validate_against_live_schema() call.
    with patch("data_console.mapping_service.get_postgres_config"), \
         patch("data_console.mapping_service.validate_profile"):
        _post(server, "/api/mappings/inventory", {"table": "acme_inventory", "column_mapping": {"sku": "item_sku"}})

    with patch("data_console.mapping_service.get_postgres_config"), \
         patch("data_console.mapping_service.fetch_profile_data", side_effect=SchemaMappingError("mapped column not found")):
        status, data = _get(server, "/api/mappings/inventory/preview")

    assert status == 400
    assert "mapped column not found" in data["error"]


def test_get_mapping_preview_returns_404_for_an_unknown_dataset(server, isolated_mapping_store):
    status, data = _get(server, "/api/mappings/not_a_real_dataset/preview")
    assert status == 404


def test_post_query_columns_returns_columns_and_suggested_mappings(server):
    with patch("data_console.mapping_service.get_postgres_config"), \
         patch("data_console.mapping_service.fetch_columns", return_value=["item_sku", "on_hand"]):
        status, data = _post(server, "/api/query-columns", {"query": "SELECT item_sku, on_hand FROM acme_inventory"})

    assert status == 200
    assert data["connected"] is True
    assert data["columns"] == ["item_sku", "on_hand"]
    assert data["suggested_mappings"]["inventory"]["sku"] == "item_sku"
    assert data["suggested_mappings"]["inventory"]["current_stock"] == "on_hand"


def test_post_query_columns_returns_400_for_an_unsafe_query_without_touching_the_database(server):
    with patch("data_console.mapping_service.get_postgres_config") as mock_config:
        status, data = _post(server, "/api/query-columns", {"query": "DROP TABLE inventory"})

    assert status == 400
    assert "must start with SELECT" in data["error"]
    mock_config.assert_not_called()


def test_post_query_columns_returns_400_when_query_is_missing(server):
    status, data = _post(server, "/api/query-columns", {})
    assert status == 400
    assert "'query'" in data["error"]


def test_post_query_columns_reports_not_connected_when_config_is_missing(server):
    with patch("data_console.mapping_service.get_postgres_config", side_effect=MissingConfigError("no env vars")):
        status, data = _post(server, "/api/query-columns", {"query": "SELECT 1"})

    assert status == 200
    assert data["connected"] is False


def test_post_query_columns_returns_400_with_the_database_error_for_a_malformed_query(server):
    with patch("data_console.mapping_service.get_postgres_config"), \
         patch("data_console.mapping_service.fetch_columns", side_effect=PostgresIntegrationError("syntax error")):
        status, data = _post(server, "/api/query-columns", {"query": "SELECT * FROM not_a_real_table"})

    assert status == 400
    assert "Could not run that query" in data["error"]


def test_post_mapping_from_query_validates_and_saves_then_shows_up_in_get_mappings(server, isolated_mapping_store):
    with patch("data_console.mapping_service.get_postgres_config"), \
         patch("data_console.mapping_service.validate_profile"):
        status, data = _post(
            server,
            "/api/mappings/inventory/from-query",
            {"query": "SELECT item_sku AS sku FROM acme_inventory;", "column_mapping": {"sku": "sku"}},
        )

    assert status == 200
    assert data == {"saved": True}

    status, data = _get(server, "/api/mappings")
    assert data["mappings"]["inventory"] == {
        "status": "mapped",
        "source_kind": "query",
        "table": None,
        "query": "SELECT item_sku AS sku FROM acme_inventory",
        "file_id": None,
        "filename": None,
        "sheet_url": None,
        "column_mapping": {"sku": "sku"},
        "unavailable_fields": [],
        "computed_date_fields": {},
    }


def test_post_mapping_from_query_returns_400_for_an_unsafe_query_and_saves_nothing(server, isolated_mapping_store):
    status, data = _post(
        server, "/api/mappings/inventory/from-query", {"query": "DELETE FROM inventory", "column_mapping": {"sku": "sku"}}
    )

    assert status == 400
    assert "must start with SELECT" in data["error"]
    status, data = _get(server, "/api/mappings")
    assert data["mappings"]["inventory"] == {"status": "not_mapped"}


def test_post_mapping_from_query_returns_400_when_live_validation_fails(server, isolated_mapping_store):
    with patch("data_console.mapping_service.get_postgres_config"), \
         patch("data_console.mapping_service.validate_profile", side_effect=SchemaMappingError("mapped column not found")):
        status, data = _post(
            server,
            "/api/mappings/inventory/from-query",
            {"query": "SELECT 1 AS sku", "column_mapping": {"sku": "sku"}},
        )

    assert status == 400
    assert "mapped column not found" in data["error"]
    status, data = _get(server, "/api/mappings")
    assert data["mappings"]["inventory"] == {"status": "not_mapped"}


def test_post_mapping_from_query_returns_404_for_an_unknown_dataset(server, isolated_mapping_store):
    status, data = _post(
        server, "/api/mappings/not_a_real_dataset/from-query", {"query": "SELECT 1", "column_mapping": {"a": "b"}}
    )
    assert status == 404


def test_post_mapping_from_query_returns_400_when_query_is_missing(server, isolated_mapping_store):
    status, data = _post(server, "/api/mappings/inventory/from-query", {"column_mapping": {"a": "b"}})
    assert status == 400
    assert "'query'" in data["error"]


def test_get_mapping_preview_for_a_query_mapping_returns_remapped_rows(server, isolated_mapping_store):
    with patch("data_console.mapping_service.get_postgres_config"), \
         patch("data_console.mapping_service.validate_profile"):
        _post(
            server,
            "/api/mappings/inventory/from-query",
            {"query": "SELECT item_sku AS sku FROM acme_inventory", "column_mapping": {"sku": "sku"}},
        )

    with patch("data_console.mapping_service.get_postgres_config"), \
         patch("data_console.mapping_service.fetch_profile_data", return_value=[{"sku": "SKU-1"}]):
        status, data = _get(server, "/api/mappings/inventory/preview")

    assert status == 200
    assert data["connected"] is True
    assert data["rows"] == [{"sku": "SKU-1"}]


def test_post_upload_returns_file_id_columns_and_suggested_mappings(server, isolated_uploads_dir):
    csv_bytes = b"item_sku,on_hand\nSKU-1,10\nSKU-2,20\n"
    status, data = _post_raw(server, "/api/uploads", csv_bytes, {"Content-Type": "text/csv", "X-Filename": "inventory.csv"})

    assert status == 200
    assert data["kind"] == "csv"
    assert data["filename"] == "inventory.csv"
    assert data["columns"] == ["item_sku", "on_hand"]
    assert data["suggested_mappings"]["inventory"]["sku"] == "item_sku"
    assert data["suggested_mappings"]["inventory"]["current_stock"] == "on_hand"
    assert (isolated_uploads_dir / f"{data['file_id']}.csv").read_bytes() == csv_bytes


def test_post_upload_of_a_non_utf8_csv_succeeds_instead_of_hanging(server, isolated_uploads_dir):
    # Regression test for a real production bug: a real CSV export (the
    # DataCo Supply Chain dataset) embeds Latin-1 bytes that are not valid
    # UTF-8. Reading it used to raise UnicodeDecodeError deep inside
    # file_store.py with nothing prepared to catch it, which crashed the
    # whole request and left the browser's upload hanging with no response
    # at all (not even an error) - this proves the full HTTP path now
    # returns a clean 200, not that empty-connection failure.
    csv_bytes = "order_date,quantity,city\n2025-08-15,120,".encode("utf-8") + "São Paulo\n".encode("latin-1")
    status, data = _post_raw(server, "/api/uploads", csv_bytes, {"Content-Type": "text/csv", "X-Filename": "orders.csv"})

    assert status == 200
    assert data["columns"] == ["order_date", "quantity", "city"]


def test_post_upload_url_decodes_the_filename_header(server, isolated_uploads_dir):
    status, data = _post_raw(
        server, "/api/uploads", b"a,b\n1,2\n", {"Content-Type": "text/csv", "X-Filename": "orders%20data.csv"}
    )
    assert status == 200
    assert data["filename"] == "orders data.csv"


def test_post_db_connection_tests_before_committing_then_sets_the_runtime_override(server):
    from data_console.runtime_db_config import get_postgres_config

    with patch("data_console.serve_data_console.fetch_rows", return_value=[{"?column?": 1}]) as mock_fetch:
        status, data = _post(
            server, "/api/db-connection",
            {"host": "db.internal", "port": 5433, "database": "supplymind", "user": "alice", "password": "s3cr3t"},
        )

    assert status == 200
    assert data == {"connected": True, "host": "db.internal", "port": 5433, "database": "supplymind", "user": "alice"}
    assert "s3cr3t" not in json.dumps(data)
    mock_fetch.assert_called_once()
    (query,), kwargs = mock_fetch.call_args
    assert query == "SELECT 1"

    config = get_postgres_config()
    assert config.host == "db.internal"
    assert config.port == 5433
    assert config.database == "supplymind"
    assert config.user == "alice"
    assert config.password == "s3cr3t"


def test_post_db_connection_returns_400_and_does_not_set_override_when_connection_fails(server):
    with patch("data_console.serve_data_console.fetch_rows", side_effect=PostgresIntegrationError("auth failed")):
        status, data = _post(
            server, "/api/db-connection",
            {"host": "db.internal", "database": "supplymind", "user": "alice", "password": "wrong"},
        )

    assert status == 400
    assert "Could not connect" in data["error"]
    assert "auth failed" in data["error"]

    with patch("data_console.runtime_db_config.load_postgres_config", side_effect=MissingConfigError("not set")):
        from data_console.runtime_db_config import get_postgres_config

        with pytest.raises(MissingConfigError):
            get_postgres_config()


def test_post_db_connection_returns_400_when_a_required_field_is_missing(server):
    status, data = _post(server, "/api/db-connection", {"host": "db.internal", "database": "d", "user": "u"})
    assert status == 400
    assert "password" in data["error"]


def test_post_upload_returns_400_when_filename_header_is_missing(server, isolated_uploads_dir):
    status, data = _post_raw(server, "/api/uploads", b"a,b\n1,2\n", {"Content-Type": "text/csv"})
    assert status == 400
    assert "X-Filename" in data["error"]


def test_post_upload_returns_400_when_body_is_empty(server, isolated_uploads_dir):
    status, data = _post_raw(server, "/api/uploads", b"", {"Content-Type": "text/csv", "X-Filename": "empty.csv"})
    assert status == 400
    assert "empty" in data["error"]


def test_post_upload_of_a_zip_returns_one_member_per_csv_inside(server, isolated_uploads_dir):
    archive = _zip_bytes({
        "orders.csv": b"order_dt,qty\n2025-01-01,10\n",
        "inventory.csv": b"item_sku,on_hand\nSKU-1,5\n",
    })
    status, data = _post_raw(server, "/api/uploads", archive, {"Content-Type": "application/zip", "X-Filename": "bundle.zip"})

    assert status == 200
    assert data["kind"] == "zip"
    assert data["filename"] == "bundle.zip"
    names = sorted(member["filename"] for member in data["members"])
    assert names == ["inventory.csv", "orders.csv"]
    orders_member = next(m for m in data["members"] if m["filename"] == "orders.csv")
    assert orders_member["columns"] == ["order_dt", "qty"]
    assert orders_member["suggested_mappings"]["customer_orders"]["order_date"] == "order_dt"
    assert (isolated_uploads_dir / f"{orders_member['file_id']}.csv").exists()


def test_post_upload_of_a_zip_with_no_csv_members_returns_400(server, isolated_uploads_dir):
    archive = _zip_bytes({"README.txt": b"nothing to map"})
    status, data = _post_raw(server, "/api/uploads", archive, {"Content-Type": "application/zip", "X-Filename": "bundle.zip"})
    assert status == 400
    assert "no .csv files" in data["error"]


def test_post_upload_of_a_non_zip_blob_named_zip_returns_400(server, isolated_uploads_dir):
    status, data = _post_raw(server, "/api/uploads", b"not actually a zip", {"Content-Type": "application/zip", "X-Filename": "bundle.zip"})
    assert status == 400
    assert "not a valid zip" in data["error"]


def test_post_mapping_from_file_validates_and_saves_then_shows_up_in_get_mappings(
    server, isolated_mapping_store, isolated_uploads_dir
):
    _, upload = _post_raw(
        server,
        "/api/uploads",
        b"item_sku,on_hand\nSKU-1,10\n",
        {"Content-Type": "text/csv", "X-Filename": "inventory.csv"},
    )

    status, data = _post(
        server,
        "/api/mappings/inventory/from-file",
        {
            "file_id": upload["file_id"],
            "filename": upload["filename"],
            "column_mapping": {"sku": "item_sku", "current_stock": "on_hand", "safety_stock": "on_hand", "daily_demand_rate": "on_hand", "lead_time_days": "on_hand"},
        },
    )
    assert status == 200
    assert data == {"saved": True}

    status, data = _get(server, "/api/mappings")
    assert data["mappings"]["inventory"]["status"] == "mapped"
    assert data["mappings"]["inventory"]["source_kind"] == "file"
    assert data["mappings"]["inventory"]["file_id"] == upload["file_id"]
    assert data["mappings"]["inventory"]["filename"] == "inventory.csv"


def test_post_mapping_from_file_returns_400_for_an_incomplete_mapping_and_saves_nothing(
    server, isolated_mapping_store, isolated_uploads_dir
):
    _, upload = _post_raw(
        server, "/api/uploads", b"item_sku,on_hand\nSKU-1,10\n", {"Content-Type": "text/csv", "X-Filename": "inventory.csv"}
    )

    status, data = _post(
        server,
        "/api/mappings/inventory/from-file",
        {"file_id": upload["file_id"], "filename": upload["filename"], "column_mapping": {"sku": "item_sku"}},
    )
    assert status == 400
    assert "missing a column mapping" in data["error"]

    status, data = _get(server, "/api/mappings")
    assert data["mappings"]["inventory"] == {"status": "not_mapped"}


def test_post_mapping_from_file_returns_404_for_an_unknown_dataset(server, isolated_mapping_store):
    status, data = _post(
        server, "/api/mappings/not_a_real_dataset/from-file", {"file_id": "x", "filename": "a.csv", "column_mapping": {"a": "b"}}
    )
    assert status == 404


def test_post_mapping_from_file_returns_400_when_file_id_is_missing(server, isolated_mapping_store):
    status, data = _post(
        server, "/api/mappings/inventory/from-file", {"filename": "a.csv", "column_mapping": {"a": "b"}}
    )
    assert status == 400
    assert "'file_id'" in data["error"]


def test_get_mapping_preview_for_a_file_mapping_returns_remapped_rows(
    server, isolated_mapping_store, isolated_uploads_dir
):
    _, upload = _post_raw(
        server,
        "/api/uploads",
        b"order_dt,qty\n2025-01-01,10\n2025-01-02,20\n",
        {"Content-Type": "text/csv", "X-Filename": "orders.csv"},
    )
    _post(
        server,
        "/api/mappings/customer_orders/from-file",
        {
            "file_id": upload["file_id"],
            "filename": upload["filename"],
            "column_mapping": {"order_date": "order_dt", "quantity": "qty"},
        },
    )

    status, data = _get(server, "/api/mappings/customer_orders/preview")
    assert status == 200
    assert data["connected"] is True
    assert data["rows"] == [
        {"order_date": "2025-01-01", "quantity": "10"},
        {"order_date": "2025-01-02", "quantity": "20"},
    ]


def test_get_mapping_preview_for_a_file_mapping_reports_a_missing_file_as_a_clear_error(
    server, isolated_mapping_store
):
    store = MappingStore(isolated_mapping_store)
    from data_console.mapping_store import DatasetMapping

    store.save(
        "inventory",
        DatasetMapping(
            status="mapped", file_id="does-not-exist", filename="gone.csv", column_mapping={"sku": "sku"}, source_kind="file"
        ),
    )

    status, data = _get(server, "/api/mappings/inventory/preview")
    assert status == 400
    assert "does-not-exist" in data["error"]


_SHEET_URL = "https://docs.google.com/spreadsheets/d/abc/pub?gid=0&single=true&output=csv"


def test_post_sheet_columns_returns_columns_and_suggested_mappings(server):
    with patch("data_console.sheet_mapping_service.fetch_sheet_csv", return_value=b"item_sku,on_hand\nSKU-1,10\n"):
        status, data = _post(server, "/api/sheet-columns", {"url": _SHEET_URL})

    assert status == 200
    assert data["connected"] is True
    assert data["columns"] == ["item_sku", "on_hand"]
    assert data["suggested_mappings"]["inventory"]["sku"] == "item_sku"


def test_post_sheet_columns_returns_400_for_a_non_google_url(server):
    # Exercises the real fetch_sheet_csv() end to end (nothing mocked) -
    # its own host-validation must reject this before any network call is
    # even attempted. test_sheet_fetcher.py separately proves urlopen()
    # itself is never called for a rejected host; not re-asserted here
    # since patching urllib.request.urlopen globally would also intercept
    # this test's own HTTP client talking to the local test server.
    status, data = _post(server, "/api/sheet-columns", {"url": "https://example.com/data.csv"})

    assert status == 400
    assert "docs.google.com" in data["error"]


def test_post_sheet_columns_returns_400_when_the_link_is_not_actually_public(server):
    from data_console.sheet_fetcher import SheetFetchError

    with patch("data_console.sheet_mapping_service.fetch_sheet_csv", side_effect=SheetFetchError("not public")):
        status, data = _post(server, "/api/sheet-columns", {"url": _SHEET_URL})

    assert status == 400
    assert "not public" in data["error"]


def test_post_sheet_columns_returns_400_when_url_is_missing(server):
    status, data = _post(server, "/api/sheet-columns", {})
    assert status == 400
    assert "'url'" in data["error"]


def test_post_mapping_from_sheet_validates_and_saves_then_shows_up_in_get_mappings(server, isolated_mapping_store):
    with patch("data_console.sheet_mapping_service.fetch_sheet_csv", return_value=b"order_dt,qty\n2025-01-01,10\n"):
        status, data = _post(
            server,
            "/api/mappings/customer_orders/from-sheet",
            {"url": _SHEET_URL, "column_mapping": {"order_date": "order_dt", "quantity": "qty"}},
        )

    assert status == 200
    assert data == {"saved": True}

    status, data = _get(server, "/api/mappings")
    assert data["mappings"]["customer_orders"] == {
        "status": "mapped",
        "source_kind": "sheet",
        "table": None,
        "query": None,
        "file_id": None,
        "filename": None,
        "sheet_url": _SHEET_URL,
        "column_mapping": {"order_date": "order_dt", "quantity": "qty"},
        "unavailable_fields": [],
        "computed_date_fields": {},
    }


def test_post_mapping_from_sheet_returns_400_for_an_incomplete_mapping_and_saves_nothing(server, isolated_mapping_store):
    with patch("data_console.sheet_mapping_service.fetch_sheet_csv", return_value=b"order_dt,qty\n2025-01-01,10\n"):
        status, data = _post(
            server,
            "/api/mappings/customer_orders/from-sheet",
            {"url": _SHEET_URL, "column_mapping": {"order_date": "order_dt"}},
        )

    assert status == 400
    assert "missing a column mapping" in data["error"]
    status, data = _get(server, "/api/mappings")
    assert data["mappings"]["customer_orders"] == {"status": "not_mapped"}


def test_post_mapping_from_sheet_returns_404_for_an_unknown_dataset(server, isolated_mapping_store):
    status, data = _post(
        server, "/api/mappings/not_a_real_dataset/from-sheet", {"url": _SHEET_URL, "column_mapping": {"a": "b"}}
    )
    assert status == 404


def test_post_mapping_from_sheet_returns_400_when_url_is_missing(server, isolated_mapping_store):
    status, data = _post(server, "/api/mappings/customer_orders/from-sheet", {"column_mapping": {"a": "b"}})
    assert status == 400
    assert "'url'" in data["error"]


def test_get_mapping_preview_for_a_sheet_mapping_returns_remapped_rows(server, isolated_mapping_store):
    with patch("data_console.sheet_mapping_service.fetch_sheet_csv", return_value=b"order_dt,qty\n2025-01-01,10\n"):
        _post(
            server,
            "/api/mappings/customer_orders/from-sheet",
            {"url": _SHEET_URL, "column_mapping": {"order_date": "order_dt", "quantity": "qty"}},
        )

        status, data = _get(server, "/api/mappings/customer_orders/preview")

    assert status == 200
    assert data["connected"] is True
    assert data["rows"] == [{"order_date": "2025-01-01", "quantity": "10"}]


def test_get_mapping_preview_for_a_sheet_mapping_reports_a_broken_link_as_a_clear_error(server, isolated_mapping_store):
    from data_console.sheet_fetcher import SheetFetchError

    with patch("data_console.sheet_mapping_service.fetch_sheet_csv", return_value=b"order_dt,qty\n2025-01-01,10\n"):
        _post(
            server,
            "/api/mappings/customer_orders/from-sheet",
            {"url": _SHEET_URL, "column_mapping": {"order_date": "order_dt", "quantity": "qty"}},
        )

    with patch("data_console.sheet_mapping_service.fetch_sheet_csv", side_effect=SheetFetchError("no longer public")):
        status, data = _get(server, "/api/mappings/customer_orders/preview")

    assert status == 400
    assert "no longer public" in data["error"]
