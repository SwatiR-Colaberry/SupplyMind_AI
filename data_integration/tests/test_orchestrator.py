import json
from unittest.mock import patch

from data_integration.audit_trail import AuditStore, AuditTrailWriteError
from data_integration.orchestrator import (
    PostgresDataset,
    SheetsDataset,
    available_for_analysis,
    run_integration,
    run_integration_with_audit,
)
from data_integration.postgres_connector import PostgresIntegrationError


def test_run_integration_all_sources_available():
    datasets = [
        PostgresDataset(name="customer_orders", query="SELECT * FROM orders"),
        SheetsDataset(name="inventory", spreadsheet_id="sheet-1", worksheet_name="Inventory"),
    ]
    with patch("data_integration.orchestrator.postgres_connector.fetch_rows") as mock_pg, patch(
        "data_integration.orchestrator.sheets_connector.fetch_rows"
    ) as mock_sheets:
        mock_pg.return_value = [{"order_id": 1}]
        mock_sheets.return_value = [{"sku": "A1"}]

        results = run_integration(datasets)

    assert [r.outcome for r in results] == ["success", "success"]
    data = available_for_analysis(results)
    assert data == {"customer_orders": [{"order_id": 1}], "inventory": [{"sku": "A1"}]}


def test_run_integration_isolates_one_failure_from_the_rest():
    datasets = [
        PostgresDataset(name="customer_orders", query="SELECT * FROM orders"),
        PostgresDataset(name="suppliers", query="SELECT * FROM suppliers"),
    ]
    with patch("data_integration.orchestrator.postgres_connector.fetch_rows") as mock_pg:
        mock_pg.side_effect = [
            PostgresIntegrationError("PostgreSQL unavailable after 3 attempts"),
            [{"supplier_id": 7}],
        ]

        results = run_integration(datasets)

    outcomes = {r.name: r.outcome for r in results}
    assert outcomes == {"customer_orders": "failure", "suppliers": "success"}

    data = available_for_analysis(results)
    assert data == {"suppliers": [{"supplier_id": 7}]}

    failed = next(r for r in results if r.name == "customer_orders")
    assert "PostgresIntegrationError" in failed.error or "unavailable" in failed.error


def test_run_integration_logs_missing_data_as_error():
    datasets = [PostgresDataset(name="warehouses", query="SELECT * FROM warehouses")]
    with patch("data_integration.orchestrator.postgres_connector.fetch_rows") as mock_pg, patch(
        "data_integration.orchestrator.logger.error"
    ) as mock_log_error:
        mock_pg.side_effect = PostgresIntegrationError("PostgreSQL unavailable after 3 attempts")

        results = run_integration(datasets)

    assert results[0].outcome == "failure"

    mock_log_error.assert_called_once()
    _, kwargs = mock_log_error.call_args
    extra = kwargs["extra"]
    assert extra["outcome"] == "failure"
    assert extra["source"] == "postgresql"
    assert extra["context"]["dataset"] == "warehouses"


def test_run_integration_with_audit_creates_one_entry_per_dataset(tmp_path):
    datasets = [PostgresDataset(name="customer_orders", query="SELECT * FROM orders")]
    audit_store = AuditStore(tmp_path / "audit.jsonl")
    with patch("data_integration.orchestrator.postgres_connector.fetch_rows") as mock_pg:
        mock_pg.return_value = [{"order_id": 1}]
        run_integration_with_audit(datasets, audit_store)

    lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["dataset"] == "customer_orders"


def test_run_integration_with_audit_reprocessing_same_data_does_not_duplicate(tmp_path):
    datasets = [PostgresDataset(name="customer_orders", query="SELECT * FROM orders")]
    audit_store = AuditStore(tmp_path / "audit.jsonl")
    with patch("data_integration.orchestrator.postgres_connector.fetch_rows") as mock_pg:
        mock_pg.return_value = [{"order_id": 1}]
        run_integration_with_audit(datasets, audit_store)
        run_integration_with_audit(datasets, audit_store)

    lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1


def test_run_integration_with_audit_records_error_detail_on_failure(tmp_path):
    datasets = [PostgresDataset(name="warehouses", query="SELECT * FROM warehouses")]
    audit_store = AuditStore(tmp_path / "audit.jsonl")
    with patch("data_integration.orchestrator.postgres_connector.fetch_rows") as mock_pg:
        mock_pg.side_effect = PostgresIntegrationError("PostgreSQL unavailable after 3 attempts")
        run_integration_with_audit(datasets, audit_store)

    lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["outcome"] == "failure"
    assert "unavailable" in record["error"]


def test_run_integration_with_audit_does_not_dedupe_repeated_failures(tmp_path):
    datasets = [PostgresDataset(name="warehouses", query="SELECT * FROM warehouses")]
    audit_store = AuditStore(tmp_path / "audit.jsonl")
    with patch("data_integration.orchestrator.postgres_connector.fetch_rows") as mock_pg:
        mock_pg.side_effect = PostgresIntegrationError("PostgreSQL unavailable after 3 attempts")
        run_integration_with_audit(datasets, audit_store)
        run_integration_with_audit(datasets, audit_store)

    lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2


def test_run_integration_with_audit_isolates_audit_write_failure_from_other_datasets(tmp_path):
    datasets = [
        PostgresDataset(name="customer_orders", query="SELECT * FROM orders"),
        PostgresDataset(name="suppliers", query="SELECT * FROM suppliers"),
    ]
    audit_store = AuditStore(tmp_path / "audit.jsonl")
    with patch("data_integration.orchestrator.postgres_connector.fetch_rows") as mock_pg, patch.object(
        audit_store, "record"
    ) as mock_record, patch("data_integration.orchestrator.logger.error") as mock_log_error:
        mock_pg.side_effect = [[{"order_id": 1}], [{"supplier_id": 7}]]
        mock_record.side_effect = [AuditTrailWriteError("disk full"), mock_record.return_value]

        results = run_integration_with_audit(datasets, audit_store)

    assert [r.outcome for r in results] == ["success", "success"]
    assert available_for_analysis(results) == {
        "customer_orders": [{"order_id": 1}],
        "suppliers": [{"supplier_id": 7}],
    }
    assert mock_log_error.call_args.kwargs["extra"]["event"] == "audit_trail_unavailable"
    assert mock_log_error.call_args.kwargs["extra"]["context"]["dataset"] == "customer_orders"


def test_run_integration_with_audit_reprocessing_reordered_rows_does_not_duplicate(tmp_path):
    """No ORDER BY on the sample queries means identical data can come back reordered."""
    datasets = [PostgresDataset(name="customer_orders", query="SELECT * FROM orders")]
    audit_store = AuditStore(tmp_path / "audit.jsonl")
    with patch("data_integration.orchestrator.postgres_connector.fetch_rows") as mock_pg:
        mock_pg.side_effect = [
            [{"order_id": 1}, {"order_id": 2}],
            [{"order_id": 2}, {"order_id": 1}],
        ]
        run_integration_with_audit(datasets, audit_store)
        run_integration_with_audit(datasets, audit_store)

    lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
