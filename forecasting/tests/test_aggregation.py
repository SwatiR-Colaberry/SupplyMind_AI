from datetime import date, datetime

import pytest

from forecasting.aggregation import AggregationError, aggregate_monthly_demand
from forecasting.demand_model import DemandPoint


def test_aggregate_monthly_demand_sums_quantity_per_calendar_month():
    rows = [
        {"order_date": date(2025, 1, 5), "quantity": 10},
        {"order_date": date(2025, 1, 20), "quantity": 15},
        {"order_date": date(2025, 2, 3), "quantity": 7},
    ]

    result = aggregate_monthly_demand(rows)

    assert result == [DemandPoint("2025-01", 25.0), DemandPoint("2025-02", 7.0)]


def test_aggregate_monthly_demand_returns_periods_in_sorted_order():
    rows = [
        {"order_date": date(2025, 3, 1), "quantity": 1},
        {"order_date": date(2025, 1, 1), "quantity": 1},
        {"order_date": date(2025, 2, 1), "quantity": 1},
    ]

    result = aggregate_monthly_demand(rows)

    assert [p.period for p in result] == ["2025-01", "2025-02", "2025-03"]


def test_aggregate_monthly_demand_accepts_datetime_and_iso_string_dates():
    rows = [
        {"order_date": datetime(2025, 1, 5, 10, 30), "quantity": 10},
        {"order_date": "2025-01-06", "quantity": 5},
        {"order_date": "2025-01-07T09:00:00", "quantity": 5},
    ]

    result = aggregate_monthly_demand(rows)

    assert result == [DemandPoint("2025-01", 20.0)]


def test_aggregate_monthly_demand_skips_rows_with_missing_date_or_quantity():
    rows = [
        {"order_date": date(2025, 1, 5), "quantity": 10},
        {"order_date": None, "quantity": 10},
        {"order_date": date(2025, 1, 6), "quantity": None},
    ]

    result = aggregate_monthly_demand(rows)

    assert result == [DemandPoint("2025-01", 10.0)]


def test_aggregate_monthly_demand_returns_empty_list_for_no_rows():
    assert aggregate_monthly_demand([]) == []


def test_aggregate_monthly_demand_honors_custom_field_names():
    rows = [{"txn_date": date(2025, 1, 5), "units_sold": 10}]

    result = aggregate_monthly_demand(rows, date_field="txn_date", quantity_field="units_sold")

    assert result == [DemandPoint("2025-01", 10.0)]


def test_aggregate_monthly_demand_rejects_unparseable_date():
    rows = [{"order_date": "not-a-date", "quantity": 10}]

    with pytest.raises(AggregationError, match="could not parse 'order_date'"):
        aggregate_monthly_demand(rows)


def test_aggregate_monthly_demand_rejects_unparseable_quantity():
    rows = [{"order_date": date(2025, 1, 5), "quantity": "lots"}]

    with pytest.raises(AggregationError, match="could not parse 'quantity'"):
        aggregate_monthly_demand(rows)
