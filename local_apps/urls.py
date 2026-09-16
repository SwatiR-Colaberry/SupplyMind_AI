"""Shared URLs for the local SupplyMind AI dev servers' cross-app nav bars.

All 5 screens - Data Console (data_console/serve_data_console.py), the
static dashboard/control_tower_*.html pages Data Console itself serves
under /dashboard/, the AI Assistant (chat_interface/serve_chat_ui.py),
the What-If Simulator (scenario_simulation/serve_scenario_simulator.py),
and Learn (learn/serve_learn.py) - are served from one origin by
local_apps/unified_server.py, with the AI Assistant, What-If Simulator,
and Learn mounted under /chat/, /simulate/, and /learn/ path prefixes.
Before that module existed, each of the non-dashboard screens ran its
own HTTPServer on its own port, and this module built separate
cross-origin URLs (one per port) for their nav bars to link to each
other. Now there is exactly one origin (DATA_CONSOLE_URL), and every
other URL below is a path under it - kept as separate named constants
(not inlined at each call site) so "where does the AI Assistant live"
still has exactly one answer if that path ever changes.

The AI Assistant's, What-If Simulator's, and Learn's own standalone
servers (python3 -m chat_interface.serve_chat_ui, etc.) still exist for
isolated debugging - they default to their own ports (see each module's
own DEFAULT_PORT), independent of this module, which now only describes
the merged, normal-use topology.

Deliberately dependency-free (stdlib `os` only): importing this module
must never pull in a heavier stack (dashboard/live_refresh.py's full
agent-orchestration imports, chat_interface's evaluator, etc.) into a
server whose own job is just rendering an HTML page - the same "missing
third module" cycle-avoidance pattern this repo already uses for a
single literal, applied here to a small set of them together so the
server modules (and dashboard/live_refresh.py, which builds the static
pages' own nav) can all import this one module directly with no risk of
a cycle in either direction.
"""

from __future__ import annotations

import os

DATA_CONSOLE_PORT = int(os.environ.get("SUPPLYMIND_DATA_CONSOLE_PORT", 8766))

DATA_CONSOLE_URL = f"http://127.0.0.1:{DATA_CONSOLE_PORT}/"
LIVE_DASHBOARD_URL = f"{DATA_CONSOLE_URL}dashboard/control_tower_real_data.html"
CHAT_UI_URL = f"{DATA_CONSOLE_URL}chat/"
SCENARIO_SIMULATOR_URL = f"{DATA_CONSOLE_URL}simulate/"
LEARN_URL = f"{DATA_CONSOLE_URL}learn/"
