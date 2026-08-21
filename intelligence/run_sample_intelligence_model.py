"""Runnable entry point for STORY-012.

Pulls customer_orders via the STORY-001 data_integration orchestrator
(the same audited pull forecasting/run_sample_forecast.py uses) and feeds
the result through the STORY-012 IntelligenceModel, printing the full
Observe -> Understand -> Predict -> Recommend trace. This is what makes
the acceptance criteria demoable end-to-end: "given raw data inputs, when
processed through the model, then the system provides observations" and
"given observations, when analyzed, then the system provides
understanding insights."

Shares forecasting/run_sample_forecast.py's default audit log file (same
SUPPLYMIND_AUDIT_LOG_PATH env var) for the data-pull audit trail, so a
customer_orders pull already recorded by one script is recognized as
already-processed by the other, not double-logged. The intelligence
model's own per-stage audit trail is a separate file
(intelligence/stage_audit_log.jsonl by default, override via
SUPPLYMIND_STAGE_AUDIT_LOG_PATH) - it audits a different unit of work
(pipeline stages, not dataset pulls) and re-running this script with the
same row data still gets a fresh run_id, so distinct stage-audit records
are expected on every run, unlike the dataset-pull audit trail's
content-fingerprint dedup.

Usage:
    SUPPLYMIND_PG_HOST=... SUPPLYMIND_PG_DATABASE=... SUPPLYMIND_PG_USER=... \\
    SUPPLYMIND_PG_PASSWORD=... \\
        python -m intelligence.run_sample_intelligence_model

With no real PostgreSQL credentials configured (the current state of this
environment), customer_orders fails to fetch and the pipeline halts at
the Observe stage with "no raw data provided to observe" - the "data not
processed through all stages" failure path, triggered by a fully missing
dataset rather than a partially incomplete one, and still fully recorded
in the stage audit trail (every stage from observe onward shows up,
either "failure" or "not_processed" - never silently absent).
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

import data_integration
import intelligence
from data_integration.audit_trail import AuditStore
from data_integration.orchestrator import PostgresDataset, available_for_analysis, run_integration_with_audit
from intelligence.audit_trail import StageAuditStore
from intelligence.model import IntelligenceModel

CUSTOMER_ORDERS_DATASET = PostgresDataset(
    name="customer_orders", query="SELECT * FROM customer_orders LIMIT 500"
)

DEFAULT_AUDIT_LOG_PATH = Path(data_integration.__file__).resolve().parent / "audit_log.jsonl"
DEFAULT_STAGE_AUDIT_LOG_PATH = Path(intelligence.__file__).resolve().parent / "stage_audit_log.jsonl"


def _audit_store() -> AuditStore:
    path = os.environ.get("SUPPLYMIND_AUDIT_LOG_PATH", str(DEFAULT_AUDIT_LOG_PATH))
    return AuditStore(path)


def _stage_audit_store() -> StageAuditStore:
    path = os.environ.get("SUPPLYMIND_STAGE_AUDIT_LOG_PATH", str(DEFAULT_STAGE_AUDIT_LOG_PATH))
    return StageAuditStore(path)


def main() -> int:
    audit_store = _audit_store()
    results = run_integration_with_audit([CUSTOMER_ORDERS_DATASET], audit_store)
    analysis_ready = available_for_analysis(results)
    raw_rows = analysis_ready.get("customer_orders", [])

    model = IntelligenceModel(_stage_audit_store())
    run = model.run(raw_rows)

    summary = {
        "run_id": run.run_id,
        "pipeline_outcome": run.outcome,
        "rows_available": len(raw_rows),
        "stages": [
            {"stage": r.stage, "outcome": r.outcome, "error": r.error}
            for r in run.results
        ],
        "observation": _stage_payload(run.observation),
        "understanding": _stage_payload(run.understanding),
        "prediction": _stage_payload(run.prediction),
        "recommendation": _stage_payload(run.recommendation),
    }
    print(json.dumps(summary, indent=2, default=str))

    return 0 if run.outcome == "success" else 1


def _stage_payload(output: object | None) -> dict | None:
    return asdict(output) if output is not None else None


if __name__ == "__main__":
    sys.exit(main())
