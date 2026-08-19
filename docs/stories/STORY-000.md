# STORY-000 — Command Center

**Release:** r0 (pre-work, built before the term's stories start)
**Narrative:** As the project owner, I want a single page that shows what SupplyMind AI is, what it's meant to move, and how far along it is, so that I have one place to demo from and one place that keeps everyone honest about what's actually built.

## Acceptance criteria

- Given the Command Center, when it is opened, then every tab is reachable and every card drills down one level.
- Given sample mode, when any tab is shown, then the sample data is visibly labelled as sample.
- Given the data files, when any tab renders, then its content comes from `.colaberry/plan.json` and `.colaberry/progress.json` read at runtime rather than from hard-coded values.
- Given `.colaberry/manifest.json`, when any tab is shown, then it displays how old the data is and warns when that age exceeds a week.
- Trust — no tab shows a number, a connection or a result the project has not actually produced.

## Notes

Entry point is `index.html` at the repo root, per the GitHub Pages constraint (root or `docs/`, and `docs/` is reserved for platform-generated requirements/traceability content). Page assets live under `command-center/`. Data is fetched at runtime from `.colaberry/*.json` — nothing is inlined.
