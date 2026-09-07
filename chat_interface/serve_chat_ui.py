"""Local browser chat UI for STORY-010 (REQ-016 / AI Chat Interface).

A minimal, dependency-free HTTP server (Python's stdlib http.server - no
new dependency, per CLAUDE.md's 12-factor "explicit dependencies"
principle) that serves one self-contained HTML page with a text box, and
answers every submitted query through the exact same ChatEvaluator /
ChatAuditStore path chat_interface/run_sample_chat_interface.py already
exercises. No query-classification or answer-formatting logic is
duplicated in JavaScript.

A purely static, client-side-only chat widget was considered and
rejected: rendering a DashboardSnapshot once into HTML (the way
dashboard/render.py already does for a read-only dashboard) works fine
there, but a chat box has to run real classification/lookup code on every
submission and durably log every interaction (the Trust AC). Baking
router.classify_query()/chat.answer_query() into JavaScript would mean a
second copy of that logic to keep in sync by hand, and a browser page has
no way to append to this repo's JSONL audit trail on its own - any
interaction typed into a static page would silently bypass the Trust AC
for that entry point. Routing every submission through this tiny local
server instead keeps ChatEvaluator as the single source of truth for
both the answer and the audit record, so a browser-typed query is exactly
as audited as a query run through the batch demo.

Dev-only, on the same footing as scripts/local_test_db.py's own local
Postgres server - not part of chat_interface/'s own trust-spine
(router.py/chat.py/audit_trail.py/evaluator.py all work with zero server
involved), just a way to interact with it from a browser instead of a
script. Not a production service: no auth, no TLS, single-threaded,
binds to 127.0.0.1 only, and exits if the port is already taken rather
than silently reusing whatever is already listening there.

Usage:
    python3 -m chat_interface.serve_chat_ui
    # then open http://127.0.0.1:8765 in a browser

    # against real Postgres data instead of the synthetic "healthy" set:
    eval "$(python3 scripts/local_test_db.py)"
    python3 -m chat_interface.serve_chat_ui
"""

from __future__ import annotations

import html as html_lib
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from string import Template

from chat_interface.evaluator import ChatEvaluator
from chat_interface.run_sample_chat_interface import (
    build_healthy_snapshot,
    build_real_data_snapshot,
    chat_audit_store,
)
from dashboard.metrics import DashboardSnapshot
from data_integration.config import MissingConfigError, load_postgres_config

DEFAULT_PORT = 8765

# A chat query has no legitimate reason to be anywhere near this size;
# capping it here means a bad-faith or malformed Content-Length can never
# make this single-threaded dev server block reading an enormous (or,
# per _read_request_body's own validation, negative) body. 64 KiB is
# generously larger than any real question typed into the text box.
MAX_REQUEST_BODY_BYTES = 65536

