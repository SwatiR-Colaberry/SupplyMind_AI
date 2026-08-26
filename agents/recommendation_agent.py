"""Recommendation generation agent (STORY-006 / REQ-010).

Wraps recommendation/synthesis.py (pure, deterministic computation) as an
Agent so it plugs into the existing Orchestrator (STORY-002) without any
changes to orchestration logic. Unlike the other agents in this repo,
its input isn't raw external data - it's the AgentResponse outputs other
agents (demand forecasting, stockout risk, risk detection, and any
future agent following the same contract) already produced and had
validated. A typical caller runs the Orchestrator once to gather those
outputs, then calls this agent (directly, or via a second
Orchestrator.coordinate() pass) with them as context.

Query contract (via AgentQuery.context):
    "agent_outputs": list[AgentResponse] - the already-validated outputs
        of other agents. Required, and must contain only AgentResponse
        instances (this is an internal, agent-to-agent boundary, not an
        external one - a caller assembling this list wrong is a genuine
        "data processing error", not a hostile input to sanitize).

Of the failure paths this story names, two don't apply to this agent
and are noted here rather than silently skipped: "Recommendation API
failure" (there is no external recommendation API - this agent, like
every other agent in this repo, is pure Python computation, per
CLAUDE.md's "production systems must be deterministic" principle) and
"User interface display issues" (this agent has no UI of its own; the
logged recommendations it produces are what a UI would render from, not
something this agent renders itself).
"""

from __future__ import annotations

from typing import Any

from agents.contracts import AgentQuery, AgentResponse
from agents.logging_setup import get_logger
from recommendation.synthesis import (
    ConfidenceConflict,
    Recommendation,
    RecommendationError,
    RecommendationSet,
    SubjectConflict,
    synthesize_recommendations,
)

logger = get_logger()

# Applied when at least one conflict was detected, so a caller can't
# mistake a synthesized-but-disputed recommendation for a clean one.
# Mirrors the shape (not the exact numbers) of the confidence-lowering
# convention RiskDetectionAgent already uses for its own "Data quality
# notes".
CONFIDENCE_PENALTY_PER_CONFLICT = 0.15
MIN_CONFIDENCE_WITH_CONFLICTS = 0.3


class RecommendationAgent:
    name = "recommendation_agent"

    def run(self, query: AgentQuery) -> AgentResponse:
        """Synthesize other agents' outputs into one actionable recommendation.

        Handles (returns status="error" for, rather than raising - a
        raised exception here would surface in the Orchestrator as
        "agent_communication_failed", the wrong classification for a
        data problem the caller can act on):
        - no agent_outputs provided, or every one of them status="error"
          ("recommendation generation failure" failure path - nothing to
          synthesize)
        - agent_outputs containing something other than an AgentResponse
          ("data processing errors" failure path - a malformed input to
          this internal boundary, not a single agent's fault)

        Handles (included in the "ok" response rather than as an error -
        the "conflicting agent outputs" acceptance criterion asks for
        recommendations generated AND conflicts highlighted, not one or
        the other): one or more agents reporting a genuine conflict
        (same subject, different severity, or a wide confidence spread).

        Any other, truly unexpected exception is left to propagate -
        the Orchestrator already has a dedicated, tested path for an
        agent raising (agent_communication_failed, isolated per-agent),
        so this agent does not duplicate that handling.
        """
        raw_outputs: Any = query.context.get("agent_outputs")
        try:
            # Materialized once, not iterated twice - raw_outputs may be a
            # generator/iterator (e.g. a caller writing a genexpr instead
            # of a list comp), which the old two-pass validate-then-use
            # code silently exhausted on the first pass, making every
            # agent output vanish before synthesis ever saw them.
            outputs: list[Any] = list(raw_outputs) if raw_outputs else []
        except TypeError:
            return self._error_response(
                "agent_outputs must be an iterable of AgentResponse objects", error_class="ValueError"
            )

        if not all(isinstance(o, AgentResponse) for o in outputs):
            return self._error_response(
                "agent_outputs must contain only AgentResponse objects", error_class="ValueError"
            )

        try:
            result = synthesize_recommendations(outputs)
        except RecommendationError as exc:
            return self._error_response(str(exc), error_class="RecommendationError")

        self._log_recommendations(result.recommendations)
        self._log_conflicts(result.subject_conflicts, result.confidence_conflict)

        return AgentResponse(
            agent_name=self.name,
            status="ok",
            recommendation=self._format_recommendation(result),
            confidence=self._confidence(result),
        )

    def _log_recommendations(self, recommendations: list[Recommendation]) -> None:
        for rec in recommendations:
            logger.info(
                "recommendation_generated",
                extra={
                    "event": "recommendation_generated",
                    "outcome": "success",
                    "context": {
                        "source_agent": rec.agent_name,
                        "confidence": rec.confidence,
                        "recommendation": rec.text,
                    },
                },
            )

    def _log_conflicts(
        self, subject_conflicts: list[SubjectConflict], confidence_conflict: ConfidenceConflict | None
    ) -> None:
        for conflict in subject_conflicts:
            logger.warning(
                "recommendation_conflict_detected",
                extra={
                    "event": "recommendation_conflict_detected",
                    "outcome": "failure",
                    "error_class": "ConflictingAgentOutputs",
                    "context": {
                        "subject": conflict.subject,
                        "subject_kind": conflict.subject_kind,
                        "entries": [
                            {"agent_name": agent, "severity": severity, "detail": detail}
                            for agent, severity, detail in conflict.entries
                        ],
                    },
                },
            )
        if confidence_conflict:
            logger.warning(
                "recommendation_confidence_conflict_detected",
                extra={
                    "event": "recommendation_confidence_conflict_detected",
                    "outcome": "failure",
                    "error_class": "ConflictingAgentOutputs",
                    "context": {
                        "agent_names": confidence_conflict.agent_names,
                        "spread": round(confidence_conflict.spread, 2),
                    },
                },
            )

    def _error_response(self, message: str, error_class: str) -> AgentResponse:
        logger.warning(
            "recommendation_generation_failed",
            extra={
                "event": "recommendation_generation_failed",
                "outcome": "failure",
                "error_class": error_class,
                "context": {"detail": message},
            },
        )
        return AgentResponse(agent_name=self.name, status="error", error=message)

    @staticmethod
    def _format_recommendation(result: RecommendationSet) -> str:
        parts = "; ".join(f"{r.agent_name}: {r.text}" for r in result.recommendations)
        summary = f"Recommendations ({len(result.recommendations)} agent(s)): {parts}"
        if result.excluded_agents:
            summary += " | Excluded (agent error): " + ", ".join(result.excluded_agents)

        conflict_descriptions = [c.description for c in result.subject_conflicts]
        if result.confidence_conflict:
            conflict_descriptions.append(result.confidence_conflict.description)
        if conflict_descriptions:
            summary += " | CONFLICTS DETECTED: " + "; ".join(conflict_descriptions)
        return summary

    @staticmethod
    def _confidence(result: RecommendationSet) -> float:
        scored = [r.confidence for r in result.recommendations if r.confidence is not None]
        base = sum(scored) / len(scored) if scored else 1.0

        num_conflicts = len(result.subject_conflicts) + (1 if result.confidence_conflict else 0)
        if num_conflicts:
            base = max(MIN_CONFIDENCE_WITH_CONFLICTS, base - CONFIDENCE_PENALTY_PER_CONFLICT * num_conflicts)
        return base
