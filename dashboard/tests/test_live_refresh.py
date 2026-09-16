from __future__ import annotations

import os
from unittest.mock import patch

from data_console.file_store import UnknownUploadError
from data_console.mapping_service import MappingPreviewResult
from data_console.mapping_store import DatasetMapping
from dashboard.approval_store import ApprovalStore
from dashboard.live_refresh import (
    _dataset_result_from_data_console,
    _nav_links,
    latest_real_data_snapshot,
    refresh_real_data_dashboard,
)
from data_integration.connection_profile import SchemaMappingError
from data_integration.orchestrator import DatasetResult


def _mock_store(mapping: DatasetMapping | None):
    store = type("Store", (), {"get": staticmethod(lambda kind: mapping)})()
    return patch("dashboard.live_refresh.MappingStore", return_value=store)


def test_returns_none_when_data_console_has_no_mapping_at_all():
    with _mock_store(None):
        assert _dataset_result_from_data_console("inventory") is None


def test_returns_a_failure_result_when_marked_unavailable_instead_of_falling_back():
    # An explicit "this data genuinely isn't here" must not silently fall
    # back to the hardcoded Postgres query - that would contradict what the
    # tenant already told data_console.
    with _mock_store(DatasetMapping(status="unavailable")):
        result = _dataset_result_from_data_console("delivery_records")

    assert result.name == "delivery_records"
    assert result.outcome == "failure"
    assert "unavailable" in result.error


def test_returns_a_success_result_with_the_previewed_rows_for_a_file_mapping():
    mapping = DatasetMapping(status="mapped", file_id="f1", filename="orders.csv", column_mapping={"sku": "sku"}, source_kind="file")
    preview_result = MappingPreviewResult(rows=[{"sku": "SKU-1"}], row_count=1, truncated=False)
    with _mock_store(mapping), patch("dashboard.live_refresh.preview_mapping", return_value=preview_result):
        result = _dataset_result_from_data_console("inventory")

    assert result.outcome == "success"
    assert result.rows == [{"sku": "SKU-1"}]
    assert result.source_type == "csv_upload"


def test_returns_a_success_result_with_postgresql_source_type_for_a_table_mapping():
    mapping = DatasetMapping(status="mapped", table="acme_inventory", column_mapping={"sku": "sku"}, source_kind="table")
    preview_result = MappingPreviewResult(rows=[{"sku": "SKU-1"}], row_count=1, truncated=False)
    with _mock_store(mapping), patch("dashboard.live_refresh.preview_mapping", return_value=preview_result):
        result = _dataset_result_from_data_console("inventory")

    assert result.source_type == "postgresql"


def test_returns_a_failure_result_when_the_saved_mapping_no_longer_validates():
    # E.g. the underlying table/file changed shape since Save Mapping - the
    # same SchemaMappingError the console's own "Preview" button would show.
    mapping = DatasetMapping(status="mapped", table="acme_inventory", column_mapping={"sku": "sku"}, source_kind="table")
    with _mock_store(mapping), patch(
        "dashboard.live_refresh.preview_mapping", side_effect=SchemaMappingError("mapped column not found")
    ):
        result = _dataset_result_from_data_console("inventory")

    assert result.outcome == "failure"
    assert "mapped column not found" in result.error


def test_returns_a_failure_result_when_the_uploaded_file_is_gone():
    mapping = DatasetMapping(status="mapped", file_id="gone", filename="orders.csv", column_mapping={"sku": "sku"}, source_kind="file")
    with _mock_store(mapping), patch(
        "dashboard.live_refresh.preview_mapping", side_effect=UnknownUploadError("no such upload")
    ):
        result = _dataset_result_from_data_console("inventory")

    assert result.outcome == "failure"
    assert "no such upload" in result.error


def test_nav_links_is_the_same_fixed_nav_every_other_screen_shows():
    # Regression: this used to be a richer 6-item nav (these 4 plus both
    # demo-scenario links folded in), which a user flagged as
    # inconsistent with the plain 4-item nav every other screen shows -
    # the 2 demo scenarios (still buildable, just not linked from the
    # running app - a user later asked whether they were even needed
    # anymore once real data existed, and the answer was no) aren't
    # linked from anywhere in this nav at all. Grew to 5 items
    # (2026-09-13) with the addition of Learn.
    links = _nav_links("partial_failure")

    labels = [label for label, _, _, _ in links]
    assert labels == ["Data Console", "Live Data", "AI Assistant", "What-If Simulator", "Glossary"]
    for _, href, _, _ in links:
        assert href.startswith("http://127.0.0.1:")


def test_nav_links_carries_the_same_per_tab_icons_theme_py_uses():
    # Added 2026-09-13 after a user picked "small per-tab icons" for the
    # nav bar - this page's own _render_nav() (see dashboard/render.py)
    # needed the same icon/accent pairs local_apps.theme's
    # _NAV_DESTINATIONS carries, or its nav bar would be the one screen
    # left behind as plain text once every other screen gained icons.
    from local_apps import theme

    links = _nav_links("partial_failure")
    icons = {label: (icon, accent) for label, _, icon, accent in links}

    assert icons["Data Console"] == (theme.ICON_DATABASE, "brand")
    assert icons["Live Data"] == (theme.ICON_PULSE, "warning")
    assert icons["AI Assistant"] == (theme.ICON_CHAT, "accent-2")
    assert icons["What-If Simulator"] == (theme.ICON_SIMULATOR, "success")
    assert icons["Glossary"] == (theme.ICON_GLOSSARY, "accent-2")


