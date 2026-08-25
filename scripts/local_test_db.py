"""Local Postgres test environment for running this repo's demo scripts against real data.

Not part of the shipped data_integration/forecasting/risk_detection code -
this is dev/test tooling only, per CLAUDE.md's /scripts convention
("repo-root operational scripts... single clear responsibility").

Uses pgserver (a self-contained, pip-installed PostgreSQL binary - no
Docker, no system package manager, no root needed) so every story's
"no real .../ schema exists yet" demo gap can be closed locally without
provisioning any external service. Data lives in scripts/.local_pgdata/
(gitignored). The server process persists after this script exits
(cleanup_mode=None while running - see pgserver's own docs for what that
governs) so a shell session can start it once, export the printed env
vars, and run any of this repo's run_sample_*.py scripts against it;
--stop shuts it down cleanly later.

Seeds three tables matching the column-name assumptions already
documented in this repo's demo scripts and STORY-005's
risk_detection/anomaly_detection.py module docstring:
    customer_orders(order_date, sku, quantity)
    inventory(sku, current_stock, safety_stock, daily_demand_rate, lead_time_days)
    delivery_records(po_id, supplier, expected_date, actual_date)
Seed data deliberately includes one demand-spike month, one critically
low-stock SKU, and one badly late delivery, so a demo run against this
database actually exercises the anomaly/risk-detection paths instead of
reporting "nothing anomalous found."

Usage:
    python3 scripts/local_test_db.py            # start (or reuse) + seed + print env exports
    eval "$(python3 scripts/local_test_db.py)"  # ...and load them into the current shell
    python3 scripts/local_test_db.py --reseed   # wipe and reseed the three tables
    python3 scripts/local_test_db.py --stop     # cleanly stop the server
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pgserver
import psycopg2

PGDATA = Path(__file__).resolve().parent / ".local_pgdata"
DATABASE_NAME = "supplymind_test"
POSTGRES_USER = "postgres"
POSTGRES_PASSWORD = "local-test-only"  # ignored by pgserver's trust auth over its unix socket; required as non-empty by data_integration/config.py's _require_env

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS customer_orders (
    order_id SERIAL PRIMARY KEY,
    order_date DATE NOT NULL,
    sku TEXT NOT NULL,
    quantity NUMERIC NOT NULL
);

CREATE TABLE IF NOT EXISTS inventory (
    sku TEXT PRIMARY KEY,
    current_stock NUMERIC NOT NULL,
    safety_stock NUMERIC NOT NULL,
    daily_demand_rate NUMERIC NOT NULL,
    lead_time_days NUMERIC NOT NULL
);

CREATE TABLE IF NOT EXISTS delivery_records (
    po_id TEXT PRIMARY KEY,
    supplier TEXT,
    expected_date DATE NOT NULL,
    actual_date DATE NOT NULL
);
"""


def _customer_orders_rows() -> list[tuple]:
    # 12 months of steady demand (roughly 95-108 units), one deliberate
    # spike month (2025-07 at 900) so a live demo run against this
    # database actually finds a demand anomaly instead of reporting none.
    monthly_totals = {
        "2025-01": 100, "2025-02": 108, "2025-03": 95, "2025-04": 103,
        "2025-05": 99, "2025-06": 105, "2025-07": 900, "2025-08": 101,
        "2025-09": 97, "2025-10": 106, "2025-11": 102, "2025-12": 104,
    }
    return [(f"{period}-15", "SKU-WIDGET", total) for period, total in monthly_totals.items()]


def _inventory_rows() -> list[tuple]:
    return [
        ("SKU-WIDGET", 500.0, 50.0, 15.0, 10.0),  # healthy
        ("SKU-GADGET", 200.0, 40.0, 8.0, 14.0),  # healthy
        ("SKU-GIZMO", 3.0, 30.0, 6.0, 12.0),  # critical: already below safety stock
    ]


