"""Runnable entry point for STORY-014.

Two scenarios are printed, following the same convention
supplier_evaluation/run_sample_supplier_evaluation.py established for a
story whose acceptance criteria can't be fully exercised by this
environment's real data alone:

1. "real_data" - pulls delivery_records via the STORY-001 data_integration
   orchestrator (the same audited pull supplier_evaluation's demo uses)
   and runs it through ShipmentDelayEvaluator. With this repo's current
   environment (no PostgreSQL credentials configured), the pull returns
   no rows and the analysis completes with zero delays found - a clean
   "no delivery data provided" outcome, not a crash.
2. "synthetic_delays" - a small, deterministic set of delivery rows
   chosen to exercise every acceptance criterion in one pass:
     - "Acme" - two on-time deliveries -> no delay pattern, no cost
       (baseline: an analysis that finds nothing still runs cleanly).
     - "SlowFreight" - three delayed deliveries of increasing severity
       (medium/high/critical) -> AC1: the delays are grouped into one
       recurring SupplierDelayPattern rather than three unrelated
       incidents. One row carries a real `transportation_cost`, one
       carries an unusable one (the "cost calculation errors" failure
       path - flagged, not fatal) -> AC2: a cost analysis is produced
       for every delay regardless, with the bad cost dropped to $0 and
       reported separately in cost_errors.
   Every delayed PO's analysis is confirmed present in the audit trail
   after the run (AC3: an audit trail of delay analysis is maintained),
   printed alongside the costs rather than only asserted in tests.

Query contract (logged assumption, not escalated - same situation
risk_detection/run_sample_risk_detection.py and
supplier_evaluation/run_sample_supplier_evaluation.py were in: no real
schema exists yet to confirm against): delivery_records has one row per
purchase order with po_id, supplier, expected_date, actual_date, and
optionally transportation_cost - see
risk_detection/anomaly_detection.py's REQUIRED_DELIVERY_FIELDS and
shipment_delay_analysis/delay_analysis.py's DEFAULT_COST_PER_DAY_LATE
docstring.

Usage:
    SUPPLYMIND_PG_HOST=... SUPPLYMIND_PG_DATABASE=... SUPPLYMIND_PG_USER=... \\
    SUPPLYMIND_PG_PASSWORD=... \\
        python -m shipment_delay_analysis.run_sample_shipment_delay_analysis
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

import data_integration
import shipment_delay_analysis
from data_integration.audit_trail import AuditStore
from data_integration.orchestrator import PostgresDataset, available_for_analysis, run_integration_with_audit
from shipment_delay_analysis.audit_trail import ShipmentDelayAuditStore
from shipment_delay_analysis.evaluator import ShipmentDelayAnalysisRun, ShipmentDelayEvaluator

DELIVERY_RECORDS_DATASET = PostgresDataset(
    name="delivery_records",
    query="SELECT po_id, supplier, expected_date, actual_date, transportation_cost FROM delivery_records LIMIT 500",
)

DEFAULT_AUDIT_LOG_PATH = Path(data_integration.__file__).resolve().parent / "audit_log.jsonl"
DEFAULT_ANALYSIS_AUDIT_LOG_PATH = (
    Path(shipment_delay_analysis.__file__).resolve().parent / "delay_analysis_audit_log.jsonl"
)

SYNTHETIC_DELIVERY_ROWS = [
    {"supplier": "Acme", "po_id": "PO-1001", "expected_date": "2025-01-05", "actual_date": "2025-01-05"},
    {"supplier": "Acme", "po_id": "PO-1002", "expected_date": "2025-01-12", "actual_date": "2025-01-12"},
    {
        "supplier": "SlowFreight",
        "po_id": "PO-2001",
        "expected_date": "2025-01-03",
        "actual_date": "2025-01-05",  # 2 days late -> medium
    },
    {
        "supplier": "SlowFreight",
        "po_id": "PO-2002",
        "expected_date": "2025-01-10",
        "actual_date": "2025-01-15",  # 5 days late -> high
        "transportation_cost": 600,
    },
    {
        "supplier": "SlowFreight",
        "po_id": "PO-2003",
        "expected_date": "2025-01-17",
        "actual_date": "2025-02-04",  # 18 days late -> critical
        "transportation_cost": "not-a-number",  # cost calculation error - flagged, not fatal
    },
]


def _audit_store() -> AuditStore:
    path = os.environ.get("SUPPLYMIND_AUDIT_LOG_PATH", str(DEFAULT_AUDIT_LOG_PATH))
    return AuditStore(path)


def _analysis_audit_store() -> ShipmentDelayAuditStore:
    path = os.environ.get("SUPPLYMIND_SHIPMENT_DELAY_AUDIT_LOG_PATH", str(DEFAULT_ANALYSIS_AUDIT_LOG_PATH))
    return ShipmentDelayAuditStore(path)


def _summarize(scenario: str, run: ShipmentDelayAnalysisRun, audit_store: ShipmentDelayAuditStore) -> dict:
    audit_records = audit_store.records_for_analysis(run.analysis_id)
    return {
        "scenario": scenario,
        "analysis_id": run.analysis_id,
        "outcome": run.outcome,
        "error": run.crash_error,
        "warnings": run.warnings,
        "total_cost": run.total_cost,
        "delay_costs": [asdict(c) for c in run.delay_costs],
        "patterns": [asdict(p) for p in run.patterns],
        "audit_trail": {
            "records_written": len(audit_records),
            "pos_audited": sorted(r.po_id for r in audit_records if r.po_id is not None),
        },
    }


def _real_data_scenario(evaluator: ShipmentDelayEvaluator, audit_store: ShipmentDelayAuditStore) -> dict:
    results = run_integration_with_audit([DELIVERY_RECORDS_DATASET], _audit_store())
    analysis_ready = available_for_analysis(results)
    delivery_rows = analysis_ready.get("delivery_records", [])

    run = evaluator.run(delivery_rows, analysis_id="real_data")
    return _summarize("real_data", run, audit_store)


def _synthetic_delays_scenario(evaluator: ShipmentDelayEvaluator, audit_store: ShipmentDelayAuditStore) -> dict:
    run = evaluator.run(SYNTHETIC_DELIVERY_ROWS, analysis_id="synthetic_delays")
    return _summarize("synthetic_delays", run, audit_store)


def main() -> int:
    audit_store = _analysis_audit_store()
    evaluator = ShipmentDelayEvaluator(audit_store)

    scenarios = [
        _real_data_scenario(evaluator, audit_store),
        _synthetic_delays_scenario(evaluator, audit_store),
    ]
    print(json.dumps(scenarios, indent=2, default=str))

    synthetic = scenarios[1]
    demo_succeeded = (
        synthetic["outcome"] == "success"
        and synthetic["audit_trail"]["records_written"] == len(synthetic["delay_costs"]) == 3
        and [p["supplier"] for p in synthetic["patterns"]] == ["SlowFreight"]
        and synthetic["patterns"][0]["delay_count"] == 3
        and synthetic["total_cost"] > 0
    )
    return 0 if demo_succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
