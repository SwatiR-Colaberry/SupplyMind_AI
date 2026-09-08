from unittest.mock import patch

import pytest

from data_console.mapping_store import DatasetMapping, MappingStore
from data_console.sheet_fetcher import SheetFetchError
from data_console.sheet_mapping_service import preview_sheet_mapping, probe_sheet_columns, save_mapping_from_sheet
from data_integration.connection_profile import SchemaMappingError

_URL = "https://docs.google.com/spreadsheets/d/abc/pub?gid=0&single=true&output=csv"


def test_probe_sheet_columns_returns_the_real_header_row():
    with patch("data_console.sheet_mapping_service.fetch_sheet_csv", return_value=b"item_sku,on_hand\nSKU-1,10\n"):
        assert probe_sheet_columns(_URL) == ["item_sku", "on_hand"]


def test_save_mapping_from_sheet_validates_against_real_headers_before_persisting(tmp_path):
    store = MappingStore(tmp_path / "mappings.json")
    with patch("data_console.sheet_mapping_service.fetch_sheet_csv", return_value=b"order_dt,qty\n2025-01-01,10\n"):
        save_mapping_from_sheet(
            "customer_orders", _URL, {"order_date": "order_dt", "quantity": "qty"}, store=store
        )

    saved = store.get("customer_orders")
    assert saved == DatasetMapping(
        status="mapped",
        sheet_url=_URL,
        column_mapping={"order_date": "order_dt", "quantity": "qty"},
        source_kind="sheet",
    )


def test_save_mapping_from_sheet_rejects_a_mapped_column_not_present_in_the_sheet(tmp_path):
    store = MappingStore(tmp_path / "mappings.json")
    with patch("data_console.sheet_mapping_service.fetch_sheet_csv", return_value=b"order_dt\n2025-01-01\n"):
        with pytest.raises(SchemaMappingError):
            save_mapping_from_sheet(
                "customer_orders", _URL, {"order_date": "order_dt", "quantity": "not_there"}, store=store
            )

    assert store.get("customer_orders") is None


def test_save_mapping_from_sheet_propagates_a_fetch_error_and_saves_nothing(tmp_path):
    store = MappingStore(tmp_path / "mappings.json")
    with patch("data_console.sheet_mapping_service.fetch_sheet_csv", side_effect=SheetFetchError("not public")):
        with pytest.raises(SheetFetchError):
            save_mapping_from_sheet("customer_orders", _URL, {"order_date": "d", "quantity": "q"}, store=store)

    assert store.get("customer_orders") is None


def test_preview_sheet_mapping_returns_remapped_rows():
    mapping = DatasetMapping(
        status="mapped", sheet_url=_URL, column_mapping={"order_date": "order_dt", "quantity": "qty"}, source_kind="sheet"
    )
    with patch(
        "data_console.sheet_mapping_service.fetch_sheet_csv",
        return_value=b"order_dt,qty\n2025-01-01,10\n2025-01-02,20\n",
    ):
        result = preview_sheet_mapping("customer_orders", mapping)

    assert result.rows == [
        {"order_date": "2025-01-01", "quantity": "10"},
        {"order_date": "2025-01-02", "quantity": "20"},
    ]
    assert result.row_count == 2
    assert result.truncated is False


def test_preview_sheet_mapping_marks_truncated_at_the_row_limit():
    from data_console.query_builder import PREVIEW_ROW_LIMIT

    mapping = DatasetMapping(
        status="mapped", sheet_url=_URL, column_mapping={"order_date": "d", "quantity": "q"}, source_kind="sheet"
    )
    header = b"d,q\n"
    body = b"".join(f"2025-01-01,{i}\n".encode() for i in range(PREVIEW_ROW_LIMIT + 5))
    with patch("data_console.sheet_mapping_service.fetch_sheet_csv", return_value=header + body):
        result = preview_sheet_mapping("customer_orders", mapping)

    assert result.row_count == PREVIEW_ROW_LIMIT
    assert result.truncated is True


def test_preview_sheet_mapping_reports_a_now_broken_link_as_a_clear_error():
    # The sheet's sharing was revoked, or it was deleted, since Save Mapping.
    mapping = DatasetMapping(
        status="mapped", sheet_url=_URL, column_mapping={"order_date": "d", "quantity": "q"}, source_kind="sheet"
    )
    with patch("data_console.sheet_mapping_service.fetch_sheet_csv", side_effect=SheetFetchError("no longer public")):
        with pytest.raises(SheetFetchError):
            preview_sheet_mapping("customer_orders", mapping)


def test_preview_sheet_mapping_reports_a_schema_change_as_a_clear_error():
    # The sheet's own columns were renamed since Save Mapping.
    mapping = DatasetMapping(
        status="mapped", sheet_url=_URL, column_mapping={"order_date": "order_dt", "quantity": "qty"}, source_kind="sheet"
    )
    with patch("data_console.sheet_mapping_service.fetch_sheet_csv", return_value=b"different_column\nx\n"):
        with pytest.raises(SchemaMappingError):
            preview_sheet_mapping("customer_orders", mapping)
