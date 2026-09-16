"""Delivery Intelligence agent (product spec's "Delivery Intelligence" feature).

Wraps delivery_intelligence/narrative.py (pure computation) as an Agent
so it plugs into the existing Orchestrator (STORY-002) without any
changes to orchestration logic. Unlike most agents in this repo, and
like RecommendationAgent (STORY-006), this agent's input isn't raw
external data - it's other agents' own already-validated AgentResponse
outputs (specifically stockout_risk_agent's and
supplier_evaluation_agent's), so a caller runs the Orchestrator's stage 1
first, then calls this agent (directly, or via a second
Orchestrator.coordinate() pass) with those outputs as context - the same
two-stage shape dashboard/live_refresh.py's own _stage1_results/
_stage2_results already use for RecommendationAgent.

Query contract (via AgentQuery.context):
    "agent_outputs": list[AgentResponse] - the already-validated outputs
        of other agents, same contract RecommendationAgent's own
        "agent_outputs" uses. Required. This agent reads only
        stockout_risk_agent's and supplier_evaluation_agent's own
        findings out of that list - any other agent's output is simply
        not relevant to this correlation and is ignored, not an error.

No findings on this agent's own AgentResponse (mirrors
RecommendationAgent, which also has none) - each SupplierImpactNarrative
already names its own affected SKUs in `detail`; there is no further
per-subject breakdown a downstream consumer needs beyond that text.
"""

from __future__ import annotations

from typing import Any

from agents.contracts import AgentFinding, AgentQuery, AgentResponse
from agents.logging_setup import get_logger
from delivery_intelligence.narrative import SupplierImpactNarrative, build_supplier_impact_narratives

logger = get_logger()

_STOCKOUT_AGENT_NAME = "stockout_risk_agent"
_SUPPLIER_AGENT_NAME = "supplier_evaluation_agent"


class DeliveryIntelligenceAgent:
    name = "delivery_intelligence_agent"

    def run(self, query: AgentQuery) -> AgentResponse:
        """Correlate stockout_risk_agent's and supplier_evaluation_agent's own outputs.

        Handles (returns status="ok" with an explanatory recommendation
        and confidence=None, not an error - this agent has nothing to
        add yet, not a malfunction):
        - stockout_risk_agent or supplier_evaluation_agent missing from
          agent_outputs, or either one reporting status="error"
        - both present and "ok", but no SKU has a supplier attached yet
          (the optional inventory "supplier" field isn't mapped)

        Handles (returns status="ok" with confidence=1.0 - a genuine,
        confident answer either way):
        - both present, mapped, but no flagged supplier's SKUs are
          currently at elevated risk ("no supplier-driven stockout risk
          detected" is itself a real, checked answer)
        - one or more real supplier-impact narratives found

        Handles (returns status="error" for): agent_outputs missing
        entirely, or containing something other than an AgentResponse -
        the same "data processing errors" failure path
        RecommendationAgent's own agent_outputs validation uses.
        """
        raw_outputs: Any = query.context.get("agent_outputs")
        try:
            outputs: list[Any] = list(raw_outputs) if raw_outputs else []
        except TypeError:
            return self._error_response("agent_outputs must be an iterable of AgentResponse objects")
        if not all(isinstance(o, AgentResponse) for o in outputs):
            return self._error_response("agent_outputs must contain only AgentResponse objects")

        stockout_findings = self._findings_from(outputs, _STOCKOUT_AGENT_NAME)
        supplier_findings = self._findings_from(outputs, _SUPPLIER_AGENT_NAME)

        if stockout_findings is None or supplier_findings is None:
            return AgentResponse(
                agent_name=self.name,
                status="ok",
                recommendation=(
                    "Delivery Intelligence needs both stockout risk and supplier evaluation results, "
                    "and at least one wasn't available this run."
                ),
                confidence=None,
            )
        if not any(f.supplier is not None for f in stockout_findings):
            return AgentResponse(
                agent_name=self.name,
                status="ok",
                recommendation=(
                    "No supplier is mapped on inventory data yet, so stockout risk can't be linked "
                    "back to a supplier."
                ),
                confidence=None,
            )

        narratives = build_supplier_impact_narratives(stockout_findings, supplier_findings)
        self._log_narratives(narratives)

        return AgentResponse(
            agent_name=self.name,
            status="ok",
            recommendation=self._format_recommendation(narratives),
            confidence=1.0,
        )

    @staticmethod
    def _findings_from(outputs: list[AgentResponse], agent_name: str) -> list[AgentFinding] | None:
        response = next((o for o in outputs if o.agent_name == agent_name), None)
        if response is None or response.status != "ok":
            return None
        return response.findings

    def _log_narratives(self, narratives: list[SupplierImpactNarrative]) -> None:
        for narrative in narratives:
            logger.warning(
                "delivery_intelligence_supplier_impact_detected",
                extra={
                    "event": "delivery_intelligence_supplier_impact_detected",
                    "outcome": "success",
                    "context": {
                        "supplier": narrative.supplier,
                        "severity": narrative.severity,
                        "affected_skus": narrative.affected_skus,
                    },
                },
            )

    def _error_response(self, message: str) -> AgentResponse:
        logger.warning(
            "delivery_intelligence_failed",
            extra={
                "event": "delivery_intelligence_failed",
                "outcome": "failure",
                "error_class": "ValueError",
                "context": {"detail": message},
            },
        )
        return AgentResponse(agent_name=self.name, status="error", error=message)

    @staticmethod
    def _format_recommendation(narratives: list[SupplierImpactNarrative]) -> str:
        if not narratives:
            return "No supplier-driven stockout risk detected: no flagged supplier's products are currently at elevated risk."
        parts = "; ".join(n.detail for n in narratives)
        return f"Delivery Intelligence ({len(narratives)} supplier(s) with at-risk products): {parts}"
