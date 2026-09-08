from unittest.mock import patch

import pytest

from data_console.preview_runner import run_preview
from data_console.query_builder import InvalidSelectionError, JoinSpec, PREVIEW_ROW_LIMIT, QuerySelection, SelectedColumn
from data_console.schema_inspector import ColumnInfo


def _columns(*names):
    return [ColumnInfo(name=n, data_type="text", nullable=True) for n in names]


def test_run_preview_fetches_live_schema_builds_query_and_returns_rows():
    selection = QuerySelection(base_table="inventory", columns=[SelectedColumn("inventory", "sku")])

    with patch("data_console.preview_runner.schema_inspector.list_columns") as mock_columns, \
         patch("data_console.preview_runner.fetch_rows") as mock_fetch:
        mock_columns.return_value = _columns("sku", "current_stock")
        mock_fetch.return_value = [{"sku": "SKU-1"}, {"sku": "SKU-2"}]

        result = run_preview(selection)

    assert result.sql == f'SELECT "inventory"."sku" FROM "inventory" LIMIT {PREVIEW_ROW_LIMIT}'
    assert result.rows == [{"sku": "SKU-1"}, {"sku": "SKU-2"}]
    assert result.row_count == 2
    assert result.truncated is False
    mock_columns.assert_called_once_with("inventory")


def test_run_preview_marks_truncated_when_row_count_hits_the_cap():
    selection = QuerySelection(base_table="inventory", columns=[SelectedColumn("inventory", "sku")])
    full_page = [{"sku": f"SKU-{i}"} for i in range(PREVIEW_ROW_LIMIT)]

    with patch("data_console.preview_runner.schema_inspector.list_columns", return_value=_columns("sku")), \
         patch("data_console.preview_runner.fetch_rows", return_value=full_page):
        result = run_preview(selection)

    assert result.row_count == PREVIEW_ROW_LIMIT
    assert result.truncated is True


def test_run_preview_fetches_schema_for_both_sides_of_a_join():
    selection = QuerySelection(
        base_table="inventory",
        columns=[SelectedColumn("inventory", "sku"), SelectedColumn("customer_orders", "quantity")],
        join=JoinSpec("inventory", "sku", "customer_orders", "sku"),
    )

    with patch("data_console.preview_runner.schema_inspector.list_columns") as mock_columns, \
         patch("data_console.preview_runner.fetch_rows", return_value=[]):
        mock_columns.side_effect = lambda table: _columns("sku", "quantity")
        run_preview(selection)

    called_tables = {call.args[0] for call in mock_columns.call_args_list}
    assert called_tables == {"inventory", "customer_orders"}


def test_run_preview_raises_invalid_selection_before_ever_calling_fetch_rows():
    selection = QuerySelection(base_table="inventory", columns=[SelectedColumn("inventory", "not_a_real_column")])

    with patch("data_console.preview_runner.schema_inspector.list_columns", return_value=_columns("sku")), \
         patch("data_console.preview_runner.fetch_rows") as mock_fetch:
        with pytest.raises(InvalidSelectionError):
            run_preview(selection)

    mock_fetch.assert_not_called()


def test_run_preview_treats_a_table_with_zero_columns_back_as_nonexistent():
    selection = QuerySelection(base_table="empty_table", columns=[SelectedColumn("empty_table", "x")])

    with patch("data_console.preview_runner.schema_inspector.list_columns", return_value=[]), \
         patch("data_console.preview_runner.fetch_rows") as mock_fetch:
        with pytest.raises(InvalidSelectionError, match="does not exist"):
            run_preview(selection)

    mock_fetch.assert_not_called()
