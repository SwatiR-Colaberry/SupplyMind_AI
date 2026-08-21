from __future__ import annotations

from inventory_risk.data_quality import assess_inventory_data_quality


def _row(**overrides) -> dict:
    defaults = dict(sku="SKU-1", current_stock=100.0, safety_stock=20.0, daily_demand_rate=5.0, lead_time_days=10.0)
    defaults.update(overrides)
    return defaults


def test_clean_rows_pass_through_with_no_warnings():
    rows = [_row(), _row(sku="SKU-2")]
    report = assess_inventory_data_quality(rows)
    assert report.clean_rows == rows
    assert report.flagged_rows == []
    assert report.warnings == []


def test_empty_input_warns_no_data():
    report = assess_inventory_data_quality([])
    assert report.total_rows == 0
    assert report.clean_rows == []
    assert report.warnings == ["no inventory data provided"]


def test_missing_required_field_is_flagged():
    rows = [_row(), _row(sku="SKU-2", current_stock=None)]
    report = assess_inventory_data_quality(rows)
    assert len(report.clean_rows) == 1
    assert len(report.flagged_rows) == 1
    assert "missing field(s)" in report.flagged_rows[0].reasons[0]
    assert "current_stock" in report.flagged_rows[0].reasons[0]


def test_entirely_missing_key_is_treated_as_missing_field():
    row = _row(sku="SKU-2")
    del row["lead_time_days"]
    report = assess_inventory_data_quality([row])
    assert len(report.flagged_rows) == 1
    assert "lead_time_days" in report.flagged_rows[0].reasons[0]


def test_non_numeric_value_is_flagged():
    rows = [_row(sku="SKU-2", current_stock="a lot")]
    report = assess_inventory_data_quality(rows)
    assert len(report.flagged_rows) == 1
    assert "not numeric" in report.flagged_rows[0].reasons[0]


def test_boolean_value_is_not_accepted_as_numeric():
    # isinstance(True, int) is True in Python - must be explicitly excluded.
    rows = [_row(sku="SKU-2", daily_demand_rate=True)]
    report = assess_inventory_data_quality(rows)
    assert len(report.flagged_rows) == 1
    assert "not numeric" in report.flagged_rows[0].reasons[0]


def test_negative_current_stock_is_flagged():
    rows = [_row(sku="SKU-2", current_stock=-1.0)]
    report = assess_inventory_data_quality(rows)
    assert len(report.flagged_rows) == 1
    assert "current_stock is negative" in report.flagged_rows[0].reasons[0]


def test_negative_safety_stock_is_flagged():
    rows = [_row(sku="SKU-2", safety_stock=-1.0)]
    report = assess_inventory_data_quality(rows)
    assert "safety_stock is negative" in report.flagged_rows[0].reasons[0]


def test_negative_daily_demand_rate_is_flagged():
    rows = [_row(sku="SKU-2", daily_demand_rate=-1.0)]
    report = assess_inventory_data_quality(rows)
    assert "daily_demand_rate is negative" in report.flagged_rows[0].reasons[0]


def test_non_positive_lead_time_is_flagged():
    for bad_lead_time in (0.0, -5.0):
        rows = [_row(sku="SKU-2", lead_time_days=bad_lead_time)]
        report = assess_inventory_data_quality(rows)
        assert "lead_time_days must be positive" in report.flagged_rows[0].reasons[0]


def test_mixed_clean_and_flagged_rows_are_partitioned_and_warned():
    rows = [_row(sku="SKU-1"), _row(sku="SKU-2", current_stock=-1.0), _row(sku="SKU-3")]
    report = assess_inventory_data_quality(rows)
    assert report.total_rows == 3
    assert [r["sku"] for r in report.clean_rows] == ["SKU-1", "SKU-3"]
    assert len(report.flagged_rows) == 1
    assert report.flagged_rows[0].row["sku"] == "SKU-2"
    assert "1 of 3" in report.warnings[0]


def test_nan_value_is_flagged_not_treated_as_clean():
    # Regression: NaN passes isinstance(value, float) and every `< 0`/`<= 0`
    # comparison against NaN evaluates False, so without an explicit check a
    # NaN value silently reached clean_rows and, downstream, a confident
    # "low risk" classification - the worst possible outcome for corrupted data.
    for field_name in ("current_stock", "safety_stock", "daily_demand_rate", "lead_time_days"):
        rows = [_row(sku="SKU-NAN", **{field_name: float("nan")})]
        report = assess_inventory_data_quality(rows)
        assert report.clean_rows == [], f"{field_name}=NaN should not be treated as clean"
        assert f"{field_name} is NaN" in report.flagged_rows[0].reasons


def test_multiple_issues_on_one_row_are_all_reported():
    rows = [_row(sku="SKU-2", current_stock=-1.0, safety_stock=-1.0)]
    report = assess_inventory_data_quality(rows)
    reasons = report.flagged_rows[0].reasons
    assert len(reasons) == 2
