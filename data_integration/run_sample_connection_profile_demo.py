"""Runnable demo for the connection-profile / schema-mapping layer.

Out-of-plan work - see PROGRESS.md's Scope Log entry ("Proposed: Dataset
Onboarding & Schema Mapping"). Proves the actual point of
data_integration/connection_profile.py: two fictitious tenants whose
PostgreSQL tables use completely different column names both produce a
demand forecast and an intelligence-model run through the *exact same*,
unmodified agents/demand_forecasting_agent.py and intelligence/model.py -
no date_field/quantity_field overrides, no per-tenant code branch.

This environment has no live PostgreSQL configured (same state every
other run_sample_*.py script in this repo finds), so this script does not
call fetch_profile_data() (the real, audited, I/O-performing path - see
its own tests in data_integration/tests/test_connection_profile.py for
that path exercised against a mocked connection). Instead it uses
synthetic raw rows standing in for what postgres_connector.fetch_rows()
would return, so the part that's actually new here - validation and
remap_rows() turning two different schemas into one canonical shape - is
demonstrated for real, not mocked away.

Usage:
    python -m data_integration.run_sample_connection_profile_demo
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from agents.contracts import AgentQuery
from agents.demand_forecasting_agent import DemandForecastingAgent
from agents.orchestrator import Orchestrator
from data_integration.config import PostgresConfig
from data_integration.connection_profile import (
    ConnectionProfile,
    SchemaMappingError,
    remap_rows,
    validate_mapping_completeness,
)
from intelligence.audit_trail import StageAuditStore
from intelligence.model import IntelligenceModel

# Two fictitious tenants, same dataset kind, incompatible column names.
ACME_PROFILE = ConnectionProfile(
    tenant_id="acme_retail",
    dataset_kind="customer_orders",
    postgres=PostgresConfig(host="acme-db.example", port=5432, database="acme", user="u", password="p"),
    query="SELECT * FROM orders",
    column_mapping={"order_date": "OrderDate", "quantity": "Qty"},
)
ACME_RAW_ROWS = [{"OrderDate": f"2025-{m:02d}-01", "Qty": 100 + 8 * m} for m in range(1, 13)]

GLOBEX_PROFILE = ConnectionProfile(
    tenant_id="globex_supply",
    dataset_kind="customer_orders",
    postgres=PostgresConfig(host="globex-db.example", port=5432, database="globex", user="u", password="p"),
    query="SELECT * FROM sales_transactions",
    column_mapping={"order_date": "txn_dt", "quantity": "units_sold"},
)
GLOBEX_RAW_ROWS = [{"txn_dt": f"2025-{m:02d}-01", "units_sold": 300 - 15 * m} for m in range(1, 13)]

# A third, misconfigured tenant - proves the fail-loud-at-connect-time path.
BROKEN_PROFILE = ConnectionProfile(
    tenant_id="initech_widgets",
    dataset_kind="customer_orders",
    postgres=PostgresConfig(host="initech-db.example", port=5432, database="initech", user="u", password="p"),
    query="SELECT * FROM orders",
    column_mapping={"order_date": "OrderDate"},  # "quantity" never mapped
)


def _run_tenant(profile: ConnectionProfile, raw_rows: list[dict], stage_audit_path: Path) -> dict:
    validate_mapping_completeness(profile)  # fail loud before touching any downstream code
    canonical_rows = remap_rows(raw_rows, profile.column_mapping)

    orchestrator = Orchestrator([DemandForecastingAgent()])
    run = orchestrator.coordinate(
        AgentQuery(text="forecast customer demand", context={"demand_history": canonical_rows})
    )
    forecast_response = run.results[0].response

    intelligence_run = IntelligenceModel(StageAuditStore(stage_audit_path)).run(
        canonical_rows, run_id=f"demo-{profile.tenant_id}"
    )

    return {
        "tenant_id": profile.tenant_id,
        "raw_column_names": list(raw_rows[0].keys()),
        "forecasting_agent_recommendation": forecast_response.recommendation if forecast_response else None,
        "intelligence_pipeline_outcome": intelligence_run.outcome,
        "intelligence_recommendation": (
            intelligence_run.recommendation.action if intelligence_run.recommendation else None
        ),
    }


def main() -> int:
    stage_audit_path = Path(tempfile.mkdtemp()) / "demo_stage_audit_log.jsonl"
    results = [
        _run_tenant(ACME_PROFILE, ACME_RAW_ROWS, stage_audit_path),
        _run_tenant(GLOBEX_PROFILE, GLOBEX_RAW_ROWS, stage_audit_path),
    ]

    try:
        validate_mapping_completeness(BROKEN_PROFILE)
        broken_result = "unexpectedly passed validation"
    except SchemaMappingError as exc:
        broken_result = f"caught at connect-time, as expected: {exc}"

    print(json.dumps({"tenants": results, "misconfigured_tenant": broken_result}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
