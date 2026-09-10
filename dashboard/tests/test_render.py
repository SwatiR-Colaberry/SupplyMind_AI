from unittest.mock import patch

from agents.contracts import AgentFinding
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
    # Plain-English takeaway for a first-time reader sits above the fold;
    # the technical headline is still present but tucked behind <details>.
    assert "Needs attention soon." in html
    assert "<summary>Show details</summary>" in html


def test_plain_takeaway_covers_every_severity_and_the_error_case():
    from dashboard.render import _plain_takeaway

    def metric(status, severity):
        return DashboardMetric(
            metric_id="x", label="X", status=status, headline="h", source_agent="x", severity=severity
        )

    assert _plain_takeaway(metric("ok", "critical")) == "Needs immediate attention."
    assert _plain_takeaway(metric("ok", "high")) == "Needs attention soon."
    assert _plain_takeaway(metric("ok", "medium")) == "Worth a look."
    assert _plain_takeaway(metric("ok", "low")) == "Low risk - no action needed."
    assert _plain_takeaway(metric("ok", None)) == "No risk flagged."
    assert _plain_takeaway(metric("error", None)) == "This couldn't be checked - see details below."
    # An error tile's plain takeaway must not be overridden by a stale
    # severity value - a crashed/errored agent has nothing to be "low
    # risk" about, so status is checked first regardless of severity.
    assert _plain_takeaway(metric("error", "critical")) == "This couldn't be checked - see details below."


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

    assert "Where this data came from" in html
    assert "customer_orders" in html and "500 row(s)" in html
    assert "delivery_records" in html and "connection refused" in html


def test_renders_no_nav_bar_when_nav_links_is_omitted():
    html = render_dashboard_html(_snapshot())

    assert '<div class="nav-bar">' not in html


def test_renders_a_nav_bar_linking_sibling_pages_with_the_current_page_as_a_non_link():
    html = render_dashboard_html(
        _snapshot(),
        nav_links=[("Live Data", None), ("Demo: Partial Data", "control_tower_partial_failure.html")],
    )

    assert '<span class="nav-item nav-current">Live Data</span>' in html
    assert '<a class="nav-item" href="control_tower_partial_failure.html">Demo: Partial Data</a>' in html


def test_subtitle_appears_when_provided_and_is_escaped():
    html = render_dashboard_html(_snapshot(), subtitle="<b>Live</b> Data")

    assert "<b>Live</b> Data" not in html
    assert "&lt;b&gt;Live&lt;/b&gt; Data" in html


def test_a_long_headline_of_distinct_segments_is_capped_with_a_more_count():
    from dashboard.render import _condense_headline

    segments = [f"PO-{i} (2 day(s) late - delay cost $600.00)" for i in range(50)]
    condensed = _condense_headline("; ".join(segments))

    assert len(condensed) < 400
    assert "PO-0 " in condensed
    assert "(+45 more, 50 unique of 50 total)" in condensed


def test_a_long_headline_of_repeated_segments_collapses_the_duplicates():
    from dashboard.render import _condense_headline

    repeated = "73 (missing field(s): safety_stock, daily_demand_rate)"
    text = "inventory data flagged for review: " + "; ".join([repeated] * 500)
    condensed = _condense_headline(text)

    assert len(condensed) < 400
    assert condensed.count(repeated) == 2  # once embedded in the prefixed first segment, once standalone
    assert "inventory data flagged for review: " + repeated in condensed
    assert "(+498 more, 2 unique of 500 total)" in condensed


def test_a_short_headline_is_never_condensed():
    from dashboard.render import _condense_headline

    assert _condense_headline("short and fine") == "short and fine"


def test_show_details_renders_the_condensed_headline_not_the_raw_one():
    huge_headline = "x: " + "; ".join(f"item-{i}" for i in range(1000))
    metric = DashboardMetric(
        metric_id="stockout_risk_agent", label="Stockout Risk", status="ok", headline=huge_headline,
        source_agent="stockout_risk_agent",
    )

    html = render_dashboard_html(_snapshot(metrics=[metric]))

    assert huge_headline not in html
    assert "more, 1000 unique of 1000 total)" in html


def test_renders_no_charts_sections_when_nothing_qualifies():
    html = render_dashboard_html(_snapshot())

    assert '<div class="charts-section">' not in html
    assert '<div class="explore-section">' not in html


def test_renders_quick_review_and_explore_sections_when_findings_exist():
    metric = DashboardMetric(
        metric_id="stockout_risk_agent",
        label="Stockout Risk",
        status="ok",
        headline="fine",
        source_agent="stockout_risk_agent",
        confidence=0.8,
        severity="critical",
        critical_findings=1,
        findings=[AgentFinding(subject="SKU-1", subject_kind="sku", severity="critical", detail="stockout imminent")],
    )

    html = render_dashboard_html(_snapshot(metrics=[metric]))

    assert '<div class="charts-section">' in html
    assert "Findings by area" in html
    assert "Confidence by area" in html
    assert '<div class="explore-section">' in html
    assert "Which SKUs need attention in Stockout Risk?" in html
    assert "<svg" in html


def test_explore_question_labels_are_html_escaped_not_injected():
    metric = DashboardMetric(
        metric_id="x",
        label="X",
        status="ok",
        headline="fine",
        source_agent="x",
        findings=[AgentFinding(subject="<script>alert(1)</script>", subject_kind="sku", severity="low", detail="d")],
    )

    html = render_dashboard_html(_snapshot(metrics=[metric]))

    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_a_charts_render_failure_falls_back_without_taking_down_the_page():
    metric = DashboardMetric(
        metric_id="stockout_risk_agent", label="Stockout Risk", status="ok", headline="fine",
        source_agent="stockout_risk_agent", critical_findings=1,
    )

    with patch("dashboard.render.build_chart_specs", side_effect=ValueError("simulated charts bug")):
        html = render_dashboard_html(_snapshot(metrics=[metric]))

    assert "Stockout Risk" in html
    assert '<div class="charts-section">' not in html
    assert '<div class="explore-section">' not in html


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
