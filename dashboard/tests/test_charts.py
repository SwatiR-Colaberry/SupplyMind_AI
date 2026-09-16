from agents.contracts import AgentFinding, AgentResponse
from agents.orchestrator import CoordinationResult
from dashboard.charts import ChartBar, ChartSpec, build_chart_specs, render_bar_chart
from dashboard.metrics import build_dashboard


def _ok_result(agent_name, recommendation="all clear", confidence=0.9, findings=None):
    return CoordinationResult(
        agent_name=agent_name,
        outcome="success",
        response=AgentResponse(
            agent_name=agent_name, status="ok", recommendation=recommendation, confidence=confidence,
            findings=findings or [],
        ),
    )


def _error_result(agent_name, error="no data"):
    return CoordinationResult(
        agent_name=agent_name, outcome="success", response=AgentResponse(agent_name=agent_name, status="error", error=error),
    )


def test_findings_by_area_chart_only_includes_metrics_with_critical_or_high_findings():
    findings = [
        AgentFinding(subject="SKU-1", subject_kind="sku", severity="critical", detail="x"),
        AgentFinding(subject="SKU-2", subject_kind="sku", severity="low", detail="y"),
    ]
    snapshot = build_dashboard(
        [
            _ok_result("stockout_risk_agent", findings=findings),
            _ok_result("risk_detection_agent", findings=[]),  # nothing critical/high -> excluded
        ]
    )

    quick_review, _ = build_chart_specs(snapshot)

    findings_chart = next(c for c in quick_review if c.chart_id == "findings-by-area")
    assert [b.label for b in findings_chart.bars] == ["Stockout Risk"]
    assert findings_chart.bars[0].value == 1  # 1 critical + 0 high


def test_confidence_by_area_chart_sorts_highest_confidence_first():
    snapshot = build_dashboard(
        [_ok_result("stockout_risk_agent", confidence=0.4), _ok_result("risk_detection_agent", confidence=0.9)]
    )

    quick_review, _ = build_chart_specs(snapshot)

    confidence_chart = next(c for c in quick_review if c.chart_id == "confidence-by-area")
    assert [b.label for b in confidence_chart.bars] == ["Supply Chain Risk", "Stockout Risk"]
    assert [b.value for b in confidence_chart.bars] == [90, 40]
    assert confidence_chart.value_suffix == "%"


def test_no_quick_review_charts_when_nothing_qualifies():
    snapshot = build_dashboard([_error_result("stockout_risk_agent")])

    quick_review, explore = build_chart_specs(snapshot)

    assert quick_review == []
    assert explore == []


def test_explore_chart_is_built_per_agent_and_subject_kind_with_severity_ordering():
    findings = [
        AgentFinding(subject="SKU-LOW", subject_kind="sku", severity="low", detail="fine"),
        AgentFinding(subject="SKU-CRIT", subject_kind="sku", severity="critical", detail="urgent"),
    ]
    snapshot = build_dashboard([_ok_result("stockout_risk_agent", findings=findings)])

    _, explore = build_chart_specs(snapshot)

    assert len(explore) == 1
    chart = explore[0]
    assert chart.chart_id == "stockout_risk_agent-sku"
    assert chart.question == "Which SKUs need attention in Stockout Risk?"
    # Most severe first.
    assert [b.label for b in chart.bars] == ["SKU-CRIT", "SKU-LOW"]
    assert chart.omitted_count == 0


def test_explore_chart_splits_by_subject_kind_when_one_agent_mixes_kinds():
    # risk_detection_agent mixes "period"/"po"/"sku" findings in one response -
    # must become 3 separate charts, not one chart mixing unrelated axes.
    findings = [
        AgentFinding(subject="2025-07", subject_kind="period", severity="high", detail="demand spike"),
        AgentFinding(subject="PO-1", subject_kind="po", severity="medium", detail="delay"),
    ]
    snapshot = build_dashboard([_ok_result("risk_detection_agent", findings=findings)])

    _, explore = build_chart_specs(snapshot)

    chart_ids = {c.chart_id for c in explore}
    assert chart_ids == {"risk_detection_agent-period", "risk_detection_agent-po"}


def test_explore_chart_title_singularizes_category_correctly():
    # Regression: naively stripping a trailing "s" off "categories" gives
    # "categorie", not "category" - the title must use a real singular.
    findings = [AgentFinding(subject="Electronics", subject_kind="category", severity="medium", detail="demand up")]
    snapshot = build_dashboard([_ok_result("demand_forecasting_agent", findings=findings)])

    _, explore = build_chart_specs(snapshot)

    chart = next(c for c in explore if c.chart_id == "demand_forecasting_agent-category")
    assert chart.title == "Demand Forecast by category"
    assert chart.question == "Which categories need attention in Demand Forecast?"


def test_explore_chart_caps_bars_and_reports_the_omitted_count():
    findings = [
        AgentFinding(subject=f"SKU-{i}", subject_kind="sku", severity="low", detail="x") for i in range(20)
    ]
    snapshot = build_dashboard([_ok_result("stockout_risk_agent", findings=findings)])

    _, explore = build_chart_specs(snapshot)

    chart = explore[0]
    assert len(chart.bars) == 12
    assert chart.omitted_count == 8


def test_error_status_metric_produces_no_explore_chart_even_with_findings_field_set():
    snapshot = build_dashboard([_error_result("stockout_risk_agent")])

    _, explore = build_chart_specs(snapshot)

    assert explore == []


def test_render_bar_chart_scales_bars_relative_to_the_largest_value():
    spec = ChartSpec(
        chart_id="x", title="X", question="Q?",
        bars=[ChartBar(label="A", value=10, color="#111"), ChartBar(label="B", value=5, color="#222")],
    )

    svg = render_bar_chart(spec)

    assert svg.startswith("<svg")
    assert svg.count("<rect") == 2
    # The larger bar's width attribute must be exactly double the smaller one's.
    import re

    widths = [float(w) for w in re.findall(r'width="([\d.]+)"', svg)]
    assert widths[0] == widths[1] * 2


def test_render_bar_chart_escapes_a_malicious_label():
    spec = ChartSpec(
        chart_id="x", title="X", question="Q?", bars=[ChartBar(label="<script>alert(1)</script>", value=1, color="#111")],
    )

    svg = render_bar_chart(spec)

    assert "<script>alert" not in svg
    assert "&lt;script&gt;" in svg


def test_render_bar_chart_truncates_an_overlong_label():
    spec = ChartSpec(
        chart_id="x", title="X", question="Q?",
        bars=[ChartBar(label="A" * 50, value=1, color="#111")],
    )

    svg = render_bar_chart(spec)

    assert "A" * 50 not in svg
    assert "…" in svg


def test_render_bar_chart_with_no_bars_renders_a_placeholder_not_broken_svg():
    spec = ChartSpec(chart_id="x", title="X", question="Q?", bars=[])

    assert render_bar_chart(spec) == '<div class="chart-empty">Nothing to chart yet.</div>'