def test_nav_links_marks_live_data_current_only_on_the_real_data_scenario():
    real_data_links = {label: href for label, href, _, _ in _nav_links("real_data")}
    other_links = {label: href for label, href, _, _ in _nav_links("partial_failure")}

    assert real_data_links["Live Data"] is None
    assert other_links["Live Data"] is not None
    assert other_links["Live Data"].startswith("http://127.0.0.1:")


def test_refresh_real_data_dashboard_is_idempotent_and_returns_a_real_data_summary(tmp_path):
    # No mappings -> every dataset falls back to the hardcoded Postgres
    # pull, which is itself mocked out here so this test never touches a
    # real database or the real control_tower_real_data.html file. The
    # call itself must succeed and return a well-formed summary (not
    # raise); calling it twice back to back must be safe (same file
    # overwritten, no duplicated state), matching this repo's Idempotency
    # & Replayability rule.
    failed = [
        DatasetResult(name=name, source_type="postgres", outcome="failure", error="no credentials configured")
        for name in ("customer_orders", "delivery_records", "inventory")
    ]
    env = {
        "SUPPLYMIND_AUDIT_LOG_PATH": str(tmp_path / "audit_log.jsonl"),
        "SUPPLYMIND_DASHBOARD_AUDIT_LOG_PATH": str(tmp_path / "dashboard_audit_log.jsonl"),
        "SUPPLYMIND_APPROVAL_LOG_PATH": str(tmp_path / "dashboard_approvals.jsonl"),
    }
    with (
        patch("dashboard.live_refresh.MappingStore") as mock_store_cls,
        patch("dashboard.live_refresh.run_integration_with_audit", return_value=failed),
        patch("dashboard.live_refresh.DEFAULT_HTML_DIR", tmp_path),
        patch.dict(os.environ, env),
    ):
        mock_store_cls.return_value.get.return_value = None
        first = refresh_real_data_dashboard()
        second = refresh_real_data_dashboard()

    assert first["scenario"] == "real_data" == second["scenario"]
    assert first["build_outcome"] == "success" == second["build_outcome"]
    assert first["html_path"] == second["html_path"]
    assert (tmp_path / "control_tower_real_data.html").is_file()


def test_refresh_real_data_dashboard_updates_latest_real_data_snapshot(tmp_path):
    # This is what lets chat_interface/serve_chat_ui.py's current_snapshot()
    # share Run Analysis's own latest result instead of running a second,
    # independently-stale pipeline - see that function's own docstring.
    assert latest_real_data_snapshot() is None

    failed = [
        DatasetResult(name=name, source_type="postgres", outcome="failure", error="no credentials configured")
        for name in ("customer_orders", "delivery_records", "inventory")
    ]
    env = {
        "SUPPLYMIND_AUDIT_LOG_PATH": str(tmp_path / "audit_log.jsonl"),
        "SUPPLYMIND_DASHBOARD_AUDIT_LOG_PATH": str(tmp_path / "dashboard_audit_log.jsonl"),
        "SUPPLYMIND_APPROVAL_LOG_PATH": str(tmp_path / "dashboard_approvals.jsonl"),
    }
    with (
        patch("dashboard.live_refresh.MappingStore") as mock_store_cls,
        patch("dashboard.live_refresh.run_integration_with_audit", return_value=failed),
        patch("dashboard.live_refresh.DEFAULT_HTML_DIR", tmp_path),
        patch.dict(os.environ, env),
    ):
        mock_store_cls.return_value.get.return_value = None
        refresh_real_data_dashboard()

    snapshot = latest_real_data_snapshot()
    assert snapshot is not None
    assert snapshot.dashboard_id == "real_data"


def test_refresh_real_data_dashboard_renders_a_previously_recorded_approval(tmp_path):
    # A decision recorded through the ApprovalStore (what data_console's
    # /api/approve route does) must show up the next time this same
    # dashboard_id ("real_data") is rendered - proves the 2 entry points
    # (this module's own render call, and the API route) really do share
    # one persisted store rather than drifting.
    failed = [
        DatasetResult(name=name, source_type="postgres", outcome="failure", error="no credentials configured")
        for name in ("customer_orders", "delivery_records", "inventory")
    ]
    approval_path = tmp_path / "dashboard_approvals.jsonl"
    ApprovalStore(approval_path).record(
        dashboard_id="real_data", metric_id="stockout_risk_agent", decision="approved", reviewer="ali", note="ok"
    )
    env = {
        "SUPPLYMIND_AUDIT_LOG_PATH": str(tmp_path / "audit_log.jsonl"),
        "SUPPLYMIND_DASHBOARD_AUDIT_LOG_PATH": str(tmp_path / "dashboard_audit_log.jsonl"),
        "SUPPLYMIND_APPROVAL_LOG_PATH": str(approval_path),
    }
    with (
        patch("dashboard.live_refresh.MappingStore") as mock_store_cls,
        patch("dashboard.live_refresh.run_integration_with_audit", return_value=failed),
        patch("dashboard.live_refresh.DEFAULT_HTML_DIR", tmp_path),
        patch.dict(os.environ, env),
    ):
        mock_store_cls.return_value.get.return_value = None
        refresh_real_data_dashboard()

    html = (tmp_path / "control_tower_real_data.html").read_text(encoding="utf-8")
    assert "Approved by ali" in html
    assert "Approval History" in html
