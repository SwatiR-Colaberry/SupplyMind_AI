"""Best-effort column-name guesses to speed up mapping setup - never authoritative.

Every suggestion here is a starting point a human still confirms or
overrides before it's saved; data_integration/connection_profile.py's own
docstring is explicit that a column mapping must be "explicit, never
inferred" at the point it's actually used for a live pull - this module
only helps pre-fill the mapping form faster, it never makes a mapping
decision on its own, and nothing here is trusted without going back
through connection_profile.py's own validate_profile() before a mapping
is saved.
"""

from __future__ import annotations

from data_console.column_requirements import DatasetRequirements

# Not exhaustive - drawn partly from this repo's own multi-tenant test
# fixture (risk_detection/run_sample_multi_tenant_risk_detection.py's
# acme_/globex_ tables use item_sku, on_hand, min_stock, daily_use,
# lead_days, order_dt, qty, po_number, due_date, received_date) and
# partly common real-world naming variants. A field with no synonym
# listed here still gets suggested via an exact-name match; it just
# won't be suggested under an unlisted alternate name until this list
# grows - a missed suggestion only costs the user picking it from a
# dropdown themselves, never a wrong mapping being saved silently.
SYNONYMS: dict[str, list[str]] = {
    "order_date": ["order_dt", "orderdate", "txn_date", "transaction_date", "date"],
    "quantity": ["qty", "units_sold", "order_qty", "amount"],
    "sku": ["item_sku", "product_id", "item_code", "product_sku", "sku_id"],
    "current_stock": ["on_hand", "stock_qty", "quantity_on_hand", "stock_level", "available_stock"],
    "safety_stock": ["min_stock", "reorder_point", "min_quantity", "safety_qty"],
    "daily_demand_rate": ["daily_use", "avg_daily_demand", "daily_sales_rate", "usage_rate"],
    "lead_time_days": ["lead_days", "leadtime", "supplier_lead_time", "delivery_lead_time"],
    "po_id": ["po_number", "purchase_order_id", "order_id", "po"],
    "expected_date": ["due_date", "expected_delivery_date", "promised_date"],
    "actual_date": ["received_date", "delivered_date", "actual_delivery_date"],
    "supplier": ["vendor", "supplier_name", "vendor_name"],
    "transportation_cost": ["shipping_cost", "freight_cost", "delivery_cost"],
}


def _suggest_one(field_name: str, lower_to_actual: dict[str, str]) -> str | None:
    if field_name in lower_to_actual:
        return lower_to_actual[field_name]
    for synonym in SYNONYMS.get(field_name, []):
        if synonym in lower_to_actual:
            return lower_to_actual[synonym]
    return None


def suggest_mapping(available_columns: list[str], requirements: DatasetRequirements) -> dict[str, str | None]:
    """Pure. One suggested column name (or None if no plausible match was found) per
    required+optional field in `requirements`, keyed on the canonical field name."""
    lower_to_actual = {name.strip().lower(): name for name in available_columns}
    fields = [c.name for c in requirements.required] + [c.name for c in requirements.optional]
    return {field: _suggest_one(field, lower_to_actual) for field in fields}
