"""Inventory data-quality assessment, ahead of stockout-risk prediction (STORY-004 / REQ-006, REQ-011).

Pure computation - no I/O. This is what satisfies the acceptance
criterion "given inaccurate inventory data, when the system attempts
prediction, then it should flag the data for review": this module
decides what "inaccurate" means for a raw inventory row and separates
usable rows from ones that must be flagged, rather than the risk model
silently computing a number from corrupted input.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

REQUIRED_FIELDS = ("sku", "current_stock", "safety_stock", "daily_demand_rate", "lead_time_days")
NUMERIC_FIELDS = ("current_stock", "safety_stock", "daily_demand_rate", "lead_time_days")

# Not required - each enables one extra InventoryPosition capability when
# present (incoming_stock -> "expected inventory" accounts for confirmed
# replenishment; unit_price -> revenue_at_risk; demand_std_dev ->
# estimate_stockout_probability's statistical mode and a recommended
# safety stock) but a row missing them, or carrying a bad value for one,
# is still perfectly usable for the core risk assessment - so an invalid
# optional value drops just that field (see _normalize_optional_fields
# below), not the whole row into flagged_rows the way a bad required
# field does.
OPTIONAL_NUMERIC_FIELDS = ("incoming_stock", "unit_price", "demand_std_dev")


@dataclass(frozen=True)
class FlaggedRow:
    row: dict[str, Any]
    reasons: list[str]


@dataclass(frozen=True)
class InventoryDataQualityReport:
    total_rows: int
    clean_rows: list[dict[str, Any]]
    flagged_rows: list[FlaggedRow]
    warnings: list[str] = field(default_factory=list)


def _row_issues(row: dict[str, Any]) -> list[str]:
    missing = [f for f in REQUIRED_FIELDS if f not in row or row[f] is None]
    if missing:
        # Can't validate values below without the fields present at all.
        return [f"missing field(s): {', '.join(missing)}"]

    issues: list[str] = []
    for field_name in NUMERIC_FIELDS:
        value = row[field_name]
        # Decimal alongside int/float: a real PostgreSQL NUMERIC column
        # comes back from psycopg2 as decimal.Decimal, not float - a row
        # pulled from a live database would otherwise fail this check on
        # every field and get flagged "not numeric" despite being
        # perfectly valid data. See assess_inventory_data_quality() below,
        # which normalizes every accepted value to float before it
        # reaches inventory_risk/risk_model.py, so nothing downstream
        # needs to know about Decimal.
        if not isinstance(value, (int, float, Decimal)) or isinstance(value, bool):
            issues.append(f"{field_name} is not numeric: {value!r}")
        elif isinstance(value, float) and math.isnan(value):
            # NaN passes isinstance(value, float) and every `< 0`/`<= 0`
            # comparison below evaluates False for NaN, so it must be
            # rejected explicitly here or it silently reaches the risk
            # model as "clean" data.
            issues.append(f"{field_name} is NaN")
        elif isinstance(value, Decimal) and value.is_nan():
            issues.append(f"{field_name} is NaN")
    if issues:
        # Comparisons below assume numeric, non-NaN values - bail before running them.
        return issues

    if row["current_stock"] < 0:
        issues.append(f"current_stock is negative: {row['current_stock']}")
    if row["safety_stock"] < 0:
        issues.append(f"safety_stock is negative: {row['safety_stock']}")
    if row["daily_demand_rate"] < 0:
        issues.append(f"daily_demand_rate is negative: {row['daily_demand_rate']}")
    if row["lead_time_days"] <= 0:
        issues.append(f"lead_time_days must be positive: {row['lead_time_days']}")

    return issues


def _normalize_optional_fields(row: dict[str, Any]) -> dict[str, Any]:
    """Best-effort float normalization for whichever OPTIONAL_NUMERIC_FIELDS
    are present and genuinely valid on this row - present-but-invalid (wrong
    type, negative, NaN) is dropped silently rather than flagging the whole
    row, since these fields are enhancements the core assessment doesn't
    depend on (see OPTIONAL_NUMERIC_FIELDS's own docstring)."""
    normalized: dict[str, Any] = {}
    for field_name in OPTIONAL_NUMERIC_FIELDS:
        value = row.get(field_name)
        if value is None:
            continue
        if not isinstance(value, (int, float, Decimal)) or isinstance(value, bool):
            continue
        if isinstance(value, float) and math.isnan(value):
            continue
        if isinstance(value, Decimal) and value.is_nan():
            continue
        value = float(value)
        if value < 0:
            continue
        normalized[field_name] = value
    return normalized


def assess_inventory_data_quality(rows: list[dict[str, Any]]) -> InventoryDataQualityReport:
    """Split raw inventory rows into clean vs. flagged-for-review, with reasons.

    Handles: missing required fields, non-numeric values, negative
    stock/demand, and a non-positive lead time - each a distinct,
    human-readable reason attached to the offending row rather than one
    generic "bad data" flag. A flagged row is excluded from `clean_rows`
    so downstream risk assessment never runs the arithmetic on
    corrupted input; it is not discarded, it is returned in
    `flagged_rows` so the caller can act on the review.
    """
    clean_rows: list[dict[str, Any]] = []
    flagged_rows: list[FlaggedRow] = []

    for row in rows:
        issues = _row_issues(row)
        if issues:
            flagged_rows.append(FlaggedRow(row=row, reasons=issues))
        else:
            # Normalize to float here, the one place in this repo that
            # already fully validates a row's numeric fields, rather than
            # passing Decimal/int through unchanged - inventory_risk/risk_model.py
            # mixes these values with plain float constants (e.g.
            # MEDIUM_COVERAGE_RATIO), and Decimal arithmetic against a
            # float raises TypeError rather than silently coercing.
            base = {k: v for k, v in row.items() if k not in OPTIONAL_NUMERIC_FIELDS}
            clean_rows.append({**base, **{f: float(row[f]) for f in NUMERIC_FIELDS}, **_normalize_optional_fields(row)})

    warnings: list[str] = []
    if not rows:
        warnings.append("no inventory data provided")
    elif flagged_rows:
        warnings.append(
            f"{len(flagged_rows)} of {len(rows)} inventory row(s) flagged for review "
            f"(missing/invalid fields); excluded from risk assessment"
        )

    return InventoryDataQualityReport(
        total_rows=len(rows),
        clean_rows=clean_rows,
        flagged_rows=flagged_rows,
        warnings=warnings,
    )
