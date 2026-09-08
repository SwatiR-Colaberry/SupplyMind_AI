import pytest

from data_console.query_builder import (
    PREVIEW_ROW_LIMIT,
    InvalidSelectionError,
    JoinSpec,
    QuerySelection,
    SelectedColumn,
    build_preview_query,
)

INVENTORY_SCHEMA = {
    "inventory": {"sku", "current_stock", "safety_stock"},
    "customer_orders": {"order_date", "quantity", "sku"},
}


def test_builds_a_simple_select_with_a_hard_row_limit():
    selection = QuerySelection(
        base_table="inventory",
        columns=[SelectedColumn("inventory", "sku"), SelectedColumn("inventory", "current_stock")],
    )

    query = build_preview_query(selection, INVENTORY_SCHEMA)

    assert query == f'SELECT "inventory"."sku", "inventory"."current_stock" FROM "inventory" LIMIT {PREVIEW_ROW_LIMIT}'


def test_builds_a_join_query_with_qualified_columns_on_both_sides():
    selection = QuerySelection(
        base_table="inventory",
        columns=[SelectedColumn("inventory", "sku"), SelectedColumn("customer_orders", "quantity")],
        join=JoinSpec("inventory", "sku", "customer_orders", "sku"),
    )

    query = build_preview_query(selection, INVENTORY_SCHEMA)

    assert query == (
        'SELECT "inventory"."sku", "customer_orders"."quantity" FROM "inventory" '
        'JOIN "customer_orders" ON "inventory"."sku" = "customer_orders"."sku" '
        f'LIMIT {PREVIEW_ROW_LIMIT}'
    )


def test_join_query_puts_the_non_base_table_after_join_regardless_of_left_right_order():
    # The join's "left"/"right" are just labels for the ON clause - the base
    # table should never be the one that gets a second JOIN clause added.
    selection = QuerySelection(
        base_table="customer_orders",
        columns=[SelectedColumn("customer_orders", "quantity")],
        join=JoinSpec("inventory", "sku", "customer_orders", "sku"),
    )

    query = build_preview_query(selection, INVENTORY_SCHEMA)

    assert query.startswith('SELECT "customer_orders"."quantity" FROM "customer_orders" JOIN "inventory"')


def test_raises_when_no_columns_are_selected():
    selection = QuerySelection(base_table="inventory", columns=[])

    with pytest.raises(InvalidSelectionError, match="select at least one column"):
        build_preview_query(selection, INVENTORY_SCHEMA)


def test_raises_when_base_table_does_not_exist_in_the_live_schema():
    selection = QuerySelection(base_table="not_a_real_table", columns=[SelectedColumn("not_a_real_table", "x")])

    with pytest.raises(InvalidSelectionError, match="does not exist"):
        build_preview_query(selection, INVENTORY_SCHEMA)


def test_raises_when_a_selected_column_does_not_exist():
    selection = QuerySelection(base_table="inventory", columns=[SelectedColumn("inventory", "not_a_real_column")])

    with pytest.raises(InvalidSelectionError, match="does not exist"):
        build_preview_query(selection, INVENTORY_SCHEMA)


def test_raises_when_a_column_references_a_table_outside_the_selection():
    selection = QuerySelection(
        base_table="inventory",
        columns=[SelectedColumn("inventory", "sku"), SelectedColumn("customer_orders", "quantity")],
        join=None,
    )

    with pytest.raises(InvalidSelectionError, match="isn't part of this selection"):
        build_preview_query(selection, INVENTORY_SCHEMA)


def test_raises_when_joining_a_table_to_itself():
    selection = QuerySelection(
        base_table="inventory",
        columns=[SelectedColumn("inventory", "sku")],
        join=JoinSpec("inventory", "sku", "inventory", "sku"),
    )

    with pytest.raises(InvalidSelectionError, match="cannot join a table to itself"):
        build_preview_query(selection, INVENTORY_SCHEMA)


def test_raises_when_the_join_does_not_include_the_base_table():
    schema = {**INVENTORY_SCHEMA, "delivery_records": {"po_id"}}
    selection = QuerySelection(
        base_table="inventory",
        columns=[SelectedColumn("inventory", "sku")],
        join=JoinSpec("customer_orders", "sku", "delivery_records", "po_id"),
    )

    with pytest.raises(InvalidSelectionError, match="must include the base table"):
        build_preview_query(selection, schema)


def test_raises_when_a_join_column_does_not_exist():
    selection = QuerySelection(
        base_table="inventory",
        columns=[SelectedColumn("inventory", "sku")],
        join=JoinSpec("inventory", "not_a_real_column", "customer_orders", "sku"),
    )

    with pytest.raises(InvalidSelectionError, match="does not exist"):
        build_preview_query(selection, INVENTORY_SCHEMA)


def test_identifiers_containing_a_double_quote_are_safely_escaped_not_broken_out_of():
    schema = {'weird"table': {'weird"column'}}
    selection = QuerySelection(base_table='weird"table', columns=[SelectedColumn('weird"table', 'weird"column')])

    query = build_preview_query(selection, schema)

    assert query == f'SELECT "weird""table"."weird""column" FROM "weird""table" LIMIT {PREVIEW_ROW_LIMIT}'
