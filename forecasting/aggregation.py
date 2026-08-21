"""Aggregates raw transactional rows into monthly demand points.

Bridges data_integration's raw dataset rows (shape unknown until a real
schema exists - see data_integration/run_sample_integration.py's own
logged assumption about customer_orders) and forecasting/demand_model's
DemandPoint input. Kept separate from demand_model.py so the forecasting
math has zero knowledge of row-level field names.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from forecasting.demand_model import DemandPoint


class AggregationError(ValueError):
    """Raised when raw rows can't be aggregated into monthly demand points."""


def _to_period(raw_date: Any, date_field: str) -> str:
    if hasattr(raw_date, "year") and hasattr(raw_date, "month"):
        # Covers both datetime.datetime and datetime.date (what a real
        # PostgreSQL driver hands back for a date/timestamp column).
        return f"{raw_date.year:04d}-{raw_date.month:02d}"
    try:
        parsed = datetime.fromisoformat(str(raw_date))
    except ValueError as exc:
        raise AggregationError(
            f"could not parse '{date_field}' value {raw_date!r} into a period"
        ) from exc
    return f"{parsed.year:04d}-{parsed.month:02d}"


def aggregate_monthly_demand(
    rows: list[dict[str, Any]],
    date_field: str = "order_date",
    quantity_field: str = "quantity",
) -> list[DemandPoint]:
    """Sum quantity_field per calendar month of date_field.

    Handles: a row missing either field (skipped - a data quality gap
    surfaced separately by forecasting/data_quality.py, not silently
    dropped without a trace, and not fatal to the rest of the rows) and
    a date or quantity value that can't be parsed (raises
    AggregationError immediately - malformed source data is a data
    integrity problem the caller should see, not paper over).
    """
    totals: dict[str, float] = defaultdict(float)
    for row in rows:
        raw_date = row.get(date_field)
        raw_quantity = row.get(quantity_field)
        if raw_date is None or raw_quantity is None:
            continue
        try:
            quantity = float(raw_quantity)
        except (TypeError, ValueError) as exc:
            raise AggregationError(
                f"could not parse '{quantity_field}' value {raw_quantity!r} as a number"
            ) from exc
        period = _to_period(raw_date, date_field)
        totals[period] += quantity

    return [DemandPoint(period=period, quantity=total) for period, total in sorted(totals.items())]
