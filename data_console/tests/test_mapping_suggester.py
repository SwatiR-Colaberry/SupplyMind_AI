from data_console.column_requirements import DELIVERY_RECORDS, INVENTORY
from data_console.mapping_suggester import suggest_mapping


def test_suggests_an_exact_case_insensitive_name_match_first():
    result = suggest_mapping(["SKU", "current_stock", "safety_stock", "daily_demand_rate", "lead_time_days"], INVENTORY)

    assert result["sku"] == "SKU"
    assert result["current_stock"] == "current_stock"


def test_suggests_a_known_synonym_when_no_exact_name_matches():
    # The exact columns risk_detection/run_sample_multi_tenant_risk_detection.py's
    # acme_inventory table uses - the real case that surfaced this need.
    result = suggest_mapping(["item_sku", "on_hand", "min_stock", "daily_use", "lead_days"], INVENTORY)

    assert result == {
        "sku": "item_sku",
        "current_stock": "on_hand",
        "safety_stock": "min_stock",
        "daily_demand_rate": "daily_use",
        "lead_time_days": "lead_days",
    }


def test_returns_none_for_a_field_with_no_plausible_match():
    result = suggest_mapping(["completely_unrelated_column"], INVENTORY)

    assert all(value is None for value in result.values())


def test_covers_both_required_and_optional_fields():
    result = suggest_mapping(["po_id", "expected_date", "actual_date", "supplier"], DELIVERY_RECORDS)

    assert result["po_id"] == "po_id"
    assert result["supplier"] == "supplier"
    assert result["transportation_cost"] is None


def test_exact_match_is_preferred_over_a_synonym_even_if_both_are_present():
    result = suggest_mapping(["sku", "item_sku"], INVENTORY)

    assert result["sku"] == "sku"
