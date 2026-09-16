"""Regenerates the "Live Data" control-tower page (STORY-009's "real_data"
scenario) from whatever data_console currently has mapped, on demand.

Extracted out of dashboard/run_sample_dashboard.py so that module and
data_console/serve_data_console.py (which wants to trigger the same
regeneration from a "Run Analysis" button) don't have to import each other
directly - run_sample_dashboard.py already legitimately depends on
data_console's mapping modules (MappingStore, preview_mapping), so having
serve_data_console.py import run_sample_dashboard.py back would be exactly
the "A imports B imports A" cycle the Modular Composition Rule forbids.
This module is the "missing third module" that rule's own docstring names
for exactly this shape of problem: both run_sample_dashboard.py and
serve_data_console.py depend on this one, and this one depends only on
data_console's plain mapping modules - never on serve_data_console.py
itself - so no cycle exists. _nav_links() below reads its URLs from
local_apps/urls.py, a small dependency-free module every local server
(including this one) can import with no cycle risk - see that module's
own docstring.
"""

from __future__ import annotations

import os
from pathlib import Path

import dashboard
import data_integration
import local_apps.urls as app_urls
from local_apps import theme
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
from dashboard.approval_store import ApprovalStore
from dashboard.audit_trail import DashboardAuditStore
from dashboard.data_freshness import build_data_freshness
from dashboard.evaluator import DashboardBuildRun, DashboardEvaluator
from dashboard.metrics import DashboardSnapshot
from dashboard.render import render_dashboard_html
from data_console.file_store import UnknownUploadError
from data_console.mapping_service import preview_mapping
from data_console.mapping_store import MappingStore
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
    # SELECT * (not a fixed column list) - same reasoning as
    # customer_orders' own query above: the required 5 fields are always
    # read by key (inventory_risk/data_quality.py's REQUIRED_FIELDS), so
    # any of this repo's optional inventory columns (incoming_stock,
    # unit_price, demand_std_dev, supplier) already present on this table
    # flow straight through with no query change needed the next time one
    # is added - a fixed column list here previously meant incoming_stock/
    # unit_price/demand_std_dev/supplier were silently dropped on this
    # fallback path even when a real inventory table already had them.
    PostgresDataset(name="inventory", query="SELECT * FROM inventory LIMIT 500"),
]

DEFAULT_AUDIT_LOG_PATH = Path(data_integration.__file__).resolve().parent / "audit_log.jsonl"
DEFAULT_DASHBOARD_AUDIT_LOG_PATH = Path(dashboard.__file__).resolve().parent / "dashboard_audit_log.jsonl"
DEFAULT_APPROVAL_LOG_PATH = Path(dashboard.__file__).resolve().parent / "dashboard_approvals.jsonl"
DEFAULT_HTML_DIR = Path(dashboard.__file__).resolve().parent

# Plain-English label per scenario, and the fixed page order every rendered
# page's nav bar shows - so opening any one of the 3 files feels like
# navigating one application with three views. "real_data" is the only one
# an actual user's own connected data ever appears in.
_SCENARIO_LABELS = {
    "real_data": "Live Data",
    "partial_failure": "Demo: Partial Data",
    "synthetic_healthy": "Demo: Healthy Example",
}
_SCENARIO_ORDER = ["real_data", "partial_failure", "synthetic_healthy"]


def _nav_links(current_scenario: str) -> list[tuple[str, str | None, str, str]]:
    """The same fixed nav every other screen shows (see
    local_apps/theme.py's render_nav_bar) - a user flagged (2026-09-11)
    that this page's earlier 6-item nav (these 4 plus both demo-scenario
    links folded in) read as inconsistent with the rest of the app, which
    already uses the plain 4-item nav. "Live Data" is this nav's only
    entry that can ever be the current page; the 2 demo scenarios aren't
    linked from anywhere in this nav at all (a user later asked directly
    whether they were still needed once real data existed - the answer
    was no; they still exist, buildable via
    dashboard/run_sample_dashboard.py, as acceptance-criteria regression
    proof, just not linked from the running app). Grew to 5 items
    (2026-09-13) with the addition of learn/serve_learn.py, labeled
    "Glossary" (see local_apps/theme.py's own _NAV_DESTINATIONS for why).

    Each entry now also carries an icon SVG and accent name (2026-09-13,
    matching theme.py's own _NAV_DESTINATIONS entry-for-entry) so this
    page's nav bar (rendered by this module's own _render_nav(), not
    theme.render_nav_bar() - this one supports an arbitrary href-or-None
    list for demo-scenario linking, which the fixed 5-destination
    render_nav_bar() doesn't) shows the same per-tab icons every other
    screen's nav bar does, instead of falling behind as a plain-text
    exception once icons were added elsewhere."""
    return [
        ("Data Console", app_urls.DATA_CONSOLE_URL, theme.ICON_DATABASE, "brand"),
        ("Live Data", None if current_scenario == "real_data" else app_urls.LIVE_DASHBOARD_URL, theme.ICON_PULSE, "warning"),
        ("AI Assistant", app_urls.CHAT_UI_URL, theme.ICON_CHAT, "accent-2"),
        ("What-If Simulator", app_urls.SCENARIO_SIMULATOR_URL, theme.ICON_SIMULATOR, "success"),
        ("Glossary", app_urls.LEARN_URL, theme.ICON_GLOSSARY, "accent-2"),
    ]


