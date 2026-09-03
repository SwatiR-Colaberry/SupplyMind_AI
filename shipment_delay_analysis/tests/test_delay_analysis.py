import pytest

from shipment_delay_analysis.delay_analysis import (
    DEFAULT_COST_PER_DAY_LATE,
    DelayCostError,
    analyze_shipment_delays,
)


def _row(supplier, po_id, expected, actual, transportation_cost=None):
    row = {"supplier": supplier, "po_id": po_id, "expected_date": expected, "actual_date": actual}
    if transportation_cost is not None:
        row["transportation_cost"] = transportation_cost
    return row


# --- AC1: given shipment data, when analyzed, the system identifies delay patterns ---


def test_analyze_shipment_delays_groups_delays_into_a_per_supplier_pattern():
    rows = [
        _row("Acme", "PO-1", "2025-01-01", "2025-01-06"),  # 5 days late -> high
        _row("Acme", "PO-2", "2025-01-05", "2025-01-11"),  # 6 days late -> critical
        _row("Acme", "PO-3", "2025-01-10", "2025-01-10"),  # on time
    ]

    report = analyze_shipment_delays(rows)

    assert len(report.patterns) == 1
    pattern = report.patterns[0]
    assert pattern.supplier == "Acme"
    assert pattern.delay_count == 2
    assert pattern.total_delay_days == 11
    assert pattern.avg_delay_days == 5.5
    assert pattern.worst_severity == "critical"


def test_analyze_shipment_delays_sorts_patterns_worst_cost_first():
    rows = [
        _row("SmallDelay", "PO-1", "2025-01-01", "2025-01-03"),  # 2 days late -> medium
        _row("BigDelay", "PO-2", "2025-01-01", "2025-01-21"),  # 20 days late -> critical
    ]

    report = analyze_shipment_delays(rows)

    assert [p.supplier for p in report.patterns] == ["BigDelay", "SmallDelay"]


def test_analyze_shipment_delays_groups_delays_with_no_usable_supplier_under_unknown():
    rows = [{"po_id": "PO-1", "expected_date": "2025-01-01", "actual_date": "2025-01-10"}]

    report = analyze_shipment_delays(rows)

    assert report.patterns[0].supplier == "Unknown"


# --- AC2: given shipment delays, when costs are calculated, cost analysis is provided ---


def test_analyze_shipment_delays_calculates_delay_cost_from_the_flat_per_day_rate():
    rows = [_row("Acme", "PO-1", "2025-01-01", "2025-01-06")]  # 5 days late

    report = analyze_shipment_delays(rows, cost_per_day_late=100.0)

    cost = report.delay_costs[0]
    assert cost.delay_days == 5
    assert cost.delay_cost == 500.0
    assert cost.base_transportation_cost == 0.0
    assert cost.total_cost == 500.0
    assert report.total_delay_cost == 500.0
    # No row carried a transportation_cost, so the total base transportation
    # cost is $0 - distinct from total_delay_cost, which is the $500 delay
    # penalty (total_transportation_cost must never double-count delay_cost).
    assert report.total_transportation_cost == 0.0


def test_analyze_shipment_delays_layers_delay_cost_on_top_of_a_rows_own_transportation_cost():
    rows = [_row("Acme", "PO-1", "2025-01-01", "2025-01-06", transportation_cost=750)]  # 5 days late

    report = analyze_shipment_delays(rows, cost_per_day_late=100.0)

    cost = report.delay_costs[0]
    assert cost.base_transportation_cost == 750.0
    assert cost.delay_cost == 500.0
    assert cost.total_cost == 1250.0
    assert report.total_delay_cost == 500.0
    assert report.total_transportation_cost == 750.0


def test_analyze_shipment_delays_uses_the_default_cost_per_day_late_when_not_given():
    rows = [_row("Acme", "PO-1", "2025-01-01", "2025-01-06")]  # 5 days late

    report = analyze_shipment_delays(rows)

    assert report.delay_costs[0].delay_cost == 5 * DEFAULT_COST_PER_DAY_LATE


def test_analyze_shipment_delays_rejects_a_negative_cost_per_day_late():
    with pytest.raises(DelayCostError):
        analyze_shipment_delays([_row("Acme", "PO-1", "2025-01-01", "2025-01-06")], cost_per_day_late=-1.0)


# --- Failure path: cost calculation errors ---


def test_analyze_shipment_delays_flags_an_unusable_transportation_cost_instead_of_raising():
    rows = [_row("Acme", "PO-1", "2025-01-01", "2025-01-06", transportation_cost="not-a-number")]

    report = analyze_shipment_delays(rows)

    assert len(report.cost_errors) == 1
    assert report.cost_errors[0]["po_id"] == "PO-1"
    assert "not a valid number" in report.cost_errors[0]["reason"]
    # the delay is still costed - only the base transportation cost is dropped to $0
    assert report.delay_costs[0].base_transportation_cost == 0.0
    assert report.delay_costs[0].delay_cost > 0
    assert any("invalid transportation_cost" in w for w in report.warnings)


def test_analyze_shipment_delays_flags_a_negative_transportation_cost():
    rows = [_row("Acme", "PO-1", "2025-01-01", "2025-01-06", transportation_cost=-5)]

    report = analyze_shipment_delays(rows)

    assert len(report.cost_errors) == 1
    assert report.delay_costs[0].base_transportation_cost == 0.0


# --- Failure path: incorrect delay analysis (delegated to detect_supplier_delays) ---


def test_analyze_shipment_delays_flags_rows_with_missing_or_invalid_fields_rather_than_dropping_them_silently():
    rows = [
        {"supplier": "Acme", "po_id": "PO-1", "expected_date": "2025-01-01"},  # missing actual_date
        _row("Acme", "PO-2", "2025-01-01", "not-a-date"),
    ]

    report = analyze_shipment_delays(rows)

    assert len(report.flagged_rows) == 2
    assert report.delay_costs == []
    assert report.patterns == []


# --- Boundary cases ---


def test_analyze_shipment_delays_handles_no_rows():
    report = analyze_shipment_delays([])

    assert report.delay_costs == []
    assert report.patterns == []
    assert report.total_delay_cost == 0.0
    assert report.total_transportation_cost == 0.0
    assert "no delivery data provided" in report.warnings


def test_analyze_shipment_delays_handles_no_delays_found():
    rows = [_row("Acme", "PO-1", "2025-01-01", "2025-01-01")]  # on time

    report = analyze_shipment_delays(rows)

    assert report.delay_costs == []
    assert report.patterns == []
    assert report.total_delay_cost == 0.0
