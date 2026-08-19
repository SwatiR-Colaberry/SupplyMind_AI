# SupplyMind AI

Command Center for SupplyMind AI — a static, no-build front end for viewing supply chain data.

## Structure

- `index.html` — entry point, loads the Command Center app
- `command-center/css/` — stylesheet tokens and app styles
- `command-center/js/` — app logic, data, and tabs
  - `command-center/js/tabs/overview.js` — Overview tab (in progress)
  - `command-center/js/tabs/stub.js` — placeholder for tabs not yet built
- `docs/stories/` — story specs (e.g. `STORY-000.md`)

## Running locally

This is a static site with no build step. Serve the folder with any static file server, for example:

```bash
python3 -m http.server 8000
```

Then open `http://localhost:8000` in your browser.
