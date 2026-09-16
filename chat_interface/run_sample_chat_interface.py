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

Three DashboardSnapshots are built:

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
3. "real" - whatever this environment's real Postgres actually returns via
   the same DATASETS dashboard/run_sample_dashboard.py already pulls. With
   no credentials configured, every stage-1 pull fails, same as that
   module's own "real_data" scenario. Against scripts/local_test_db.py's
   seeded local Postgres (one demand-spike month, one critically low-stock
   SKU, one badly late delivery - see that script's own docstring), this
   is what proves the chat interface gives real, specific answers off a
   live, non-synthetic dataset, not just hand-tuned example rows. Like
   dashboard/run_sample_dashboard.py's own "real_data" scenario, this one
   is purely informational - it does not gate the exit code, since
   asserting a specific answer here would be asserting a fact about the
   environment this script does not control.

Four queries are run across the two synthetic snapshots, covering AC1's
happy path plus both documented failure paths, and gate the exit code:
  - "what's our stockout risk?" against "healthy"  -> answered
  - "what's the weather today?" against "healthy"  -> unsupported (AC2)
  - "what's the demand forecast?" against "partial" -> data_unavailable
  - "how reliable are our suppliers?" against "partial" -> data_unavailable

Three more queries run against "real" whenever real data is available,
printed but not gated:
  - "what's our stockout risk?" (expect SKU-GIZMO's critical shortage)
  - "any anomalies in our supply chain?" (expect the 2025-07 demand spike)
  - "which shipments are delayed?" (expect Beta Logistics' 15-day-late PO)

Every interaction runs through ChatEvaluator, so each one also leaves an
audited record (Trust AC) - verified directly against the audit store
after all queries run.

Usage:
    python -m chat_interface.run_sample_chat_interface
    # against real data:
    eval "$(python3 scripts/local_test_db.py)"
    python3 -m chat_interface.run_sample_chat_interface
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import chat_interface
import data_integration
from agents.contracts import AgentQuery, AgentResponse
from agents.data_quality_monitoring_agent import DataQualityMonitoringAgent
from agents.delivery_intelligence_agent import DeliveryIntelligenceAgent
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
    DATASETS,
    SYNTHETIC_DELIVERY_ROWS,
    SYNTHETIC_DEMAND_HISTORY,
    SYNTHETIC_INVENTORY_ROWS,
)
from data_integration.audit_trail import AuditStore
from data_integration.orchestrator import available_for_analysis, run_integration_with_audit

DEFAULT_AUDIT_LOG_PATH = Path(chat_interface.__file__).resolve().parent / "chat_audit_log.jsonl"
DEFAULT_DATA_INTEGRATION_AUDIT_LOG_PATH = Path(data_integration.__file__).resolve().parent / "audit_log.jsonl"


def chat_audit_store() -> ChatAuditStore:
    path = os.environ.get("SUPPLYMIND_CHAT_AUDIT_LOG_PATH", str(DEFAULT_AUDIT_LOG_PATH))
    return ChatAuditStore(path)


def _data_integration_audit_store() -> AuditStore:
    path = os.environ.get("SUPPLYMIND_AUDIT_LOG_PATH", str(DEFAULT_DATA_INTEGRATION_AUDIT_LOG_PATH))
    return AuditStore(path)


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
    # Mirrors dashboard/live_refresh.py's own _stage2_results() exactly -
    # DeliveryIntelligenceAgent runs first, over stage-1's own outputs, so
    # its cross-agent narrative (if any) is itself one more output
    # RecommendationAgent's synthesis sees.
    stage1_outputs: list[AgentResponse] = [r.response for r in stage1_results if r.response is not None]

    delivery_intelligence = Orchestrator([DeliveryIntelligenceAgent()])
    delivery_intelligence_run = delivery_intelligence.coordinate(
        AgentQuery(text="correlate delivery intelligence", context={"agent_outputs": stage1_outputs})
    )

    recommendation_inputs = stage1_outputs + [
        r.response for r in delivery_intelligence_run.results if r.response is not None
    ]
    recommendation = Orchestrator([RecommendationAgent()])
    recommendation_run = recommendation.coordinate(
        AgentQuery(text="generate recommendations", context={"agent_outputs": recommendation_inputs})
    )

    return delivery_intelligence_run.results + recommendation_run.results


def _build_snapshot(dashboard_id: str, context: dict) -> DashboardSnapshot:
    stage1_results = _stage1_results(context)
    stage2_results = _stage2_results(stage1_results)
    return build_dashboard(stage1_results + stage2_results, dashboard_id=dashboard_id)


def build_healthy_snapshot() -> DashboardSnapshot:
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


def build_real_data_snapshot() -> DashboardSnapshot:
    dataset_results = run_integration_with_audit(DATASETS, _data_integration_audit_store())
    analysis_ready = available_for_analysis(dataset_results)
    context = {
        "demand_history": analysis_ready.get("customer_orders", []),
        "delivery_rows": analysis_ready.get("delivery_records", []),
        "inventory_rows": analysis_ready.get("inventory", []),
    }
    return _build_snapshot("chat-demo-real", context)


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


# (query, snapshot-getter, interaction_id, expected status) - the exit
# code is gated on these four only. Driving both the _ask() calls and the
# gate off one list means adding a fifth case here automatically joins
# the gate too, rather than needing a second, easy-to-forget manual edit
# to a hand-indexed assertion (that omission is exactly what let an
# earlier revision of this function silently fold the "real_data"
# scenario's interactions into the gate - see PROGRESS.md's STORY-010
# pre-commit-review entry).
_GATED_CASES = [
    ("what's our stockout risk?", "healthy", "chat-demo-1", "answered"),
    ("what's the weather today?", "healthy", "chat-demo-2", "unsupported"),
    ("what's the demand forecast?", "partial", "chat-demo-3", "data_unavailable"),
    ("how reliable are our suppliers?", "partial", "chat-demo-4", "data_unavailable"),
]


def main() -> int:
    store = chat_audit_store()
    evaluator = ChatEvaluator(store)

    snapshots = {"healthy": build_healthy_snapshot(), "partial": _partial_snapshot()}
    interactions = [
        _ask(evaluator, query, snapshots[snapshot_key], interaction_id)
        for query, snapshot_key, interaction_id, _ in _GATED_CASES
    ]

    # Informational only - see module docstring for why this scenario
    # never gates the exit code, including via the audit check below.
    real = build_real_data_snapshot()
    real_interactions = [
        _ask(evaluator, "what's our stockout risk?", real, "chat-demo-real-1"),
        _ask(evaluator, "any anomalies in our supply chain?", real, "chat-demo-real-2"),
        _ask(evaluator, "which shipments are delayed?", real, "chat-demo-real-3"),
    ]

    print(json.dumps({"synthetic": interactions, "real_data": real_interactions}, indent=2))

    all_gated_audited = all(store.has_recorded(i["interaction_id"]) for i in interactions)
    demo_succeeded = all_gated_audited and all(
        actual["outcome"] == "success" and actual["status"] == expected_status
        for actual, (_, _, _, expected_status) in zip(interactions, _GATED_CASES)
    )
    return 0 if demo_succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
