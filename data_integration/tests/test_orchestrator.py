from unittest.mock import patch

from data_integration.orchestrator import (
    PostgresDataset,
    SheetsDataset,
    available_for_analysis,
    run_integration,
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
