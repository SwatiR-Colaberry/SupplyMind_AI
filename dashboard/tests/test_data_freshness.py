from data_integration.orchestrator import DatasetResult
from dashboard.data_freshness import build_data_freshness


def test_builds_one_entry_per_dataset_with_a_shared_pulled_at():
    results = [
        DatasetResult(name="customer_orders", source_type="postgres", outcome="success", rows=[{}, {}]),
        DatasetResult(name="delivery_records", source_type="postgres", outcome="failure", rows=[], error="connection refused"),
    ]

    entries = build_data_freshness(results, pulled_at="2026-09-04T12:00:00+00:00")

    assert len(entries) == 2
    by_dataset = {e.dataset: e for e in entries}
    assert by_dataset["customer_orders"].outcome == "success"
    assert by_dataset["customer_orders"].row_count == 2
    assert by_dataset["customer_orders"].error is None
    assert by_dataset["delivery_records"].outcome == "failure"
    assert by_dataset["delivery_records"].error == "connection refused"
    assert all(e.pulled_at == "2026-09-04T12:00:00+00:00" for e in entries)


def test_pulled_at_defaults_to_now_when_not_supplied():
    from datetime import datetime

    results = [DatasetResult(name="inventory", source_type="postgres", outcome="success", rows=[])]

    entries = build_data_freshness(results)

    assert len(entries) == 1
    datetime.fromisoformat(entries[0].pulled_at)  # raises ValueError if not ISO-8601


def test_empty_dataset_results_produces_an_empty_list():
    assert build_data_freshness([]) == []