# string.Template's $-placeholders, not str.format()'s {}, since this
# page embeds a nontrivial amount of literal CSS and JavaScript - with
# .format() every literal brace in that CSS/JS has to be escaped as {{/}}
# by hand, and a future edit that adds one un-escaped brace either raises
# KeyError or silently mangles the page. $-placeholders need no such
# escaping for CSS/JS, which use {} constantly but never $.
_PAGE_TEMPLATE = Template("""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>SupplyMind AI Chat</title>
<style>
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; background: #f5f6f8; color: #1a1a1a; margin: 0; padding: 24px; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .meta { color: #666; font-size: 13px; margin-bottom: 20px; }
  .chat-box { max-width: 720px; }
  .transcript { display: flex; flex-direction: column; gap: 10px; margin-bottom: 16px; min-height: 60px; }
  .bubble { background: white; border-radius: 8px; padding: 10px 14px; box-shadow: 0 1px 2px rgba(0,0,0,0.08); font-size: 13px; }
  .bubble-query { font-weight: 600; color: #333; }
  .bubble-answer { margin-top: 6px; color: #333; }
  .bubble-answered { border-left: 4px solid #2e7d32; }
  .bubble-data_unavailable { border-left: 4px solid #f9a825; }
  .bubble-unsupported { border-left: 4px solid #757575; }
  .bubble-error { border-left: 4px solid #c62828; }
  .meta-line { font-size: 11px; color: #888; margin-top: 4px; }
  form { display: flex; gap: 8px; }
  input[type=text] { flex: 1; padding: 10px 12px; border: 1px solid #ccc; border-radius: 6px; font-size: 14px; }
  button { padding: 10px 18px; border: none; border-radius: 6px; background: #1565c0; color: white; font-size: 14px; cursor: pointer; }
  button:disabled { background: #90a4ae; cursor: default; }
  .placeholder { color: #888; font-size: 13px; }
</style>
</head>
<body>
  <h1>SupplyMind AI Chat</h1>
  <div class="meta">answering from dashboard $dashboard_id ($data_source) - generated $generated_at</div>
  <div class="chat-box">
    <div class="transcript" id="transcript">
      <div class="placeholder">Ask a question, e.g. "what's our stockout risk?"</div>
    </div>
    <form id="chat-form">
      <input type="text" id="query" placeholder="Ask about stockout risk, suppliers, shipments, demand forecast..." autocomplete="off" />
      <button type="submit" id="submit-btn">Ask</button>
    </form>
  </div>
<script>
const form = document.getElementById('chat-form');
const input = document.getElementById('query');
const transcript = document.getElementById('transcript');
const submitBtn = document.getElementById('submit-btn');
let firstMessage = true;

function escapeHtml(s) {
  const div = document.createElement('div');
  div.textContent = s;
  return div.innerHTML;
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const query = input.value.trim();
  if (!query) return;
  if (firstMessage) {
    transcript.innerHTML = '';
    firstMessage = false;
  }
  submitBtn.disabled = true;
  input.disabled = true;
  try {
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({query: query}),
    });
    const data = await response.json();
    const statusClass = 'bubble-' + (data.status || 'error');
    const bubble = document.createElement('div');
    bubble.className = 'bubble ' + statusClass;
    bubble.innerHTML =
      '<div class="bubble-query">' + escapeHtml(query) + '</div>' +
      '<div class="bubble-answer">' + escapeHtml(data.answer) + '</div>' +
      '<div class="meta-line">status: ' + escapeHtml(data.status) +
      (data.topic ? ' | topic: ' + escapeHtml(data.topic) : '') +
      (data.confidence !== null && data.confidence !== undefined ? ' | confidence: ' + Math.round(data.confidence * 100) + '%' : '') +
      ' | interaction: ' + escapeHtml(data.interaction_id) + '</div>';
    transcript.appendChild(bubble);
  } catch (err) {
    const bubble = document.createElement('div');
    bubble.className = 'bubble bubble-error';
    bubble.textContent = 'Chat interface failure: could not reach the server.';
    transcript.appendChild(bubble);
  } finally {
    submitBtn.disabled = false;
    input.disabled = false;
    input.value = '';
    input.focus();
    transcript.scrollTop = transcript.scrollHeight;
  }
});
</script>
</body>
</html>""")


def _render_page(snapshot: DashboardSnapshot, data_source: str) -> str:
    return _PAGE_TEMPLATE.substitute(
        dashboard_id=html_lib.escape(snapshot.dashboard_id),
        data_source=html_lib.escape(data_source),
        generated_at=html_lib.escape(snapshot.generated_at),
    )


def _choose_snapshot() -> tuple[DashboardSnapshot, str]:
    """Pick real Postgres data when fully configured, else the synthetic "healthy" set.

    Gated on load_postgres_config() actually succeeding - the same
    canonical check data_integration/postgres_connector.py itself uses -
    rather than a single hardcoded env var name. An earlier revision only
    checked SUPPLYMIND_PG_HOST; with that var set but one of the other
    three required ones (SUPPLYMIND_PG_DATABASE/_USER/_PASSWORD) missing
    or misspelled, every dataset pull would fail inside
    build_real_data_snapshot() while the UI's own banner still claimed
    "real Postgres data" - a misleading label for what was actually an
    all-error snapshot. Falling back to synthetic data here on any
    MissingConfigError keeps the label honest without duplicating the
    four required variable names.
    """
    try:
        load_postgres_config()
    except MissingConfigError:
        return build_healthy_snapshot(), "synthetic demo data - set SUPPLYMIND_PG_HOST etc. for real data"
    return build_real_data_snapshot(), "real Postgres data"


