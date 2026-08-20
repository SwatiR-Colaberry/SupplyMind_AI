"""Interface AI agents implement to be coordinated by the Orchestrator."""

from __future__ import annotations

from typing import Protocol

from agents.contracts import AgentQuery, AgentResponse


class Agent(Protocol):
    name: str

    def run(self, query: AgentQuery) -> AgentResponse: ...
