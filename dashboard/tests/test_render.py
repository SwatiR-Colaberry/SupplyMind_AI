from unittest.mock import patch

from agents.contracts import AgentFinding
from dashboard.approval_store import ApprovalRecord
from dashboard.data_freshness import DataFreshnessEntry
from dashboard.metrics import DashboardMetric, DashboardSnapshot
from dashboard.render import GLOSSARY, render_answer_lines, render_dashboard_html
from local_apps import theme


def _approval(**overrides) -> ApprovalRecord:
    defaults = dict(
        decision_id="dec-1",
        dashboard_id="dash-1",
        metric_id="stockout_risk_agent",
        decision="approved",
        reviewer="ali",
        note="",
        timestamp="2026-09-11T00:00:00+00:00",
    )
    defaults.update(overrides)
    return ApprovalRecord(**defaults)


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


def test_kpi_row_is_a_fixed_6_column_grid_so_it_never_wraps():
    # Regression (2026-09-12, second report): widening the KPI cards to
    # fight crowding (prior entry) made a fixed set of always-exactly-6
    # cards wrap onto a second line under auto-fit once the row got
    # narrower than 6 widened cards needed - a user asked for these back
    # on one line. A literal 6-column grid can't wrap; columns only ever
    # shrink together.
    html = render_dashboard_html(_snapshot())

    assert "grid-template-columns: repeat(6, minmax(0, 1fr));" in html


def test_stat_card_no_longer_clips_its_own_tooltip():
    # Regression (2026-09-12, fourth report): .stat-card had overflow:
    # hidden (added for a since-redundant ellipsis pass), which silently
    # clipped the "Overall status" KPI's own .term tooltip - a popover
    # positioned above its trigger, cut off by the very next ancestor's
    # edge. A user reported the tooltip "not visible."
    html = render_dashboard_html(_snapshot())

    stat_card_start = html.index(".stat-card {")
    stat_card_rule = html[stat_card_start : html.index("}", stat_card_start)]
    assert "overflow: hidden" not in stat_card_rule


def test_page_no_longer_has_its_own_glossary_panel():
    # Regression (2026-09-13): this page's compact glossary panel (added
    # 2026-09-12, fifth report) moved to a dedicated Glossary screen
    # (learn/serve_learn.py) after a follow-up pointed out it was "only
    # accessible in [the] what if tab" and that the panel "has not been
    # removed from other pages" once that screen existed. The inline
    # .term tooltips _wrap_glossary_terms() applies (see the test below)
    # are unaffected - only the standalone panel is gone.
    html = render_dashboard_html(_snapshot())

    assert "glossary-panel" not in html


def test_page_includes_the_shared_first_visit_onboarding_overlay():
    html = render_dashboard_html(_snapshot())

    assert '<div class="onboarding-overlay" id="onboarding-overlay">' in html


def test_intro_copy_does_not_use_the_phrase_plain_english():
    # A user asked to remove wording like "A plain-English" from the app.
    html = render_dashboard_html(_snapshot())

    assert "plain-English" not in html
    assert "plain-english" not in html.lower()


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
    assert "Supply chain " in html
    # "risk score" is a GLOSSARY term (2026-09-12) - wrapped in its own
    # tooltip span rather than appearing as plain contiguous text.
    assert '<span class="term" data-tooltip="A single 0-100 score' in html
    assert '<span class="hl-value">65/100</span>' in html
    assert "confidence</span>: 80%" in html
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

    def _boom(metric, dashboard_id, latest_approval):
        if metric.metric_id == "risk_detection_agent":
            raise ValueError("simulated rendering bug")
        return real_render_tile(metric, dashboard_id, latest_approval)

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
        nav_links=[
            ("Live Data", None, theme.ICON_PULSE, "warning"),
            ("Demo: Partial Data", "control_tower_partial_failure.html", theme.ICON_DATABASE, "brand"),
        ],
    )

    assert '<span class="nav-item nav-current">' in html
    assert "Live Data</span>" in html
    assert 'href="control_tower_partial_failure.html">' in html
    assert "Demo: Partial Data</a>" in html


def test_renders_page_header_with_the_shared_background_treatment_and_icon():
    html = render_dashboard_html(_snapshot())

    assert '<div class="page-header">' in html
    assert '<div class="page-header-icon page-header-icon-warning">' in html
    assert "<h1>Executive Control Tower</h1>" in html


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
    assert "unique of" in html and "total)" in html
    assert html.count('<span class="hl-value">1000</span>') == 2


def test_show_details_renders_each_segment_on_its_own_line():
    metric = DashboardMetric(
        metric_id="x", label="X", status="ok", source_agent="x",
        headline="PO-1 (2 day(s) late - delay cost $600.00); PO-2 (high risk)",
    )

    html = render_dashboard_html(_snapshot(metrics=[metric]))

    # Regression (2026-09-12): rendered as <div> per segment before; a
    # user said this "is not in a list" - now a real <ul>/<li>.
    assert '<ul class="tile-headline">' in html
    assert html.count('<li class="headline-line">') == 2
    assert "PO-" in html and "delay cost" in html and "risk)" in html


