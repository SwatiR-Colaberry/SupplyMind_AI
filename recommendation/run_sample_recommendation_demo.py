"""Runnable entry point for STORY-006.

Demonstrates the two-stage pipeline this story adds: stage 1 runs the
existing analysis agents (STORY-003's DemandForecastingAgent, STORY-004's
StockoutRiskAgent, STORY-005's RiskDetectionAgent, STORY-013's
SupplierEvaluationAgent, STORY-014's ShipmentDelayAnalysisAgent,
STORY-015's DataQualityMonitoringAgent) through the STORY-002 Orchestrator
against supply chain data; stage 2 feeds their AgentResponse outputs into
STORY-006's RecommendationAgent through a second Orchestrator.coordinate()
call. This is what makes acceptance criterion 1 demoable end-to-end:
"given AI agent outputs, when the system processes them, then it should
generate actionable recommendations."
SupplierEvaluationAgent, ShipmentDelayAnalysisAgent, and
DataQualityMonitoringAgent were each added once their own story was
complete - see agents/supplier_evaluation_agent.py's,
agents/shipment_delay_analysis_agent.py's, and
agents/data_quality_monitoring_agent.py's own docstrings for why each
needed a dedicated Agent wrapper rather than piggybacking on
RiskDetectionAgent's existing "delivery_rows" handling (per-PO delay vs.
per-supplier aggregate reliability vs. per-PO delay-cost vs. a
whole-batch Data Quality Score are four distinct findings, not duplicate
signals).

Nothing in agents/orchestrator.py is modified to support this -
RecommendationAgent is just another Agent plugged into coordinate(), same
as every other agent in this repo. What's new here (not in STORY-005's
demo) is that stage 2's *input* is stage 1's *output* - real AgentResponse
objects, not raw rows - so this is the first script in this repo to chain
two Orchestrator.coordinate() calls back to back.

Uses run_integration_with_audit()/the same DATASETS
risk_detection/run_sample_risk_detection.py queries for its "real data"
scenario - same audit trail, same logged schema assumptions.

Two scenarios are printed:
1. "real_data" - real (or, with no credentials configured, empty) supply
   chain data through all stage-1 analysis agents, then through
   RecommendationAgent. With this repo's current environment (no
   PostgreSQL credentials), every stage-1 dataset pull fails, so every
   analysis agent returns status="error" - except
   DataQualityMonitoringAgent (STORY-015), which reports status="ok" even
   on zero rows ("no data at all" is itself the alert-worthy finding that
   agent exists to surface, not a failure of it - see its own docstring).
   RecommendationAgent's own "no successful agent output" failure path -
   acceptance criterion 1's failure path, chained through two stages
   instead of one - is exercised directly by
   agents/tests/test_recommendation_agent.py and
   recommendation/tests/test_synthesis.py instead, since this scenario no
   longer reaches it with a data-quality agent always present.
2. "engineered_conflict" - a deliberately constructed disagreement: the
   same SKU assessed from two different inventory snapshots (a current
   reading StockoutRiskAgent sees, a stale one RiskDetectionAgent sees),
   so their outputs genuinely disagree about its severity. This proves
   acceptance criterion 2 ("given conflicting agent outputs... it should
   highlight the conflicts") end-to-end, not just in a unit test - real
   single-snapshot production data can't exercise this path, since both
   agents call the exact same deterministic risk model on the same input
   and can only ever agree with each other (see PROGRESS.md's STORY-006
   step 3 entry).

Usage:
    SUPPLYMIND_PG_HOST=... SUPPLYMIND_PG_DATABASE=... SUPPLYMIND_PG_USER=... \\
    SUPPLYMIND_PG_PASSWORD=... \\
        python -m recommendation.run_sample_recommendation_demo
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import data_integration
from agents.contracts import AgentQuery, AgentResponse
from agents.data_quality_monitoring_agent import DataQualityMonitoringAgent
from agents.demand_forecasting_agent import DemandForecastingAgent
from agents.orchestrator import CoordinationRun, Orchestrator
from agents.recommendation_agent import RecommendationAgent
from agents.risk_detection_agent import RiskDetectionAgent
from agents.shipment_delay_analysis_agent import ShipmentDelayAnalysisAgent
from agents.stockout_risk_agent import StockoutRiskAgent
from agents.supplier_evaluation_agent import SupplierEvaluationAgent
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


def _stage2_recommendation(agent_outputs: list[AgentResponse]) -> tuple[CoordinationRun, AgentResponse | None]:
    stage2 = Orchestrator([RecommendationAgent()])
    run = stage2.coordinate(AgentQuery(text="generate recommendations", context={"agent_outputs": agent_outputs}))
    response = run.results[0].response if run.results else None
    return run, response


def _stage2_failure_reason(run: CoordinationRun) -> str | None:
    # A stage-2 CoordinationResult can fail (timeout, an unhandled
    # exception, a response that never validates) without the
    # Orchestrator itself crashing - run.crash_error stays None and
    # run.results stays non-empty in that case, so this has to look at
    # the individual result's own error, not just the two orchestrator-
    # level signals.
    if run.crash_error is not None:
        return run.crash_error
    failed = [r for r in run.results if r.outcome == "failure"]
    return failed[0].error if failed else None


def _summarize(
    scenario: str, agent_outputs: list[AgentResponse], run: CoordinationRun, response: AgentResponse | None
) -> dict:
    succeeded = response is not None and response.status == "ok"
    return {
        "scenario": scenario,
        "correlation_id": run.correlation_id,
        "coordination_outcome": run.outcome,
        "stage1_agents": [{"agent": o.agent_name, "status": o.status} for o in agent_outputs],
        "recommendation_succeeded": succeeded,
        "recommendation": response.recommendation if response else None,
        "confidence": response.confidence if response else None,
        "conflict_detected": bool(succeeded and "CONFLICTS DETECTED" in (response.recommendation or "")),
        "error": (response.error if response else None) or _stage2_failure_reason(run),
    }


def _real_data_scenario() -> dict:
    audit_store = _audit_store()
    results = run_integration_with_audit(DATASETS, audit_store)
    analysis_ready = available_for_analysis(results)

    context = {
        "demand_history": analysis_ready.get("customer_orders", []),
        "delivery_rows": analysis_ready.get("delivery_records", []),
        "inventory_rows": analysis_ready.get("inventory", []),
    }
    stage1 = Orchestrator(
        [
            DemandForecastingAgent(),
            StockoutRiskAgent(),
            RiskDetectionAgent(),
            SupplierEvaluationAgent(),
            ShipmentDelayAnalysisAgent(),
            DataQualityMonitoringAgent(),
        ]
    )
    stage1_run = stage1.coordinate(AgentQuery(text="analyze supply chain", context=context))
    agent_outputs = [r.response for r in stage1_run.results if r.response is not None]

    run, response = _stage2_recommendation(agent_outputs)
    return _summarize("real_data", agent_outputs, run, response)


def _conflict_scenario() -> dict:
    """Synthetic scenario: two agents assess the same SKU from two
    different snapshots of inventory data, so they genuinely disagree -
    see module docstring for why real single-snapshot production data
    can't exercise this path."""
    fresh_snapshot = [
        {
            "sku": "SKU-CONFLICT",
            "current_stock": 2.0,
            "safety_stock": 20.0,
            "daily_demand_rate": 5.0,
            "lead_time_days": 10.0,
        }
    ]
    # current_stock=100.0 with daily_demand_rate=5.0 => 20 days of supply,
    # >= 1.5x the 10-day lead time => "low" - the starkest possible
    # disagreement against the fresh snapshot's "critical" read.
    # RiskDetectionAgent builds its findings directly from each detector's
    # own output (agents/risk_detection_agent.py's _findings_from_signals),
    # not from risk_score.contributions, which deliberately drops any
    # zero-point stockout assessment (every "low"); see PROGRESS.md's
    # STORY-006 bug-fix entry for why that distinction matters here -
    # findings must include "low" so a genuinely critical-vs-low
    # disagreement is still visible to RecommendationAgent.
    stale_snapshot = [
        {
            "sku": "SKU-CONFLICT",
            "current_stock": 100.0,
            "safety_stock": 20.0,
            "daily_demand_rate": 5.0,
            "lead_time_days": 10.0,
        }
    ]

    stockout_run = Orchestrator([StockoutRiskAgent()]).coordinate(
        AgentQuery(text="assess current inventory", context={"inventory_rows": fresh_snapshot})
    )
    risk_run = Orchestrator([RiskDetectionAgent()]).coordinate(
        AgentQuery(text="assess stale inventory snapshot", context={"inventory_rows": stale_snapshot})
    )
    agent_outputs = [
        r.response for r in (stockout_run.results + risk_run.results) if r.response is not None
    ]

    run, response = _stage2_recommendation(agent_outputs)
    return _summarize("engineered_conflict", agent_outputs, run, response)


def main() -> int:
    scenarios = [_real_data_scenario(), _conflict_scenario()]
    print(json.dumps(scenarios, indent=2))

    conflict_scenario = scenarios[1]
    return 0 if conflict_scenario["recommendation_succeeded"] and conflict_scenario["conflict_detected"] else 1


if __name__ == "__main__":
    sys.exit(main())
