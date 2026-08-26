# STORY-006 — Recommendation Generation

As a supply chain manager, I want actionable recommendations, so that I can make informed decisions.

**Release:** r2 · Risk and Recommendation (weeks 5–6)
**Owner:** AI Developer
**Blocked by:** STORY-005

## The requirement this satisfies

- **REQ-010** (Functional, must) — The system must generate actionable recommendations based on AI agent outputs.

## How to build it

Develop the Recommendation Agent to synthesize AI agent outputs into actionable business recommendations.

## Failure paths you must handle

- Recommendation generation failure
- Conflicting agent outputs
- Data processing errors
- Recommendation API failure
- User interface display issues

## Acceptance — your stop condition

Tick each box as it genuinely passes. This file is yours — the platform reads
the same criteria out of `.colaberry/progress.json`, which Claude Code keeps in
step (see the managed block in CLAUDE.md). Ticking something you have not
actually met only misleads you.

- [x] Given AI agent outputs, when the system processes them, then it should generate actionable recommendations.
- [x] Given conflicting agent outputs, when the system generates recommendations, then it should highlight the conflicts.
- [x] Trust: The system logs all recommendations with timestamps and supporting data.

When every box above is ticked, stop and show the demo.
