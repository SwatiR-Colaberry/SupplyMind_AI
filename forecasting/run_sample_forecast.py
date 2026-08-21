"""Runnable entry point for STORY-003.

Pulls customer_orders via the STORY-001 data_integration orchestrator,
feeds the result into the STORY-003 DemandForecastingAgent through the
STORY-002 Orchestrator, and prints the forecast. This is what makes the
acceptance criteria demoable end-to-end: "given historical demand data,
when the system processes it, then it should provide demand forecasts."

Nothing in data_integration/ or agents/orchestrator.py is modified to
support this - the forecasting agent is just another Agent plugged into
the existing coordinate() call.

Uses run_integration_with_audit(), the same STORY-011 trust-spine wrapper
data_integration/run_sample_integration.py uses, not the bare
run_integration() - every dataset pull in this repo gets an audit
record, and this script is no exception. It shares that script's
default audit log file (same SUPPLYMIND_AUDIT_LOG_PATH env var), so a
customer_orders pull already recorded by one script is recognized as
already-processed by the other, not double-logged.

Usage:
    SUPPLYMIND_PG_HOST=... SUPPLYMIND_PG_DATABASE=... SUPPLYMIND_PG_USER=... \\
    SUPPLYMIND_PG_PASSWORD=... \\
        python -m forecasting.run_sample_forecast

With no real PostgreSQL credentials configured (the current state of
this environment), customer_orders fails to fetch and this script
prints the resulting "no historical demand data provided" error
response - the same "notify the user of potential inaccuracies"
behavior the acceptance criteria require, just triggered by a fully
missing dataset rather than a partially incomplete one.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import data_integration
from agents.contracts import AgentQuery
from agents.demand_forecasting_agent import DemandForecastingAgent
from agents.orchestrator import Orchestrator
from data_integration.audit_trail import AuditStore
from data_integration.orchestrator import PostgresDataset, available_for_analysis, run_integration_with_audit

CUSTOMER_ORDERS_DATASET = PostgresDataset(
    name="customer_orders", query="SELECT * FROM customer_orders LIMIT 500"
)

DEFAULT_AUDIT_LOG_PATH = Path(data_integration.__file__).resolve().parent / "audit_log.jsonl"


def _audit_store() -> AuditStore:
    path = os.environ.get("SUPPLYMIND_AUDIT_LOG_PATH", str(DEFAULT_AUDIT_LOG_PATH))
    return AuditStore(path)


def main() -> int:
    audit_store = _audit_store()
    results = run_integration_with_audit([CUSTOMER_ORDERS_DATASET], audit_store)
    analysis_ready = available_for_analysis(results)
    demand_history = analysis_ready.get("customer_orders", [])

    orchestrator = Orchestrator([DemandForecastingAgent()])
    run = orchestrator.coordinate(
        AgentQuery(text="forecast customer demand", context={"demand_history": demand_history})
    )

    forecast_result = run.results[0]
    agent_response = forecast_result.response
    # forecast_result.outcome only reflects whether the agent returned a
    # *structurally valid* AgentResponse (the STORY-002 contract) - an
    # "error" status (e.g. no historical data) is still a valid response,
    # not a coordination failure. Whether a forecast was actually produced
    # is agent_response.status == "ok".
    forecasting_succeeded = (
        forecast_result.outcome == "success" and agent_response is not None and agent_response.status == "ok"
    )

    summary = {
        "correlation_id": run.correlation_id,
        "coordination_outcome": run.outcome,
        "rows_available": len(demand_history),
        "forecasting_succeeded": forecasting_succeeded,
        "recommendation": agent_response.recommendation if agent_response else None,
        "error": (agent_response.error if agent_response else None) or forecast_result.error,
    }
    print(json.dumps(summary, indent=2))

    return 0 if forecasting_succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
