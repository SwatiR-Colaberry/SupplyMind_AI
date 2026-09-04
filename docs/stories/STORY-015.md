# STORY-015 — Data Quality Monitoring

As a data steward, I want to monitor data quality, so that I can ensure accurate and reliable data for decision-making.

**Release:** r3 · Advanced Analytics (weeks 7–8)
**Owner:** Data Steward
**Blocked by:** STORY-013

## The requirement this satisfies

- **REQ-017** (Functional, must) — The system must monitor data quality and provide a Data Quality Score.

## How to build it

Implement data quality monitoring tools to assess data accuracy and reliability. Log each quality check with results and timestamps.

## Failure paths you must handle

- Incorrect Data Quality Score
- Quality monitoring fails
- Audit trail not recorded for quality checks

## Acceptance — your stop condition

Tick each box as it genuinely passes. This file is yours — the platform reads
the same criteria out of `.colaberry/progress.json`, which Claude Code keeps in
step (see the managed block in CLAUDE.md). Ticking something you have not
actually met only misleads you.

- [x] Given data inputs, When quality checks are performed, Then the system provides a Data Quality Score
- [x] Given poor data quality, When detected, Then the system alerts the data steward
- [x] Trust: Given data quality monitoring, Then an audit trail of quality checks is maintained

When every box above is ticked, stop and show the demo.
