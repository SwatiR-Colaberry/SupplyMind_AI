from unittest.mock import patch

from data_console.schema_inspector import ColumnInfo, list_columns, list_tables
from data_integration.config import PostgresConfig

_CONFIG = PostgresConfig(host="h", port=5432, database="d", user="u", password="p")


def test_list_tables_returns_table_names_in_the_order_fetch_rows_gives_them():
    with patch("data_console.schema_inspector.get_postgres_config", return_value=_CONFIG), \
         patch("data_console.schema_inspector.fetch_rows") as mock_fetch:
        mock_fetch.return_value = [{"table_name": "customer_orders"}, {"table_name": "inventory"}]

        tables = list_tables()

    assert tables == ["customer_orders", "inventory"]
    mock_fetch.assert_called_once()
    (query,), kwargs = mock_fetch.call_args
    assert "information_schema.tables" in query
    assert kwargs.get("params", ()) == ()
    assert kwargs["config"] == _CONFIG


def test_list_tables_with_no_tables_returns_empty_list():
    with patch("data_console.schema_inspector.get_postgres_config", return_value=_CONFIG), \
         patch("data_console.schema_inspector.fetch_rows", return_value=[]):
        assert list_tables() == []


def test_list_columns_returns_typed_column_info_in_ordinal_order():
    with patch("data_console.schema_inspector.get_postgres_config", return_value=_CONFIG), \
         patch("data_console.schema_inspector.fetch_rows") as mock_fetch:
        mock_fetch.return_value = [
            {"column_name": "sku", "data_type": "text", "is_nullable": "NO"},
            {"column_name": "current_stock", "data_type": "numeric", "is_nullable": "YES"},
        ]

        columns = list_columns("inventory")

    assert columns == [
        ColumnInfo(name="sku", data_type="text", nullable=False),
        ColumnInfo(name="current_stock", data_type="numeric", nullable=True),
    ]
    args, kwargs = mock_fetch.call_args
    assert kwargs["params"] == ("inventory",)
    assert kwargs["config"] == _CONFIG


def test_list_columns_for_an_unknown_table_returns_empty_list_not_an_error():
    with patch("data_console.schema_inspector.get_postgres_config", return_value=_CONFIG), \
         patch("data_console.schema_inspector.fetch_rows", return_value=[]):
        assert list_columns("does_not_exist") == []
