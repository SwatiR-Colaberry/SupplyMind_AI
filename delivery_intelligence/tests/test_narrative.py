from __future__ import annotations

from agents.contracts import AgentFinding
from delivery_intelligence.narrative import build_supplier_impact_narratives


def _stockout(sku: str, severity: str, supplier: str | None) -> AgentFinding:
    return AgentFinding(subject=sku, subject_kind="sku", severity=severity, detail=f"{sku} detail", supplier=supplier)


def _supplier(name: str, severity: str) -> AgentFinding:
    return AgentFinding(subject=name, subject_kind="supplier", severity=severity, detail=f"{name} is unreliable")


def test_returns_empty_when_no_stockout_finding_has_a_supplier():
    stockout_findings = [_stockout("SKU-1", "critical", supplier=None)]
    supplier_findings = [_supplier("Acme", "high")]

    result = build_supplier_impact_narratives(stockout_findings, supplier_findings)

    assert result == []


def test_returns_empty_when_the_linked_supplier_is_not_flagged():
    stockout_findings = [_stockout("SKU-1", "critical", supplier="Acme")]
    supplier_findings = [_supplier("Acme", "low")]

    result = build_supplier_impact_narratives(stockout_findings, supplier_findings)

    assert result == []


def test_returns_empty_when_the_flagged_suppliers_skus_are_all_healthy():
    stockout_findings = [_stockout("SKU-1", "low", supplier="Acme")]
    supplier_findings = [_supplier("Acme", "critical")]

    result = build_supplier_impact_narratives(stockout_findings, supplier_findings)

    assert result == []


def test_builds_one_narrative_per_flagged_supplier_with_at_risk_skus():
    stockout_findings = [
        _stockout("SKU-1", "critical", supplier="Acme"),
        _stockout("SKU-2", "medium", supplier="Acme"),
        _stockout("SKU-3", "low", supplier="Acme"),  # healthy - excluded
        _stockout("SKU-4", "high", supplier=None),  # no supplier link - excluded
    ]
    supplier_findings = [_supplier("Acme", "high")]

    result = build_supplier_impact_narratives(stockout_findings, supplier_findings)

    assert len(result) == 1
    narrative = result[0]
    assert narrative.supplier == "Acme"
    assert narrative.severity == "high"
    assert narrative.affected_skus == ["SKU-1", "SKU-2"]
    assert "2 product(s)" in narrative.detail
    assert "SKU-1" in narrative.detail and "SKU-2" in narrative.detail


def test_builds_a_separate_narrative_per_flagged_supplier():
    stockout_findings = [
        _stockout("SKU-1", "critical", supplier="Acme"),
        _stockout("SKU-2", "high", supplier="Globex"),
    ]
    supplier_findings = [_supplier("Acme", "critical"), _supplier("Globex", "medium")]

    result = build_supplier_impact_narratives(stockout_findings, supplier_findings)

    assert {n.supplier for n in result} == {"Acme", "Globex"}


def test_returns_empty_for_no_findings_at_all():
    assert build_supplier_impact_narratives([], []) == []
