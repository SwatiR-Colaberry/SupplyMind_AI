"""Runnable entry point for STORY-004.

Pulls an `inventory` dataset via the STORY-001 data_integration
orchestrator, feeds the result into the STORY-004 StockoutRiskAgent
through the STORY-002 Orchestrator, and prints the risk assessment.
This is what makes acceptance criterion 1 demoable end-to-end: "given
inventory data, when the system analyzes it, then it should predict
stockout risks."

Nothing in data_integration/ or agents/orchestrator.py is modified to
support this - the risk agent is just another Agent plugged into the
existing coordinate() call, same as STORY-003's DemandForecastingAgent.

Uses run_integration_with_audit(), the same STORY-011 trust-spine
wrapper the STORY-001 and STORY-003 demo scripts use, not the bare
run_integration() - every dataset pull in this repo gets an audit
record. Shares the same SUPPLYMIND_AUDIT_LOG_PATH-configurable default
audit log file those scripts use, so an inventory pull already recorded
by one script is recognized as already-processed by the others.

Query contract (logged assumption, not escalated - same situation
STORY-001/003 were in: no real inventory schema exists yet to confirm
the true column names against): the `inventory` table has one row per
SKU with columns sku, current_stock, safety_stock, daily_demand_rate,
lead_time_days - see inventory_risk/data_quality.py's REQUIRED_FIELDS.
daily_demand_rate is assumed to be a turnover-derived figure the source
system already maintains (REQ-006's "turnover"). Combining it with a
live STORY-003 forecast_demand() rate per SKU instead is a reasonable
future enhancement - the StockoutRiskAgent's row contract already
supports it (daily_demand_rate is source-agnostic) - but is out of
scope for this story's minimal demo.

Usage:
    SUPPLYMIND_PG_HOST=... SUPPLYMIND_PG_DATABASE=... SUPPLYMIND_PG_USER=... \\
    SUPPLYMIND_PG_PASSWORD=... \\
        python -m inventory_risk.run_sample_risk_assessment

With no real PostgreSQL credentials configured (the current state of
this environment), the inventory dataset fails to fetch and this script
prints the resulting "no inventory data provided" error response - the
same "flag the data for review" behavior the acceptance criteria
require, just triggered by a fully missing dataset rather than a
partially corrupted one.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import data_integration
from agents.contracts import AgentQuery
from agents.orchestrator import Orchestrator
from agents.stockout_risk_agent import StockoutRiskAgent
from data_integration.audit_trail import AuditStore
from data_integration.orchestrator import PostgresDataset, available_for_analysis, run_integration_with_audit

INVENTORY_DATASET = PostgresDataset(
    name="inventory",
    query="SELECT sku, current_stock, safety_stock, daily_demand_rate, lead_time_days FROM inventory LIMIT 500",
)

DEFAULT_AUDIT_LOG_PATH = Path(data_integration.__file__).resolve().parent / "audit_log.jsonl"


def _audit_store() -> AuditStore:
    path = os.environ.get("SUPPLYMIND_AUDIT_LOG_PATH", str(DEFAULT_AUDIT_LOG_PATH))
    return AuditStore(path)


def main() -> int:
    audit_store = _audit_store()
    results = run_integration_with_audit([INVENTORY_DATASET], audit_store)
    analysis_ready = available_for_analysis(results)
    inventory_rows = analysis_ready.get("inventory", [])

    orchestrator = Orchestrator([StockoutRiskAgent()])
    run = orchestrator.coordinate(
        AgentQuery(text="predict stockout risk", context={"inventory_rows": inventory_rows})
    )

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
            "rows_available": len(inventory_rows),
            "prediction_succeeded": False,
            "recommendation": None,
            "error": run.crash_error or "orchestrator returned no results",
        }
        print(json.dumps(summary, indent=2))
        return 1

    risk_result = run.results[0]
    agent_response = risk_result.response
    # risk_result.outcome only reflects whether the agent returned a
    # *structurally valid* AgentResponse (the STORY-002 contract) - an
    # "error" status (e.g. no inventory data) is still a valid response,
    # not a coordination failure. Whether a prediction was actually
    # produced is agent_response.status == "ok".
    prediction_succeeded = (
        risk_result.outcome == "success" and agent_response is not None and agent_response.status == "ok"
    )

    summary = {
        "correlation_id": run.correlation_id,
        "coordination_outcome": run.outcome,
        "rows_available": len(inventory_rows),
        "prediction_succeeded": prediction_succeeded,
        "recommendation": agent_response.recommendation if agent_response else None,
        "error": (agent_response.error if agent_response else None) or risk_result.error,
    }
    print(json.dumps(summary, indent=2))

    return 0 if prediction_succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
