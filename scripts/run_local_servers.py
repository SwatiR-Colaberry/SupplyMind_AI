"""Single-command launcher for the SupplyMind AI local dev server.

Data Console (data_console/serve_data_console.py, which already also
serves the dashboard's static pages under /dashboard/), the AI Assistant
(chat_interface/serve_chat_ui.py), the What-If Simulator
(scenario_simulation/serve_scenario_simulator.py), and Learn
(learn/serve_learn.py) used to each run their own HTTPServer on their own
port. An earlier revision of this script started the first 3 in one
process (one thread each) so a single command would make every nav-bar
link actually work, without merging them into one server - a real user
still had to cross origins to move between screens, just with one less
terminal command to remember.

local_apps/unified_server.py now finishes that merge: one HTTPServer,
one port, with the AI Assistant, What-If Simulator, and Learn's own
routes mounted under /chat/, /simulate/, and /learn/. This script now
just builds and runs that one server - no more threads needed for
separate listening sockets.

Usage:
    python3 scripts/run_local_servers.py
    # then open http://127.0.0.1:8766 in a browser

    # against the repo's own seeded local Postgres:
    eval "$(python3 scripts/local_test_db.py)"
    python3 scripts/run_local_servers.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Running this file directly (`python3 scripts/run_local_servers.py`, this
# module's own documented usage - matching scripts/local_test_db.py's
# convention) puts only scripts/ itself on sys.path, not the repo root,
# so the repo-internal import below would otherwise fail with
# ModuleNotFoundError.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import local_apps.unified_server as unified_server  # noqa: E402


def main() -> int:
    try:
        server = unified_server.build_server()
    except OSError as exc:
        # Failure-First Design: a bind failure here is almost always "the
        # unified server (or a leftover standalone data_console) is
        # already running" - a clear, actionable message beats a bare
        # traceback pointing at socket internals.
        print(
            f"Could not start the local server: {exc}\n"
            "Is it already running, or is a standalone data_console instance "
            "(python3 -m data_console.serve_data_console) still up? Stop it first, "
            "or override the port via SUPPLYMIND_DATA_CONSOLE_PORT.",
            file=sys.stderr,
        )
        return 1

    print(f"SupplyMind AI listening on http://127.0.0.1:{server.server_port}", file=sys.stderr)
    print("  /            Data Console", file=sys.stderr)
    print("  /dashboard/  Live Data + demo dashboards", file=sys.stderr)
    print("  /chat/       AI Assistant", file=sys.stderr)
    print("  /simulate/   What-If Simulator", file=sys.stderr)
    print("  /learn/      Glossary", file=sys.stderr)
    print("Press Ctrl+C to stop.", file=sys.stderr)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
