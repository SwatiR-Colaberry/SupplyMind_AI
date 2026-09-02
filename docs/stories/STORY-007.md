# STORY-007 — Root Cause Analysis

As a supply chain manager, I want to perform root cause analysis, so that I can understand underlying issues.

**Release:** r3 · Advanced Analytics (weeks 7–8)
**Owner:** Data Analyst
**Blocked by:** STORY-006

## The requirement this satisfies

- **REQ-013** (Functional, must) — The system must perform AI-powered root cause analysis for supply chain issues.

## How to build it

Implement algorithms to trace supply chain issues back to their root causes using available data.

## Failure paths you must handle

- Analysis failure
- Insufficient data
- Incorrect causal inference
- Data processing errors
- Analysis API failure

## Acceptance — your stop condition

Tick each box as it genuinely passes. This file is yours — the platform reads
the same criteria out of `.colaberry/progress.json`, which Claude Code keeps in
step (see the managed block in CLAUDE.md). Ticking something you have not
actually met only misleads you.

- [x] Given a supply chain issue, when the system analyzes it, then it should provide a root cause analysis.
- [x] Given insufficient data for analysis, when the system attempts root cause analysis, then it should notify the user of limitations.
- [x] Trust: The system logs all root cause analyses with timestamps and confidence levels.

When every box above is ticked, stop and show the demo.
