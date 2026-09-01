# STORY-013 — Supplier Reliability Evaluation

As a supply chain manager, I want to evaluate supplier reliability, so that I can assess risks and improve supply chain performance.

**Release:** r2 · Risk and Recommendation (weeks 5–6)
**Owner:** Supply Chain Manager
**Blocked by:** STORY-012

## The requirement this satisfies

- **REQ-007** (Functional, must) — The system must evaluate supplier reliability, delivery performance, and generate a Supplier Risk Score.

## How to build it

Implement supplier evaluation algorithms to calculate reliability and performance metrics. Log each evaluation with a timestamp and score.

## Failure paths you must handle

- Incorrect Supplier Risk Score
- Evaluation process fails
- Audit trail not recorded for evaluations

## Acceptance — your stop condition

Tick each box as it genuinely passes. This file is yours — the platform reads
the same criteria out of `.colaberry/progress.json`, which Claude Code keeps in
step (see the managed block in CLAUDE.md). Ticking something you have not
actually met only misleads you.

- [x] Given supplier data, When evaluated, Then the system generates a Supplier Risk Score
- [x] Given unreliable supplier data, When processed, Then the system flags the supplier for review
- [x] Trust: Given supplier evaluation, Then an audit trail of the evaluation process is recorded

When every box above is ticked, stop and show the demo.
