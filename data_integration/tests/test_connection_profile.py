from __future__ import annotations

from unittest.mock import patch

import pytest

from data_integration.audit_trail import AuditStore
from data_integration.config import PostgresConfig
from data_integration.connection_profile import (
    ConnectionProfile,
    SchemaMappingError,
    compute_date_fields,
    fetch_profile_data,
    remap_and_compute_rows,
    remap_rows,
    validate_against_live_schema,
    validate_mapping_completeness,
    validate_profile,
)
from data_integration.postgres_connector import PostgresIntegrationError

_PG = PostgresConfig(host="db.acme.example", port=5432, database="acme", user="u", password="p")


def _profile(
    dataset_kind: str,
    column_mapping: dict[str, str],
    unavailable_fields: frozenset[str] = frozenset(),
    computed_date_fields: dict[str, dict[str, str]] | None = None,
) -> ConnectionProfile:
    return ConnectionProfile(
        tenant_id="acme",
        dataset_kind=dataset_kind,
        postgres=_PG,
        query="SELECT * FROM orders",
        column_mapping=column_mapping,
        unavailable_fields=unavailable_fields,
        computed_date_fields=computed_date_fields or {},
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


# --- unavailable_fields ---


def test_a_required_field_declared_unavailable_is_exempt_from_completeness():
    # A transaction-level dataset with no safety_stock/daily_demand_rate/
    # lead_time_days column at all - declaring them unavailable, rather than
    # leaving them unmapped, must let the rest of the mapping through.
    profile = _profile(
        "inventory",
        {"sku": "SKU", "current_stock": "OnHand"},
        unavailable_fields=frozenset({"safety_stock", "daily_demand_rate", "lead_time_days"}),
    )

    validate_mapping_completeness(profile)  # must not raise


def test_a_required_field_still_missing_and_not_declared_unavailable_still_raises():
    profile = _profile(
        "inventory", {"sku": "SKU", "current_stock": "OnHand"}, unavailable_fields=frozenset({"safety_stock"})
    )

    with pytest.raises(SchemaMappingError, match="daily_demand_rate"):
        validate_mapping_completeness(profile)


def test_validate_against_live_schema_skips_a_field_declared_unavailable():
    profile = _profile(
        "inventory",
        {"sku": "SKU", "current_stock": "OnHand"},
        unavailable_fields=frozenset({"safety_stock", "daily_demand_rate", "lead_time_days"}),
    )
    with patch("data_integration.connection_profile.postgres_connector.fetch_columns") as mock_fetch:
        mock_fetch.return_value = ["SKU", "OnHand"]  # the unavailable fields' columns genuinely don't exist

        validate_against_live_schema(profile)  # must not raise


# --- computed_date_fields ---


def test_a_required_date_field_declared_computed_is_exempt_from_completeness():
    # A real export with an order date plus a scheduled-days column instead
    # of an explicit expected-delivery-date column.
    profile = _profile(
        "delivery_records",
        {"po_id": "PONumber", "actual_date": "ReceivedDate"},
        computed_date_fields={"expected_date": {"base_date_column": "OrderDate", "offset_days_column": "ScheduledDays"}},
    )

    validate_mapping_completeness(profile)  # must not raise


def test_validate_against_live_schema_checks_both_columns_a_computed_field_references():
    profile = _profile(
        "delivery_records",
        {"po_id": "PONumber", "actual_date": "ReceivedDate"},
        computed_date_fields={"expected_date": {"base_date_column": "OrderDate", "offset_days_column": "ScheduledDays"}},
    )
    with patch("data_integration.connection_profile.postgres_connector.fetch_columns") as mock_fetch:
        mock_fetch.return_value = ["PONumber", "ReceivedDate", "OrderDate", "ScheduledDays"]

        validate_against_live_schema(profile)  # must not raise


def test_validate_against_live_schema_raises_when_a_computed_field_references_a_missing_column():
    profile = _profile(
        "delivery_records",
        {"po_id": "PONumber", "actual_date": "ReceivedDate"},
        computed_date_fields={"expected_date": {"base_date_column": "OrderDate", "offset_days_column": "ScheduledDays"}},
    )
    with patch("data_integration.connection_profile.postgres_connector.fetch_columns") as mock_fetch:
        mock_fetch.return_value = ["PONumber", "ReceivedDate", "OrderDate"]  # no ScheduledDays

        with pytest.raises(SchemaMappingError, match="ScheduledDays"):
            validate_against_live_schema(profile)


def test_compute_date_fields_adds_the_base_date_plus_the_offset_in_days():
    rows = [{"OrderDate": "2026-01-01", "ScheduledDays": "4"}, {"OrderDate": "2026-01-10", "ScheduledDays": 2}]
    spec = {"expected_date": {"base_date_column": "OrderDate", "offset_days_column": "ScheduledDays"}}

    result = compute_date_fields(rows, spec)

    assert result == [{"expected_date": "2026-01-05"}, {"expected_date": "2026-01-12"}]


def test_compute_date_fields_returns_none_when_the_base_date_does_not_parse():
    rows = [{"OrderDate": "not-a-date", "ScheduledDays": "4"}]
    spec = {"expected_date": {"base_date_column": "OrderDate", "offset_days_column": "ScheduledDays"}}

    assert compute_date_fields(rows, spec) == [{"expected_date": None}]


def test_compute_date_fields_returns_none_when_the_offset_is_not_numeric():
    rows = [{"OrderDate": "2026-01-01", "ScheduledDays": "soon"}]
    spec = {"expected_date": {"base_date_column": "OrderDate", "offset_days_column": "ScheduledDays"}}

    assert compute_date_fields(rows, spec) == [{"expected_date": None}]


def test_compute_date_fields_returns_none_when_the_base_date_column_is_missing_from_the_row():
    rows = [{"ScheduledDays": "4"}]
    spec = {"expected_date": {"base_date_column": "OrderDate", "offset_days_column": "ScheduledDays"}}

    assert compute_date_fields(rows, spec) == [{"expected_date": None}]


def test_remap_and_compute_rows_merges_directly_mapped_and_computed_fields():
    rows = [{"PONumber": "PO-1", "ReceivedDate": "2026-01-08", "OrderDate": "2026-01-01", "ScheduledDays": "4"}]
    column_mapping = {"po_id": "PONumber", "actual_date": "ReceivedDate"}
    computed_date_fields = {"expected_date": {"base_date_column": "OrderDate", "offset_days_column": "ScheduledDays"}}

    result = remap_and_compute_rows(rows, column_mapping, computed_date_fields)

    assert result == [{"po_id": "PO-1", "actual_date": "2026-01-08", "expected_date": "2026-01-05"}]


def test_remap_and_compute_rows_with_no_computed_fields_behaves_exactly_like_remap_rows():
    rows = [{"PONumber": "PO-1", "ReceivedDate": "2026-01-08"}]
    column_mapping = {"po_id": "PONumber", "actual_date": "ReceivedDate"}

    assert remap_and_compute_rows(rows, column_mapping, {}) == remap_rows(rows, column_mapping)


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
