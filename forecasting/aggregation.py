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


# Tried, in order, only after datetime.fromisoformat() (this module's
# original, still-first-tried form) fails - covers a real-world operational
# export's shape, e.g. this repo's own DataCo sample dataset's
# "order date (DateOrders)" column, which reads "1/31/2018 22:56" rather
# than ISO. Kept local rather than reused from
# risk_detection.anomaly_detection.parse_delivery_date (which accepts the
# same two extra shapes): risk_detection.anomaly_detection already imports
# forecasting.demand_model, so forecasting importing back from
# risk_detection would be the exact "A imports B imports A" smell this
# repo's own Modular Composition Rule forbids, not just a style preference.
_FALLBACK_DATE_FORMATS: tuple[str, ...] = ("%m/%d/%Y %H:%M", "%m/%d/%Y")


def _to_period(raw_date: Any, date_field: str) -> str:
    if hasattr(raw_date, "year") and hasattr(raw_date, "month"):
        # Covers both datetime.datetime and datetime.date (what a real
        # PostgreSQL driver hands back for a date/timestamp column).
        return f"{raw_date.year:04d}-{raw_date.month:02d}"
    text = str(raw_date)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
        for fmt in _FALLBACK_DATE_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        raise AggregationError(f"could not parse '{date_field}' value {raw_date!r} into a period")
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


def aggregate_monthly_demand_by_group(
    rows: list[dict[str, Any]],
    group_field: str,
    date_field: str = "order_date",
    quantity_field: str = "quantity",
) -> dict[str, list[DemandPoint]]:
    """Same monthly aggregation as aggregate_monthly_demand(), split into one history per distinct group_field value.

    For a per-SKU (or per-category/per-region) demand breakdown: each
    group's own rows are aggregated exactly as aggregate_monthly_demand()
    aggregates the whole dataset, just scoped to that group's rows first.

    Handles: a row missing group_field is excluded from every group's
    history entirely (not lumped into a fabricated "unknown" bucket) -
    same presence-first discipline
    data_quality_monitoring/quality_checks.py's _validity_check() already
    applies to its own optional numeric_fields. Returns {} if no row
    carries group_field at all, rather than raising - the caller (an
    optional per-group breakdown on top of an otherwise-working aggregate
    forecast) treats that the same as "this optional field isn't mapped
    yet," not a data quality failure.
    """
    rows_by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        group_value = row.get(group_field)
        if group_value is None:
            continue
        rows_by_group[str(group_value)].append(row)

    return {
        group: aggregate_monthly_demand(group_rows, date_field, quantity_field)
        for group, group_rows in rows_by_group.items()
    }
