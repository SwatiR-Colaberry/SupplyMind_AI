from unittest.mock import patch

import pytest

from data_integration.audit_trail import AuditStore
from data_integration.config import PostgresConfig
from data_integration.connection_profile import (
    ConnectionProfile,
    SchemaMappingError,
    fetch_profile_data,
    remap_rows,
    validate_against_live_schema,
    validate_mapping_completeness,
    validate_profile,
)
from data_integration.postgres_connector import PostgresIntegrationError

_PG = PostgresConfig(host="db.acme.example", port=5432, database="acme", user="u", password="p")


def _profile(dataset_kind: str, column_mapping: dict[str, str]) -> ConnectionProfile:
    return ConnectionProfile(
        tenant_id="acme",
        dataset_kind=dataset_kind,
        postgres=_PG,
        query="SELECT * FROM orders",
        column_mapping=column_mapping,
    )


def test_complete_customer_orders_mapping_passes():
    profile = _profile("customer_orders", {"order_date": "OrderDate", "quantity": "Qty"})

    validate_mapping_completeness(profile)  # must not raise


def test_missing_customer_orders_field_raises_with_field_name_in_message():
    profile = _profile("customer_orders", {"order_date": "OrderDate"})

    with pytest.raises(SchemaMappingError, match="quantity"):
        validate_mapping_completeness(profile)


def test_missing_all_customer_orders_fields_lists_both():
    profile = _profile("customer_orders", {})

    with pytest.raises(SchemaMappingError) as exc_info:
        validate_mapping_completeness(profile)
    assert "order_date" in str(exc_info.value)
    assert "quantity" in str(exc_info.value)


def test_empty_string_mapped_column_counts_as_unmapped():
    # A blank value is not a usable column reference - must fail the same
    # way an absent key does, not be treated as "present."
    profile = _profile("customer_orders", {"order_date": "OrderDate", "quantity": ""})

    with pytest.raises(SchemaMappingError, match="quantity"):
        validate_mapping_completeness(profile)


def test_complete_inventory_mapping_passes():
    profile = _profile(
        "inventory",
        {
            "sku": "SKU",
            "current_stock": "OnHand",
            "safety_stock": "SafetyStock",
            "daily_demand_rate": "DailyDemand",
            "lead_time_days": "LeadTimeDays",
        },
    )

    validate_mapping_completeness(profile)  # must not raise


def test_incomplete_inventory_mapping_raises():
    profile = _profile("inventory", {"sku": "SKU", "current_stock": "OnHand"})

    with pytest.raises(SchemaMappingError, match="safety_stock"):
        validate_mapping_completeness(profile)


def test_error_message_names_the_tenant():
    profile = _profile("customer_orders", {})

    with pytest.raises(SchemaMappingError, match="acme"):
        validate_mapping_completeness(profile)


def test_complete_delivery_records_mapping_passes():
    # delivery_records was added for STORY-005's supplier-delay detection,
    # after customer_orders/inventory - proves the same registry mechanism
    # covers a third dataset kind with no changes beyond registering it.
    profile = _profile(
        "delivery_records", {"po_id": "PONumber", "expected_date": "DueDate", "actual_date": "ReceivedDate"}
    )

    validate_mapping_completeness(profile)  # must not raise


def test_incomplete_delivery_records_mapping_raises():
    profile = _profile("delivery_records", {"po_id": "PONumber"})

    with pytest.raises(SchemaMappingError, match="expected_date"):
        validate_mapping_completeness(profile)


# --- validate_against_live_schema / validate_profile ---


def test_validate_against_live_schema_passes_when_mapped_columns_exist():
    profile = _profile("customer_orders", {"order_date": "OrderDate", "quantity": "Qty"})
    with patch("data_integration.connection_profile.postgres_connector.fetch_columns") as mock_fetch:
        mock_fetch.return_value = ["OrderDate", "Qty", "CustomerId"]

        validate_against_live_schema(profile)  # must not raise

    mock_fetch.assert_called_once_with(profile.query, config=profile.postgres)


def test_validate_against_live_schema_raises_when_mapped_column_does_not_exist():
    profile = _profile("customer_orders", {"order_date": "OrderDate", "quantity": "Qty"})
    with patch("data_integration.connection_profile.postgres_connector.fetch_columns") as mock_fetch:
        mock_fetch.return_value = ["OrderDate", "CustomerId"]  # "Qty" is not a real column

        with pytest.raises(SchemaMappingError, match="Qty"):
            validate_against_live_schema(profile)


def test_validate_against_live_schema_reports_incomplete_mapping_without_a_kerror():
    # A caller that skips validate_mapping_completeness() must still get a
    # SchemaMappingError, not a raw KeyError from indexing an unmapped field.
    profile = _profile("customer_orders", {"order_date": "OrderDate"})

    with pytest.raises(SchemaMappingError, match="quantity"):
        validate_against_live_schema(profile)


def test_validate_against_live_schema_does_not_query_when_mapping_incomplete():
    profile = _profile("customer_orders", {"order_date": "OrderDate"})
    with patch("data_integration.connection_profile.postgres_connector.fetch_columns") as mock_fetch:
        with pytest.raises(SchemaMappingError):
            validate_against_live_schema(profile)

    mock_fetch.assert_not_called()


def test_validate_profile_runs_both_layers_and_passes_for_a_fully_valid_profile():
    profile = _profile("customer_orders", {"order_date": "OrderDate", "quantity": "Qty"})
    with patch("data_integration.connection_profile.postgres_connector.fetch_columns") as mock_fetch:
        mock_fetch.return_value = ["OrderDate", "Qty"]

        validate_profile(profile)  # must not raise


