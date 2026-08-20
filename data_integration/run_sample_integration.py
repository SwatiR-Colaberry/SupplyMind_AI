"""Runnable entry point for STORY-001.

Pulls the datasets in this story's narrative (customer orders, product
catalog, inventory, warehouses, suppliers, purchase orders, shipments,
delivery records) from PostgreSQL and Google Sheets, then prints an
integration summary. Exit code is 0 only if every dataset succeeded.

Dataset-to-source split (a logged implementation assumption, since no
real schema exists yet to confirm against): high-volume transactional
data is assumed to live in PostgreSQL; reference/master data that
business teams typically maintain by hand is assumed to live in Google
Sheets. Swap the query text and spreadsheet IDs below once real
schemas/sheets are provisioned — nothing else needs to change.

Usage:
    SUPPLYMIND_PG_HOST=... SUPPLYMIND_PG_DATABASE=... SUPPLYMIND_PG_USER=... \\
    SUPPLYMIND_PG_PASSWORD=... SUPPLYMIND_GOOGLE_SERVICE_ACCOUNT_JSON=... \\
        python -m data_integration.run_sample_integration

Every run appends one audit-trail record per dataset to the JSONL file at
SUPPLYMIND_AUDIT_LOG_PATH (default: audit_log.jsonl next to this file).
Rerunning with unchanged source data does not add duplicate entries —
that's the STORY-011 idempotency guarantee, provable end-to-end by
running this script twice and diffing the audit log's line count.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from data_integration.audit_trail import AuditStore
from data_integration.orchestrator import (
    PostgresDataset,
    SheetsDataset,
    available_for_analysis,
    run_integration_with_audit,
)

DEFAULT_AUDIT_LOG_PATH = Path(__file__).resolve().parent / "audit_log.jsonl"

# Placeholders until real sheets are provisioned.
PRODUCT_CATALOG_SHEET_ID = "REPLACE_WITH_PRODUCT_CATALOG_SHEET_ID"
WAREHOUSE_MASTER_SHEET_ID = "REPLACE_WITH_WAREHOUSE_MASTER_SHEET_ID"
SUPPLIER_MASTER_SHEET_ID = "REPLACE_WITH_SUPPLIER_MASTER_SHEET_ID"

DATASETS = [
    PostgresDataset(name="customer_orders", query="SELECT * FROM customer_orders LIMIT 500"),
    PostgresDataset(name="inventory", query="SELECT * FROM inventory LIMIT 500"),
    PostgresDataset(name="purchase_orders", query="SELECT * FROM purchase_orders LIMIT 500"),
    PostgresDataset(name="shipments", query="SELECT * FROM shipments LIMIT 500"),
    PostgresDataset(name="delivery_records", query="SELECT * FROM delivery_records LIMIT 500"),
    SheetsDataset(
        name="product_catalog", spreadsheet_id=PRODUCT_CATALOG_SHEET_ID, worksheet_name="Products"
    ),
    SheetsDataset(
        name="warehouses", spreadsheet_id=WAREHOUSE_MASTER_SHEET_ID, worksheet_name="Warehouses"
    ),
    SheetsDataset(
        name="suppliers", spreadsheet_id=SUPPLIER_MASTER_SHEET_ID, worksheet_name="Suppliers"
    ),
]


def _audit_store() -> AuditStore:
    path = os.environ.get("SUPPLYMIND_AUDIT_LOG_PATH", str(DEFAULT_AUDIT_LOG_PATH))
    return AuditStore(path)


def main() -> int:
    audit_store = _audit_store()
    results = run_integration_with_audit(DATASETS, audit_store)
    analysis_ready = available_for_analysis(results)

    summary = {
        "datasets_attempted": len(results),
        "datasets_available": len(analysis_ready),
        "datasets_failed": [r.name for r in results if r.outcome == "failure"],
    }
    print(json.dumps(summary, indent=2))

    return 0 if not summary["datasets_failed"] else 1


if __name__ == "__main__":
    sys.exit(main())
