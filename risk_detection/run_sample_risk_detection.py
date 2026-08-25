"""Runnable entry point for STORY-005.

Pulls customer_orders, delivery_records, and inventory via the STORY-001
data_integration orchestrator, feeds the results into the STORY-005
RiskDetectionAgent through the STORY-002 Orchestrator, and prints the
unified Supply Chain Risk Score. This is what makes acceptance criterion
1 demoable end-to-end: "given supply chain data, when the system
analyzes it, then it should detect anomalies and risks."

Nothing in data_integration/ or agents/orchestrator.py is modified to
support this - the risk detection agent is just another Agent plugged
into the existing coordinate() call, same as STORY-003/004's demos.

Uses run_integration_with_audit(), the same STORY-011 trust-spine wrapper
the STORY-001/003/004 demo scripts use, not the bare run_integration() -
every dataset pull in this repo gets an audit record. Shares the same
SUPPLYMIND_AUDIT_LOG_PATH-configurable default audit log file those
scripts use, so a customer_orders/inventory pull already recorded by one
script is recognized as already-processed by the others.

Query contract (logged assumption, not escalated - same situation
STORY-001/003/004 were in: no real schema exists yet to confirm
against): delivery_records has one row per purchase order with po_id,
expected_date, actual_date - see
risk_detection/anomaly_detection.py's REQUIRED_DELIVERY_FIELDS.
customer_orders and inventory reuse the same assumed shapes as the
STORY-003/004 demos.

Usage:
    SUPPLYMIND_PG_HOST=... SUPPLYMIND_PG_DATABASE=... SUPPLYMIND_PG_USER=... \\
    SUPPLYMIND_PG_PASSWORD=... \\
        python -m risk_detection.run_sample_risk_detection

With no real PostgreSQL credentials configured (the current state of
this environment), every dataset fails to fetch and this script prints
the resulting "no supply chain data provided for risk detection" error
response - the same "log the error and notify the user" behavior the
acceptance criteria require, just triggered by fully missing datasets
rather than partially incomplete ones.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import data_integration
from agents.contracts import AgentQuery
from agents.orchestrator import Orchestrator
from agents.risk_detection_agent import RiskDetectionAgent
from data_integration.audit_trail import AuditStore
from data_integration.orchestrator import PostgresDataset, available_for_analysis, run_integration_with_audit

DATASETS = [
    PostgresDataset(name="customer_orders", query="SELECT * FROM customer_orders LIMIT 500"),
    PostgresDataset(
        name="delivery_records",
        query="SELECT po_id, supplier, expected_date, actual_date FROM delivery_records LIMIT 500",
    ),
    PostgresDataset(
        name="inventory",
        query="SELECT sku, current_stock, safety_stock, daily_demand_rate, lead_time_days FROM inventory LIMIT 500",
    ),
]

DEFAULT_AUDIT_LOG_PATH = Path(data_integration.__file__).resolve().parent / "audit_log.jsonl"


def _audit_store() -> AuditStore:
    path = os.environ.get("SUPPLYMIND_AUDIT_LOG_PATH", str(DEFAULT_AUDIT_LOG_PATH))
    return AuditStore(path)


def main() -> int:
    audit_store = _audit_store()
    results = run_integration_with_audit(DATASETS, audit_store)
    analysis_ready = available_for_analysis(results)

    context = {
        "demand_history": analysis_ready.get("customer_orders", []),
        "delivery_rows": analysis_ready.get("delivery_records", []),
        "inventory_rows": analysis_ready.get("inventory", []),
    }

    orchestrator = Orchestrator([RiskDetectionAgent()])
    run = orchestrator.coordinate(AgentQuery(text="detect supply chain risk", context=context))

    if not run.results:
        # run.results is only empty when coordinate() hit its own crash
        # path (a bug in coordination logic itself, not a per-agent
        # failure - see agents/orchestrator.py's CoordinationRun). Indexing
        # run.results[0] unconditionally would turn that into an opaque
        # IndexError instead of surfacing the actual, already-captured
        # crash reason.
        summary = {
            "correlation_id": run.correlation_id,
            "coordination_outcome": run.outcome,
            "datasets_available": list(analysis_ready.keys()),
            "detection_succeeded": False,
            "recommendation": None,
            "error": run.crash_error or "orchestrator returned no results",
        }
        print(json.dumps(summary, indent=2))
        return 1

    risk_result = run.results[0]
    agent_response = risk_result.response
    # risk_result.outcome only reflects whether the agent returned a
    # *structurally valid* AgentResponse (the STORY-002 contract) - an
    # "error" status (e.g. no supply chain data) is still a valid
    # response, not a coordination failure. Whether risk detection
    # actually ran is agent_response.status == "ok".
    detection_succeeded = (
        risk_result.outcome == "success" and agent_response is not None and agent_response.status == "ok"
    )

    summary = {
        "correlation_id": run.correlation_id,
        "coordination_outcome": run.outcome,
        "datasets_available": list(analysis_ready.keys()),
        "detection_succeeded": detection_succeeded,
        "recommendation": agent_response.recommendation if agent_response else None,
        "error": (agent_response.error if agent_response else None) or risk_result.error,
    }
    print(json.dumps(summary, indent=2))

    return 0 if detection_succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
