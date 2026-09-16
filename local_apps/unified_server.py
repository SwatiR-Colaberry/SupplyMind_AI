"""One HTTPServer, one port, serving all 5 SupplyMind AI local dev screens.

Before this module existed, Data Console (data_console/serve_data_console.py,
which already also serves the dashboard's static pages under /dashboard/),
the AI Assistant (chat_interface/serve_chat_ui.py), the What-If
Simulator (scenario_simulation/serve_scenario_simulator.py), and Learn
(learn/serve_learn.py) each ran their own HTTPServer on their own port.
scripts/run_local_servers.py already merged them into one OS process -
this module finishes that merge: one HTTPServer, one port, with the AI
Assistant, What-If Simulator, and Learn's own routes mounted under
/chat/, /simulate/, and /learn/ path prefixes, so a user never has to
cross an origin to move between screens.

Design: UnifiedHandler subclasses DataConsoleHandler directly (not
multiple inheritance with ChatUIHandler/ScenarioSimulatorHandler).
DataConsoleHandler's own do_GET/do_POST/_route_get/_route_post never
reference self.server (confirmed by grep before writing this module), so
this handler's overridden do_GET/do_POST can check for a /chat,
/simulate, or /learn prefix first and fall through to super().do_GET()/
super().do_POST() for everything else, with zero risk of this module's
own server state (the AI Assistant's evaluator/snapshot) colliding with
anything data_console's own extensive routing already does - and zero
risk to data_console/serve_data_console.py itself, which this module
does not modify at all (that file is already well past CLAUDE.md's
Modular Composition size ceiling; subclassing rather than editing it
keeps this merge from being the change that would have triggered a
"split it before adding new code" obligation there).

The AI Assistant's and What-If Simulator's actual request-handling logic
is not duplicated here - both modules already expose their POST logic as
a standalone (raw_body) -> (status, payload) function, and their page
renderer as a standalone () -> str / (snapshot, data_source) -> str
function, precisely so this module (and their own still-functional
standalone servers) can share one implementation: see
chat_interface/serve_chat_ui.py's render_page()/answer_chat_query() and
scenario_simulation/serve_scenario_simulator.py's render_page()/
run_simulation(). Learn has no POST route at all (its page is entirely
static - see learn/serve_learn.py's own docstring), so it only needs the
same render_page() -> str sharing, nothing else.

The original modules' own standalone servers (python3 -m
chat_interface.serve_chat_ui, etc.) still work unchanged - useful for
isolated debugging - they are just no longer what
scripts/run_local_servers.py launches.
"""

from __future__ import annotations

import os
import sys
from http.server import HTTPServer

import chat_interface.serve_chat_ui as chat_ui
import learn.serve_learn as learn_page
import scenario_simulation.serve_scenario_simulator as scenario_simulator
from local_apps import http_helpers
from chat_interface.evaluator import ChatEvaluator
from chat_interface.run_sample_chat_interface import chat_audit_store
from dashboard.metrics import DashboardSnapshot
from data_console.serve_data_console import DataConsoleHandler
from local_apps.urls import DATA_CONSOLE_PORT

_CHAT_PREFIX = "/chat"
_SIMULATE_PREFIX = "/simulate"
_LEARN_PREFIX = "/learn"


class UnifiedServer(HTTPServer):
    """Holds the AI Assistant's own per-server state (evaluator/snapshot/
    data_source) - Data Console and the What-If Simulator need no
    server-level state of their own (see this module's own docstring)."""

    def __init__(
        self,
        address: tuple[str, int],
        chat_evaluator: ChatEvaluator,
        chat_snapshot: DashboardSnapshot,
        chat_data_source: str,
    ) -> None:
        super().__init__(address, UnifiedHandler)
        self.chat_evaluator = chat_evaluator
        self.chat_snapshot = chat_snapshot
        self.chat_data_source = chat_data_source


class UnifiedHandler(DataConsoleHandler):
    server: UnifiedServer

    def do_GET(self) -> None:
        if self.path in (_CHAT_PREFIX, f"{_CHAT_PREFIX}/"):
            snapshot, data_source = chat_ui.current_snapshot(self.server.chat_snapshot, self.server.chat_data_source)
            html = chat_ui.render_page(snapshot, data_source, api_path=f"{_CHAT_PREFIX}/api/chat")
            http_helpers.send_html(self, 200, html)
            return
        if self.path in (_SIMULATE_PREFIX, f"{_SIMULATE_PREFIX}/"):
            html = scenario_simulator.render_page(api_path=f"{_SIMULATE_PREFIX}/api/simulate")
            http_helpers.send_html(self, 200, html)
            return
        if self.path in (_LEARN_PREFIX, f"{_LEARN_PREFIX}/"):
            http_helpers.send_html(self, 200, learn_page.render_page())
            return
        super().do_GET()

    def do_POST(self) -> None:
        if self.path == f"{_CHAT_PREFIX}/api/chat":
            self._handle_chat_api()
            return
        if self.path == f"{_SIMULATE_PREFIX}/api/simulate":
            self._handle_simulate_api()
            return
        super().do_POST()

    def _handle_chat_api(self) -> None:
        try:
            raw_body = http_helpers.read_request_body(self, chat_ui.MAX_REQUEST_BODY_BYTES)
        except ValueError as exc:
            http_helpers.send_json(self, 400, {"error": f"invalid request: {exc}"})
            return
        snapshot, _ = chat_ui.current_snapshot(self.server.chat_snapshot, self.server.chat_data_source)
        status, payload = chat_ui.answer_chat_query(raw_body, self.server.chat_evaluator, snapshot)
        http_helpers.send_json(self, status, payload)

    def _handle_simulate_api(self) -> None:
        try:
            raw_body = http_helpers.read_request_body(self, scenario_simulator.MAX_REQUEST_BODY_BYTES)
        except ValueError as exc:
            http_helpers.send_json(self, 400, {"error": f"invalid request: {exc}"})
            return
        status, payload = scenario_simulator.run_simulation(raw_body)
        http_helpers.send_json(self, status, payload)


def build_server() -> UnifiedServer:
    """Construct (but do not start) the one UnifiedServer scripts/run_local_servers.py now runs."""
    port = int(os.environ.get("SUPPLYMIND_DATA_CONSOLE_PORT", DATA_CONSOLE_PORT))
    chat_snapshot, chat_data_source = chat_ui.choose_snapshot()
    chat_evaluator = ChatEvaluator(chat_audit_store())
    return UnifiedServer(("127.0.0.1", port), chat_evaluator, chat_snapshot, chat_data_source)


def main() -> int:
    server = build_server()
    print(f"SupplyMind AI listening on http://127.0.0.1:{server.server_port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
