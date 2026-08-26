"""Synthesizes multiple AI agents' outputs into actionable recommendations (STORY-006 / REQ-010).

Pure computation - no I/O. Takes the AgentResponse objects other agents
(demand forecasting, stockout risk, risk detection, and any future agent
following the same contract) already produced and validated (see
agents/contracts.py's validate_response()), and combines them into one
RecommendationSet: one Recommendation per agent that succeeded, plus any
conflicts detected between them.

Per CLAUDE.md's core principle ("LLMs are probabilistic, production
systems must be deterministic"), conflict detection is plain
comparison/arithmetic over already-computed, structured data - not
another model call, and not text/NLP parsing of the agents' own
free-text recommendations.

Two kinds of conflict, in order of precision:
- SubjectConflict: two or more agents each reported a finding
  (agents.contracts.AgentFinding) about the exact same subject (the
  same SKU, PO, or period) with a different severity. This is only as
  precise as the agents' own findings are - DemandForecastingAgent
  currently reports no findings at all (it has no per-subject
  breakdown, only an aggregate forecast), so it can never participate
  in a SubjectConflict; see agents/contracts.py's AgentFinding
  docstring for that documented limit.
- ConfidenceConflict: a coarser fallback - agents' overall confidence
  levels spread widely, even when they reported no comparable subject
  at all. Mirrors the same tolerance agents/orchestrator.py's
  _detect_data_inconsistency() already applies at the whole-
  coordination-run level, applied here too because this agent may be
  handed outputs assembled outside a single Orchestrator.coordinate()
  call.
"""

from __future__ import annotations

from dataclasses import dataclass

from agents.contracts import AgentFinding, AgentResponse

DEFAULT_CONFIDENCE_CONFLICT_THRESHOLD = 0.5

_SEVERITY_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2, "critical": 3}


class RecommendationError(ValueError):
    """Raised when there is nothing to synthesize a recommendation from."""


@dataclass(frozen=True)
class Recommendation:
    agent_name: str
    text: str
    confidence: float | None


@dataclass(frozen=True)
class SubjectConflict:
    subject: str
    subject_kind: str
    entries: list[tuple[str, str, str]]  # (agent_name, severity, detail), most severe first

    @property
    def description(self) -> str:
        parts = "; ".join(f"{agent} says {severity} ({detail})" for agent, severity, detail in self.entries)
        return f"conflicting assessments of {self.subject_kind} {self.subject}: {parts}"


@dataclass(frozen=True)
class ConfidenceConflict:
    agent_names: list[str]
    spread: float

    @property
    def description(self) -> str:
        return f"confidence spread {self.spread:.2f} across agents ({', '.join(self.agent_names)}) exceeds tolerance"


@dataclass(frozen=True)
class RecommendationSet:
    recommendations: list[Recommendation]
    subject_conflicts: list[SubjectConflict]
    confidence_conflict: ConfidenceConflict | None
    excluded_agents: list[str]  # names of agents whose output was status="error"

    @property
    def has_conflicts(self) -> bool:
        return bool(self.subject_conflicts) or self.confidence_conflict is not None


def _group_findings_by_subject(
    ok_responses: list[AgentResponse],
) -> dict[tuple[str, str], list[tuple[str, AgentFinding]]]:
    groups: dict[tuple[str, str], list[tuple[str, AgentFinding]]] = {}
    for response in ok_responses:
        for finding in response.findings:
            key = (finding.subject, finding.subject_kind)
            groups.setdefault(key, []).append((response.agent_name, finding))
    return groups


def _subject_conflicts(ok_responses: list[AgentResponse]) -> list[SubjectConflict]:
    conflicts = []
    for (subject, subject_kind), entries in _group_findings_by_subject(ok_responses).items():
        severities = {finding.severity for _, finding in entries}
        if len(severities) < 2:
            continue
        ordered = sorted(entries, key=lambda e: _SEVERITY_RANK.get(e[1].severity, 0), reverse=True)
        conflicts.append(
            SubjectConflict(
                subject=subject,
                subject_kind=subject_kind,
                entries=[(agent, finding.severity, finding.detail) for agent, finding in ordered],
            )
        )
    return sorted(conflicts, key=lambda c: c.subject)


def _confidence_conflict(ok_responses: list[AgentResponse], threshold: float) -> ConfidenceConflict | None:
    scored = [r for r in ok_responses if r.confidence is not None]
    if len(scored) < 2:
        return None
    spread = max(r.confidence for r in scored) - min(r.confidence for r in scored)
    if spread <= threshold:
        return None
    return ConfidenceConflict(agent_names=[r.agent_name for r in scored], spread=spread)


def synthesize_recommendations(
    agent_outputs: list[AgentResponse],
    confidence_conflict_threshold: float = DEFAULT_CONFIDENCE_CONFLICT_THRESHOLD,
) -> RecommendationSet:
    """Combine multiple agents' AgentResponse outputs into one RecommendationSet.

    Handles: any individual agent's output having status="error" -
    excluded from recommendations/conflict detection but does not fail
    the whole run (surfaced via excluded_agents - the "data processing
    errors" failure path for that one agent). Agents with no findings
    (e.g. DemandForecastingAgent) simply can't participate in a
    SubjectConflict - not an error.

    Raises RecommendationError (the caller turns this into an "error"
    AgentResponse - the "recommendation generation failure" failure
    path) when: agent_outputs is empty, or every agent's output has
    status="error" - there is nothing to synthesize a recommendation
    from either way.
    """
    if not agent_outputs:
        raise RecommendationError("no agent outputs provided to synthesize recommendations from")

    ok_responses = [r for r in agent_outputs if r.status == "ok"]
    excluded = [r.agent_name for r in agent_outputs if r.status != "ok"]

    if not ok_responses:
        raise RecommendationError(
            "no successful agent output to synthesize recommendations from "
            f"(all {len(agent_outputs)} agent(s) reported an error)"
        )

    recommendations = [
        Recommendation(agent_name=r.agent_name, text=r.recommendation or "", confidence=r.confidence)
        for r in ok_responses
    ]

    return RecommendationSet(
        recommendations=recommendations,
        subject_conflicts=_subject_conflicts(ok_responses),
        confidence_conflict=_confidence_conflict(ok_responses, confidence_conflict_threshold),
        excluded_agents=excluded,
    )