def test_show_details_color_codes_numbers_and_severity_words():
    metric = DashboardMetric(
        metric_id="x", label="X", status="ok", source_agent="x",
        headline="delay cost $600.00, risk level high",
    )

    html = render_dashboard_html(_snapshot(metrics=[metric]))

    assert '<span class="hl-value">$600.00</span>' in html
    assert '<span class="hl-severity" style="color:#b5540f">high</span>' in html


def test_show_details_value_highlighting_leaves_list_separator_commas_outside_the_span():
    metric = DashboardMetric(
        metric_id="x", label="X", status="ok", source_agent="x",
        headline="e.g. 1360, 1360, 1360",
    )

    html = render_dashboard_html(_snapshot(metrics=[metric]))

    assert '<span class="hl-value">1360</span>,' in html
    assert '<span class="hl-value">1360,</span>' not in html


def test_show_details_headline_highlighting_does_not_reintroduce_markup():
    metric = DashboardMetric(
        metric_id="x", label="X", status="ok", source_agent="x",
        headline="<script>alert(1)</script> costs $5",
    )

    html = render_dashboard_html(_snapshot(metrics=[metric]))

    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_renders_no_charts_sections_when_nothing_qualifies():
    html = render_dashboard_html(_snapshot())

    assert '<div class="charts-section">' not in html
    assert '<div class="explore-section">' not in html


def test_chart_card_has_a_max_width_so_a_lone_chart_does_not_stretch_full_width():
    # Regression (2026-09-12): with only one quick-review chart qualifying
    # (e.g. 0 critical/high findings, so "Findings by area" is excluded
    # and "Confidence by area" is the only card in .charts-grid), that
    # single card's 1fr grid track stretched to fill the entire row width
    # with no sibling to share it with - a user reported this as "the
    # first chart is too large." The same problem, for the same reason,
    # already existed for the explore section's own single visible panel;
    # both are now covered by one shared max-width on .chart-card itself
    # rather than two separately-maintained caps.
    metric = DashboardMetric(
        metric_id="stockout_risk_agent",
        label="Stockout Risk",
        status="ok",
        headline="fine",
        source_agent="stockout_risk_agent",
        confidence=0.8,
    )

    html = render_dashboard_html(_snapshot(metrics=[metric]))

    assert "Findings by area" not in html  # excluded: no critical/high findings
    assert "Confidence by area" in html  # the lone quick-review chart
    assert "max-width: 560px" in html


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


def test_renders_kpi_stat_cards_with_the_computed_revenue_at_risk_and_delay_cost():
    stockout_metric = DashboardMetric(
        metric_id="stockout_risk_agent", label="Stockout Risk", status="ok", headline="fine",
        source_agent="stockout_risk_agent",
        findings=[AgentFinding(subject="SKU-1", subject_kind="sku", severity="critical", detail="d", metric_value=1250.0)],
    )
    shipment_metric = DashboardMetric(
        metric_id="shipment_delay_analysis_agent", label="Shipment Delay", status="ok", headline="fine",
        source_agent="shipment_delay_analysis_agent", findings=[],
    )

    html = render_dashboard_html(_snapshot(metrics=[stockout_metric, shipment_metric]))

    assert "Revenue at risk" in html
    assert "$1,250" in html
    assert "Shipment delay cost" in html
    assert "$0" in html


def test_renders_an_em_dash_for_unknown_kpis_not_a_fabricated_zero():
    # No stockout_risk_agent tile in this snapshot at all -> unknown, not $0.
    html = render_dashboard_html(_snapshot(metrics=[]))

    assert "Revenue at risk" in html
    assert "&mdash;" in html or "—" in html


def test_a_kpi_computation_failure_falls_back_without_taking_down_the_page():
    metric = DashboardMetric(
        metric_id="stockout_risk_agent", label="Stockout Risk", status="ok", headline="fine",
        source_agent="stockout_risk_agent", critical_findings=1,
    )

    with patch("dashboard.render.compute_kpi_summary", side_effect=ValueError("simulated kpi bug")):
        html = render_dashboard_html(_snapshot(metrics=[metric]))

    assert "Stockout Risk" in html
    assert "Revenue at risk" in html  # the stat card itself still renders, just with an unknown value


