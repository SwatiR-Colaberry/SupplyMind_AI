"""Runnable entry point for STORY-010 (REQ-016 / AI Chat Interface).

Demonstrates the chat interface end-to-end: a STORY-009
dashboard.metrics.build_dashboard() snapshot (the same stage-1 analysis
fleet + stage-2 RecommendationAgent every other demo in this repo already
coordinates) stands in for "the data the agent fleet has already
computed", and ChatEvaluator answers a handful of natural-language queries
against it - no LLM, no new agent-coordination path; see
chat_interface/chat.py's own docstring for why.

Reuses dashboard/run_sample_dashboard.py's own hand-tuned synthetic rows
(SYNTHETIC_DEMAND_HISTORY/SYNTHETIC_DELIVERY_ROWS/SYNTHETIC_INVENTORY_ROWS)
rather than re-deriving new ones - those constants are already calibrated
against forecasting/data_quality.py's MIN_RECOMMENDED_POINTS and
inventory_risk/risk_model.py's coverage-ratio thresholds, so a second,
slightly different set of "healthy" rows here would just be one more thing
to keep in sync by hand. Stage-1/stage-2 Orchestrator wiring itself is
duplicated inline rather than imported, matching the convention every
other demo entry point in this repo already follows (see
dashboard/run_sample_dashboard.py's and
recommendation/run_sample_recommendation_demo.py's own near-identical
stage-1 agent lists) - each demo owns its own wiring.

Two DashboardSnapshots are built:

1. "healthy" - the full synthetic dataset (demand history, on-time
   deliveries, well-stocked inventory); every stage-1 agent succeeds.
   Proves AC1 ("given a user query... it should provide a relevant
   response via chat") against a real, successfully-processed tile.
2. "partial" - inventory data only, same shape as
   dashboard/run_sample_dashboard.py's own "partial_failure" scenario:
   DemandForecastingAgent/SupplierEvaluationAgent/
   ShipmentDelayAnalysisAgent all error for lack of data. Proves the
   "data processing errors" failure path: a query whose topic IS
   recognized still gets an honest "can't answer that right now" instead
   of a crash or a fabricated answer.

Four queries are run across these two snapshots, covering AC1's happy
path plus both documented failure paths:
  - "what's our stockout risk?" against "healthy"  -> answered
  - "what's the weather today?" against "healthy"  -> unsupported (AC2)
  - "what's the demand forecast?" against "partial" -> data_unavailable
  - "how reliable are our suppliers?" against "partial" -> data_unavailable

Every interaction runs through ChatEvaluator, so each one also leaves an
audited record (Trust AC) - verified directly against the audit store
after all four queries run.

Usage:
    python -m chat_interface.run_sample_chat_interface
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import chat_interface
from agents.contracts import AgentQuery, AgentResponse
from agents.data_quality_monitoring_agent import DataQualityMonitoringAgent
from agents.demand_forecasting_agent import DemandForecastingAgent
from agents.orchestrator import CoordinationResult, Orchestrator
from agents.recommendation_agent import RecommendationAgent
from agents.risk_detection_agent import RiskDetectionAgent
from agents.shipment_delay_analysis_agent import ShipmentDelayAnalysisAgent
from agents.stockout_risk_agent import StockoutRiskAgent
from agents.supplier_evaluation_agent import SupplierEvaluationAgent
from chat_interface.audit_trail import ChatAuditStore
from chat_interface.evaluator import ChatEvaluator, ChatInteractionRun
from dashboard.metrics import DashboardSnapshot, build_dashboard
from dashboard.run_sample_dashboard import (
    SYNTHETIC_DELIVERY_ROWS,
    SYNTHETIC_DEMAND_HISTORY,
    SYNTHETIC_INVENTORY_ROWS,
)

DEFAULT_AUDIT_LOG_PATH = Path(chat_interface.__file__).resolve().parent / "chat_audit_log.jsonl"


def _audit_store() -> ChatAuditStore:
    path = os.environ.get("SUPPLYMIND_CHAT_AUDIT_LOG_PATH", str(DEFAULT_AUDIT_LOG_PATH))
    return ChatAuditStore(path)


def _stage1_results(context: dict) -> list[CoordinationResult]:
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
    return stage1.coordinate(AgentQuery(text="analyze supply chain", context=context)).results


def _stage2_results(stage1_results: list[CoordinationResult]) -> list[CoordinationResult]:
    agent_outputs: list[AgentResponse] = [r.response for r in stage1_results if r.response is not None]
    stage2 = Orchestrator([RecommendationAgent()])
    run = stage2.coordinate(AgentQuery(text="generate recommendations", context={"agent_outputs": agent_outputs}))
    return run.results


def _build_snapshot(dashboard_id: str, context: dict) -> DashboardSnapshot:
    stage1_results = _stage1_results(context)
    stage2_results = _stage2_results(stage1_results)
    return build_dashboard(stage1_results + stage2_results, dashboard_id=dashboard_id)


def _healthy_snapshot() -> DashboardSnapshot:
    context = {
        "demand_history": SYNTHETIC_DEMAND_HISTORY,
        "delivery_rows": SYNTHETIC_DELIVERY_ROWS,
        "inventory_rows": SYNTHETIC_INVENTORY_ROWS,
    }
    return _build_snapshot("chat-demo-healthy", context)


def _partial_snapshot() -> DashboardSnapshot:
    # Inventory only - same shape dashboard/run_sample_dashboard.py's own
    # "partial_failure" scenario uses to deterministically leave
    # DemandForecastingAgent/SupplierEvaluationAgent/
    # ShipmentDelayAnalysisAgent without the data they each require.
    context = {
        "demand_history": [],
        "delivery_rows": [],
        "inventory_rows": SYNTHETIC_INVENTORY_ROWS,
    }
    return _build_snapshot("chat-demo-partial", context)


def _ask(evaluator: ChatEvaluator, query_text: str, snapshot: DashboardSnapshot, interaction_id: str) -> dict:
    run: ChatInteractionRun = evaluator.run(query_text, snapshot, interaction_id=interaction_id)
    return {
        "interaction_id": run.interaction_id,
        "query": query_text,
        "outcome": run.outcome,
        "status": run.response.status if run.response else None,
        "answer": run.response.answer if run.response else run.crash_error,
        "topic": run.response.topic if run.response else None,
    }


def main() -> int:
    audit_store = _audit_store()
    evaluator = ChatEvaluator(audit_store)

    healthy = _healthy_snapshot()
    partial = _partial_snapshot()

    interactions = [
        _ask(evaluator, "what's our stockout risk?", healthy, "chat-demo-1"),
        _ask(evaluator, "what's the weather today?", healthy, "chat-demo-2"),
        _ask(evaluator, "what's the demand forecast?", partial, "chat-demo-3"),
        _ask(evaluator, "how reliable are our suppliers?", partial, "chat-demo-4"),
    ]
    print(json.dumps(interactions, indent=2))

    all_audited = all(audit_store.has_recorded(i["interaction_id"]) for i in interactions)
    demo_succeeded = (
        interactions[0]["outcome"] == "success"
        and interactions[0]["status"] == "answered"
        and interactions[1]["outcome"] == "success"
        and interactions[1]["status"] == "unsupported"
        and interactions[2]["outcome"] == "success"
        and interactions[2]["status"] == "data_unavailable"
        and interactions[3]["outcome"] == "success"
        and interactions[3]["status"] == "data_unavailable"
        and all_audited
    )
    return 0 if demo_succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
