from unittest.mock import patch

import pytest

from data_console.file_mapping_service import preview_file_mapping, probe_upload_columns, save_mapping_from_file
from data_console.mapping_store import DatasetMapping, MappingStore
from data_integration.connection_profile import SchemaMappingError


def test_probe_upload_columns_reads_the_real_header_row():
    with patch("data_console.file_mapping_service.file_store.read_upload_columns", return_value=["sku", "on_hand"]):
        assert probe_upload_columns("file-1") == ["sku", "on_hand"]


def test_save_mapping_from_file_validates_against_real_headers_before_persisting(tmp_path):
    store = MappingStore(tmp_path / "mappings.json")
    with patch("data_console.file_mapping_service.file_store.read_upload_columns", return_value=["order_dt", "qty"]):
        save_mapping_from_file(
            "customer_orders", "file-1", "orders.csv", {"order_date": "order_dt", "quantity": "qty"}, store=store
        )

    saved = store.get("customer_orders")
    assert saved == DatasetMapping(
        status="mapped",
        file_id="file-1",
        filename="orders.csv",
        column_mapping={"order_date": "order_dt", "quantity": "qty"},
        source_kind="file",
    )


def test_save_mapping_from_file_rejects_a_mapped_column_not_present_in_the_file(tmp_path):
    store = MappingStore(tmp_path / "mappings.json")
    with patch("data_console.file_mapping_service.file_store.read_upload_columns", return_value=["sku"]):
        with pytest.raises(SchemaMappingError):
            save_mapping_from_file("inventory", "file-1", "inventory.csv", {"sku": "sku", "current_stock": "not_there"}, store=store)

    assert store.get("inventory") is None


def test_save_mapping_from_file_rejects_an_incomplete_mapping(tmp_path):
    store = MappingStore(tmp_path / "mappings.json")
    with patch("data_console.file_mapping_service.file_store.read_upload_columns", return_value=["sku"]):
        with pytest.raises(SchemaMappingError):
            save_mapping_from_file("inventory", "file-1", "inventory.csv", {"sku": "sku"}, store=store)

    assert store.get("inventory") is None


def test_preview_file_mapping_returns_remapped_rows():
    mapping = DatasetMapping(
        status="mapped", file_id="file-1", filename="orders.csv", column_mapping={"order_date": "order_dt", "quantity": "qty"}, source_kind="file"
    )
    with patch("data_console.file_mapping_service.file_store.read_upload_columns", return_value=["order_dt", "qty"]), \
         patch(
             "data_console.file_mapping_service.file_store.read_upload_rows",
             return_value=[{"order_dt": "2025-01-01", "qty": "10"}, {"order_dt": "2025-01-02", "qty": "20"}],
         ):
        result = preview_file_mapping("customer_orders", mapping)

    assert result.rows == [
        {"order_date": "2025-01-01", "quantity": "10"},
        {"order_date": "2025-01-02", "quantity": "20"},
    ]
    assert result.row_count == 2
    assert result.truncated is False


def test_preview_file_mapping_marks_truncated_at_the_row_limit():
    from data_console.query_builder import PREVIEW_ROW_LIMIT

    mapping = DatasetMapping(
        status="mapped", file_id="file-1", filename="big.csv", column_mapping={"order_date": "d", "quantity": "q"}, source_kind="file"
    )
    full_page = [{"d": f"2025-01-{i:02d}", "q": str(i)} for i in range(1, PREVIEW_ROW_LIMIT + 6)]
    with patch("data_console.file_mapping_service.file_store.read_upload_columns", return_value=["d", "q"]), \
         patch("data_console.file_mapping_service.file_store.read_upload_rows", return_value=full_page):
        result = preview_file_mapping("customer_orders", mapping)

    assert result.row_count == PREVIEW_ROW_LIMIT
    assert result.truncated is True


def test_preview_file_mapping_reports_a_schema_change_as_a_clear_error():
    # The file was re-uploaded/changed since Save Mapping - a mapped
    # column no longer exists in its current header row.
    mapping = DatasetMapping(
        status="mapped", file_id="file-1", filename="orders.csv", column_mapping={"order_date": "order_dt", "quantity": "qty"}, source_kind="file"
    )
    with patch("data_console.file_mapping_service.file_store.read_upload_columns", return_value=["different_column"]):
        with pytest.raises(SchemaMappingError):
            preview_file_mapping("customer_orders", mapping)
