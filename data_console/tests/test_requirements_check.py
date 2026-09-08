from data_console.column_requirements import DELIVERY_RECORDS, INVENTORY
from data_console.requirements_check import check_against, check_against_all_known_datasets


def test_satisfied_when_every_required_column_is_present():
    result = check_against(["sku", "current_stock", "safety_stock", "daily_demand_rate", "lead_time_days"], INVENTORY)

    assert result.satisfied is True
    assert all(c.present for c in result.required)


def test_unsatisfied_when_a_required_column_is_missing():
    result = check_against(["sku", "current_stock", "safety_stock", "daily_demand_rate"], INVENTORY)

    assert result.satisfied is False
    missing = [c.name for c in result.required if not c.present]
    assert missing == ["lead_time_days"]


def test_extra_columns_beyond_what_is_required_do_not_affect_satisfaction():
    result = check_against(
        ["sku", "current_stock", "safety_stock", "daily_demand_rate", "lead_time_days", "warehouse_zone"], INVENTORY
    )

    assert result.satisfied is True


def test_matching_is_case_insensitive_and_trims_whitespace():
    result = check_against(["SKU", " Current_Stock ", "SAFETY_STOCK", "daily_demand_rate", "LEAD_TIME_DAYS"], INVENTORY)

    assert result.satisfied is True


def test_optional_columns_are_checked_but_never_affect_satisfied():
    satisfied_without_optional = check_against(["po_id", "expected_date", "actual_date"], DELIVERY_RECORDS)
    satisfied_with_optional = check_against(
        ["po_id", "expected_date", "actual_date", "supplier", "transportation_cost"], DELIVERY_RECORDS
    )

    assert satisfied_without_optional.satisfied is True
    assert all(not c.present for c in satisfied_without_optional.optional)
    assert satisfied_with_optional.satisfied is True
    assert all(c.present for c in satisfied_with_optional.optional)


def test_empty_column_list_is_unsatisfied_for_every_required_field():
    result = check_against([], INVENTORY)

    assert result.satisfied is False
    assert all(not c.present for c in result.required)


def test_check_against_all_known_datasets_returns_one_result_per_dataset_in_fixed_order():
    results = check_against_all_known_datasets(["sku", "current_stock", "safety_stock", "daily_demand_rate", "lead_time_days"])

    assert [r.dataset_name for r in results] == ["customer_orders", "inventory", "delivery_records"]
    by_name = {r.dataset_name: r for r in results}
    assert by_name["inventory"].satisfied is True
    assert by_name["customer_orders"].satisfied is False
    assert by_name["delivery_records"].satisfied is False
