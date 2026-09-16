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

Three scenarios are printed and each renders its own HTML file:

1. "real_data" - see dashboard/live_refresh.py's refresh_real_data_dashboard()
   docstring, which this script's main() now just calls: for each of the 3
   datasets, prefers whatever data_console (the local mapping tool at
   data_console/serve_data_console.py) has saved a mapping for - a live
   Postgres table/query, an uploaded CSV, or a Google Sheet - and falls
   back to this module's own hardcoded DATASETS pull via STORY-011's
   audited run_integration_with_audit() only for a dataset data_console
   has no mapping for at all. With nothing mapped and no credentials
   configured, every stage-1 dataset pull fails and the dashboard comes
   out "degraded" (see the "partial_failure" scenario below for why AC2 no
   longer depends on this happening to be true). Against a real, populated
   source, every stage-1 agent succeeds and the dashboard comes out "ok"
   with real findings (critical stockout risk, a demand spike, a late
   delivery - whatever the source actually contains). All outcomes are
   legitimate and this scenario reports honestly whichever one the
   environment produces - its own success bar is just "the pipeline ran
   end-to-end and produced tiles," never a specific overall_status, since
   asserting a specific status here would be asserting a fact about the
   environment this script does not control. The same regeneration also
   runs on demand from data_console's own "Run Analysis" button
   (data_console/serve_data_console.py's /api/run-analysis route) - both
   entry points call the identical dashboard.live_refresh function so
   neither can drift from the other.
2. "partial_failure" - a synthetic, environment-independent scenario:
   inventory data only, no demand history or delivery records. This
   deterministically leaves DemandForecastingAgent/
   SupplierEvaluationAgent/ShipmentDelayAnalysisAgent without the data
   they each require (status="error") while StockoutRiskAgent/
   RiskDetectionAgent/DataQualityMonitoringAgent still succeed off the
   inventory data alone, so the dashboard comes out "degraded" with a
   notification listing the 3 failed sources - this is what proves AC2
   ("given data processing errors... notify the user of issues")
   end-to-end, reliably, regardless of whether this environment happens
   to have PostgreSQL configured. Original design mistake worth naming
   directly: this scenario used to just be "real_data" with the
   implicit assumption "this environment never has real credentials" -
   the first time this was run against a real, populated database (this
   session, at the user's prompt), "real_data" instead succeeded
   cleanly and demo's own exit-code gate failed despite the dashboard
   working exactly right, because the gate had baked in that wrong
   assumption. This scenario removes the coupling between "prove AC2"
   and "hope this environment stays credential-less."
3. "synthetic_healthy" - hand-built rows (8 months of steady demand, 4
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
import sys

from dashboard.live_refresh import (  # noqa: F401 - DATASETS re-exported for chat_interface/run_sample_chat_interface.py
    DATASETS,
    _build_dashboard,
    _summarize,
    refresh_real_data_dashboard,
)
from data_integration.orchestrator import DatasetResult

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
# inventory_risk/risk_model.py's MEDIUM_DAYS_THRESHOLD (30 days), so both
# read "low" stockout risk.
SYNTHETIC_INVENTORY_ROWS = [
    {"sku": "SKU-100", "current_stock": 400.0, "safety_stock": 50.0, "daily_demand_rate": 10.0, "lead_time_days": 7.0},
    {"sku": "SKU-200", "current_stock": 600.0, "safety_stock": 80.0, "daily_demand_rate": 15.0, "lead_time_days": 7.0},
]


def _partial_failure_scenario() -> dict:
    # Inventory only - no demand_history, no delivery_rows. Deterministic
    # regardless of whether this environment has PostgreSQL configured;
    # see module docstring for why AC2's proof lives here rather than in
    # "real_data".
    context = {
        "demand_history": [],
        "delivery_rows": [],
        "inventory_rows": SYNTHETIC_INVENTORY_ROWS,
    }
    dataset_results = [
        DatasetResult(name="customer_orders", source_type="postgres", outcome="success", rows=[]),
        DatasetResult(name="delivery_records", source_type="postgres", outcome="success", rows=[]),
        DatasetResult(name="inventory", source_type="postgres", outcome="success", rows=SYNTHETIC_INVENTORY_ROWS),
    ]
    run, html_path = _build_dashboard("partial_failure", context, dataset_results)
    return _summarize("partial_failure", run, html_path)


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
    scenarios = [refresh_real_data_dashboard(), _partial_failure_scenario(), _synthetic_healthy_scenario()]
    print(json.dumps(scenarios, indent=2))

    real_data, partial_failure, synthetic = scenarios
    demo_succeeded = (
        # "real_data" reports honestly whichever way this environment's
        # actual PostgreSQL state goes - its bar is "the pipeline ran and
        # produced tiles," never a specific overall_status. See module
        # docstring.
        real_data["build_outcome"] == "success"
        and len(real_data["tiles"]) > 0
        and partial_failure["build_outcome"] == "success"
        and partial_failure["overall_status"] == "degraded"
        and partial_failure["notification"] is not None
        and synthetic["build_outcome"] == "success"
        and synthetic["overall_status"] == "ok"
        and synthetic["notification"] is None
        and synthetic["total_critical_findings"] == 0
    )
    return 0 if demo_succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
