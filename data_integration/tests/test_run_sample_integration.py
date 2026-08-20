from unittest.mock import patch

from data_integration import run_sample_integration

REQUIRED_ENV_VARS = [
    "SUPPLYMIND_PG_HOST",
    "SUPPLYMIND_PG_PORT",
    "SUPPLYMIND_PG_DATABASE",
    "SUPPLYMIND_PG_USER",
    "SUPPLYMIND_PG_PASSWORD",
    "SUPPLYMIND_GOOGLE_SERVICE_ACCOUNT_JSON",
]


def _clear_credential_env(monkeypatch):
    for var in REQUIRED_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_main_without_credentials_fails_every_dataset_and_logs_each_one(monkeypatch):
    """Data source unavailable / auth failure path: no credentials configured."""
    _clear_credential_env(monkeypatch)

    with patch("data_integration.orchestrator.logger.error") as mock_log_error:
        exit_code = run_sample_integration.main()

    assert exit_code == 1
    assert mock_log_error.call_count == len(run_sample_integration.DATASETS)

    logged_datasets = {
        call.kwargs["extra"]["context"]["dataset"] for call in mock_log_error.call_args_list
    }
    assert logged_datasets == {d.name for d in run_sample_integration.DATASETS}


def test_main_returns_success_when_every_dataset_is_available():
    with patch("data_integration.orchestrator.postgres_connector.fetch_rows") as mock_pg, patch(
        "data_integration.orchestrator.sheets_connector.fetch_rows"
    ) as mock_sheets:
        mock_pg.return_value = [{"id": 1}]
        mock_sheets.return_value = [{"id": 1}]

        exit_code = run_sample_integration.main()

    assert exit_code == 0
