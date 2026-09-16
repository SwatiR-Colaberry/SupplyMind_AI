from agents.contracts import AgentFinding, AgentResponse
from agents.orchestrator import CoordinationResult
from dashboard.metrics import build_dashboard, compute_kpi_summary


def _ok_result(agent_name, recommendation="all clear", confidence=0.9, findings=None):
    return CoordinationResult(
        agent_name=agent_name,
        outcome="success",
        response=AgentResponse(
            agent_name=agent_name,
            status="ok",
            recommendation=recommendation,
            confidence=confidence,
            findings=findings or [],
        ),
    )


def _agent_error_result(agent_name, error="no data provided"):
    return CoordinationResult(
        agent_name=agent_name,
        outcome="success",
        response=AgentResponse(agent_name=agent_name, status="error", error=error),
    )


def _orchestration_failure_result(agent_name, error="agent timed out after 10.0s"):
    return CoordinationResult(agent_name=agent_name, outcome="failure", response=None, error=error)


def test_happy_path_builds_one_ok_tile_per_agent_with_known_label():
    results = [
        _ok_result("risk_detection_agent", recommendation="Supply chain risk score 20/100 (low): ..."),
        _ok_result("stockout_risk_agent", recommendation="inventory looks healthy"),
    ]

    snapshot = build_dashboard(results, dashboard_id="dash-1")

    assert snapshot.overall_status == "ok"
    assert snapshot.notification is None
    assert snapshot.data_processing_errors == []
    labels = {m.metric_id: m.label for m in snapshot.metrics}
    assert labels == {"risk_detection_agent": "Supply Chain Risk", "stockout_risk_agent": "Stockout Risk"}
    assert all(m.status == "ok" for m in snapshot.metrics)


def test_unmapped_agent_still_gets_a_tile_using_its_raw_name_as_the_label():
    results = [_ok_result("some_future_agent")]

    snapshot = build_dashboard(results)

    assert snapshot.metrics[0].label == "some_future_agent"
    assert snapshot.overall_status == "ok"


def test_findings_severity_rolls_up_to_the_tile_and_the_snapshot():
    findings = [
        AgentFinding(subject="SKU-1", subject_kind="sku", severity="critical", detail="stockout imminent"),
        AgentFinding(subject="SKU-2", subject_kind="sku", severity="high", detail="low stock"),
        AgentFinding(subject="SKU-3", subject_kind="sku", severity="low", detail="fine"),
    ]
    results = [_ok_result("stockout_risk_agent", findings=findings)]

    snapshot = build_dashboard(results)

    tile = snapshot.metrics[0]
    assert tile.severity == "critical"  # highest of the three
    assert tile.critical_findings == 1
    assert tile.high_findings == 1
    assert snapshot.total_critical_findings == 1
    assert snapshot.total_high_findings == 1


def test_agent_level_error_produces_an_error_tile_and_a_notification():
    """Covers AC2's 'data processing errors' path where the agent itself
    reports the failure (e.g. RiskDetectionAgent's "no data" response) -
    distinct from an orchestration-level failure below."""
    results = [
        _ok_result("stockout_risk_agent"),
        _agent_error_result("risk_detection_agent", error="no supply chain data provided for risk detection"),
    ]

    snapshot = build_dashboard(results)

    assert snapshot.overall_status == "degraded"
    assert snapshot.data_processing_errors == [
        {"agent": "risk_detection_agent", "error": "no supply chain data provided for risk detection"}
    ]
    assert snapshot.notification is not None
    assert "risk_detection_agent" in snapshot.notification
    error_tile = next(m for m in snapshot.metrics if m.metric_id == "risk_detection_agent")
    assert error_tile.status == "error"
    assert error_tile.headline == "no supply chain data provided for risk detection"


def test_orchestration_level_failure_also_produces_an_error_tile():
    results = [_orchestration_failure_result("demand_forecasting_agent", error="agent timed out after 10.0s")]

    snapshot = build_dashboard(results)

    assert snapshot.overall_status == "error"  # the only agent present failed
    tile = snapshot.metrics[0]
    assert tile.status == "error"
    assert tile.headline == "agent timed out after 10.0s"


