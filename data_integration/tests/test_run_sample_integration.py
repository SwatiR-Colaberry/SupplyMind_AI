import json
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


def _use_isolated_audit_log(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPPLYMIND_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    return tmp_path / "audit.jsonl"


def test_main_without_credentials_fails_every_dataset_and_logs_each_one(monkeypatch, tmp_path):
    """Data source unavailable / auth failure path: no credentials configured."""
    _clear_credential_env(monkeypatch)
    _use_isolated_audit_log(monkeypatch, tmp_path)

    with patch("data_integration.orchestrator.logger.error") as mock_log_error:
        exit_code = run_sample_integration.main()

    assert exit_code == 1
    assert mock_log_error.call_count == len(run_sample_integration.DATASETS)

    logged_datasets = {
        call.kwargs["extra"]["context"]["dataset"] for call in mock_log_error.call_args_list
    }
    assert logged_datasets == {d.name for d in run_sample_integration.DATASETS}


def test_main_returns_success_when_every_dataset_is_available(monkeypatch, tmp_path):
    _use_isolated_audit_log(monkeypatch, tmp_path)
    with patch("data_integration.orchestrator.postgres_connector.fetch_rows") as mock_pg, patch(
        "data_integration.orchestrator.sheets_connector.fetch_rows"
    ) as mock_sheets:
        mock_pg.return_value = [{"id": 1}]
        mock_sheets.return_value = [{"id": 1}]

        exit_code = run_sample_integration.main()

    assert exit_code == 0


def test_main_creates_one_audit_entry_per_dataset_even_when_every_dataset_fails(
    monkeypatch, tmp_path
):
    """Trust: an audit trail is created for each transaction, including failures."""
    _clear_credential_env(monkeypatch)
    audit_path = _use_isolated_audit_log(monkeypatch, tmp_path)

    run_sample_integration.main()

    lines = audit_path.read_text().strip().splitlines()
    assert len(lines) == len(run_sample_integration.DATASETS)
    assert all(json.loads(line)["outcome"] == "failure" for line in lines)


def test_main_run_twice_with_unchanged_data_does_not_duplicate_audit_entries(
    monkeypatch, tmp_path
):
    """Given data is processed, when the same data is reprocessed, no duplicate entries."""
    audit_path = _use_isolated_audit_log(monkeypatch, tmp_path)
    with patch("data_integration.orchestrator.postgres_connector.fetch_rows") as mock_pg, patch(
        "data_integration.orchestrator.sheets_connector.fetch_rows"
    ) as mock_sheets:
        mock_pg.return_value = [{"id": 1}]
        mock_sheets.return_value = [{"id": 1}]

        run_sample_integration.main()
        run_sample_integration.main()

    lines = audit_path.read_text().strip().splitlines()
    assert len(lines) == len(run_sample_integration.DATASETS)
