"""What each of this repo's three known datasets actually needs, in one place.

Every list here was read directly out of the code that consumes each
column (forecasting/aggregation.py, inventory_risk/data_quality.py,
risk_detection/anomaly_detection.py, supplier_evaluation/reliability.py,
shipment_delay_analysis/delay_analysis.py) - not guessed from the demo
data. This is the single source of truth the data-console UI checks a
connected table against, so a column added or renamed in any of those
modules only needs to be reflected here once, not re-derived by every
caller.

Deliberately separate from dashboard/run_sample_dashboard.py's DATASETS
list: that module owns *how* to fetch the three tables (fixed SQL
queries against a specific Postgres); this module owns *what columns
matter once fetched*, which is what a schema browser needs regardless of
where the rows come from.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ColumnRequirement:
    name: str
    description: str
    example: str
    # True only for a field whose value is a calendar date - lets the
    # data-console mapping UI offer "compute this from another date column
    # plus a day-offset column" as an alternative to a direct column pick,
    # for a real-world export that records a base date and a lead-time/
    # transit day count instead of the date itself (e.g. an order date plus
    # "days for shipment (scheduled)", rather than an explicit expected-
    # delivery-date column). Meaningless for a non-date field like sku or
    # quantity, so it defaults False.
    is_date: bool = False


@dataclass(frozen=True)
class DatasetRequirements:
    dataset_name: str
    label: str
    required: list[ColumnRequirement] = field(default_factory=list)
    optional: list[ColumnRequirement] = field(default_factory=list)


CUSTOMER_ORDERS = DatasetRequirements(
    dataset_name="customer_orders",
    label="Customer Orders",
    required=[
        ColumnRequirement("order_date", "The date an order was placed", "2025-08-15", is_date=True),
        ColumnRequirement("quantity", "How many units were ordered", "120"),
    ],
    optional=[
        ColumnRequirement("sku", "The item ordered — without it, the demand forecast is one aggregate total instead of broken out per item", "SKU-WIDGET"),
        ColumnRequirement("category", "The item's product category — without it, the demand forecast can't be broken out per category", "Electronics"),
        ColumnRequirement("region", "Where the order shipped to/from — without it, the demand forecast can't be broken out per region", "West"),
    ],
)

INVENTORY = DatasetRequirements(
    dataset_name="inventory",
    label="Inventory",
    required=[
        ColumnRequirement("sku", "The item's ID", "SKU-WIDGET"),
        ColumnRequirement("current_stock", "How many units you have on hand right now", "500"),
        ColumnRequirement("safety_stock", "The minimum buffer you want to keep on hand", "50"),
        ColumnRequirement("daily_demand_rate", "Units you typically sell/use per day", "15"),
        ColumnRequirement("lead_time_days", "Days it takes a reorder to arrive", "10"),
    ],
    optional=[
        ColumnRequirement("incoming_stock", "Confirmed units already on order, not yet received — without it, risk is assessed on-hand stock only", "200"),
        ColumnRequirement("unit_price", "The item's selling price — without it, revenue-at-risk can't be calculated", "49.99"),
        ColumnRequirement("demand_std_dev", "Day-to-day variability in demand — without it, stockout probability falls back to a coverage-based estimate instead of a statistical one", "4.2"),
        ColumnRequirement("supplier", "Which supplier this item is sourced from — without it, Delivery Intelligence can't link this item's stockout risk back to its supplier's delivery reliability", "Acme Supply"),
    ],
)

DELIVERY_RECORDS = DatasetRequirements(
    dataset_name="delivery_records",
    label="Delivery Records",
    required=[
        ColumnRequirement("po_id", "The purchase order's ID", "PO-1003"),
        ColumnRequirement("expected_date", "When the delivery was supposed to arrive", "2025-06-15", is_date=True),
        ColumnRequirement("actual_date", "When the delivery actually arrived", "2025-06-30", is_date=True),
    ],
    optional=[
        ColumnRequirement("supplier", "The supplier's name — without it, that row cannot be scored per supplier", "Acme Supply"),
        ColumnRequirement("transportation_cost", "The delivery's shipping cost — used only for cost figures", "1200.00"),
    ],
)

# Every known dataset, in the fixed order the UI presents them.
ALL_DATASETS: list[DatasetRequirements] = [CUSTOMER_ORDERS, INVENTORY, DELIVERY_RECORDS]

BY_NAME: dict[str, DatasetRequirements] = {d.dataset_name: d for d in ALL_DATASETS}
