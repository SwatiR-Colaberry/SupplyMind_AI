"""Deterministic root cause analysis for supply chain issues (STORY-007 / REQ-013).

Pure computation - no I/O. Does not re-detect anomalies itself; it takes
signals STORY-005/013 already computed (DemandAnomaly, SupplierDelayAnomaly,
SupplierRiskScore) and correlates them against one reported Issue to
propose ranked root-cause candidates.

Per CLAUDE.md's core principle ("LLMs are probabilistic, production
systems must be deterministic"), correlation is plain matching on the
Issue's own as_of_period/supplier fields against the supplied signals'
period/supplier fields, plus a severity-derived confidence - not a model
call, and not free-text/NLP inference over the signals' own detail
strings.

Two outcomes, corresponding to the module's two failure paths:
- RootCauseAnalysisError ("insufficient data" / AC2): raised when none
  of demand_anomalies/supplier_delays/supplier_scores was supplied at
  all - there is nothing whatsoever to correlate the issue against.
- A completed RootCauseAnalysis with an empty candidate list ("data was
  supplied, but nothing in it correlates with this issue"): this is a
  genuine finding, not a data-insufficiency notice - it means the
  available data was checked and no cause was found in it, which is
  different information than "we didn't have enough data to check."
  Conflating the two would make a confident "no known cause" read as a
  vague "insufficient data" warning, or vice versa.

Guards against one specific "incorrect causal inference" failure mode
(one of STORY-007's named failure paths): a period/PO-level signal is
never allowed to serve as its own cause. If the issue itself *is* the
anomaly (e.g. the issue is period "2025-04" and a DemandAnomaly for
"2025-04" is in the supplied signals, or the issue is PO "PO-1" and a
SupplierDelayAnomaly for "PO-1" is supplied), that signal is excluded
from candidate evidence - see _is_self_reference(). This guard does not
apply to supplier-reliability evidence: unlike a period or PO, a
supplier has no finer-grained identity for this candidate to collapse
into, and a supplier's own SupplierRiskScore decomposes into concrete
delivery-level facts (not a bare restatement) - see
_supplier_reliability_candidate()'s own comment for why excluding it
would make a supplier-kind issue about a flagged supplier unexplainable.
"""

from __future__ import annotations

from dataclasses import dataclass

from agents.contracts import FindingSubjectKind
from risk_detection.anomaly_detection import DemandAnomaly, SupplierDelayAnomaly
from supplier_evaluation.reliability import SupplierRiskScore

# Deterministic severity -> confidence mapping, shared by every candidate
# type below so a "critical" demand spike and a "critical" supplier delay
# contribute the same confidence for the same strength of signal. Not
# calibrated against real outcome data (none exists yet) - a documented,
# reviewable assumption rather than a hidden magic number, same posture
# inventory_risk/risk_model.py's own zone thresholds already take.
#
# Includes "low" even though DemandAnomaly/SupplierDelayAnomaly severities
# never reach it (AnomalySeverity = "medium"|"high"|"critical" only) -
# SupplierRiskScore.severity (RiskSeverity) can be "low" while still
# flagged_for_review (e.g. too few deliveries for a confident score, see
# supplier_evaluation/reliability.py). Omitting it would silently score a
# genuinely flagged supplier-reliability candidate at 0.0 confidence,
# indistinguishable from "no candidate found."
_SEVERITY_CONFIDENCE: dict[str, float] = {"low": 0.4, "medium": 0.55, "high": 0.7, "critical": 0.85}

CauseKind = str  # "demand_spike" | "supplier_delay" | "supplier_reliability"


class RootCauseAnalysisError(ValueError):
    """Raised when no signal data at all was supplied to analyze the issue against.

    This is the "insufficient data" failure path (AC2) - the caller
    converts this into a limitations notice for the user, rather than a
    generic analysis failure.
    """


@dataclass(frozen=True)
class Issue:
    """The supply chain issue being investigated.

    as_of_period and supplier are both optional correlation keys, not
    required identifiers - a caller who only knows one of them (e.g. a
    stockout on a SKU, but not which supplier restocks it) still gets
    whatever correlation is possible from what they do know, rather than
    being forced to supply data they don't have.
    """

    subject: str
    subject_kind: FindingSubjectKind
    as_of_period: str | None = None
    supplier: str | None = None


@dataclass(frozen=True)
class RootCauseCandidate:
    cause: CauseKind
    confidence: float  # 0..1
    detail: str
    evidence_subject: str  # the period/po/supplier the correlated signal came from


@dataclass(frozen=True)
class RootCauseAnalysis:
    issue: Issue
    candidates: list[RootCauseCandidate]  # sorted most confident first; [] if nothing correlated
    confidence: float  # 0.0 if candidates is empty; else the top candidate's confidence
    note: str


