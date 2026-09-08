from data_console.column_requirements import ALL_DATASETS, BY_NAME, CUSTOMER_ORDERS, DELIVERY_RECORDS, INVENTORY


def test_all_datasets_have_at_least_one_required_column():
    for dataset in ALL_DATASETS:
        assert len(dataset.required) > 0, f"{dataset.dataset_name} has no required columns"


def test_by_name_covers_every_dataset_with_matching_keys():
    assert set(BY_NAME.keys()) == {d.dataset_name for d in ALL_DATASETS}
    assert BY_NAME["inventory"] is INVENTORY
    assert BY_NAME["customer_orders"] is CUSTOMER_ORDERS
    assert BY_NAME["delivery_records"] is DELIVERY_RECORDS


def test_inventory_requires_the_five_fields_the_stockout_risk_agent_actually_reads():
    required_names = {c.name for c in INVENTORY.required}
    assert required_names == {"sku", "current_stock", "safety_stock", "daily_demand_rate", "lead_time_days"}


def test_delivery_records_marks_supplier_and_transportation_cost_as_optional_not_required():
    required_names = {c.name for c in DELIVERY_RECORDS.required}
    optional_names = {c.name for c in DELIVERY_RECORDS.optional}
    assert required_names == {"po_id", "expected_date", "actual_date"}
    assert optional_names == {"supplier", "transportation_cost"}


def test_every_column_has_a_non_empty_description_and_example():
    for dataset in ALL_DATASETS:
        for col in dataset.required + dataset.optional:
            assert col.description.strip()
            assert col.example.strip()
