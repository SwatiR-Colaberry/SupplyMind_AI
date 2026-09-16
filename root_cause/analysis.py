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

import datetime
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


# --- causal chains -------------------------------------------------
#
# analyze_root_cause() above finds each candidate cause independently -
# "here are up to 3 plausible explanations." The product spec's own
# example ("Demand up -> Supplier delay up -> Inventory down -> Stockout
# risk up") asks for something more specific: a *chain*, where one
# candidate is shown to plausibly have caused another rather than the two
# just happening to both be true. build_causal_chain() below is additive
# on top of an already-computed RootCauseAnalysis - it never changes what
# analyze_root_cause() itself returns - and only chains two steps
# together when a real temporal link supports it (see
# _period_plausibly_precedes()); otherwise it degrades to the single
# strongest candidate, which is still a fully valid answer on its own.

CausalStepKind = str  # "demand_spike" | "supplier_delay" | "supplier_reliability" | "issue"

# Max months a demand spike is considered a plausible antecedent of a
# later supplier delay - a spike from 6 months before a delay is likely
# unrelated; the spike's own month through 2 months before the delay's
# expected date is a defensible window for "this probably strained the
# supply chain enough to contribute." Configurable, not a universal
# constant, same posture as inventory_risk/risk_model.py's own thresholds.
MAX_ANTECEDENT_MONTHS = 2

_STEP_ARROW_LABEL: dict[CausalStepKind, str] = {
    "demand_spike": "Demand ↑",
    "supplier_delay": "Supplier delay ↑",
    "supplier_reliability": "Supplier reliability risk ↑",
}


@dataclass(frozen=True)
class CausalStep:
    cause: CausalStepKind
    subject: str  # the period/po/supplier/issue-subject this step is about
    detail: str
    confidence: float  # 1.0 for the terminal "issue" step, which isn't itself a prediction


@dataclass(frozen=True)
class RootCauseChain:
    issue: Issue
    steps: list[CausalStep]  # ordered earliest cause first; the issue itself is always last
    confidence: float  # the weakest non-issue link's confidence; 0.0 if no cause was found at all
    note: str


def _period_plausibly_precedes(period: str, date_str: str, max_months_before: int = MAX_ANTECEDENT_MONTHS) -> bool:
    """True when `period` ("YYYY-MM") falls in the same month as `date_str`
    ("YYYY-MM-DD") or up to `max_months_before` months earlier - a cheap,
    explainable proxy for "could plausibly have caused this," not a claim
    of proven causation. Returns False for any unparseable input rather
    than raising - a malformed date on one candidate shouldn't break
    chain-building for every other issue."""
    try:
        period_year, period_month = (int(p) for p in period.split("-", 1))
        date = datetime.date.fromisoformat(date_str)
    except (ValueError, AttributeError, TypeError):
        return False
    period_ordinal = period_year * 12 + period_month
    date_ordinal = date.year * 12 + date.month
    return 0 <= (date_ordinal - period_ordinal) <= max_months_before


def build_causal_chain(
    analysis: RootCauseAnalysis,
    demand_anomalies: list[DemandAnomaly] | None = None,
    supplier_delays: list[SupplierDelayAnomaly] | None = None,
) -> RootCauseChain:
    """Builds a multi-step causal narrative from an already-computed
    RootCauseAnalysis, chaining a supplier_delay candidate back to a
    demand_spike that plausibly preceded it in time (see
    _period_plausibly_precedes) - e.g. "Demand spike in 2025-06 ->
    delayed delivery on PO-1003 -> stockout risk for SKU-42." With no
    temporally-plausible antecedent (or no demand_anomalies supplied at
    all), the chain is just the single strongest candidate - a fully
    valid, honest answer on its own, not a failure.

    Takes the already-computed analysis rather than re-deriving one
    itself, so a caller that already ran analyze_root_cause() (e.g.
    agents/root_cause_agent.py, which also needs the plain candidate list
    for its own recommendation text) never pays for, or risks diverging
    from, a second, redundant computation.
    """
    issue = analysis.issue
    issue_step = CausalStep(
        cause="issue", subject=issue.subject, detail=f"{issue.subject_kind} {issue.subject} flagged", confidence=1.0
    )

    if not analysis.candidates:
        return RootCauseChain(issue=issue, steps=[issue_step], confidence=0.0, note=analysis.note)

    top = analysis.candidates[0]
    steps = [CausalStep(cause=top.cause, subject=top.evidence_subject, detail=top.detail, confidence=top.confidence)]

    if top.cause == "supplier_delay":
        delay = next((d for d in (supplier_delays or []) if d.po_id == top.evidence_subject), None)
        antecedent = None
        if delay is not None:
            candidates = [
                a
                for a in (demand_anomalies or [])
                if a.direction == "spike" and _period_plausibly_precedes(a.period, delay.expected_date)
            ]
            antecedent = max(candidates, key=lambda a: _SEVERITY_CONFIDENCE.get(a.severity, 0.0), default=None)
        if antecedent is not None:
            steps.insert(
                0,
                CausalStep(
                    cause="demand_spike",
                    subject=antecedent.period,
                    detail=f"demand spike in {antecedent.period} ({antecedent.detail}) likely strained supply and contributed to the delay",
                    confidence=_SEVERITY_CONFIDENCE.get(antecedent.severity, 0.0),
                ),
            )

    steps.append(issue_step)
    chain_confidence = min(s.confidence for s in steps[:-1])
    note = f"{len(steps) - 1}-step causal chain" if len(steps) > 2 else analysis.note
    return RootCauseChain(issue=issue, steps=steps, confidence=chain_confidence, note=note)


def describe_chain(chain: RootCauseChain) -> str:
    """Renders a RootCauseChain as one arrow-joined line, e.g.
    "Demand ↑ (2025-06) -> Supplier delay ↑ (PO-1003) -> Stockout risk ↑ (SKU-42)"
    - the exact narrative shape the product spec's root-cause example asks for.
    """
    labels = [f"{_STEP_ARROW_LABEL.get(step.cause, step.cause)} ({step.subject})" for step in chain.steps[:-1]]
    issue = chain.issue
    issue_label = "Stockout risk" if issue.subject_kind == "sku" else f"{issue.subject_kind.capitalize()} risk"
    labels.append(f"{issue_label} ↑ ({issue.subject})")
    return " → ".join(labels)
