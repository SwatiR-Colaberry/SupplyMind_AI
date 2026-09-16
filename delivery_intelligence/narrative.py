"""Cross-agent delivery-intelligence narrative (product spec's "Delivery Intelligence" feature).

Pure computation - no I/O. Given stockout_risk_agent's own per-SKU
findings (each optionally carrying which supplier that SKU is sourced
from - see agents/contracts.py's AgentFinding.supplier) and
supplier_evaluation_agent's own per-supplier findings, answers the
*reverse* direction neither agent can answer on its own: "this
supplier's own delivery-reliability problem could affect N products
currently at elevated stockout risk" - not "how risky is one SKU" or
"how reliable is one supplier" in isolation.

Deliberately its own top-level package rather than folded into
root_cause/ (STORY-013's causal-chain analysis, which explains *why* one
already-named subject is at risk): this module answers the reverse
question - which subjects does *this* supplier's own problem put at
risk - a different direction of correlation, not a rewording of
root_cause's existing per-subject one.
"""

from __future__ import annotations

from dataclasses import dataclass

from agents.contracts import AgentFinding, FindingSeverity

# A supplier with no reliability problem of its own ("low" severity) has
# nothing to narrate - correlating a healthy supplier against its SKUs
# would just be noise, the same "an agent with no natural per-subject
# breakdown has nothing to add" precedent AgentFinding's own docstring
# already states. Likewise a SKU whose own stockout severity is "low" is
# not "affected" by anything, even if its supplier is flagged.
_FLAGGED_SEVERITIES: frozenset[str] = frozenset({"medium", "high", "critical"})


@dataclass(frozen=True)
class SupplierImpactNarrative:
    supplier: str
    severity: FindingSeverity  # carried from the supplier's own supplier_evaluation_agent finding
    affected_skus: list[str]  # every SKU sourced from `supplier` that is itself at elevated stockout risk
    detail: str


def build_supplier_impact_narratives(
    stockout_findings: list[AgentFinding],
    supplier_findings: list[AgentFinding],
) -> list[SupplierImpactNarrative]:
    """One SupplierImpactNarrative per supplier that is both (a) flagged by
    supplier_evaluation_agent's own findings and (b) linked, via
    AgentFinding.supplier, to at least one SKU that is itself at elevated
    stockout risk.

    Handles: a stockout finding with no supplier attached (the optional
    inventory "supplier" column isn't mapped yet) - excluded from every
    narrative, not treated as a match for a flagged supplier with no
    name. A supplier finding whose own severity is "low" - no narrative
    (see _FLAGGED_SEVERITIES above); a healthy supplier's SKUs are not
    "affected" by anything. A SKU whose own stockout severity is "low" -
    excluded from affected_skus; this correlation is specifically about
    products *currently* at elevated risk, not every product a flagged
    supplier happens to source.

    Returns [] when no supplier is both flagged and linked to an at-risk
    SKU - the honest "nothing to correlate yet" case (supplier isn't
    mapped on inventory rows at all, or a flagged supplier's SKUs are
    all currently healthy), not a fabricated narrative.
    """
    skus_by_supplier: dict[str, list[str]] = {}
    for finding in stockout_findings:
        if finding.supplier is None or finding.severity not in _FLAGGED_SEVERITIES:
            continue
        skus_by_supplier.setdefault(finding.supplier, []).append(finding.subject)

    narratives: list[SupplierImpactNarrative] = []
    for finding in supplier_findings:
        if finding.severity not in _FLAGGED_SEVERITIES:
            continue
        affected = sorted(skus_by_supplier.get(finding.subject, []))
        if not affected:
            continue
        detail = (
            f"{finding.subject}'s delivery reliability issue ({finding.detail}) could affect "
            f"{len(affected)} product(s) currently at elevated stockout risk: {', '.join(affected)}"
        )
        narratives.append(
            SupplierImpactNarrative(
                supplier=finding.subject, severity=finding.severity, affected_skus=affected, detail=detail
            )
        )
    return narratives