def _is_self_reference(issue: Issue, evidence_kind: FindingSubjectKind, evidence_subject: str) -> bool:
    return issue.subject_kind == evidence_kind and issue.subject == evidence_subject


def _demand_spike_candidate(issue: Issue, demand_anomalies: list[DemandAnomaly]) -> RootCauseCandidate | None:
    if issue.as_of_period is None:
        return None
    matches = [
        a
        for a in demand_anomalies
        if a.period == issue.as_of_period
        and a.direction == "spike"
        and not _is_self_reference(issue, "period", a.period)
    ]
    if not matches:
        return None
    strongest = max(matches, key=lambda a: _SEVERITY_CONFIDENCE.get(a.severity, 0.0))
    return RootCauseCandidate(
        cause="demand_spike",
        confidence=_SEVERITY_CONFIDENCE.get(strongest.severity, 0.0),
        detail=f"demand spike in {strongest.period} ({strongest.detail}) likely depleted available stock",
        evidence_subject=strongest.period,
    )


def _supplier_delay_candidate(
    issue: Issue, supplier_delays: list[SupplierDelayAnomaly]
) -> RootCauseCandidate | None:
    if issue.supplier is None:
        return None
    matches = [
        d
        for d in supplier_delays
        if d.supplier == issue.supplier and not _is_self_reference(issue, "po", d.po_id)
    ]
    if not matches:
        return None
    strongest = max(matches, key=lambda d: _SEVERITY_CONFIDENCE.get(d.severity, 0.0))
    return RootCauseCandidate(
        cause="supplier_delay",
        confidence=_SEVERITY_CONFIDENCE.get(strongest.severity, 0.0),
        detail=f"delayed delivery from {issue.supplier} on {strongest.po_id} ({strongest.detail})",
        evidence_subject=strongest.po_id,
    )


def _supplier_reliability_candidate(
    issue: Issue, supplier_scores: list[SupplierRiskScore]
) -> RootCauseCandidate | None:
    # No self-reference guard here, unlike the other two candidates - for
    # a period/po issue, "the same period/po" is the finest-grained
    # identity that evidence type has, so excluding it prevents a finding
    # from citing itself. "Supplier" has no finer-grained identity below
    # the supplier name itself, so the same guard would always exclude
    # the one candidate a supplier-kind issue (e.g. "why is Acme flagged")
    # most needs: Acme's own SupplierRiskScore, whose `explanation`/
    # flag_reasons decompose the aggregate flag into concrete delivery-
    # level facts (e.g. "6 of 10 deliveries delayed") rather than merely
    # restating "flagged=true" - a genuine explanation, not a circular one.
    if issue.supplier is None:
        return None
    matches = [s for s in supplier_scores if s.supplier == issue.supplier and s.flagged_for_review]
    if not matches:
        return None
    strongest = max(matches, key=lambda s: _SEVERITY_CONFIDENCE.get(s.severity, 0.0))
    return RootCauseCandidate(
        cause="supplier_reliability",
        confidence=_SEVERITY_CONFIDENCE.get(strongest.severity, 0.0),
        detail=f"{issue.supplier} has a flagged reliability history ({strongest.explanation})",
        evidence_subject=strongest.supplier,
    )


def analyze_root_cause(
    issue: Issue,
    *,
    demand_anomalies: list[DemandAnomaly] | None = None,
    supplier_delays: list[SupplierDelayAnomaly] | None = None,
    supplier_scores: list[SupplierRiskScore] | None = None,
) -> RootCauseAnalysis:
    """Correlate `issue` against already-computed anomaly/reliability signals.

    Raises RootCauseAnalysisError when demand_anomalies, supplier_delays,
    and supplier_scores are all empty/None - see module docstring.
    """
    demand_anomalies = demand_anomalies or []
    supplier_delays = supplier_delays or []
    supplier_scores = supplier_scores or []

    if not demand_anomalies and not supplier_delays and not supplier_scores:
        raise RootCauseAnalysisError(
            f"no anomaly or reliability data supplied to analyze issue "
            f"{issue.subject_kind} {issue.subject!r} against"
        )

    candidates = [
        c
        for c in (
            _demand_spike_candidate(issue, demand_anomalies),
            _supplier_delay_candidate(issue, supplier_delays),
            _supplier_reliability_candidate(issue, supplier_scores),
        )
        if c is not None
    ]
    candidates.sort(key=lambda c: c.confidence, reverse=True)

    if not candidates:
        return RootCauseAnalysis(
            issue=issue,
            candidates=[],
            confidence=0.0,
            note="no correlated cause found in the available data",
        )

    return RootCauseAnalysis(
        issue=issue,
        candidates=candidates,
        confidence=candidates[0].confidence,
        note=f"{len(candidates)} candidate cause(s) found",
    )
