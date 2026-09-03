# STORY-014 — Shipment Delay Analysis

As a logistics coordinator, I want to analyze shipment delays, so that I can optimize delivery times and reduce costs.

**Release:** r3 · Advanced Analytics (weeks 7–8)
**Owner:** Logistics Coordinator
**Blocked by:** STORY-013

## The requirement this satisfies

- **REQ-008** (Functional, must) — The system must analyze shipment delays, delivery times, and transportation costs.

## How to build it

Develop algorithms to analyze shipment delays and calculate associated costs. Ensure each analysis is logged with details and timestamps.

## Failure paths you must handle

- Incorrect delay analysis
- Cost calculation errors
- Audit trail missing for shipment analysis

## Acceptance — your stop condition

Tick each box as it genuinely passes. This file is yours — the platform reads
the same criteria out of `.colaberry/progress.json`, which Claude Code keeps in
step (see the managed block in CLAUDE.md). Ticking something you have not
actually met only misleads you.

- [x] Given shipment data, When analyzed, Then the system identifies delay patterns
- [x] Given shipment delays, When costs are calculated, Then the system provides cost analysis
- [x] Trust: Given shipment analysis, Then an audit trail of delay analysis is maintained

When every box above is ticked, stop and show the demo.
