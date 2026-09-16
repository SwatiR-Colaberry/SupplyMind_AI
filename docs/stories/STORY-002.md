# STORY-002 — Orchestrator Agent Coordination

As a supply chain manager, I want the Orchestrator Agent to coordinate AI agents, so that I receive validated responses to my queries.

**Release:** r0 · Initial Skeleton (weeks 1–2)
**Owner:** AI Developer
**Blocked by:** nothing — you can start this now

## The requirement this satisfies

- **REQ-002** (Functional, must) — The system must use AI agents to analyze supply chain data and surface important decisions.
- **REQ-004** (Functional, must) — The system must use an Orchestrator Agent to coordinate AI agents and validate their responses.

## How to build it

Implement the Orchestrator Agent to manage AI agent workflows and validate their outputs.

## Failure paths you must handle

- Agent communication failure
- Invalid agent response
- Timeout errors
- Data inconsistency
- Orchestrator crash

## Acceptance — your stop condition

Tick each box as it genuinely passes. This file is yours — the platform reads
the same criteria out of `.colaberry/progress.json`, which Claude Code keeps in
step (see the managed block in CLAUDE.md). Ticking something you have not
actually met only misleads you.

- [ ] Given a supply chain query, when the Orchestrator Agent receives it, then it should coordinate the appropriate AI agents.
- [ ] Given an invalid response from an AI agent, when the Orchestrator Agent validates it, then it should request a re-evaluation.
- [ ] Trust: The system logs all agent coordination activities with timestamps.

When every box above is ticked, stop and show the demo.
