from unittest.mock import patch

from dashboard.data_freshness import DataFreshnessEntry
from dashboard.metrics import DashboardMetric, DashboardSnapshot
from dashboard.render import render_dashboard_html


def _snapshot(**overrides) -> DashboardSnapshot:
    defaults = dict(
        dashboard_id="dash-1",
        generated_at="2026-09-04T12:00:00+00:00",
        metrics=[],
        data_processing_errors=[],
        notification=None,
        overall_status="ok",
        total_critical_findings=0,
        total_high_findings=0,
    )
    defaults.update(overrides)
    return DashboardSnapshot(**defaults)


def test_renders_an_ok_tile_with_headline_confidence_and_findings():
    metric = DashboardMetric(
        metric_id="risk_detection_agent",
        label="Supply Chain Risk",
        status="ok",
        headline="Supply chain risk score 65/100 (high): demand spike in 2025-07",
        source_agent="risk_detection_agent",
        confidence=0.8,
        severity="high",
        critical_findings=0,
        high_findings=2,
    )

    html = render_dashboard_html(_snapshot(metrics=[metric]))

    assert "Supply Chain Risk" in html
    assert "Supply chain risk score 65/100" in html
    assert "confidence: 80%" in html
    assert "2 high finding" in html
    assert "high</span>" in html or ">high<" in html


def test_renders_an_error_tile_and_the_notification_banner():
    metric = DashboardMetric(
        metric_id="risk_detection_agent",
        label="Supply Chain Risk",
        status="error",
        headline="no supply chain data provided for risk detection",
        source_agent="risk_detection_agent",
    )
    snapshot = _snapshot(
        metrics=[metric],
        data_processing_errors=[{"agent": "risk_detection_agent", "error": "no data"}],
        notification="1 of 1 data source(s) could not be processed",
        overall_status="error",
    )

    html = render_dashboard_html(snapshot)

    assert "no supply chain data provided" in html
    assert "1 of 1 data source(s) could not be processed" in html


def test_agent_supplied_text_is_html_escaped_not_injected():
    metric = DashboardMetric(
        metric_id="stockout_risk_agent",
        label="Stockout Risk",
        status="ok",
        headline="<script>alert('xss')</script>",
        source_agent="stockout_risk_agent",
    )

    html = render_dashboard_html(_snapshot(metrics=[metric]))

    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_a_single_broken_tile_falls_back_without_taking_down_the_page():
    good = DashboardMetric(
        metric_id="stockout_risk_agent",
        label="Stockout Risk",
        status="ok",
        headline="fine",
        source_agent="stockout_risk_agent",
    )
    bad = DashboardMetric(
        metric_id="risk_detection_agent",
        label="Supply Chain Risk",
        status="ok",
        headline="fine too",
        source_agent="risk_detection_agent",
    )

    real_render_tile = __import__("dashboard.render", fromlist=["_render_tile"])._render_tile

    def _boom(metric):
        if metric.metric_id == "risk_detection_agent":
            raise ValueError("simulated rendering bug")
        return real_render_tile(metric)

    with patch("dashboard.render._render_tile", side_effect=_boom):
        html = render_dashboard_html(_snapshot(metrics=[good, bad]))

    assert "Stockout Risk" in html
    assert 'Unable to render tile "risk_detection_agent"' in html


def test_renders_a_data_sources_section_with_success_and_failure_rows():
    entries = [
        DataFreshnessEntry(
            dataset="customer_orders", source_type="postgres", outcome="success",
            pulled_at="2026-09-04T12:00:00+00:00", row_count=500,
        ),
        DataFreshnessEntry(
            dataset="delivery_records", source_type="postgres", outcome="failure",
            pulled_at="2026-09-04T12:00:00+00:00", error="connection refused",
        ),
    ]

    html = render_dashboard_html(_snapshot(data_freshness=entries))

    assert "Data Sources" in html
    assert "customer_orders" in html and "500 row(s)" in html
    assert "delivery_records" in html and "connection refused" in html


def test_a_page_assembly_failure_still_returns_a_valid_fallback_page():
    # Break something only the *success* path touches (the overall-status
    # color lookup, after the per-tile loop) so the outer except runs -
    # not _esc, which _fallback_page itself also depends on and must
    # still work when the fallback path runs.
    class _BoomDict:
        def get(self, *args, **kwargs):
            raise RuntimeError("simulated page-assembly bug")

    with patch("dashboard.render._STATUS_COLORS", _BoomDict()):
        html = render_dashboard_html(_snapshot())

    assert "Dashboard rendering failed" in html
    assert html.strip().startswith("<!doctype html>")
