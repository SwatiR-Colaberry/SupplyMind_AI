"""A trivial agent for the STORY-002 walking skeleton.

Always returns a fixed, valid recommendation so the Orchestrator's
coordination and validation logic can be exercised end-to-end before any
real analysis agent exists. Real agents (demand forecasting, stockout
risk) land in later stories.
"""

from __future__ import annotations

from agents.contracts import AgentQuery, AgentResponse


class StubAnalysisAgent:
    name = "stub_analysis_agent"

    def run(self, query: AgentQuery) -> AgentResponse:
        return AgentResponse(
            agent_name=self.name,
            status="ok",
            recommendation=f"Sample recommendation for: {query.text}",
            confidence=0.5,
        )
