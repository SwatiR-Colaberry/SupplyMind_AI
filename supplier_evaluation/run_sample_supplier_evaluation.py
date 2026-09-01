"""Runnable entry point for STORY-013.

Two scenarios are printed, following the same convention
recommendation/run_sample_recommendation_demo.py established for a story
whose acceptance criteria can't be fully exercised by this environment's
real data alone:

1. "real_data" - pulls delivery_records via the STORY-001 data_integration
   orchestrator (the same audited pull risk_detection/run_sample_risk_detection.py
   uses) and runs it through SupplierEvaluator. With this repo's current
   environment (no PostgreSQL credentials configured), the pull returns no
   rows and the evaluation completes with zero suppliers evaluated - a
   clean "no delivery data provided" outcome, not a crash.
2. "synthetic_suppliers" - a small, deterministic set of delivery rows
   spanning three suppliers chosen to exercise every acceptance criterion
   in one pass:
     - "Acme" - consistently on-time -> a low, unflagged Supplier Risk
       Score (AC1: a score is generated for supplier data).
     - "SlowFreight" - three deliveries, each 15-20 days late -> a high
       Supplier Risk Score, flagged for review (AC2: unreliable supplier
       data is flagged).
     - "NewVendor" - a single on-time delivery - too small a sample to
       score confidently, flagged for review for a different reason than
       SlowFreight (insufficient data, not poor performance), showing the
       "Incorrect Supplier Risk Score" failure path is handled rather than
       silently producing a falsely-reassuring perfect score.
   Every supplier's evaluation is confirmed present in the audit trail
   after the run (AC3: an audit trail of the evaluation process is
   recorded), printed alongside the scores rather than only asserted in
   tests.

Query contract (logged assumption, not escalated - same situation
risk_detection/run_sample_risk_detection.py was in: no real schema exists
yet to confirm against): delivery_records has one row per purchase order
with po_id, supplier, expected_date, actual_date - see
risk_detection/anomaly_detection.py's REQUIRED_DELIVERY_FIELDS, reused
here via supplier_evaluation/reliability.py.

Usage:
    SUPPLYMIND_PG_HOST=... SUPPLYMIND_PG_DATABASE=... SUPPLYMIND_PG_USER=... \\
    SUPPLYMIND_PG_PASSWORD=... \\
        python -m supplier_evaluation.run_sample_supplier_evaluation
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

import data_integration
import supplier_evaluation
from data_integration.audit_trail import AuditStore
from data_integration.orchestrator import PostgresDataset, available_for_analysis, run_integration_with_audit
from supplier_evaluation.audit_trail import SupplierEvaluationAuditStore
from supplier_evaluation.evaluator import SupplierEvaluationRun, SupplierEvaluator

DELIVERY_RECORDS_DATASET = PostgresDataset(
    name="delivery_records",
    query="SELECT po_id, supplier, expected_date, actual_date FROM delivery_records LIMIT 500",
)

DEFAULT_AUDIT_LOG_PATH = Path(data_integration.__file__).resolve().parent / "audit_log.jsonl"
DEFAULT_EVALUATION_AUDIT_LOG_PATH = (
    Path(supplier_evaluation.__file__).resolve().parent / "evaluation_audit_log.jsonl"
)

SYNTHETIC_DELIVERY_ROWS = [
    {"supplier": "Acme", "po_id": "PO-1001", "expected_date": "2025-01-05", "actual_date": "2025-01-05"},
    {"supplier": "Acme", "po_id": "PO-1002", "expected_date": "2025-01-12", "actual_date": "2025-01-12"},
    {"supplier": "Acme", "po_id": "PO-1003", "expected_date": "2025-01-19", "actual_date": "2025-01-20"},
    {"supplier": "SlowFreight", "po_id": "PO-2001", "expected_date": "2025-01-03", "actual_date": "2025-01-23"},
    {"supplier": "SlowFreight", "po_id": "PO-2002", "expected_date": "2025-01-10", "actual_date": "2025-01-28"},
    {"supplier": "SlowFreight", "po_id": "PO-2003", "expected_date": "2025-01-17", "actual_date": "2025-02-01"},
    {"supplier": "NewVendor", "po_id": "PO-3001", "expected_date": "2025-01-15", "actual_date": "2025-01-15"},
]


def _audit_store() -> AuditStore:
    path = os.environ.get("SUPPLYMIND_AUDIT_LOG_PATH", str(DEFAULT_AUDIT_LOG_PATH))
    return AuditStore(path)


def _evaluation_audit_store() -> SupplierEvaluationAuditStore:
    path = os.environ.get("SUPPLYMIND_SUPPLIER_EVALUATION_AUDIT_LOG_PATH", str(DEFAULT_EVALUATION_AUDIT_LOG_PATH))
    return SupplierEvaluationAuditStore(path)


def _summarize(scenario: str, run: SupplierEvaluationRun, audit_store: SupplierEvaluationAuditStore) -> dict:
    audit_records = audit_store.records_for_evaluation(run.evaluation_id)
    return {
        "scenario": scenario,
        "evaluation_id": run.evaluation_id,
        "outcome": run.outcome,
        "error": run.crash_error,
        "warnings": run.warnings,
        "unattributable_row_count": len(run.unattributable_rows),
        "scores": [
            {
                "supplier": s.supplier,
                "score": s.score,
                "severity": s.severity,
                "flagged_for_review": s.flagged_for_review,
                "flag_reasons": s.flag_reasons,
                "metrics": asdict(s.metrics),
                "explanation": s.explanation,
            }
            for s in run.scores
        ],
        "audit_trail": {
            "records_written": len(audit_records),
            "suppliers_audited": sorted(r.supplier for r in audit_records),
        },
    }


def _real_data_scenario(evaluator: SupplierEvaluator, audit_store: SupplierEvaluationAuditStore) -> dict:
    results = run_integration_with_audit([DELIVERY_RECORDS_DATASET], _audit_store())
    analysis_ready = available_for_analysis(results)
    delivery_rows = analysis_ready.get("delivery_records", [])

    run = evaluator.run(delivery_rows, evaluation_id="real_data")
    return _summarize("real_data", run, audit_store)


def _synthetic_suppliers_scenario(evaluator: SupplierEvaluator, audit_store: SupplierEvaluationAuditStore) -> dict:
    run = evaluator.run(SYNTHETIC_DELIVERY_ROWS, evaluation_id="synthetic_suppliers")
    return _summarize("synthetic_suppliers", run, audit_store)


def main() -> int:
    audit_store = _evaluation_audit_store()
    evaluator = SupplierEvaluator(audit_store)

    scenarios = [
        _real_data_scenario(evaluator, audit_store),
        _synthetic_suppliers_scenario(evaluator, audit_store),
    ]
    print(json.dumps(scenarios, indent=2, default=str))

    synthetic = scenarios[1]
    flagged = {s["supplier"] for s in synthetic["scores"] if s["flagged_for_review"]}
    unflagged = {s["supplier"] for s in synthetic["scores"] if not s["flagged_for_review"]}
    demo_succeeded = (
        synthetic["outcome"] == "success"
        and synthetic["audit_trail"]["records_written"] == len(synthetic["scores"])
        and flagged == {"SlowFreight", "NewVendor"}
        and unflagged == {"Acme"}
    )
    return 0 if demo_succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
