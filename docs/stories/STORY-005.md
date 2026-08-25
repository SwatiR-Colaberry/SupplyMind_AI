# STORY-005 — Risk Detection and Anomaly Analysis

As a supply chain manager, I want to detect risks and anomalies, so that I can address them proactively.

**Release:** r2 · Risk and Recommendation (weeks 5–6)
**Owner:** AI Developer
**Blocked by:** STORY-004

## The requirement this satisfies

- **REQ-009** (Functional, must) — The system must detect anomalies such as unexpected demand spikes and supplier delays.
- **REQ-012** (Functional, must) — The system must create a unified Supply Chain Risk Score and explain why it is high.

## How to build it

Implement anomaly detection algorithms to identify risks in supply chain data.

## Failure paths you must handle

- Anomaly detection failure
- False positives
- Data inconsistency
- Algorithm performance issues
- Notification system failure

## Acceptance — your stop condition

Tick each box as it genuinely passes. This file is yours — the platform reads
the same criteria out of `.colaberry/progress.json`, which Claude Code keeps in
step (see the managed block in CLAUDE.md). Ticking something you have not
actually met only misleads you.

- [x] Given supply chain data, when the system analyzes it, then it should detect anomalies and risks.
- [x] Given an anomaly detection failure, when the system identifies it, then it should log the error and notify the user.
- [x] Trust: The system logs all detected anomalies and risks with timestamps and severity levels.

When every box above is ticked, stop and show the demo.
