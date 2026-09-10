from __future__ import annotations

from unittest.mock import patch

from data_console.file_store import UnknownUploadError
from data_console.mapping_service import MappingPreviewResult
from data_console.mapping_store import DatasetMapping
from dashboard.run_sample_dashboard import _dataset_result_from_data_console, _nav_links
from data_integration.connection_profile import SchemaMappingError


def _mock_store(mapping: DatasetMapping | None):
    store = type("Store", (), {"get": staticmethod(lambda kind: mapping)})()
    return patch("dashboard.run_sample_dashboard.MappingStore", return_value=store)


def test_returns_none_when_data_console_has_no_mapping_at_all():
    with _mock_store(None):
        assert _dataset_result_from_data_console("inventory") is None


def test_returns_a_failure_result_when_marked_unavailable_instead_of_falling_back():
    # An explicit "this data genuinely isn't here" must not silently fall
    # back to the hardcoded Postgres query - that would contradict what the
    # tenant already told data_console.
    with _mock_store(DatasetMapping(status="unavailable")):
        result = _dataset_result_from_data_console("delivery_records")

    assert result.name == "delivery_records"
    assert result.outcome == "failure"
    assert "unavailable" in result.error


def test_returns_a_success_result_with_the_previewed_rows_for_a_file_mapping():
    mapping = DatasetMapping(status="mapped", file_id="f1", filename="orders.csv", column_mapping={"sku": "sku"}, source_kind="file")
    preview_result = MappingPreviewResult(rows=[{"sku": "SKU-1"}], row_count=1, truncated=False)
    with _mock_store(mapping), patch("dashboard.run_sample_dashboard.preview_mapping", return_value=preview_result):
        result = _dataset_result_from_data_console("inventory")

    assert result.outcome == "success"
    assert result.rows == [{"sku": "SKU-1"}]
    assert result.source_type == "csv_upload"


def test_returns_a_success_result_with_postgresql_source_type_for_a_table_mapping():
    mapping = DatasetMapping(status="mapped", table="acme_inventory", column_mapping={"sku": "sku"}, source_kind="table")
    preview_result = MappingPreviewResult(rows=[{"sku": "SKU-1"}], row_count=1, truncated=False)
    with _mock_store(mapping), patch("dashboard.run_sample_dashboard.preview_mapping", return_value=preview_result):
        result = _dataset_result_from_data_console("inventory")

    assert result.source_type == "postgresql"


def test_returns_a_failure_result_when_the_saved_mapping_no_longer_validates():
    # E.g. the underlying table/file changed shape since Save Mapping - the
    # same SchemaMappingError the console's own "Preview" button would show.
    mapping = DatasetMapping(status="mapped", table="acme_inventory", column_mapping={"sku": "sku"}, source_kind="table")
    with _mock_store(mapping), patch(
        "dashboard.run_sample_dashboard.preview_mapping", side_effect=SchemaMappingError("mapped column not found")
    ):
        result = _dataset_result_from_data_console("inventory")

    assert result.outcome == "failure"
    assert "mapped column not found" in result.error


def test_returns_a_failure_result_when_the_uploaded_file_is_gone():
    mapping = DatasetMapping(status="mapped", file_id="gone", filename="orders.csv", column_mapping={"sku": "sku"}, source_kind="file")
    with _mock_store(mapping), patch(
        "dashboard.run_sample_dashboard.preview_mapping", side_effect=UnknownUploadError("no such upload")
    ):
        result = _dataset_result_from_data_console("inventory")

    assert result.outcome == "failure"
    assert "no such upload" in result.error


def test_nav_links_leads_with_a_data_console_link_then_all_3_scenarios_with_current_as_none():
    links = _nav_links("partial_failure")

    assert links[0][0] == "Data Console"
    assert links[0][1].startswith("http://127.0.0.1:")
    assert links[1:] == [
        ("Live Data", "control_tower_real_data.html"),
        ("Demo: Partial Data", None),
        ("Demo: Healthy Example", "control_tower_synthetic_healthy.html"),
    ]
