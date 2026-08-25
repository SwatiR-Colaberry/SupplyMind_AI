"""Per-tenant connection profile and schema-mapping registry.

Out-of-plan work - not part of any story currently in .colaberry/plan.json.
See PROGRESS.md's Scope Log entry ("Proposed: Dataset Onboarding & Schema
Mapping") for the problem this extends: connecting a new tenant's data
source today requires hand-writing the query and hardcoding field names
per tenant, as every run_sample_*.py script in this repo does. This module
lets a tenant's dataset be described declaratively instead: credentials +
a table/query + a mapping from canonical field name (the names
forecasting/, inventory_risk/, and intelligence/ already expect) to that
tenant's actual column name.

Validation is layered on purpose:
    1. validate_mapping_completeness() - pure, no I/O. Every field a
       dataset kind requires must have a mapped column *before* any
       connection is attempted.
    2. validate_against_live_schema() - confirms the mapped columns
       actually exist in the real table/query, not just that the profile
       claims they do.
Both are "fail loud at connect-time, not at forecast-time" - a bad mapping
must never reach forecasting/demand_model.py as a KeyError three stages
downstream.

Once a profile is validated, remap_rows() translates a tenant's raw rows
(their column names) into canonical field names - the step that lets
forecasting/, inventory_risk/, and intelligence/ consume any tenant's data
completely unchanged.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from data_integration import postgres_connector
from data_integration.audit_trail import AuditStore
from data_integration.config import PostgresConfig
from inventory_risk.data_quality import REQUIRED_FIELDS as INVENTORY_REQUIRED_FIELDS
from risk_detection.anomaly_detection import REQUIRED_DELIVERY_FIELDS

DatasetKind = Literal["customer_orders", "inventory", "delivery_records"]

# Matches agents/demand_forecasting_agent.py's DEFAULT_DATE_FIELD /
# DEFAULT_QUANTITY_FIELD - the canonical names every downstream stage
# already expects a customer_orders row to carry.
CUSTOMER_ORDERS_REQUIRED_FIELDS: tuple[str, ...] = ("order_date", "quantity")

REQUIRED_FIELDS_BY_DATASET_KIND: dict[DatasetKind, tuple[str, ...]] = {
    "customer_orders": CUSTOMER_ORDERS_REQUIRED_FIELDS,
    "inventory": INVENTORY_REQUIRED_FIELDS,
    # delivery_records added for STORY-005 (risk_detection/anomaly_detection.py's
    # detect_supplier_delays()) - reuses that module's own field tuple
    # directly, the same "one canonical source, no duplicated list" pattern
    # "inventory" already follows against inventory_risk/data_quality.py.
    "delivery_records": REQUIRED_DELIVERY_FIELDS,
}


@dataclass(frozen=True)
class ConnectionProfile:
    """One tenant's connection and schema mapping for one dataset kind.

    column_mapping maps a canonical field name (e.g. "order_date") to the
    actual column name in this tenant's table (e.g. "OrderDate",
    "order_dt"). A tenant whose schema already uses the canonical names
    still needs an identity mapping ({"order_date": "order_date", ...}) -
    explicit, never inferred, so a missing mapping is always a loud
    validation error rather than a silent guess.
    """

    tenant_id: str
    dataset_kind: DatasetKind
    postgres: PostgresConfig
    query: str
    column_mapping: dict[str, str] = field(default_factory=dict)


class SchemaMappingError(ValueError):
    """Raised when a ConnectionProfile's column_mapping cannot support its dataset_kind."""


def validate_mapping_completeness(profile: ConnectionProfile) -> None:
    """Raise SchemaMappingError if any field required by profile.dataset_kind is unmapped.

    Pure - no database access. The cheapest possible check, run before any
    connection attempt, so a profile missing a required mapping never gets
    as far as a live query.
    """
    required = REQUIRED_FIELDS_BY_DATASET_KIND[profile.dataset_kind]
    missing = [f for f in required if not profile.column_mapping.get(f)]
    if missing:
        raise SchemaMappingError(
            f"tenant '{profile.tenant_id}' ({profile.dataset_kind}) is missing a column mapping "
            f"for required field(s): {', '.join(missing)}"
        )