def test_every_tile_gets_approve_and_reject_buttons_with_correct_ids():
    metric = DashboardMetric(
        metric_id="stockout_risk_agent", label="Stockout Risk", status="ok", headline="fine",
        source_agent="stockout_risk_agent",
    )

    html = render_dashboard_html(_snapshot(dashboard_id="dash-1", metrics=[metric]))

    # dashboard_id/metric_id are JSON-then-HTML-escaped for embedding inside
    # the onclick attribute, so a plain double quote comes out as &quot;.
    assert "submitApproval(&quot;dash-1&quot;, &quot;stockout_risk_agent&quot;, 'approved')" in html
    assert "submitApproval(&quot;dash-1&quot;, &quot;stockout_risk_agent&quot;, 'rejected')" in html


def test_a_tile_with_a_latest_approval_shows_its_badge():
    metric = DashboardMetric(
        metric_id="stockout_risk_agent", label="Stockout Risk", status="ok", headline="fine",
        source_agent="stockout_risk_agent",
    )
    latest = _approval(decision="approved", reviewer="ali")

    html = render_dashboard_html(
        _snapshot(dashboard_id="dash-1", metrics=[metric]), latest_approvals={"stockout_risk_agent": latest}
    )

    assert "Approved by ali" in html
    assert 'approval-badge-approved' in html


def test_a_tile_with_no_recorded_decision_has_no_badge():
    metric = DashboardMetric(
        metric_id="stockout_risk_agent", label="Stockout Risk", status="ok", headline="fine",
        source_agent="stockout_risk_agent",
    )

    html = render_dashboard_html(_snapshot(metrics=[metric]))

    assert '<span class="approval-badge' not in html


def test_approval_history_section_renders_every_decision_newest_first():
    older = _approval(decision_id="dec-1", decision="approved", reviewer="ali", timestamp="2026-09-11T00:00:00+00:00")
    newer = _approval(decision_id="dec-2", decision="rejected", reviewer="ram", note="need more evidence", timestamp="2026-09-11T01:00:00+00:00")

    html = render_dashboard_html(_snapshot(metrics=[]), approval_history=[older, newer])

    assert "Approval History" in html
    # Newest first: "Rejected by ram" must appear before "Approved by ali".
    assert html.index("Rejected") < html.index("Approved")
    assert "need more evidence" in html


def test_no_approval_history_section_when_no_decisions_exist():
    html = render_dashboard_html(_snapshot(metrics=[]))

    assert "<h2>Approval History</h2>" not in html


def test_an_approval_history_render_failure_falls_back_without_taking_down_the_page():
    metric = DashboardMetric(
        metric_id="stockout_risk_agent", label="Stockout Risk", status="ok", headline="fine",
        source_agent="stockout_risk_agent",
    )

    with patch("dashboard.render._render_approval_history_section", side_effect=ValueError("simulated bug")):
        html = render_dashboard_html(_snapshot(metrics=[metric]), approval_history=[_approval()])

    assert "Stockout Risk" in html
    assert "<h2>Approval History</h2>" not in html


def test_approval_ids_are_escaped_safely_inside_the_onclick_attribute():
    metric = DashboardMetric(
        metric_id='sku"; alert(1); //', label="Weird", status="ok", headline="fine", source_agent="a",
    )

    html = render_dashboard_html(_snapshot(dashboard_id='d"><script>', metrics=[metric]))

    # Neither identifier's raw, unescaped form may appear in the output -
    # both must be safely encoded for both the JS-string and HTML-attribute boundaries.
    assert '"; alert(1); //' not in html
    assert "<script>alert" not in html.lower()


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


def test_glossary_terms_get_a_tooltip_wherever_they_appear_in_a_headline():
    # Added 2026-09-12 after a user said jargon in this technical prose
    # ("z-score", "stockout probability", ...) "does not explain anything"
    # on its own - generalizes the earlier confidence/risk-score-only
    # tooltips into a reusable glossary.
    metric = DashboardMetric(
        metric_id="x", label="X", status="ok", source_agent="x",
        headline="z-score 2.19 for SKU-1; stockout probability 98% with safety stock 30",
    )

    html = render_dashboard_html(_snapshot(metrics=[metric]))

    assert f'<span class="term" data-tooltip="{GLOSSARY["z-score"]}">z-score</span>' in html
    assert f'<span class="term" data-tooltip="{GLOSSARY["stockout probability"]}">stockout probability</span>' in html
    assert f'<span class="term" data-tooltip="{GLOSSARY["safety stock"]}">safety stock</span>' in html


def test_render_answer_lines_produces_the_same_list_and_glossary_treatment():
    # Public entry point chat_interface/serve_chat_ui.py calls directly so
    # its own chat answers get identical treatment without a second copy
    # of this module's condense/split/highlight/glossary pipeline.
    html = render_answer_lines("Risk score 65/100 (high); confidence 0.8")

    assert '<ul class="tile-headline">' in html
    assert html.count("<li class=\"headline-line\">") == 2
    assert 'class="term" data-tooltip="A single 0-100 score' in html
    assert '<span class="hl-value">65/100</span>' in html