def _delivery_rows() -> list[tuple]:
    return [
        ("PO-1001", "Acme Supply", "2025-06-01", "2025-06-02"),  # on time
        ("PO-1002", "Acme Supply", "2025-06-10", "2025-06-11"),  # on time
        ("PO-1003", "Beta Logistics", "2025-06-15", "2025-06-30"),  # badly late (15 days)
    ]


def _start_server() -> "pgserver.PostgresServer":
    PGDATA.mkdir(parents=True, exist_ok=True)
    return pgserver.get_server(str(PGDATA), cleanup_mode=None)


def _socket_dir(server: "pgserver.PostgresServer") -> str:
    match = re.search(r"host=([^&]+)", server.get_uri())
    if not match:
        raise RuntimeError(f"could not parse socket directory from pgserver URI: {server.get_uri()}")
    return match.group(1)


def _ensure_database_exists(socket_dir: str) -> None:
    conn = psycopg2.connect(
        host=socket_dir, port=5432, dbname="postgres", user=POSTGRES_USER, connect_timeout=10
    )
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DATABASE_NAME,))
            if cur.fetchone() is None:
                cur.execute(f"CREATE DATABASE {DATABASE_NAME}")
    finally:
        conn.close()


def _connect_to_test_database(socket_dir: str) -> "psycopg2.extensions.connection":
    return psycopg2.connect(
        host=socket_dir, port=5432, dbname=DATABASE_NAME, user=POSTGRES_USER, connect_timeout=10
    )


def _seed(conn: "psycopg2.extensions.connection", reseed: bool) -> None:
    with conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)

            if reseed:
                cur.execute("TRUNCATE customer_orders, inventory, delivery_records")

            cur.execute("SELECT COUNT(*) FROM customer_orders")
            if cur.fetchone()[0] == 0:
                cur.executemany(
                    "INSERT INTO customer_orders (order_date, sku, quantity) VALUES (%s, %s, %s)",
                    _customer_orders_rows(),
                )

            cur.execute("SELECT COUNT(*) FROM inventory")
            if cur.fetchone()[0] == 0:
                cur.executemany(
                    "INSERT INTO inventory (sku, current_stock, safety_stock, daily_demand_rate, lead_time_days) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    _inventory_rows(),
                )

            cur.execute("SELECT COUNT(*) FROM delivery_records")
            if cur.fetchone()[0] == 0:
                cur.executemany(
                    "INSERT INTO delivery_records (po_id, supplier, expected_date, actual_date) VALUES (%s, %s, %s, %s)",
                    _delivery_rows(),
                )


def _print_env_exports(socket_dir: str) -> None:
    # stdout carries only the exports, so `eval "$(python3 scripts/local_test_db.py)"`
    # works - every informational message in this script goes to stderr instead.
    print(f"export SUPPLYMIND_PG_HOST='{socket_dir}'")
    print("export SUPPLYMIND_PG_PORT='5432'")
    print(f"export SUPPLYMIND_PG_DATABASE='{DATABASE_NAME}'")
    print(f"export SUPPLYMIND_PG_USER='{POSTGRES_USER}'")
    print(f"export SUPPLYMIND_PG_PASSWORD='{POSTGRES_PASSWORD}'")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reseed", action="store_true", help="wipe and reseed the three tables")
    parser.add_argument("--stop", action="store_true", help="stop the local test server and exit")
    args = parser.parse_args()

    if args.stop:
        if not PGDATA.exists():
            print("no local test database has been started", file=sys.stderr)
            return 0
        server = pgserver.get_server(str(PGDATA), cleanup_mode="stop")
        server.cleanup()
        print("stopped local test Postgres server", file=sys.stderr)
        return 0

    server = _start_server()
    socket_dir = _socket_dir(server)
    _ensure_database_exists(socket_dir)

    conn = _connect_to_test_database(socket_dir)
    try:
        _seed(conn, reseed=args.reseed)
    finally:
        conn.close()

    print(f"local test Postgres ready at {socket_dir} (database: {DATABASE_NAME})", file=sys.stderr)
    _print_env_exports(socket_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