def validate_against_live_schema(profile: ConnectionProfile) -> None:
    """Raise SchemaMappingError if a mapped column doesn't exist in the live query result.

    Calls validate_mapping_completeness() first (defense in depth for a
    caller that skips straight to this function) so a field missing from
    column_mapping entirely is still reported as the specific completeness
    error, not an opaque KeyError. Probes the real table/query via
    postgres_connector.fetch_columns() - zero rows fetched, just column
    names - so a bad mapping is caught before any real data is pulled.
    """
    validate_mapping_completeness(profile)
    required = REQUIRED_FIELDS_BY_DATASET_KIND[profile.dataset_kind]
    live_columns = set(postgres_connector.fetch_columns(profile.query, config=profile.postgres))
    missing = [
        f"{canonical_field} -> '{profile.column_mapping[canonical_field]}'"
        for canonical_field in required
        if profile.column_mapping[canonical_field] not in live_columns
    ]
    if missing:
        raise SchemaMappingError(
            f"tenant '{profile.tenant_id}' ({profile.dataset_kind}): mapped column(s) not found "
            f"in the live query result: {', '.join(missing)}"
        )


def validate_profile(profile: ConnectionProfile) -> None:
    """Run both validation layers, in order: completeness, then live schema.

    The single entry point a connect flow should call - cheapest check
    first, so an incomplete mapping never costs a database round-trip.
    """
    validate_mapping_completeness(profile)
    validate_against_live_schema(profile)


def remap_rows(rows: list[dict[str, Any]], column_mapping: dict[str, str]) -> list[dict[str, Any]]:
    """Translate raw rows (a tenant's own column names) into canonical field names.

    Pure - no I/O. Each output row has exactly one key per canonical field
    in column_mapping, whose value comes from that row's actual column. A
    row missing one of the mapped columns gets that canonical key set to
    None rather than the row being dropped or the key omitted -
    forecasting/aggregation.py and inventory_risk/data_quality.py already
    treat a None/missing field as a data-quality issue to flag, so this
    function doesn't need to duplicate that judgment; it only translates
    column names, nothing else. Columns present in a row but not named in
    column_mapping are dropped - only fields the mapping declares are
    meaningful to downstream code.
    """
    return [{canonical: row.get(actual) for canonical, actual in column_mapping.items()} for row in rows]


def _content_fingerprint(rows: list[dict[str, Any]]) -> str:
    """Deterministic fingerprint of remapped rows, used as the audit idempotency key.

    Same approach as data_integration/orchestrator.py's own (private)
    _content_fingerprint(): rows are sorted into a canonical order before
    hashing, since row order is not part of dataset identity.
    """
    canonical_rows = sorted(json.dumps(row, sort_keys=True, default=str) for row in rows)
    return hashlib.sha256(json.dumps(canonical_rows).encode("utf-8")).hexdigest()


def fetch_profile_data(profile: ConnectionProfile, audit_store: AuditStore) -> list[dict[str, Any]]:
    """Validate, fetch, remap, and audit one tenant's data pull.

    Unlike data_integration/orchestrator.py's run_integration_with_audit()
    (STORY-001/STORY-011), which isolates a bad dataset so a sibling pull
    in the same batch can still succeed, this raises immediately on a
    validation or fetch failure: a ConnectionProfile is one tenant's whole
    pull, with no sibling to protect, so there is nothing gained by
    swallowing the error into a result object instead of letting the
    caller's own error handling (or an agent layer above this, the same
    way agents/demand_forecasting_agent.py's callers handle its typed
    errors) decide what to do.

    Every attempt is still recorded to audit_store regardless of outcome -
    a validation/fetch failure is recorded before the exception propagates,
    keyed with a fresh id each time (a repeated failure is itself
    diagnostic, not something to dedup away); a success is keyed by tenant
    + dataset kind + a content fingerprint of the remapped rows, so
    re-pulling identical data doesn't duplicate the trail, mirroring
    run_integration_with_audit()'s own idempotency keying exactly.
    """
    dataset_label = f"{profile.tenant_id}:{profile.dataset_kind}"
    try:
        validate_profile(profile)
        raw_rows = postgres_connector.fetch_rows(profile.query, config=profile.postgres)
    except (SchemaMappingError, postgres_connector.PostgresIntegrationError) as exc:
        audit_store.record(
            idempotency_key=f"{dataset_label}:error:{uuid.uuid4()}",
            dataset=dataset_label,
            source_type="postgresql",
            outcome="failure",
            error=str(exc),
        )
        raise

    remapped = remap_rows(raw_rows, profile.column_mapping)
    audit_store.record(
        idempotency_key=f"{dataset_label}:{_content_fingerprint(remapped)}",
        dataset=dataset_label,
        source_type="postgresql",
        outcome="success",
        row_count=len(remapped),
    )
    return remapped
