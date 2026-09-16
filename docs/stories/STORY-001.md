# STORY-001 — Basic Data Integration and Analysis

As a supply chain manager, I want to integrate data from PostgreSQL and Google Sheets, so that I can analyze supply chain data.

**Release:** r0 · Initial Skeleton (weeks 1–2)
**Owner:** Data Engineer
**Blocked by:** nothing — you can start this now

## The requirement this satisfies

- **REQ-001** (Functional, must) — The system must analyze customer orders, product catalogs, inventory, warehouses, suppliers, purchase orders, shipments, delivery records, product demand, transportation costs, and supplier performance data.
- **REQ-018** (Constraint, must) — The system must connect to PostgreSQL and Google Sheets for data access.

## How to build it

Set up data pipelines to connect PostgreSQL and Google Sheets, ensuring data is available for analysis.

## Failure paths you must handle

- Data source unavailable
- Incorrect data format
- Network issues
- Authentication failure
- Data corruption

## Acceptance — your stop condition

Tick each box as it genuinely passes. This file is yours — the platform reads
the same criteria out of `.colaberry/progress.json`, which Claude Code keeps in
step (see the managed block in CLAUDE.md). Ticking something you have not
actually met only misleads you.

- [ ] Given data in PostgreSQL and Google Sheets, when the system integrates it, then it should be available for analysis.
- [ ] Given missing data, when the system attempts integration, then it should log an error.
- [ ] Trust: The system logs all data integration attempts with timestamps.

When every box above is ticked, stop and show the demo.
