"""Runnable entry point for STORY-009 (REQ-015 / Executive Control Tower).

Demonstrates the full pipeline this story assembles: stage 1 runs the
existing analysis agents (STORY-003's DemandForecastingAgent, STORY-004's
StockoutRiskAgent, STORY-005's RiskDetectionAgent, STORY-013's
SupplierEvaluationAgent, STORY-014's ShipmentDelayAnalysisAgent,
STORY-015's DataQualityMonitoringAgent) through the STORY-002 Orchestrator
against supply chain data - the exact same stage-1 fleet
recommendation/run_sample_recommendation_demo.py already coordinates;
stage 2 chains STORY-006's RecommendationAgent on their outputs; then
STORY-009's own DashboardEvaluator turns the combined stage-1 + stage-2
AgentResponses into a DashboardSnapshot, audits every tile, and renders
it to a self-contained HTML "control tower" page.

Two scenarios are printed and each renders its own HTML file:

1. "real_data" - real (or, with no credentials configured, empty) supply
   chain data pulled via STORY-011's audited run_integration_with_audit().
   With this repo's current environment (no PostgreSQL credentials), every
   stage-1 dataset pull fails, so every stage-1 agent reports status="error"
   except DataQualityMonitoringAgent (STORY-015), which reports "ok" even
   on zero rows - "no data at all" is itself the alert-worthy finding that
   agent exists to surface (see agents/data_quality_monitoring_agent.py's
   own docstring), the same behavior
   recommendation/run_sample_recommendation_demo.py's own "real_data"
   scenario documents. RecommendationAgent still succeeds off that one
   agent's output, so the dashboard ends up "degraded" (some tiles failed,
   others didn't) with a notification listing the failed data sources -
   this is what proves AC2 ("given data processing errors... notify the
   user of issues") end-to-end, not just in a unit test.
2. "synthetic_healthy" - hand-built rows (8 months of steady demand, 4
   on-time deliveries for one supplier, 2 well-stocked SKUs) feeding every
   stage-1 agent successfully, so the dashboard ends up "ok" with no
   notification - AC1's clean path ("given supply chain data... display
   key metrics on the dashboard").

Query contract (logged assumption, not escalated - same situation every
other demo script in this repo is in: no real schema exists yet to
confirm against): reuses exactly the same customer_orders/
delivery_records/inventory shapes
recommendation/run_sample_recommendation_demo.py's DATASETS already
standardized on.

Usage:
    SUPPLYMIND_PG_HOST=... SUPPLYMIND_PG_DATABASE=... SUPPLYMIND_PG_USER=... \\
    SUPPLYMIND_PG_PASSWORD=... \\
        python -m dashboard.run_sample_dashboard
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import dashboard
import data_integration
from agents.contracts import AgentQuery, AgentResponse
from agents.data_quality_monitoring_agent import DataQualityMonitoringAgent
from agents.demand_forecasting_agent import DemandForecastingAgent
from agents.orchestrator import CoordinationResult, Orchestrator
from agents.recommendation_agent import RecommendationAgent
from agents.risk_detection_agent import RiskDetectionAgent
from agents.shipment_delay_analysis_agent import ShipmentDelayAnalysisAgent
from agents.stockout_risk_agent import StockoutRiskAgent
from agents.supplier_evaluation_agent import SupplierEvaluationAgent
from dashboard.audit_trail import DashboardAuditStore
from dashboard.data_freshness import build_data_freshness
from dashboard.evaluator import DashboardBuildRun, DashboardEvaluator
from dashboard.render import render_dashboard_html
from data_integration.audit_trail import AuditStore
from data_integration.orchestrator import (
    DatasetResult,
    PostgresDataset,
    available_for_analysis,
    run_integration_with_audit,
)

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
DEFAULT_DASHBOARD_AUDIT_LOG_PATH = Path(dashboard.__file__).resolve().parent / "dashboard_audit_log.jsonl"
DEFAULT_HTML_DIR = Path(dashboard.__file__).resolve().parent

# 8 months of steady demand (>= forecasting/data_quality.py's
# MIN_RECOMMENDED_POINTS=6, so the forecast doesn't carry a low-confidence
# note) with mild month-to-month variation but no spike - two orders per
# month, quantities close enough together that no z-score anomaly fires.
SYNTHETIC_DEMAND_HISTORY = [
    {"order_date": f"2025-{month:02d}-05", "quantity": qty}
    for month, qty in enumerate(
        [210, 205, 215, 220, 208, 218, 212, 216], start=1
    )
] + [
    {"order_date": f"2025-{month:02d}-20", "quantity": qty}
    for month, qty in enumerate(
        [195, 200, 198, 202, 199, 197, 201, 203], start=1
    )
]

# One supplier, four on-time-or-early deliveries (>=
# supplier_evaluation/reliability.py's MIN_DELIVERIES_FOR_CONFIDENT_SCORE=3),
# no delays - a clean shipment_delay_analysis / supplier_evaluation read.
SYNTHETIC_DELIVERY_ROWS = [
    {"po_id": "PO-9001", "supplier": "Acme Logistics", "expected_date": "2025-08-05", "actual_date": "2025-08-05", "transportation_cost": 1200.0},
    {"po_id": "PO-9002", "supplier": "Acme Logistics", "expected_date": "2025-08-12", "actual_date": "2025-08-11", "transportation_cost": 1150.0},
    {"po_id": "PO-9003", "supplier": "Acme Logistics", "expected_date": "2025-08-19", "actual_date": "2025-08-19", "transportation_cost": 1180.0},
    {"po_id": "PO-9004", "supplier": "Acme Logistics", "expected_date": "2025-08-26", "actual_date": "2025-08-25", "transportation_cost": 1160.0},
]

# Two well-stocked SKUs - days_of_supply comfortably above
# inventory_risk/risk_model.py's MEDIUM_COVERAGE_RATIO (1.5x lead time),
# so both read "low" stockout risk.
SYNTHETIC_INVENTORY_ROWS = [
    {"sku": "SKU-100", "current_stock": 300.0, "safety_stock": 50.0, "daily_demand_rate": 10.0, "lead_time_days": 7.0},
    {"sku": "SKU-200", "current_stock": 450.0, "safety_stock": 80.0, "daily_demand_rate": 15.0, "lead_time_days": 7.0},
]


def _audit_store() -> AuditStore:
    path = os.environ.get("SUPPLYMIND_AUDIT_LOG_PATH", str(DEFAULT_AUDIT_LOG_PATH))
    return AuditStore(path)


def _dashboard_audit_store() -> DashboardAuditStore:
    path = os.environ.get("SUPPLYMIND_DASHBOARD_AUDIT_LOG_PATH", str(DEFAULT_DASHBOARD_AUDIT_LOG_PATH))
    return DashboardAuditStore(path)


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


def _build_dashboard(scenario: str, context: dict, dataset_results: list[DatasetResult]) -> tuple[DashboardBuildRun, Path | None]:
    stage1_results = _stage1_results(context)
    stage2_results = _stage2_results(stage1_results)
    freshness = build_data_freshness(dataset_results)

    evaluator = DashboardEvaluator(_dashboard_audit_store())
    run = evaluator.run(stage1_results + stage2_results, dashboard_id=scenario, data_freshness=freshness)

    html_path = None
    if run.snapshot is not None:
        html_path = DEFAULT_HTML_DIR / f"control_tower_{scenario}.html"
        html_path.write_text(render_dashboard_html(run.snapshot), encoding="utf-8")

    return run, html_path


def _summarize(scenario: str, run: DashboardBuildRun, html_path: Path | None) -> dict:
    snapshot = run.snapshot
    summary = {
        "scenario": scenario,
        "build_outcome": run.outcome,
        "crash_error": run.crash_error,
        "overall_status": None,
        "notification": None,
        "tiles": [],
        "data_sources": [],
        "total_critical_findings": None,
        "total_high_findings": None,
        "html_path": str(html_path) if html_path else None,
    }
    if snapshot is not None:
        summary.update(
            overall_status=snapshot.overall_status,
            notification=snapshot.notification,
            tiles=[{"metric_id": m.metric_id, "status": m.status, "severity": m.severity} for m in snapshot.metrics],
            data_sources=[{"dataset": e.dataset, "outcome": e.outcome} for e in snapshot.data_freshness],
            total_critical_findings=snapshot.total_critical_findings,
            total_high_findings=snapshot.total_high_findings,
        )
    return summary


def _real_data_scenario() -> dict:
    dataset_results = run_integration_with_audit(DATASETS, _audit_store())
    analysis_ready = available_for_analysis(dataset_results)
    context = {
        "demand_history": analysis_ready.get("customer_orders", []),
        "delivery_rows": analysis_ready.get("delivery_records", []),
        "inventory_rows": analysis_ready.get("inventory", []),
    }
    run, html_path = _build_dashboard("real_data", context, dataset_results)
    return _summarize("real_data", run, html_path)


def _synthetic_healthy_scenario() -> dict:
    context = {
        "demand_history": SYNTHETIC_DEMAND_HISTORY,
        "delivery_rows": SYNTHETIC_DELIVERY_ROWS,
        "inventory_rows": SYNTHETIC_INVENTORY_ROWS,
    }
    # No real data_integration pull for this scenario - synthesize a
    # matching DatasetResult list so it still gets an honest Data Sources
    # section, the same shape run_integration_with_audit() would have
    # returned had this data actually been pulled live.
    dataset_results = [
        DatasetResult(name="customer_orders", source_type="postgres", outcome="success", rows=SYNTHETIC_DEMAND_HISTORY),
        DatasetResult(name="delivery_records", source_type="postgres", outcome="success", rows=SYNTHETIC_DELIVERY_ROWS),
        DatasetResult(name="inventory", source_type="postgres", outcome="success", rows=SYNTHETIC_INVENTORY_ROWS),
    ]
    run, html_path = _build_dashboard("synthetic_healthy", context, dataset_results)
    return _summarize("synthetic_healthy", run, html_path)


def main() -> int:
    scenarios = [_real_data_scenario(), _synthetic_healthy_scenario()]
    print(json.dumps(scenarios, indent=2))

    real_data, synthetic = scenarios
    demo_succeeded = (
        real_data["build_outcome"] == "success"
        and real_data["overall_status"] == "degraded"
        and real_data["notification"] is not None
        and synthetic["build_outcome"] == "success"
        and synthetic["overall_status"] == "ok"
        and synthetic["notification"] is None
        and synthetic["total_critical_findings"] == 0
    )
    return 0 if demo_succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