def _audit_store() -> AuditStore:
    path = os.environ.get("SUPPLYMIND_AUDIT_LOG_PATH", str(DEFAULT_AUDIT_LOG_PATH))
    return AuditStore(path)


def _dashboard_audit_store() -> DashboardAuditStore:
    path = os.environ.get("SUPPLYMIND_DASHBOARD_AUDIT_LOG_PATH", str(DEFAULT_DASHBOARD_AUDIT_LOG_PATH))
    return DashboardAuditStore(path)


def approval_store() -> ApprovalStore:
    """Public (not prefixed) - data_console/serve_data_console.py's own
    /api/approve route needs the same store this module reads from when
    rendering, so both processes/entry points read and write the exact
    same JSONL file rather than drifting."""
    path = os.environ.get("SUPPLYMIND_APPROVAL_LOG_PATH", str(DEFAULT_APPROVAL_LOG_PATH))
    return ApprovalStore(path)


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
    """RecommendationAgent (STORY-006) synthesizes every stage-1 output; DeliveryIntelligenceAgent
    runs first, over those same stage-1 outputs, so its own cross-agent narrative (if any) is itself
    one more output RecommendationAgent's synthesis sees - the same "one more agent joins the
    aggregate recommendation surface, no special-casing needed" shape every other agent already has.
    """
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


def _build_dashboard(scenario: str, context: dict, dataset_results: list[DatasetResult]) -> tuple[DashboardBuildRun, Path | None]:
    stage1_results = _stage1_results(context)
    stage2_results = _stage2_results(stage1_results)
    freshness = build_data_freshness(dataset_results)

    evaluator = DashboardEvaluator(_dashboard_audit_store())
    run = evaluator.run(stage1_results + stage2_results, dashboard_id=scenario, data_freshness=freshness)

    html_path = None
    if run.snapshot is not None:
        approvals = approval_store()
        html_path = DEFAULT_HTML_DIR / f"control_tower_{scenario}.html"
        html = render_dashboard_html(
            run.snapshot,
            nav_links=_nav_links(scenario),
            subtitle=_SCENARIO_LABELS[scenario],
            latest_approvals=approvals.latest_by_metric(scenario),
            approval_history=approvals.records_for_dashboard(scenario),
        )
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


# The most recent successful "real_data" snapshot built by
# refresh_real_data_dashboard(), in-process only (not persisted - a fresh
# process has none until "Run Analysis" is clicked at least once, the same
# as control_tower_real_data.html itself needing at least one build before
# it exists on disk). Exists so a consumer that wants "whatever Run
# Analysis last produced" - see chat_interface/serve_chat_ui.py's
# current_snapshot() - can share this exact result instead of running its
# own separately-stale pipeline. A failed run (run.snapshot is None) never
# overwrites a prior good one - see refresh_real_data_dashboard() below.
_latest_real_data_snapshot: DashboardSnapshot | None = None


def latest_real_data_snapshot() -> DashboardSnapshot | None:
    """The DashboardSnapshot from the most recent successful refresh_real_data_dashboard() call, or None if "Run Analysis" has never succeeded yet in this process."""
    return _latest_real_data_snapshot


def refresh_real_data_dashboard() -> dict:
    """Re-runs the full stage1+stage2 agent pipeline against whatever
    data_console currently has mapped (falling back to this module's own
    hardcoded Postgres DATASETS for any dataset data_console hasn't mapped
    at all) and re-renders control_tower_real_data.html in place.

    Idempotent and safe to call repeatedly (e.g. from a "Run Analysis"
    button clicked more than once): each call re-derives the dashboard from
    current source data and overwrites the same file - there is no
    incremental/appended state to duplicate. Never raises for a data
    problem (a bad mapping, an unreachable database, missing credentials)
    - those come back as a "failure" entry in the returned summary's
    data_sources / an "error"-status tile, the same as every other path
    through this pipeline; only a genuine bug would raise, and callers
    (the CLI's main(), serve_data_console.py's /api/run-analysis handler)
    are expected to let that propagate rather than mask it.
    """
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
    if run.snapshot is not None:
        global _latest_real_data_snapshot
        _latest_real_data_snapshot = run.snapshot
    return _summarize("real_data", run, html_path)
