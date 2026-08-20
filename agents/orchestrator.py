"""Coordinates AI agents and validates their responses.

Receives a supply chain query, dispatches it to the configured agents,
and validates each response against the AgentResponse contract. An
invalid response gets one re-evaluation attempt before being recorded as
a failure. Every coordination step is logged with a timestamp so agent
coordination activity is fully traceable after the fact. Unexpected
errors inside coordination itself (not a single agent's fault) are
caught so a bug here can't crash the caller.
"""

from __future__ import annotations

import concurrent.futures
import uuid
from dataclasses import dataclass, field
from typing import Literal

from agents.base import Agent
from agents.contracts import AgentQuery, AgentResponse, ResponseValidationError, validate_response
from agents.logging_setup import get_logger

logger = get_logger()

MAX_REEVALUATION_ATTEMPTS = 1
DEFAULT_AGENT_TIMEOUT_SECONDS = 10.0
MAX_CONFIDENCE_SPREAD = 0.5


def _detect_data_inconsistency(responses: list[AgentResponse]) -> str | None:
    """Flag when successful agents disagree too widely to present as one answer.

    Agents analyzing the same integrated data should converge to a
    similar confidence level. A wide spread signals the agents likely
    drew from inconsistent underlying data or reached contradictory
    conclusions - surfaced here rather than silently averaged together
    or picked from. Returns None when there's nothing to compare (fewer
    than two confident responses) or the spread is within tolerance.
    """
    confidences = [r.confidence for r in responses if r.status == "ok" and r.confidence is not None]
    if len(confidences) < 2:
        return None
    spread = max(confidences) - min(confidences)
    if spread > MAX_CONFIDENCE_SPREAD:
        return f"confidence spread {spread:.2f} across agents exceeds {MAX_CONFIDENCE_SPREAD}"
    return None


@dataclass
class CoordinationResult:
    agent_name: str
    outcome: Literal["success", "failure"]
    response: AgentResponse | None = None
    error: str | None = None
    reevaluated: bool = False


CoordinationOutcome = Literal["success", "partial", "inconsistent", "crashed"]


@dataclass
class CoordinationRun:
    """The full outcome of one coordinate() call, for callers who need more than logs.

    inconsistency and crash_error surface the data-inconsistency and
    orchestrator-crash failure paths programmatically, so a caller
    reading only this object (never the log stream) still sees them.
    """

    correlation_id: str
    query_text: str
    results: list[CoordinationResult] = field(default_factory=list)
    inconsistency: str | None = None
    crash_error: str | None = None

    @property
    def outcome(self) -> CoordinationOutcome:
        # `is not None`, not truthiness: an exception raised with no
        # message (e.g. `raise ValueError()`) sets crash_error to "",
        # which must still read as "crashed", not fall through silently.
        if self.crash_error is not None:
            return "crashed"
        if self.inconsistency is not None:
            return "inconsistent"
        if self.results and all(r.outcome == "success" for r in self.results):
            return "success"
        return "partial"