class ChatUIServer(HTTPServer):
    """Holds this server's snapshot/evaluator as instance state, not handler class state.

    BaseHTTPRequestHandler is instantiated fresh per request, so per-server
    configuration has to live somewhere else. An earlier revision set it
    directly on the ChatUIHandler *class* (ChatUIHandler.evaluator = ...),
    which works only because this process ever runs one server - two
    ChatUIServer instances in the same process (or a future switch to
    ThreadingHTTPServer) would silently clobber each other's snapshot
    through that shared class state. Storing it on the server instance
    instead (self.server.evaluator, read from the handler) is the standard
    idiom for passing per-server state into BaseHTTPRequestHandler.
    """

    def __init__(self, address: tuple[str, int], evaluator: ChatEvaluator, snapshot: DashboardSnapshot, data_source: str) -> None:
        super().__init__(address, ChatUIHandler)
        self.evaluator = evaluator
        self.snapshot = snapshot
        self.data_source = data_source


class ChatUIHandler(BaseHTTPRequestHandler):
    server: ChatUIServer

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        pass  # chat_interface's own JSON logger already covers every chat interaction

    def do_GET(self) -> None:
        if self.path != "/":
            self._send_json(404, {"error": "not found"})
            return
        body = _render_page(self.server.snapshot, self.server.data_source).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path != "/api/chat":
            self._send_json(404, {"error": "not found"})
            return

        try:
            raw_body = self._read_request_body()
        except ValueError as exc:
            # Malformed/absurd Content-Length ("User interface display
            # issues" / malformed-request failure path): reject cleanly
            # with 400 rather than letting an uncaught ValueError propagate
            # (previously: a non-numeric header crashed the handler with no
            # response at all) or blocking forever on self.rfile.read() with
            # a negative length (previously: wedged this single-threaded
            # server for every other client until the connection closed).
            self._send_json(400, {"error": f"invalid request: {exc}"})
            return

        try:
            payload = json.loads(raw_body or b"{}")
            query_text = payload["query"]
            if not isinstance(query_text, str):
                raise TypeError("'query' must be a string")
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            # Same failure path as above, for a body that read fine but
            # doesn't contain a usable query - distinct from a query
            # answer_query() itself would handle, rejected here before it
            # ever reaches ChatEvaluator.
            self._send_json(400, {"error": f"invalid request: {exc}"})
            return

        run = self.server.evaluator.run(query_text, self.server.snapshot)
        if run.outcome == "crashed":
            self._send_json(
                200,
                {
                    "interaction_id": run.interaction_id,
                    "status": "error",
                    "answer": f"Chat interface failure: {run.crash_error}",
                    "topic": None,
                    "confidence": None,
                },
            )
            return

        response = run.response
        self._send_json(
            200,
            {
                "interaction_id": run.interaction_id,
                "status": response.status,
                "answer": response.answer,
                "topic": response.topic,
                "confidence": response.confidence,
            },
        )

    def _read_request_body(self) -> bytes:
        """Read and validate the request body per Content-Length, or raise ValueError.

        Raises for: a non-numeric header (int() would otherwise raise an
        uncaught ValueError deeper in the stdlib); a negative length
        (self.rfile.read() on a live socket would otherwise block forever
        waiting for EOF that never comes, wedging this single-threaded
        server for every other client); a length beyond
        MAX_REQUEST_BODY_BYTES (no legitimate chat query is anywhere near
        that large).
        """
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return b""
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError(f"Content-Length {raw_length!r} is not a valid integer") from exc
        if length < 0:
            raise ValueError(f"Content-Length {length} must not be negative")
        if length > MAX_REQUEST_BODY_BYTES:
            raise ValueError(f"Content-Length {length} exceeds the {MAX_REQUEST_BODY_BYTES}-byte limit")
        return self.rfile.read(length) if length else b""

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    port = int(os.environ.get("SUPPLYMIND_CHAT_UI_PORT", DEFAULT_PORT))
    snapshot, data_source = _choose_snapshot()
    evaluator = ChatEvaluator(chat_audit_store())

    server = ChatUIServer(("127.0.0.1", port), evaluator, snapshot, data_source)
    print(f"SupplyMind AI Chat listening on http://127.0.0.1:{port} ({data_source})", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
