"""Contracts between the Orchestrator and AI agents.

AgentResponse is the only currency an agent may hand back to the
Orchestrator. What counts as an "invalid response" for the STORY-002
acceptance criteria is exactly what validate_response() rejects here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

AgentResponseStatus = Literal["ok", "error"]
FindingSubjectKind = Literal["sku", "po", "period"]
FindingSeverity = Literal["low", "medium", "high", "critical"]


@dataclass(frozen=True)
class AgentQuery:
    text: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentFinding:
    """One atomic, per-subject claim an agent can attach to its AgentResponse.

    Exists so a downstream agent (STORY-006's RecommendationAgent) can
    line up findings from different agents that concern the same
    subject (e.g. the same SKU) and detect precise disagreement, instead
    of only being able to compare agents' free-text recommendations.
    Optional - an agent with no natural per-subject breakdown (e.g.
    DemandForecastingAgent, which only forecasts an aggregate total) has
    nothing to add here and leaves AgentResponse.findings empty.
    """

    subject: str  # e.g. "SKU-123", "PO-1003", "2025-07"
    subject_kind: FindingSubjectKind
    severity: FindingSeverity
    detail: str


@dataclass(frozen=True)
class AgentResponse:
    agent_name: str
    status: AgentResponseStatus
    recommendation: str | None = None
    confidence: float | None = None
    error: str | None = None
    findings: list[AgentFinding] = field(default_factory=list)


class ResponseValidationError(ValueError):
    """Raised when an agent's return value does not satisfy the AgentResponse contract."""


def validate_response(response: Any) -> AgentResponse:
    """Validate an agent's return value against the AgentResponse contract.

    Handles: wrong type, an unrecognized status value, an "ok" status
    with no usable recommendation, an out-of-range confidence, an
    "error" status with no error message. Does not handle whether a
    recommendation is factually correct - that is a concern for the
    agent itself, not the contract layer.
    """
    if not isinstance(response, AgentResponse):
        raise ResponseValidationError(f"expected AgentResponse, got {type(response).__name__}")
    if response.status == "ok":
        if not response.recommendation or not response.recommendation.strip():
            raise ResponseValidationError("ok response missing a non-empty recommendation")
        if response.confidence is not None and not (0.0 <= response.confidence <= 1.0):
            raise ResponseValidationError(f"confidence {response.confidence} out of range [0, 1]")
    elif response.status == "error":
        if not response.error:
            raise ResponseValidationError("error response missing an error message")
    else:
        # AgentResponseStatus is a Literal["ok", "error"], but Python
        # doesn't enforce that at runtime - an agent can still hand back
        # any string. Reject it explicitly rather than silently passing
        # a response that satisfies none of this contract's invariants.
        raise ResponseValidationError(f"unknown status '{response.status}'; expected 'ok' or 'error'")
    return response
