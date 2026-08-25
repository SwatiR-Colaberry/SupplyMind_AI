"""Multi-tenant demo for STORY-005's risk detection, through the real connection-profile layer.

Extends data_integration/run_sample_connection_profile_demo.py's proof
(two tenants, incompatible column names, one unmodified downstream agent)
in two ways specific to STORY-005:
    1. Exercises RiskDetectionAgent, not just DemandForecastingAgent - and
       therefore exercises delivery_records, the dataset kind STORY-005
       added to data_integration/connection_profile.py's
       REQUIRED_FIELDS_BY_DATASET_KIND registry (it didn't exist when the
       original demo was written).
    2. Uses fetch_profile_data() - the real, audited, I/O-performing path
       - against a real running PostgreSQL server (scripts/local_test_db.py),
       instead of synthetic rows standing in for it. The original demo
       could not do this ("no live PostgreSQL configured... does not call
       fetch_profile_data()"); this environment now has one.

Two fictitious tenants share this local test database (different tables,
not different servers - a deliberate simplification for a local demo; a
real multi-tenant deployment would give each tenant their own
credentials, which ConnectionProfile.postgres already supports per-profile)
but use completely different column names for the same three canonical
dataset kinds. remap_rows() (inside fetch_profile_data()) is what makes
RiskDetectionAgent correct for both without a single per-tenant branch in
its own code - acme_retail has a demand spike, a critical SKU, and a late
delivery seeded in; globex_supply has none, to show the same pipeline
correctly produces two very different risk scores from two differently-shaped
schemas.

Prerequisite: the local test Postgres must already be running -
    eval "$(python3 scripts/local_test_db.py)"

Usage:
    python -m risk_detection.run_sample_multi_tenant_risk_detection
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import psycopg2

from agents.contracts import AgentQuery
from agents.orchestrator import Orchestrator
from agents.risk_detection_agent import RiskDetectionAgent
from data_integration.audit_trail import AuditStore
from data_integration.config import load_postgres_config
from data_integration.connection_profile import ConnectionProfile, SchemaMappingError, fetch_profile_data
from data_integration.connection_profile import validate_mapping_completeness

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS acme_orders (order_dt DATE, qty NUMERIC);
CREATE TABLE IF NOT EXISTS acme_inventory (
    item_sku TEXT PRIMARY KEY, on_hand NUMERIC, min_stock NUMERIC, daily_use NUMERIC, lead_days NUMERIC
);
CREATE TABLE IF NOT EXISTS acme_deliveries (po_number TEXT PRIMARY KEY, due_date DATE, received_date DATE);

CREATE TABLE IF NOT EXISTS globex_orders (txn_date DATE, units_sold NUMERIC);
CREATE TABLE IF NOT EXISTS globex_inventory (
    part_code TEXT PRIMARY KEY, stock_qty NUMERIC, safety_qty NUMERIC,
    avg_daily_demand NUMERIC, replenish_lead_days NUMERIC
);
CREATE TABLE IF NOT EXISTS globex_deliveries (order_ref TEXT PRIMARY KEY, promised_by DATE, arrived_on DATE);
"""


def _acme_orders_rows() -> list[tuple]:
    # Steady ~95-108/month, one deliberate spike (2025-07 at 850).
    totals = {
        "2025-01": 100, "2025-02": 104, "2025-03": 98, "2025-04": 101,
        "2025-05": 96, "2025-06": 103, "2025-07": 850, "2025-08": 99,
        "2025-09": 102, "2025-10": 97, "2025-11": 105, "2025-12": 100,
    }
    return [(f"{period}-10", total) for period, total in totals.items()]


def _globex_orders_rows() -> list[tuple]:
    # Steady demand, no anomaly - the contrast case.
    totals = {
        "2025-01": 200, "2025-02": 205, "2025-03": 198, "2025-04": 210,
        "2025-05": 195, "2025-06": 202, "2025-07": 208, "2025-08": 199,
        "2025-09": 204, "2025-10": 197, "2025-11": 206, "2025-12": 201,
    }
    return [(f"{period}-10", total) for period, total in totals.items()]


def _seed(conn) -> None:
    with conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)

            cur.execute("SELECT COUNT(*) FROM acme_orders")
            if cur.fetchone()[0] == 0:
                cur.executemany("INSERT INTO acme_orders (order_dt, qty) VALUES (%s, %s)", _acme_orders_rows())
                cur.execute(
                    "INSERT INTO acme_inventory (item_sku, on_hand, min_stock, daily_use, lead_days) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    ("SKU-ACME-1", 4.0, 40.0, 8.0, 12.0),  # critical: below safety stock
                )
                cur.execute(
                    "INSERT INTO acme_deliveries (po_number, due_date, received_date) VALUES (%s, %s, %s)",
                    ("PO-ACME-1", "2025-05-01", "2025-05-20"),  # 19 days late
                )

            cur.execute("SELECT COUNT(*) FROM globex_orders")
            if cur.fetchone()[0] == 0:
                cur.executemany(
                    "INSERT INTO globex_orders (txn_date, units_sold) VALUES (%s, %s)", _globex_orders_rows()
                )
                cur.execute(
                    "INSERT INTO globex_inventory "
                    "(part_code, stock_qty, safety_qty, avg_daily_demand, replenish_lead_days) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    ("SKU-GLOBEX-1", 400.0, 50.0, 10.0, 10.0),  # healthy
                )
                cur.execute(
                    "INSERT INTO globex_deliveries (order_ref, promised_by, arrived_on) VALUES (%s, %s, %s)",
                    ("PO-GLOBEX-1", "2025-05-01", "2025-05-02"),  # on time
                )


