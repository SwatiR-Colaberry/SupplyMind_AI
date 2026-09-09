from unittest.mock import patch

import pytest

from data_console.mapping_service import (
    clear_mapping,
    mark_unavailable,
    preview_mapping,
    probe_query_columns,
    save_mapping,
    save_mapping_from_query,
)
from data_console.mapping_store import DatasetMapping, MappingStore
from data_console.raw_query_validator import UnsafeQueryError
from data_integration.config import PostgresConfig
from data_integration.connection_profile import SchemaMappingError

_CONFIG = PostgresConfig(host="h", port=5432, database="d", user="u", password="p")


def test_save_mapping_validates_before_persisting(tmp_path):
    store = MappingStore(tmp_path / "mappings.json")

    with patch("data_console.mapping_service.get_postgres_config", return_value=_CONFIG), \
         patch("data_console.mapping_service.validate_profile") as mock_validate:
        save_mapping("inventory", "acme_inventory", {"sku": "item_sku"}, store=store)

    mock_validate.assert_called_once()
    saved = store.get("inventory")
    assert saved == DatasetMapping(status="mapped", table="acme_inventory", column_mapping={"sku": "item_sku"})


def test_save_mapping_uses_a_properly_quoted_select_star_query(tmp_path):
    store = MappingStore(tmp_path / "mappings.json")

    with patch("data_console.mapping_service.get_postgres_config", return_value=_CONFIG), \
         patch("data_console.mapping_service.validate_profile") as mock_validate:
        save_mapping("inventory", 'weird"table', {"sku": "sku"}, store=store)

    profile = mock_validate.call_args[0][0]
    assert profile.query == 'SELECT * FROM "weird""table"'


def test_save_mapping_does_not_persist_when_validation_fails(tmp_path):
    store = MappingStore(tmp_path / "mappings.json")

    with patch("data_console.mapping_service.get_postgres_config", return_value=_CONFIG), \
         patch("data_console.mapping_service.validate_profile", side_effect=SchemaMappingError("missing field")):
        with pytest.raises(SchemaMappingError):
            save_mapping("inventory", "acme_inventory", {}, store=store)

    assert store.get("inventory") is None


def test_mark_unavailable_persists_the_unavailable_status(tmp_path):
    store = MappingStore(tmp_path / "mappings.json")

    mark_unavailable("delivery_records", store=store)

    assert store.get("delivery_records") == DatasetMapping(status="unavailable")


def test_clear_mapping_resets_to_not_mapped(tmp_path):
    store = MappingStore(tmp_path / "mappings.json")
    store.save("inventory", DatasetMapping(status="mapped", table="inventory", column_mapping={"sku": "sku"}))

    clear_mapping("inventory", store=store)

    assert store.get("inventory") is None


def test_preview_mapping_raises_lookup_error_when_nothing_is_saved(tmp_path):
    store = MappingStore(tmp_path / "mappings.json")

    with pytest.raises(LookupError):
        preview_mapping("inventory", store=store)


def test_preview_mapping_raises_lookup_error_when_marked_unavailable(tmp_path):
    store = MappingStore(tmp_path / "mappings.json")
    mark_unavailable("inventory", store=store)

    with pytest.raises(LookupError):
        preview_mapping("inventory", store=store)


def test_preview_mapping_returns_remapped_rows_with_a_limit_appended(tmp_path):
    store = MappingStore(tmp_path / "mappings.json")
    store.save("inventory", DatasetMapping(status="mapped", table="acme_inventory", column_mapping={"sku": "item_sku"}))

    with patch("data_console.mapping_service.get_postgres_config", return_value=_CONFIG), \
         patch("data_console.mapping_service.fetch_profile_data") as mock_fetch:
        mock_fetch.return_value = [{"sku": "SKU-1"}, {"sku": "SKU-2"}]
        result = preview_mapping("inventory", store=store)

    assert result.rows == [{"sku": "SKU-1"}, {"sku": "SKU-2"}]
    assert result.row_count == 2
    assert result.truncated is False
    profile = mock_fetch.call_args[0][0]
    assert profile.query == 'SELECT * FROM (SELECT * FROM "acme_inventory") AS mapping_preview LIMIT 500'


