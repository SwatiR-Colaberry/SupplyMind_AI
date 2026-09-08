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
        ColumnRequirement("order_date", "The date an order was placed", "2025-08-15"),
        ColumnRequirement("quantity", "How many units were ordered", "120"),
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
)

DELIVERY_RECORDS = DatasetRequirements(
    dataset_name="delivery_records",
    label="Delivery Records",
    required=[
        ColumnRequirement("po_id", "The purchase order's ID", "PO-1003"),
        ColumnRequirement("expected_date", "When the delivery was supposed to arrive", "2025-06-15"),
        ColumnRequirement("actual_date", "When the delivery actually arrived", "2025-06-30"),
    ],
    optional=[
        ColumnRequirement("supplier", "The supplier's name - without it, that row can't be scored per-supplier", "Acme Supply"),
        ColumnRequirement("transportation_cost", "The delivery's shipping cost - only used for cost figures", "1200.00"),
    ],
)

# Every known dataset, in the fixed order the UI presents them.
ALL_DATASETS: list[DatasetRequirements] = [CUSTOMER_ORDERS, INVENTORY, DELIVERY_RECORDS]

BY_NAME: dict[str, DatasetRequirements] = {d.dataset_name: d for d in ALL_DATASETS}