# One (dataset_kind, query, column_mapping) spec per context key, per
# tenant - postgres isn't known until main() reads it from the live local
# test DB's env vars, so full ConnectionProfiles are only built there
# (_build_profiles()), not at import time with a placeholder value.
ACME_SPECS = {
    "demand_history": ("customer_orders", "SELECT * FROM acme_orders", {"order_date": "order_dt", "quantity": "qty"}),
    "inventory_rows": (
        "inventory",
        "SELECT * FROM acme_inventory",
        {
            "sku": "item_sku", "current_stock": "on_hand", "safety_stock": "min_stock",
            "daily_demand_rate": "daily_use", "lead_time_days": "lead_days",
        },
    ),
    "delivery_rows": (
        "delivery_records",
        "SELECT * FROM acme_deliveries",
        {"po_id": "po_number", "expected_date": "due_date", "actual_date": "received_date"},
    ),
}

GLOBEX_SPECS = {
    "demand_history": (
        "customer_orders", "SELECT * FROM globex_orders", {"order_date": "txn_date", "quantity": "units_sold"}
    ),
    "inventory_rows": (
        "inventory",
        "SELECT * FROM globex_inventory",
        {
            "sku": "part_code", "current_stock": "stock_qty", "safety_stock": "safety_qty",
            "daily_demand_rate": "avg_daily_demand", "lead_time_days": "replenish_lead_days",
        },
    ),
    "delivery_rows": (
        "delivery_records",
        "SELECT * FROM globex_deliveries",
        {"po_id": "order_ref", "expected_date": "promised_by", "actual_date": "arrived_on"},
    ),
}

# A third, misconfigured tenant - proves the fail-loud-at-connect-time path
# for the newly-registered delivery_records dataset kind specifically.
BROKEN_DELIVERY_SPEC = (
    "delivery_records",
    "SELECT * FROM acme_deliveries",
    {"po_id": "po_number"},  # expected_date/actual_date never mapped
)


def _build_profiles(
    tenant_id: str, specs: dict[str, tuple[str, str, dict[str, str]]], pg_config
) -> dict[str, ConnectionProfile]:
    return {
        context_key: ConnectionProfile(
            tenant_id=tenant_id, dataset_kind=dataset_kind, postgres=pg_config, query=query, column_mapping=mapping
        )
        for context_key, (dataset_kind, query, mapping) in specs.items()
    }


def _run_tenant(profiles: dict[str, ConnectionProfile], audit_store: AuditStore) -> dict:
    context = {context_key: fetch_profile_data(profile, audit_store) for context_key, profile in profiles.items()}

    orchestrator = Orchestrator([RiskDetectionAgent()])
    run = orchestrator.coordinate(AgentQuery(text="detect supply chain risk", context=context))
    response = run.results[0].response

    return {
        "tenant_id": next(iter(profiles.values())).tenant_id,
        # This tenant's own column names, before remap_rows() translated
        # them - what actually differs between tenants and proves
        # RiskDetectionAgent never sees it.
        "tenant_column_names": {key: list(p.column_mapping.values()) for key, p in profiles.items()},
        "coordination_outcome": run.outcome,
        "risk_response_status": response.status if response else None,
        "recommendation": response.recommendation if response else None,
    }


def main() -> int:
    pg_config = load_postgres_config()

    conn = psycopg2.connect(
        host=pg_config.host, port=pg_config.port, dbname=pg_config.database,
        user=pg_config.user, password=pg_config.password, connect_timeout=10,
    )
    try:
        _seed(conn)
    finally:
        conn.close()

    audit_store = AuditStore(Path(tempfile.mkdtemp()) / "demo_multi_tenant_audit_log.jsonl")
    results = [
        _run_tenant(_build_profiles("acme_retail", ACME_SPECS, pg_config), audit_store),
        _run_tenant(_build_profiles("globex_supply", GLOBEX_SPECS, pg_config), audit_store),
    ]

    broken_dataset_kind, broken_query, broken_mapping = BROKEN_DELIVERY_SPEC
    broken_profile = ConnectionProfile(
        tenant_id="initech_widgets",
        dataset_kind=broken_dataset_kind,
        postgres=pg_config,
        query=broken_query,
        column_mapping=broken_mapping,
    )
    try:
        validate_mapping_completeness(broken_profile)
        broken_result = "unexpectedly passed validation"
    except SchemaMappingError as exc:
        broken_result = f"caught at connect-time, as expected: {exc}"

    print(json.dumps({"tenants": results, "misconfigured_tenant": broken_result}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