def test_preview_mapping_marks_truncated_at_the_row_limit(tmp_path):
    from data_console.query_builder import PREVIEW_ROW_LIMIT

    store = MappingStore(tmp_path / "mappings.json")
    store.save("inventory", DatasetMapping(status="mapped", table="inventory", column_mapping={"sku": "sku"}))
    full_page = [{"sku": f"SKU-{i}"} for i in range(PREVIEW_ROW_LIMIT)]

    with patch("data_console.mapping_service.get_postgres_config", return_value=_CONFIG), \
         patch("data_console.mapping_service.fetch_profile_data", return_value=full_page):
        result = preview_mapping("inventory", store=store)

    assert result.truncated is True


def test_probe_query_columns_rejects_an_unsafe_query_before_touching_the_database():
    with patch("data_console.mapping_service.get_postgres_config") as mock_config:
        with pytest.raises(UnsafeQueryError):
            probe_query_columns("DROP TABLE inventory")
    mock_config.assert_not_called()


def test_probe_query_columns_returns_the_cleaned_querys_result_columns():
    with patch("data_console.mapping_service.get_postgres_config", return_value=_CONFIG), \
         patch("data_console.mapping_service.fetch_columns", return_value=["item_sku", "on_hand"]) as mock_fetch:
        columns = probe_query_columns("SELECT item_sku, on_hand FROM acme_inventory;")

    assert columns == ["item_sku", "on_hand"]
    mock_fetch.assert_called_once_with("SELECT item_sku, on_hand FROM acme_inventory", config=_CONFIG)


def test_save_mapping_from_query_rejects_an_unsafe_query_and_does_not_persist(tmp_path):
    store = MappingStore(tmp_path / "mappings.json")

    with pytest.raises(UnsafeQueryError):
        save_mapping_from_query("inventory", "DELETE FROM inventory", {"sku": "item_sku"}, store=store)

    assert store.get("inventory") is None


def test_save_mapping_from_query_validates_then_persists_the_cleaned_query(tmp_path):
    store = MappingStore(tmp_path / "mappings.json")

    with patch("data_console.mapping_service.get_postgres_config", return_value=_CONFIG), \
         patch("data_console.mapping_service.validate_profile") as mock_validate:
        save_mapping_from_query(
            "inventory",
            "SELECT i.item_sku AS sku FROM acme_inventory i;",
            {"sku": "sku"},
            store=store,
        )

    profile = mock_validate.call_args[0][0]
    assert profile.query == "SELECT i.item_sku AS sku FROM acme_inventory i"
    saved = store.get("inventory")
    assert saved == DatasetMapping(
        status="mapped",
        query="SELECT i.item_sku AS sku FROM acme_inventory i",
        column_mapping={"sku": "sku"},
        source_kind="query",
    )


def test_save_mapping_from_query_does_not_persist_when_live_validation_fails(tmp_path):
    store = MappingStore(tmp_path / "mappings.json")

    with patch("data_console.mapping_service.get_postgres_config", return_value=_CONFIG), \
         patch("data_console.mapping_service.validate_profile", side_effect=SchemaMappingError("missing field")):
        with pytest.raises(SchemaMappingError):
            save_mapping_from_query("inventory", "SELECT 1 AS sku", {}, store=store)

    assert store.get("inventory") is None


def test_preview_mapping_for_a_query_mapping_wraps_the_saved_query(tmp_path):
    store = MappingStore(tmp_path / "mappings.json")
    store.save(
        "inventory",
        DatasetMapping(
            status="mapped",
            query="SELECT item_sku AS sku FROM acme_inventory",
            column_mapping={"sku": "sku"},
            source_kind="query",
        ),
    )

    with patch("data_console.mapping_service.get_postgres_config", return_value=_CONFIG), \
         patch("data_console.mapping_service.fetch_profile_data", return_value=[{"sku": "SKU-1"}]) as mock_fetch:
        result = preview_mapping("inventory", store=store)

    assert result.rows == [{"sku": "SKU-1"}]
    profile = mock_fetch.call_args[0][0]
    assert profile.query == (
        "SELECT * FROM (SELECT item_sku AS sku FROM acme_inventory) AS mapping_preview LIMIT 500"
    )
