"""Runnable entry point for STORY-015.

Three scenarios are printed, following the same convention
supplier_evaluation/run_sample_supplier_evaluation.py and
shipment_delay_analysis/run_sample_shipment_delay_analysis.py established
for a story whose acceptance criteria can't be fully exercised by this
environment's real data alone:

1. "real_data" - pulls delivery_records via the STORY-001 data_integration
   orchestrator (the same audited pull the other two evaluators' demos
   use) and runs it through DataQualityEvaluator. With this repo's
   current environment (no PostgreSQL credentials configured), the pull
   returns no rows and the check completes with an alert-worthy "no rows
   available to assess data quality" outcome rather than a fabricated
   score or a crash.
2. "synthetic_good_quality" - every row carries all three required
   fields -> AC1: the system provides a Data Quality Score (100/100,
   "good"), and no alert is raised.
3. "synthetic_poor_quality" - most rows are missing a required field ->
   AC2: given poor data quality, the system alerts the data steward (a
   logged `data_quality_alert_raised` event plus a run-level audit
   record).

Every scenario's audit trail is confirmed present after the run (AC3/
Trust: an audit trail of quality checks is maintained), printed alongside
the score rather than only asserted in tests.

Query contract (logged assumption, not escalated - same situation
risk_detection/run_sample_risk_detection.py, supplier_evaluation's, and
shipment_delay_analysis's own demos are in: no real schema exists yet to
confirm against): reuses the same delivery_records shape and required
fields (po_id, expected_date, actual_date - see
risk_detection/anomaly_detection.py's REQUIRED_DELIVERY_FIELDS) the other
two stories already standardized on, rather than inventing a fourth
dataset shape - data quality monitoring is meant to apply to data this
repo already ingests, not a new source.

Usage:
    SUPPLYMIND_PG_HOST=... SUPPLYMIND_PG_DATABASE=... SUPPLYMIND_PG_USER=... \\
    SUPPLYMIND_PG_PASSWORD=... \\
        python -m data_quality_monitoring.run_sample_data_quality_monitoring
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

import data_integration
import data_quality_monitoring
from data_integration.audit_trail import AuditStore
from data_integration.orchestrator import PostgresDataset, available_for_analysis, run_integration_with_audit
from data_quality_monitoring.audit_trail import QualityAuditStore
from data_quality_monitoring.evaluator import DataQualityCheckRun, DataQualityEvaluator
from risk_detection.anomaly_detection import REQUIRED_DELIVERY_FIELDS

DELIVERY_RECORDS_DATASET = PostgresDataset(
    name="delivery_records",
    query="SELECT po_id, supplier, expected_date, actual_date, transportation_cost FROM delivery_records LIMIT 500",
)

DEFAULT_AUDIT_LOG_PATH = Path(data_integration.__file__).resolve().parent / "audit_log.jsonl"
DEFAULT_QUALITY_AUDIT_LOG_PATH = (
    Path(data_quality_monitoring.__file__).resolve().parent / "quality_audit_log.jsonl"
)

SYNTHETIC_GOOD_QUALITY_ROWS = [
    {"po_id": "PO-1001", "expected_date": "2025-01-05", "actual_date": "2025-01-05"},
    {"po_id": "PO-1002", "expected_date": "2025-01-12", "actual_date": "2025-01-14"},
    {"po_id": "PO-1003", "expected_date": "2025-01-20", "actual_date": "2025-01-20"},
]

SYNTHETIC_POOR_QUALITY_ROWS = [
    {"po_id": "PO-2001", "expected_date": "2025-01-05", "actual_date": "2025-01-05"},  # complete
    {"po_id": "PO-2002", "expected_date": "2025-01-12"},  # missing actual_date
    {"po_id": "PO-2003"},  # missing expected_date and actual_date
    {"expected_date": "2025-01-20", "actual_date": "2025-01-22"},  # missing po_id
]


def _audit_store() -> AuditStore:
    path = os.environ.get("SUPPLYMIND_AUDIT_LOG_PATH", str(DEFAULT_AUDIT_LOG_PATH))
    return AuditStore(path)


def _quality_audit_store() -> QualityAuditStore:
    path = os.environ.get("SUPPLYMIND_QUALITY_AUDIT_LOG_PATH", str(DEFAULT_QUALITY_AUDIT_LOG_PATH))
    return QualityAuditStore(path)


def _summarize(scenario: str, run: DataQualityCheckRun, audit_store: QualityAuditStore) -> dict:
    audit_records = audit_store.records_for_check(run.check_id)
    return {
        "scenario": scenario,
        "check_id": run.check_id,
        "outcome": run.outcome,
        "error": run.crash_error,
        "poor_quality": run.poor_quality,
        "report": asdict(run.report) if run.report is not None else None,
        "audit_trail": {
            "records_written": len(audit_records),
            "dimensions_audited": sorted(r.dimension for r in audit_records if r.dimension is not None),
            "alert_recorded": any(r.dimension is None for r in audit_records),
        },
    }


def _real_data_scenario(evaluator: DataQualityEvaluator, audit_store: QualityAuditStore) -> dict:
    results = run_integration_with_audit([DELIVERY_RECORDS_DATASET], _audit_store())
    analysis_ready = available_for_analysis(results)
    delivery_rows = analysis_ready.get("delivery_records", [])

    run = evaluator.run(delivery_rows, required_fields=REQUIRED_DELIVERY_FIELDS, check_id="real_data")
    return _summarize("real_data", run, audit_store)


def _synthetic_good_quality_scenario(evaluator: DataQualityEvaluator, audit_store: QualityAuditStore) -> dict:
    run = evaluator.run(
        SYNTHETIC_GOOD_QUALITY_ROWS, required_fields=REQUIRED_DELIVERY_FIELDS, check_id="synthetic_good_quality"
    )
    return _summarize("synthetic_good_quality", run, audit_store)


def _synthetic_poor_quality_scenario(evaluator: DataQualityEvaluator, audit_store: QualityAuditStore) -> dict:
    run = evaluator.run(
        SYNTHETIC_POOR_QUALITY_ROWS, required_fields=REQUIRED_DELIVERY_FIELDS, check_id="synthetic_poor_quality"
    )
    return _summarize("synthetic_poor_quality", run, audit_store)


def main() -> int:
    audit_store = _quality_audit_store()
    evaluator = DataQualityEvaluator(audit_store)

    scenarios = [
        _real_data_scenario(evaluator, audit_store),
        _synthetic_good_quality_scenario(evaluator, audit_store),
        _synthetic_poor_quality_scenario(evaluator, audit_store),
    ]
    print(json.dumps(scenarios, indent=2, default=str))

    good, poor = scenarios[1], scenarios[2]
    demo_succeeded = (
        good["outcome"] == "success"
        and good["report"]["overall_score"] == 100.0
        and good["poor_quality"] is False
        and good["audit_trail"]["records_written"] == 1
        and poor["outcome"] == "success"
        and poor["poor_quality"] is True
        and poor["report"]["overall_score"] == 25.0
        and poor["audit_trail"]["alert_recorded"] is True
    )
    return 0 if demo_succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
