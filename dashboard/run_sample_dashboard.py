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

1. "real_data" - for each of the 3 datasets, prefers whatever data_console
   (the local mapping tool at data_console/serve_data_console.py) has
   saved a mapping for - a live Postgres table/query, an uploaded CSV, or
   a Google Sheet, however that tenant actually connected it - and falls
   back to this module's own hardcoded DATASETS pull via STORY-011's
   audited run_integration_with_audit() only for a dataset data_console
   has no mapping for at all. See _dataset_result_from_data_console()
   below. With nothing mapped and no credentials configured, every
   stage-1 dataset pull fails and the dashboard comes out "degraded" (see
   the "partial_failure" scenario below for why AC2 no longer depends on
   this happening to be true). Against a real, populated source, every
   stage-1 agent succeeds and the dashboard comes out "ok" with real
   findings (critical stockout risk, a demand spike, a late delivery -
   whatever the source actually contains). All outcomes are legitimate
   and this scenario reports honestly whichever one the environment
   produces - its own success bar is just "the pipeline ran end-to-end
   and produced tiles," never a specific overall_status, since asserting
   a specific status here would be asserting a fact about the
   environment this script does not control.
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
from data_console.file_store import UnknownUploadError
from data_console.mapping_service import preview_mapping
from data_console.mapping_store import MappingStore
from data_console.serve_data_console import DEFAULT_PORT as _DATA_CONSOLE_DEFAULT_PORT
from data_console.sheet_fetcher import InvalidSheetUrlError, SheetFetchError
from data_integration.audit_trail import AuditStore
from data_integration.config import MissingConfigError
from data_integration.connection_profile import SchemaMappingError
from data_integration.orchestrator import (
    DatasetResult,
    PostgresDataset,
    available_for_analysis,
    run_integration_with_audit,
)
from data_integration.postgres_connector import PostgresIntegrationError

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


# Plain-English label per scenario, and the fixed page order every
# rendered page's nav bar shows - so opening any one of the 3 files feels
# like navigating one application with three views, not three unrelated
# files that happen to live in the same folder. "real_data" is the only
# one an actual user's own connected data ever appears in; the other two
# are this story's own fixed acceptance-criteria demonstrations, labeled
# "Demo:" so that distinction is never ambiguous from the tab bar alone.
_SCENARIO_LABELS = {
    "real_data": "Live Data",
    "partial_failure": "Demo: Partial Data",
    "synthetic_healthy": "Demo: Healthy Example",
}
_SCENARIO_ORDER = ["real_data", "partial_failure", "synthetic_healthy"]

# data_console/serve_data_console.py can serve these same 3 files itself
# (at /dashboard/control_tower_<scenario>.html, reading the same files this
# script writes) so that a single running server is "the app" and this link
# and that route are two ends of the same connection - but a statically
# generated page can't know at write-time whether it is being opened
# straight from disk or through that server, so this always links to the
# server's own root. Respects SUPPLYMIND_DATA_CONSOLE_PORT the same way
# data_console.serve_data_console.main() does, so the link still resolves
# if that server wasn't started on its default port.
_DATA_CONSOLE_URL = f"http://127.0.0.1:{os.environ.get('SUPPLYMIND_DATA_CONSOLE_PORT', _DATA_CONSOLE_DEFAULT_PORT)}/"


def _nav_links(current_scenario: str) -> list[tuple[str, str | None]]:
    return [("Data Console", _DATA_CONSOLE_URL)] + [
        (_SCENARIO_LABELS[s], None if s == current_scenario else f"control_tower_{s}.html")
        for s in _SCENARIO_ORDER
    ]


def _build_dashboard(scenario: str, context: dict, dataset_results: list[DatasetResult]) -> tuple[DashboardBuildRun, Path | None]:
    stage1_results = _stage1_results(context)
    stage2_results = _stage2_results(stage1_results)
    freshness = build_data_freshness(dataset_results)

    evaluator = DashboardEvaluator(_dashboard_audit_store())
    run = evaluator.run(stage1_results + stage2_results, dashboard_id=scenario, data_freshness=freshness)

    html_path = None
    if run.snapshot is not None:
        html_path = DEFAULT_HTML_DIR / f"control_tower_{scenario}.html"
        html = render_dashboard_html(run.snapshot, nav_links=_nav_links(scenario), subtitle=_SCENARIO_LABELS[scenario])
        html_path.write_text(html, encoding="utf-8")

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


# data_console/mapping_store.DatasetMapping.source_kind -> the DatasetResult
# source_type label this repo already uses for the same kind of pull
# elsewhere (file_mapping_service.py's own audit records use "csv_upload";
# a table/query mapping is a live Postgres pull either way).
_SOURCE_TYPE_BY_MAPPING_KIND = {"table": "postgresql", "query": "postgresql", "file": "csv_upload", "sheet": "google_sheets"}


def _dataset_result_from_data_console(dataset_kind: str) -> DatasetResult | None:
    """Sources one dataset from data_console's own saved mapping - a live
    table/query, an uploaded CSV, or a Google Sheet, however that tenant
    actually connected it - via the same preview_mapping() the console's own
    "Preview" button calls (capped at PREVIEW_ROW_LIMIT rows, the same cap
    this module's own hardcoded DATASETS queries already use).

    Returns None when data_console has no mapping at all for this dataset,
    so the caller falls back to the hardcoded DATASETS pull below - a
    dataset never onboarded through data_console keeps working exactly as
    before. A mapping explicitly marked "unavailable" is reported as a
    failure rather than None: falling back to the hardcoded query would
    contradict what the tenant already told data_console.
    """
    mapping = MappingStore().get(dataset_kind)
    if mapping is None:
        return None
    source_type = _SOURCE_TYPE_BY_MAPPING_KIND.get(mapping.source_kind, "postgresql")
    if mapping.status == "unavailable":
        return DatasetResult(name=dataset_kind, source_type=source_type, outcome="failure", error="marked unavailable in data_console")
    try:
        result = preview_mapping(dataset_kind)
    except (
        SchemaMappingError,
        MissingConfigError,
        PostgresIntegrationError,
        UnknownUploadError,
        InvalidSheetUrlError,
        SheetFetchError,
    ) as exc:
        return DatasetResult(name=dataset_kind, source_type=source_type, outcome="failure", error=str(exc))
    return DatasetResult(name=dataset_kind, source_type=source_type, outcome="success", rows=result.rows)


def _real_data_scenario() -> dict:
    mapped_results = {d.name: _dataset_result_from_data_console(d.name) for d in DATASETS}
    unmapped_datasets = [d for d in DATASETS if mapped_results[d.name] is None]
    legacy_results = {r.name: r for r in run_integration_with_audit(unmapped_datasets, _audit_store())}
    dataset_results = [
        mapped_results[d.name] if mapped_results[d.name] is not None else legacy_results[d.name] for d in DATASETS
    ]

    analysis_ready = available_for_analysis(dataset_results)
    context = {
        "demand_history": analysis_ready.get("customer_orders", []),
        "delivery_rows": analysis_ready.get("delivery_records", []),
        "inventory_rows": analysis_ready.get("inventory", []),
    }
    run, html_path = _build_dashboard("real_data", context, dataset_results)
    return _summarize("real_data", run, html_path)


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
    scenarios = [_real_data_scenario(), _partial_failure_scenario(), _synthetic_healthy_scenario()]
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