# --- remap_rows ---


def test_remap_rows_translates_tenant_column_names_to_canonical_names():
    mapping = {"order_date": "OrderDate", "quantity": "Qty"}
    raw_rows = [
        {"OrderDate": "2026-01-15", "Qty": 10, "CustomerId": 7},
        {"OrderDate": "2026-02-15", "Qty": 12, "CustomerId": 8},
    ]

    remapped = remap_rows(raw_rows, mapping)

    assert remapped == [
        {"order_date": "2026-01-15", "quantity": 10},
        {"order_date": "2026-02-15", "quantity": 12},
    ]


def test_remap_rows_drops_columns_not_named_in_the_mapping():
    remapped = remap_rows([{"OrderDate": "2026-01-15", "Qty": 10, "Notes": "rush"}], {"order_date": "OrderDate"})

    assert remapped == [{"order_date": "2026-01-15"}]


def test_remap_rows_sets_none_for_a_row_missing_a_mapped_column():
    # Missing entirely, not just null - a row that never had the column.
    remapped = remap_rows([{"OrderDate": "2026-01-15"}], {"order_date": "OrderDate", "quantity": "Qty"})

    assert remapped == [{"order_date": "2026-01-15", "quantity": None}]


def test_remap_rows_handles_empty_input():
    assert remap_rows([], {"order_date": "OrderDate"}) == []


def test_remap_rows_two_tenants_different_column_names_produce_the_same_canonical_shape():
    # The point of the whole module: two companies with differently-named
    # columns end up as identical, canonical-field rows.
    tenant_a_mapping = {"order_date": "OrderDate", "quantity": "Qty"}
    tenant_a_rows = [{"OrderDate": "2026-01-15", "Qty": 100}]

    tenant_b_mapping = {"order_date": "order_dt", "quantity": "units_sold"}
    tenant_b_rows = [{"order_dt": "2026-01-15", "units_sold": 100}]

    assert remap_rows(tenant_a_rows, tenant_a_mapping) == remap_rows(tenant_b_rows, tenant_b_mapping)


# --- fetch_profile_data ---


def _audit_lines(tmp_path) -> list[dict]:
    import json

    return [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().strip().splitlines()]


def test_fetch_profile_data_validates_fetches_remaps_and_audits_on_success(tmp_path):
    profile = _profile("customer_orders", {"order_date": "OrderDate", "quantity": "Qty"})
    audit_store = AuditStore(tmp_path / "audit.jsonl")
    with patch("data_integration.connection_profile.postgres_connector.fetch_columns") as mock_columns, \
         patch("data_integration.connection_profile.postgres_connector.fetch_rows") as mock_rows:
        mock_columns.return_value = ["OrderDate", "Qty"]
        mock_rows.return_value = [{"OrderDate": "2026-01-15", "Qty": 10}]

        result = fetch_profile_data(profile, audit_store)

    assert result == [{"order_date": "2026-01-15", "quantity": 10}]
    mock_rows.assert_called_once_with(profile.query, config=profile.postgres)
    records = _audit_lines(tmp_path)
    assert len(records) == 1
    assert records[0]["dataset"] == "acme:customer_orders"
    assert records[0]["outcome"] == "success"
    assert records[0]["row_count"] == 1


def test_fetch_profile_data_raises_and_audits_on_invalid_mapping(tmp_path):
    profile = _profile("customer_orders", {"order_date": "OrderDate"})  # missing "quantity"
    audit_store = AuditStore(tmp_path / "audit.jsonl")

    with pytest.raises(SchemaMappingError):
        fetch_profile_data(profile, audit_store)

    records = _audit_lines(tmp_path)
    assert len(records) == 1
    assert records[0]["outcome"] == "failure"


def test_fetch_profile_data_raises_and_audits_on_fetch_failure(tmp_path):
    profile = _profile("customer_orders", {"order_date": "OrderDate", "quantity": "Qty"})
    audit_store = AuditStore(tmp_path / "audit.jsonl")
    with patch("data_integration.connection_profile.postgres_connector.fetch_columns") as mock_columns, \
         patch("data_integration.connection_profile.postgres_connector.fetch_rows") as mock_rows:
        mock_columns.return_value = ["OrderDate", "Qty"]
        mock_rows.side_effect = PostgresIntegrationError("unreachable")

        with pytest.raises(PostgresIntegrationError):
            fetch_profile_data(profile, audit_store)

    records = _audit_lines(tmp_path)
    assert len(records) == 1
    assert records[0]["outcome"] == "failure"


def test_fetch_profile_data_reprocessing_identical_data_does_not_duplicate_audit_trail(tmp_path):
    profile = _profile("customer_orders", {"order_date": "OrderDate", "quantity": "Qty"})
    audit_store = AuditStore(tmp_path / "audit.jsonl")
    with patch("data_integration.connection_profile.postgres_connector.fetch_columns") as mock_columns, \
         patch("data_integration.connection_profile.postgres_connector.fetch_rows") as mock_rows:
        mock_columns.return_value = ["OrderDate", "Qty"]
        mock_rows.return_value = [{"OrderDate": "2026-01-15", "Qty": 10}]

        fetch_profile_data(profile, audit_store)
        fetch_profile_data(profile, audit_store)

    assert len(_audit_lines(tmp_path)) == 1