class Orchestrator:
    """Coordinates a fixed set of AI agents against a single query."""

    def __init__(self, agents: list[Agent], agent_timeout_seconds: float = DEFAULT_AGENT_TIMEOUT_SECONDS) -> None:
        self._agents = agents
        self._agent_timeout_seconds = agent_timeout_seconds

    def coordinate(self, query: AgentQuery) -> CoordinationRun:
        """Dispatch `query` to every configured agent, validating each response.

        Per-agent failures never reach this method (_run_agent isolates
        them). This method's own try/except exists for the remaining
        case: a bug in the coordination logic itself (e.g. the
        consistency check raising on unexpected input). That's caught
        here, logged as orchestrator_crashed, and returned as a crashed
        CoordinationRun instead of propagating out of coordinate().
        """
        correlation_id = str(uuid.uuid4())
        logger.info(
            "orchestration_started",
            extra={
                "event": "orchestration_started",
                "correlation_id": correlation_id,
                "context": {"query": query.text, "agent_count": len(self._agents)},
            },
        )
        try:
            results = [self._run_agent(agent, query, correlation_id) for agent in self._agents]

            successful_responses = [
                r.response for r in results if r.outcome == "success" and r.response is not None
            ]
            inconsistency = _detect_data_inconsistency(successful_responses)
            if inconsistency:
                logger.warning(
                    "agent_responses_inconsistent",
                    extra={
                        "event": "agent_responses_inconsistent",
                        "outcome": "failure",
                        "correlation_id": correlation_id,
                        "context": {"query": query.text, "detail": inconsistency},
                    },
                )
            run = CoordinationRun(
                correlation_id=correlation_id, query_text=query.text, results=results, inconsistency=inconsistency
            )
        except Exception as exc:
            logger.error(
                "orchestrator_crashed",
                extra={
                    "event": "orchestrator_crashed",
                    "outcome": "failure",
                    "error_class": exc.__class__.__name__,
                    "correlation_id": correlation_id,
                    "context": {"query": query.text},
                },
            )
            run = CoordinationRun(correlation_id=correlation_id, query_text=query.text, crash_error=str(exc))

        logger.info(
            "orchestration_completed",
            extra={
                "event": "orchestration_completed",
                "outcome": run.outcome,
                "correlation_id": correlation_id,
                "context": {"query": query.text, "results": len(run.results)},
            },
        )
        return run

    def _call_agent(self, agent: Agent, query: AgentQuery) -> AgentResponse:
        """Run agent.run(query) off-thread so a hung agent can't block the orchestrator.

        A synchronous Python call can't be forcibly interrupted, so this
        bounds how long the orchestrator *waits* for the agent, not how
        long the agent's thread actually runs. shutdown(wait=False) lets
        the orchestrator move on immediately on timeout instead of
        blocking on a thread that may never finish.
        """
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(agent.run, query)
            return future.result(timeout=self._agent_timeout_seconds)
        finally:
            executor.shutdown(wait=False)

    def _run_agent(self, agent: Agent, query: AgentQuery, correlation_id: str) -> CoordinationResult:
        reevaluated = False
        for attempt in range(MAX_REEVALUATION_ATTEMPTS + 1):
            try:
                raw_response = self._call_agent(agent, query)
            except concurrent.futures.TimeoutError:
                logger.error(
                    "agent_timeout",
                    extra={
                        "event": "agent_timeout",
                        "outcome": "failure",
                        "error_class": "TimeoutError",
                        "correlation_id": correlation_id,
                        "context": {
                            "agent": agent.name,
                            "attempt": attempt,
                            "query": query.text,
                            "timeout_seconds": self._agent_timeout_seconds,
                        },
                    },
                )
                return CoordinationResult(
                    agent_name=agent.name,
                    outcome="failure",
                    error=f"agent timed out after {self._agent_timeout_seconds}s",
                    reevaluated=reevaluated,
                )
            except Exception as exc:
                logger.error(
                    "agent_communication_failed",
                    extra={
                        "event": "agent_communication_failed",
                        "outcome": "failure",
                        "error_class": exc.__class__.__name__,
                        "correlation_id": correlation_id,
                        "context": {"agent": agent.name, "attempt": attempt, "query": query.text},
                    },
                )
                return CoordinationResult(
                    agent_name=agent.name,
                    outcome="failure",
                    error=str(exc),
                    reevaluated=reevaluated,
                )

            try:
                response = validate_response(raw_response)
            except ResponseValidationError as exc:
                logger.warning(
                    "agent_response_invalid",
                    extra={
                        "event": "agent_response_invalid",
                        "outcome": "failure",
                        "error_class": exc.__class__.__name__,
                        "correlation_id": correlation_id,
                        "context": {"agent": agent.name, "attempt": attempt, "query": query.text},
                    },
                )
                reevaluated = True
                continue

            logger.info(
                "agent_response_validated",
                extra={
                    "event": "agent_response_validated",
                    "outcome": "success",
                    "correlation_id": correlation_id,
                    "context": {"agent": agent.name, "attempt": attempt, "query": query.text},
                },
            )
            return CoordinationResult(
                agent_name=agent.name,
                outcome="success",
                response=response,
                reevaluated=reevaluated,
            )

        return CoordinationResult(
            agent_name=agent.name,
            outcome="failure",
            error="agent response still invalid after re-evaluation",
            reevaluated=True,
        )