def test_no_results_at_all_produces_a_clean_empty_snapshot_not_a_crash():
    snapshot = build_dashboard([])

    assert snapshot.metrics == []
    assert snapshot.overall_status == "error"
    assert snapshot.notification == "no agent data available - the dashboard has nothing to display."


def test_every_agent_failing_marks_the_whole_snapshot_error_not_degraded():
    results = [
        _agent_error_result("risk_detection_agent"),
        _orchestration_failure_result("stockout_risk_agent"),
    ]

    snapshot = build_dashboard(results)

    assert snapshot.overall_status == "error"
    assert len(snapshot.data_processing_errors) == 2


def test_build_dashboard_generates_a_dashboard_id_when_none_is_supplied():
    snapshot = build_dashboard([_ok_result("stockout_risk_agent")])

    assert snapshot.dashboard_id  # non-empty, uuid4 by default


def test_metric_carries_the_full_findings_list_not_just_the_severity_counts():
    findings = [
        AgentFinding(subject="SKU-1", subject_kind="sku", severity="critical", detail="stockout imminent"),
        AgentFinding(subject="SKU-2", subject_kind="sku", severity="medium", detail="watch closely"),
    ]

    snapshot = build_dashboard([_ok_result("stockout_risk_agent", findings=findings)])

    assert snapshot.metrics[0].findings == findings
    # critical_findings/high_findings only ever count critical+high - the
    # medium finding above must still survive on the tile itself.
    assert len(snapshot.metrics[0].findings) == 2


def test_error_tile_has_no_findings():
    snapshot = build_dashboard([_agent_error_result("stockout_risk_agent")])

    assert snapshot.metrics[0].findings == []


def test_compute_kpi_summary_sums_metric_value_across_findings():
    findings = [
        AgentFinding(subject="SKU-1", subject_kind="sku", severity="critical", detail="d", metric_value=1000.0),
        AgentFinding(subject="SKU-2", subject_kind="sku", severity="high", detail="d", metric_value=250.0),
    ]
    snapshot = build_dashboard([_ok_result("stockout_risk_agent", findings=findings)])

    kpis = compute_kpi_summary(snapshot)

    assert kpis.total_revenue_at_risk == 1250.0
    assert kpis.total_shipment_delay_cost is None  # that metric isn't in this snapshot at all


def test_compute_kpi_summary_is_none_when_no_finding_carries_a_value():
    # Every SKU assessed, but none has a unit_price - genuinely unknown,
    # not zero, per KPISummary's own "never default to a false midpoint"
    # rule.
    findings = [AgentFinding(subject="SKU-1", subject_kind="sku", severity="low", detail="d", metric_value=None)]
    snapshot = build_dashboard([_ok_result("stockout_risk_agent", findings=findings)])

    assert compute_kpi_summary(snapshot).total_revenue_at_risk is None


def test_compute_kpi_summary_is_zero_when_the_metric_ran_clean_with_no_findings():
    # shipment_delay_analysis_agent with an empty findings list means "ran
    # fine, found zero delays" - a real, computed 0.0, not unknown.
    snapshot = build_dashboard([_ok_result("shipment_delay_analysis_agent", findings=[])])

    assert compute_kpi_summary(snapshot).total_shipment_delay_cost == 0.0


def test_compute_kpi_summary_is_none_when_the_tile_errored():
    snapshot = build_dashboard([_agent_error_result("stockout_risk_agent")])

    assert compute_kpi_summary(snapshot).total_revenue_at_risk is None


def test_compute_kpi_summary_ignores_findings_with_no_metric_value_when_summing_the_rest():
    # A mix of priced and unpriced SKUs must sum only the priced ones,
    # not treat the unpriced SKU's None as a 0 contribution or invalidate
    # the whole sum.
    findings = [
        AgentFinding(subject="SKU-1", subject_kind="sku", severity="critical", detail="d", metric_value=500.0),
        AgentFinding(subject="SKU-2", subject_kind="sku", severity="low", detail="d", metric_value=None),
    ]
    snapshot = build_dashboard([_ok_result("stockout_risk_agent", findings=findings)])

    assert compute_kpi_summary(snapshot).total_revenue_at_risk == 500.0
