"""Runnable entry point for STORY-007.

Four scenarios, following the same convention
recommendation/run_sample_recommendation_demo.py and
supplier_evaluation/run_sample_supplier_evaluation.py established for a
story whose acceptance criteria can't be fully exercised by this
environment's real data alone:

1. "real_data" - pulls customer_orders/delivery_records via the STORY-001
   data_integration orchestrator (same audited pull, same DATASETS
   recommendation/run_sample_recommendation_demo.py uses) and runs
   RootCauseAnalysisAgent against a representative issue. With no
   PostgreSQL credentials configured, this exercises AC2's failure path
   with real integration code, not synthetic data; with the local test
   Postgres from scripts/local_test_db.py running, it pulls real seeded
   rows instead - either way, real integration code, not synthetic data.
2. "live_data" - pulls live national truck-crossing counts from
   data.bts.gov's public Border Crossing Entry Data API (Socrata, no
   auth) via BtsBorderCrossingDataset, and runs the agent against the
   most recent period actually present in that live pull. Genuinely
   queries the internet at run time - not a static snapshot - so its
   result (whether a cause correlates or not) is not asserted in
   demo_succeeded below; a network-unavailable environment degrades to
   the same clean "insufficient data" outcome scenario 1 already proves,
   which is itself correct behavior, not a demo failure.
3. "synthetic_demand_spike" - a deterministic demand history with an
   obvious spike in the issue's own period, run through the agent as a
   SKU stockout issue. Proves AC1 ("given a supply chain issue... it
   should provide a root cause analysis") and AC3 (an audit record with
   a timestamp and confidence level, confirmed present after the run) in
   one pass, independent of environment.
4. "synthetic_insufficient_data" - the same issue, but with no
   demand/delivery data supplied at all. Proves AC2 ("given insufficient
   data... it should notify the user of limitations") deterministically,
   rather than relying on scenario 1's environment-dependent absence of
   credentials to exercise it - see PROGRESS.md's STORY-006 notes on why
   depending on real infrastructure state to prove an acceptance
   criterion is worth backing up with a synthetic case as well.

Usage:
    SUPPLYMIND_PG_HOST=... SUPPLYMIND_PG_DATABASE=... SUPPLYMIND_PG_USER=... \\
    SUPPLYMIND_PG_PASSWORD=... \\
        python -m root_cause.run_sample_root_cause_analysis
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import data_integration
import root_cause
from agents.contracts import AgentQuery, AgentResponse
from agents.root_cause_agent import RootCauseAnalysisAgent
from data_integration.audit_trail import AuditStore
from data_integration.orchestrator import (
    BtsBorderCrossingDataset,
    PostgresDataset,
    available_for_analysis,
    run_integration_with_audit,
)
from root_cause.audit_trail import RootCauseAuditStore

DATASETS = [
    PostgresDataset(name="customer_orders", query="SELECT * FROM customer_orders LIMIT 500"),
    PostgresDataset(
        name="delivery_records",
        query="SELECT po_id, supplier, expected_date, actual_date FROM delivery_records LIMIT 500",
    ),
]

DEFAULT_INTEGRATION_AUDIT_LOG_PATH = Path(data_integration.__file__).resolve().parent / "audit_log.jsonl"
DEFAULT_ANALYSIS_AUDIT_LOG_PATH = Path(root_cause.__file__).resolve().parent / "analysis_audit_log.jsonl"

SYNTHETIC_DEMAND_HISTORY = [
    {"order_date": "2025-01-15", "quantity": 100},
    {"order_date": "2025-02-15", "quantity": 105},
    {"order_date": "2025-03-15", "quantity": 95},
    {"order_date": "2025-04-15", "quantity": 500},  # obvious spike
]

ISSUE_CONTEXT = {"subject": "SKU-1042", "subject_kind": "sku", "as_of_period": "2025-04"}


def _integration_audit_store() -> AuditStore:
    path = os.environ.get("SUPPLYMIND_AUDIT_LOG_PATH", str(DEFAULT_INTEGRATION_AUDIT_LOG_PATH))
    return AuditStore(path)


def _analysis_audit_store() -> RootCauseAuditStore:
    path = os.environ.get("SUPPLYMIND_ROOT_CAUSE_AUDIT_LOG_PATH", str(DEFAULT_ANALYSIS_AUDIT_LOG_PATH))
    return RootCauseAuditStore(path)


def _run_agent(
    agent: RootCauseAnalysisAgent, context: dict, analysis_id: str
) -> AgentResponse:
    query_context = {**ISSUE_CONTEXT, **context, "analysis_id": analysis_id}
    return agent.run(AgentQuery(text="what caused this issue?", context=query_context))


def _summarize(
    scenario: str, analysis_id: str, response: AgentResponse, audit_store: RootCauseAuditStore, **extra
) -> dict:
    audit_records = audit_store.records_for_analysis(analysis_id)
    return {
        "scenario": scenario,
        "analysis_id": analysis_id,
        "issue": {"subject": ISSUE_CONTEXT["subject"], "subject_kind": ISSUE_CONTEXT["subject_kind"]},
        "status": response.status,
        "recommendation": response.recommendation,
        "confidence": response.confidence,
        "error": response.error,
        "audit_trail": [
            {
                "outcome": r.outcome,
                "timestamp": r.timestamp,
                "confidence": r.confidence,
                "candidate_count": r.candidate_count,
            }
            for r in audit_records
        ],
        **extra,
    }


def _real_data_scenario(agent: RootCauseAnalysisAgent, audit_store: RootCauseAuditStore) -> dict:
    results = run_integration_with_audit(DATASETS, _integration_audit_store())
    analysis_ready = available_for_analysis(results)

    response = _run_agent(
        agent,
        {
            "demand_history": analysis_ready.get("customer_orders", []),
            "delivery_rows": analysis_ready.get("delivery_records", []),
        },
        analysis_id="real_data",
    )
    return _summarize("real_data", "real_data", response, audit_store)


def _live_data_scenario(agent: RootCauseAnalysisAgent, audit_store: RootCauseAuditStore) -> dict:
    live_dataset = BtsBorderCrossingDataset(name="bts_border_crossing")
    results = run_integration_with_audit([live_dataset], _integration_audit_store())
    analysis_ready = available_for_analysis(results)
    demand_history = analysis_ready.get("bts_border_crossing", [])

    # Correlate against whatever period the live pull actually reports as
    # most recent, rather than a fixed date - a hardcoded period would
    # eventually fall outside the API's own lookback window and silently
    # stop correlating against anything.
    as_of_period = demand_history[-1]["order_date"][:7] if demand_history else None

    response = _run_agent(
        agent, {"demand_history": demand_history, "as_of_period": as_of_period}, analysis_id="live_data"
    )
    return _summarize(
        "live_data",
        "live_data",
        response,
        audit_store,
        live_data_points_fetched=len(demand_history),
        as_of_period=as_of_period,
    )


def _synthetic_demand_spike_scenario(agent: RootCauseAnalysisAgent, audit_store: RootCauseAuditStore) -> dict:
    response = _run_agent(
        agent, {"demand_history": SYNTHETIC_DEMAND_HISTORY}, analysis_id="synthetic_demand_spike"
    )
    return _summarize("synthetic_demand_spike", "synthetic_demand_spike", response, audit_store)


def _synthetic_insufficient_data_scenario(agent: RootCauseAnalysisAgent, audit_store: RootCauseAuditStore) -> dict:
    response = _run_agent(agent, {}, analysis_id="synthetic_insufficient_data")
    return _summarize("synthetic_insufficient_data", "synthetic_insufficient_data", response, audit_store)


def main() -> int:
    audit_store = _analysis_audit_store()
    agent = RootCauseAnalysisAgent(audit_store)

    scenarios = [
        _real_data_scenario(agent, audit_store),
        _live_data_scenario(agent, audit_store),
        _synthetic_demand_spike_scenario(agent, audit_store),
        _synthetic_insufficient_data_scenario(agent, audit_store),
    ]
    print(json.dumps(scenarios, indent=2, default=str))

    spike = scenarios[2]
    insufficient = scenarios[3]
    demo_succeeded = (
        spike["status"] == "ok"
        and spike["confidence"] == 0.85
        and len(spike["audit_trail"]) == 1
        and spike["audit_trail"][0]["outcome"] == "success"
        and spike["audit_trail"][0]["confidence"] == 0.85
        and insufficient["status"] == "error"
        and "no anomaly or reliability data" in (insufficient["error"] or "")
        and len(insufficient["audit_trail"]) == 1
        and insufficient["audit_trail"][0]["outcome"] == "failure"
    )
    return 0 if demo_succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
